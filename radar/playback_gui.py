# radar.playback_gui
# ==================
#
# Standalone playback window launched from the live GUI. Rewritten for the SQLite store.
#
# Selection phase: lists the most recent tracks from pondeyes.db (one row per track, showing
# the source sensor, serial, start time, duration and point count). Click one to replay it.
#
# Playback phase: replays the chosen track's points through the sensor SNAPSHOT recorded with the
# track (pose, colour, distance gates, trail) — so a later config change never alters an old
# recording — using the SAME renderer as the live view (radar.geometry + radar.render), so it
# looks identical. The timeline is driven by the recorded high-res per-frame timing (t_rel_s).
#
# All pygame, no Tkinter.

from __future__ import annotations

import datetime as dt
import math
import threading
import time
import collections
from typing import List, Tuple

import pygame
from pygame.locals import *

from radar import constants as C
from radar import colors
from radar import geometry
from radar import render
from radar.svg_utils import fit_svg
from radar.store import TrackStore

SEL_FONT = C.MID_FONT
HDR_FONT = C.BIG_FONT


class RadarPlaybackGUI:
    # One self-contained pygame window covering selection + playback.
    def __init__(self, cfg: dict, preset_track_id: int | None = None):
        pygame.display.set_caption("PondEyes – Playback")
        self.screen = pygame.display.set_mode((1100, 750))
        self.clock = pygame.time.Clock()
        self.cfg = cfg

        # Open the same SQLite store the live app writes to.
        self.store = TrackStore(C.ROOT / "pondeyes.db")
        self.tracks = self.store.list_recent_tracks(50)     # newest first
        self.list_rects: List[pygame.Rect] = []

        # selection vs playback
        self.selection_mode = True

        # playback state
        self.data: List[Tuple[float, int, int]] = []        # (t_rel_sec, x_mm, y_mm)
        self.pose: Tuple[float, float, float] = (0.0, 0.0, 0.0)   # recorded sensor pose
        self.track_color = (0, 255, 0)
        self.sensor_min_range_mm = 0.0      # recorded distance gates (drawn like the live view)
        self.sensor_max_range_mm = 0.0
        self.capture_start = None           # real wall-clock time the track started (from first_seen)
        self.idx = 0
        self.paused = True
        self.speed = 1.0
        self.dragging_tl = False
        self.worker_alive = False
        self.thread: threading.Thread | None = None
        self.trail: collections.deque = collections.deque(maxlen=400)

        # map (HUD reserves space at the bottom for: clock row, button row, two sliders)
        HUD = 112
        self.svg_surf, self.ppm = fit_svg(
            cfg["map"], (self.screen.get_width(), self.screen.get_height() - HUD))
        self.off_x = (self.screen.get_width() - self.svg_surf.get_width()) // 2
        self.off_y = (self.screen.get_height() - HUD - self.svg_surf.get_height()) // 2
        self.proj = geometry.Projector(self.off_x, self.off_y, self.ppm,
                                       self.svg_surf.get_height())

        # HUD rects — stacked bottom-up with clear gaps so nothing overlaps:
        #   buttons row → speed slider → timeline slider; clock sits above, on the right.
        h = self.screen.get_height()
        w = self.screen.get_width()
        self.btn_play = pygame.Rect(50, h - 84, 80, 28)
        self.btn_pause = pygame.Rect(140, h - 84, 80, 28)
        self.btn_stop = pygame.Rect(230, h - 84, 80, 28)
        self.btn_trail = pygame.Rect(330, h - 84, 90, 28)
        self.btn_back = pygame.Rect(440, h - 84, 90, 28)     # back to selection list
        self.btn_exit = pygame.Rect(w - 120, h - 84, 90, 28)
        self.slide_spd = pygame.Rect(50, h - 44, 220, 8)     # speed: short, label to its right
        self.slider_tl = pygame.Rect(50, h - 22, w - 100, 8)  # timeline: full width at the bottom
        self.trail_on = True

        if preset_track_id is not None:
            self._begin_playback_id(preset_track_id)

    # ── geometry: project a recorded point through its own pose, then to pixels (shared math) ─
    def _mm_to_px(self, mx, my):
        return self.proj.mm_to_px(mx, my)

    def _local_to_world(self, xl, yl, pose):
        return geometry.local_to_world(xl, yl, pose)

    # ── load a track by id and start the replay worker ───────────────────────────────────
    def _begin_playback_id(self, track_id: int):
        rows = self.store.fetch_points(track_id)
        if not rows:
            return
        # Track header carries the RECORDED sensor snapshot (pose, colour, gates, trail) so
        # playback reconstructs the scene as-recorded — like the live view.
        track = next((t for t in self.store.list_recent_tracks(500) if t["id"] == track_id), None)
        if track is not None:
            self.pose = (track["sensor_x"], track["sensor_y"], track["sensor_heading"])
            chex = track["sensor_color"] or self.store.sensor_color(track["sensor_id"])  # fallback for pre-v2
            self.track_color = colors.hex_to_rgb(chex)
            self.sensor_min_range_mm = track["sensor_min_range_mm"] or 0.0
            self.sensor_max_range_mm = track["sensor_max_range_mm"] or 0.0
            self.trail_on = bool(track["sensor_trail_on"])           # default Trail to as-recorded
            try:
                self.capture_start = dt.datetime.fromisoformat(track["first_seen"])  # real capture time
            except (ValueError, TypeError):
                self.capture_start = None

        # Timeline: prefer the high-res monotonic t_rel_s (accurate cadence); fall back to
        # wall-clock t_iso deltas for pre-v2 recordings where t_rel_s is NULL.
        if rows[0]["t_rel_s"] is not None:
            t0 = rows[0]["t_rel_s"]
            self.data = [((r["t_rel_s"] or 0.0) - t0, r["x_mm"], r["y_mm"]) for r in rows]
        else:
            def _ts(iso):
                return dt.datetime.fromisoformat(iso).timestamp()
            t0 = _ts(rows[0]["t_iso"])
            self.data = [(_ts(r["t_iso"]) - t0, r["x_mm"], r["y_mm"]) for r in rows]

        self.trail.clear()
        self.idx = 0
        self.paused = False
        self.selection_mode = False
        self.worker_alive = True
        self.thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.thread.start()

    def _worker_loop(self):
        # Advance the playback head in real time, scaled by the speed control.
        while self.worker_alive and self.idx < len(self.data):
            if self.paused:
                time.sleep(0.05)
                continue
            if self.idx:
                prev_t = self.data[self.idx - 1][0]
                now_t = self.data[self.idx][0]
                time.sleep(max((now_t - prev_t) / self.speed, 0))
            self.idx += 1

    # ── events ───────────────────────────────────────────────────────────────────────────
    def _sel_events(self, ev):
        if ev.type == MOUSEBUTTONDOWN and ev.button == 1:
            if getattr(self, "btn_sel_exit", None) and self.btn_sel_exit.collidepoint(ev.pos):
                self.worker_alive = False
                pygame.event.post(pygame.event.Event(QUIT))
                return
            for track, rect in zip(self.tracks, self.list_rects):
                if rect.collidepoint(ev.pos):
                    self._begin_playback_id(track["id"])
                    return

    def _play_events(self, ev):
        if ev.type == MOUSEBUTTONDOWN and ev.button == 1:
            if self.btn_exit.collidepoint(ev.pos):
                self.worker_alive = False
                pygame.event.post(pygame.event.Event(QUIT))
            elif self.btn_back.collidepoint(ev.pos):
                self.worker_alive = False
                self.selection_mode = True
                self.tracks = self.store.list_recent_tracks(50)
            elif self.btn_play.collidepoint(ev.pos):
                self.paused = False
            elif self.btn_pause.collidepoint(ev.pos):
                self.paused = True
            elif self.btn_stop.collidepoint(ev.pos):
                self.idx = 0
                self.paused = True
            elif self.btn_trail.collidepoint(ev.pos):
                self.trail_on = not self.trail_on
            elif self.slider_tl.collidepoint(ev.pos):
                self.dragging_tl = True
                self._seek(ev.pos[0])
            elif self.slide_spd.collidepoint(ev.pos):
                rel = (ev.pos[0] - self.slide_spd.x) / self.slide_spd.w
                self.speed = round(1 + max(0, min(1, rel)) * 19, 1)
        elif ev.type == MOUSEBUTTONUP and ev.button == 1:
            self.dragging_tl = False
        elif ev.type == MOUSEMOTION and self.dragging_tl:
            self._seek(ev.pos[0])

    def _seek(self, mx: int):
        rel = max(0.0, min(1.0, (mx - self.slider_tl.x) / self.slider_tl.w))
        self.idx = int(rel * max(len(self.data) - 1, 0))

    # ── main loop ────────────────────────────────────────────────────────────────────────
    def run(self):
        running = True
        while running:
            for ev in pygame.event.get():
                if ev.type == QUIT or (ev.type == KEYDOWN and ev.key == K_ESCAPE):
                    running = False
                elif self.selection_mode:
                    self._sel_events(ev)
                else:
                    self._play_events(ev)

            self.screen.fill(C.BLACK)
            if self.selection_mode:
                self._draw_selection()
            else:
                self._draw_playback()
            pygame.display.flip()
            self.clock.tick(60)

        self.worker_alive = False
        pygame.time.wait(150)
        self.store.close()

    # ── drawing ──────────────────────────────────────────────────────────────────────────
    def _draw_selection(self):
        w = self.screen.get_width()
        title = HDR_FONT.render("Load Recorded Track", True, C.GREEN)
        self.screen.blit(title, (w // 2 - title.get_width() // 2, 30))

        # Obvious EXIT button (top-right) so you can leave the selection screen without a track.
        self.btn_sel_exit = pygame.Rect(w - 130, 30, 100, 36)
        pygame.draw.rect(self.screen, C.GREEN, self.btn_sel_exit, 2)
        self.screen.blit(C.FONT.render("EXIT", True, C.GREEN),
                         C.FONT.render("EXIT", True, C.GREEN).get_rect(center=self.btn_sel_exit.center))

        y0 = 110
        self.screen.blit(C.SMALL_FONT.render("Recent tracks (newest first) — click to replay   "
                                             "(or press Esc / EXIT to leave)",
                                             True, C.DIM), (60, y0 - 28))
        self.list_rects.clear()
        if not self.tracks:
            self.screen.blit(SEL_FONT.render("(no recordings yet)", True, C.DIM), (60, y0))
            return
        for i, t in enumerate(self.tracks[:16]):
            chip = colors.hex_to_rgb(self.store.sensor_color(t["sensor_id"]))
            pygame.draw.rect(self.screen, chip, pygame.Rect(60, y0 + i * 34 + 2, 14, 14))
            start = t["first_seen"][:19].replace("T", " ")
            label = (f"{t['sensor_id']}/{t['serial']}   {start}   "
                     f"{t['point_count']} pts   {t['duration_sec']:.0f}s")
            surf = SEL_FONT.render(label, True, C.GREEN)
            rect = surf.get_rect(topleft=(82, y0 + i * 34))
            self.screen.blit(surf, rect)
            self.list_rects.append(pygame.Rect(60, y0 + i * 34, w - 120, 30))

    def _draw_playback(self):
        self.screen.blit(self.svg_surf, (self.off_x, self.off_y))
        # Sensor as it was at record time — marker + FOV cone + distance-gate arcs/fill — via the
        # SAME renderer the live view uses, so playback looks identical.
        render.draw_sensor(self.screen, self.proj, self.pose, self.track_color,
                           min_range_mm=self.sensor_min_range_mm,
                           max_range_mm=self.sensor_max_range_mm, label_font=C.SMALL_FONT)

        if self.data and 0 <= self.idx < len(self.data):
            _t, x_mm, y_mm = self.data[self.idx]
            px, py = self._mm_to_px(*self._local_to_world(x_mm, y_mm, self.pose))
            now = time.monotonic()
            self.trail.append((px, py, now, self.track_color))
            if self.trail_on:
                render.draw_trail(self.screen, self.trail, now, 5.0, skip_pos=(px, py))
            render.draw_target(self.screen, px, py, self.track_color, pulse=now % 1.0)

        self._draw_hud()

    def _draw_hud(self):
        for rect, lbl in ((self.btn_play, "Play"), (self.btn_pause, "Pause"),
                          (self.btn_stop, "Stop"), (self.btn_back, "List"),
                          (self.btn_exit, "Exit")):
            pygame.draw.rect(self.screen, C.DIM, rect, 2)
            self.screen.blit(C.FONT.render(lbl, True, C.GREEN), (rect.x + 8, rect.y + 6))
        pygame.draw.rect(self.screen, C.GREEN if self.trail_on else C.DIM, self.btn_trail, 2)
        self.screen.blit(C.FONT.render("Trail", True, C.GREEN),
                         (self.btn_trail.x + 8, self.btn_trail.y + 6))

        pct = self.idx / (len(self.data) - 1) if len(self.data) > 1 else 0
        hx = self.slider_tl.x + int(pct * self.slider_tl.w)
        pygame.draw.rect(self.screen, C.DIM, self.slider_tl, 2)
        pygame.draw.circle(self.screen, C.GREEN, (hx, self.slider_tl.centery), 6)

        rel = (self.speed - 1) / 19
        sx = self.slide_spd.x + int(rel * self.slide_spd.w)
        pygame.draw.rect(self.screen, C.DIM, self.slide_spd, 2)
        pygame.draw.circle(self.screen, C.GREEN, (sx, self.slide_spd.centery), 6)
        self.screen.blit(C.FONT.render(f"{self.speed:.1f}×", True, C.GREEN),
                         (self.slide_spd.right + 10, self.slide_spd.centery - 10))

        if self.data:
            t_sec = self.data[min(self.idx, len(self.data) - 1)][0]
            elapsed = str(dt.timedelta(seconds=int(t_sec)))
            # Show the REAL wall-clock time-of-day of the current frame (capture_start + elapsed)
            # alongside the elapsed-since-acquisition clock, so the user has an absolute reference.
            if self.capture_start is not None:
                wall = (self.capture_start + dt.timedelta(seconds=t_sec)).strftime("%Y-%m-%d %H:%M:%S")
                txt = f"{wall}   (+{elapsed})"
            else:
                txt = elapsed
            clk = C.FONT.render(txt, True, C.GREEN)
            self.screen.blit(clk, (self.screen.get_width() - clk.get_width() - 20,
                                   self.btn_play.y - clk.get_height() - 8))
