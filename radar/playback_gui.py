"""
radar.playback_gui
==================

Standalone playback window launched from the live Mini-Radar GUI.

Selection phase
---------------
• Drag-and-drop a “*.csv” onto THIS window
• Click one of the 10 most-recent recordings (pulled from the latest **events.csv**)
• Edit / type a path (textbox pre-filled with today’s log folder)

Playback phase
--------------
• Same SVG map & sensor pose as live view
• Target dot, fading trail, colour like live (simplified)
• Play / Pause / Stop, Exit
• Draggable timeline head, 1×-20× speed slider
• Toggle buttons: Trail ON/OFF, Smooth ON/OFF
• Large playback clock (HH:MM:SS since start)

All in PyGame – no Tkinter, no subprocess.  Works macOS / Linux / Windows.
"""
from __future__ import annotations
import csv, os, math, time, datetime as dt, threading, collections
from pathlib import Path
from typing import List, Tuple

import pygame
from pygame.locals import *

from radar import constants as C
from radar.svg_utils import fit_svg

# ───────────────────────── project paths ──────────────────────────
LOG_ROOT = Path.cwd() / "logs"
TODAY_DIR = LOG_ROOT / dt.datetime.now().strftime("%Y-%m-%d")

# fonts for selection ui
SEL_FONT = C.MID_FONT
HDR_FONT = C.BIG_FONT


class RadarPlaybackGUI:
    """One self-contained PyGame window covering selection + playback."""
    # ─────────────────────────────── init ──────────────────────────────
    def __init__(self, cfg: dict, preset_path: str | Path | None = None):
        pygame.display.set_caption("Mini-Radar – Playback")
        self.screen = pygame.display.set_mode((1100, 750))
        self.clock  = pygame.time.Clock()

        # ── selection-phase state
        self.selection_mode = True
        self.text_active    = False
        self.text_path      = str(TODAY_DIR) + os.sep
        if preset_path:
            self.text_path = str(preset_path)
        self.recent_csv = self._scan_recent()          # newest → oldest
        self.list_rects: List[pygame.Rect] = []

        # ── playback-phase state
        self.data: List[Tuple[float, List[str]]] = []  # (t_rel_sec, row[1:])
        self.idx         = 0
        self.paused      = True
        self.speed       = 1.0
        self.dragging_tl = False
        self.worker_alive = False
        self.thread: threading.Thread | None = None
        self.latest_frame: List[str] = []

        # ── map & pose (same as live view)
        self.svg_surf, self.ppm = fit_svg(
            cfg["map"],
            (self.screen.get_width(),
             self.screen.get_height() - 90))       # leave HUD space
        self.off_x = (self.screen.get_width()  - self.svg_surf.get_width())  // 2
        self.off_y = (self.screen.get_height() - 90 - self.svg_surf.get_height()) // 2
        self.sensor_mm = cfg["sensor"][:]
        self.sensor_hd = cfg["heading"]

        # trails / smoothing toggles
        self.trail_on      = True
        self.smoothing_on  = True
        self.trails: dict[str, collections.deque] = {}
        self.motion_hist: dict[str, collections.deque] = {}

        # ── HUD rects
        h = self.screen.get_height()
        self.btn_play   = pygame.Rect(50, h - 72, 80, 30)
        self.btn_pause  = pygame.Rect(140, h - 72, 80, 30)
        self.btn_stop   = pygame.Rect(230, h - 72, 80, 30)
        self.btn_exit   = pygame.Rect(self.screen.get_width() - 120, h - 72, 90, 30)
        self.slider_tl  = pygame.Rect(50, h - 42, self.screen.get_width() - 100, 8)
        self.slide_spd  = pygame.Rect(50, h - 57, self.screen.get_width() - 100, 8)
        self.btn_trail  = pygame.Rect(self.btn_stop.right + 30, h - 72, 90, 30)
        self.btn_smooth = pygame.Rect(self.btn_trail.right + 20, h - 72, 90, 30)

        # if preset_path passed, auto-load
        if preset_path:
            self._begin_playback(Path(preset_path))

    # ───────────────────────── recent list via events.csv ─────────────
    def _scan_recent(self) -> List[Path]:
        """
        Grab last 10 recording paths from the newest events.csv (written by live GUI).
        Each row in events.csv ends with the path to the track’s CSV.
        """
        ev_files = sorted(LOG_ROOT.glob("*/events.csv"), key=os.path.getmtime,
                          reverse=True)
        if not ev_files:
            return []
        latest_evt = ev_files[0]
        recs: List[Path] = []
        with open(latest_evt) as f:
            for line in reversed(f.readlines()):
                cand = line.strip().split(",")[-1]
                p = Path(cand)
                if p.suffix.lower() == ".csv" and p.exists():
                    recs.append(p)
                if len(recs) == 10:
                    break
        return recs

    # ─────────────────────── helper: mm→px, world Xform ───────────────
    def _mm_to_px(self, mx, my):
        return (self.off_x + int(mx * self.ppm),
                self.off_y + int(self.svg_surf.get_height() - my * self.ppm))

    def _local_to_world(self, xl, yl):
        sx, sy = self.sensor_mm
        c, s = math.cos(math.radians(self.sensor_hd)), math.sin(math.radians(self.sensor_hd))
        return sx + xl * c + yl * s, sy - xl * s + yl * c

    # ───────────────────────── load csv + start worker ────────────────
    def _begin_playback(self, path: Path):
        try:
            with open(path, newline="") as f:
                rdr = csv.reader(f)
                rows = list(rdr)
        except Exception as exc:
            print("Playback load-error:", exc)
            return

        if not rows:
            print("Empty CSV – abort")
            return

        # header detection
        def _is_float(x: str) -> bool:
            try: float(x); return True
            except ValueError: return False

        if not _is_float(rows[0][0]):
            rows.pop(0)           # drop header

        if not rows:
            print("CSV had only header – abort")
            return

        # parse timestamps → relative seconds
        def _row_time(r):
            cand = r[0] if _is_float(r[0]) or 'T' in r[0] else r[1]
            if 'T' in cand:
                return dt.datetime.fromisoformat(cand).timestamp()
            val = float(cand)
            return val / 1000.0 if val > 1e11 else val

        t0 = _row_time(rows[0])
        self.data = [(_row_time(r) - t0, r[1:]) for r in rows]

        self.selection_mode = False
        self.paused = False
        self.idx = 0

        self.worker_alive = True
        self.thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.thread.start()

    # ───────────────────────── background frame loop ──────────────────
    def _worker_loop(self):
        while self.worker_alive and self.idx < len(self.data):
            if self.paused:
                time.sleep(0.05); continue

            now_t, fr = self.data[self.idx]
            self.latest_frame = fr

            if self.idx:
                prev_t, _ = self.data[self.idx - 1]
                time.sleep(max((now_t - prev_t) / self.speed, 0))
            self.idx += 1

    # ───────────────────────── selection-phase events ─────────────────
    def _sel_events(self, ev):
        if ev.type == pygame.DROPFILE and ev.file.lower().endswith(".csv"):
            self._begin_playback(Path(ev.file)); return

        if ev.type == MOUSEBUTTONDOWN and ev.button == 1:
            # list click
            for p, rect in zip(self.recent_csv, self.list_rects):
                if rect.collidepoint(ev.pos):
                    self._begin_playback(p); return
            # textbox focus
            entry_r = pygame.Rect(40, 320, self.screen.get_width() - 80, 34)
            self.text_active = entry_r.collidepoint(ev.pos)

        if ev.type == KEYDOWN and self.text_active:
            if ev.key == K_RETURN:
                self._begin_playback(Path(self.text_path))
            elif ev.key == K_BACKSPACE:
                self.text_path = self.text_path[:-1]
            elif ev.unicode and 32 <= ord(ev.unicode) < 127:
                self.text_path += ev.unicode

    # ───────────────────────── playback-phase events ─────────────────
    def _play_events(self, ev):
        if ev.type == MOUSEBUTTONDOWN and ev.button == 1:
            if self.btn_exit.collidepoint(ev.pos):
                self.worker_alive = False
                pygame.event.post(pygame.event.Event(QUIT)); return
            if self.btn_play.collidepoint(ev.pos):
                self.paused = False
            elif self.btn_pause.collidepoint(ev.pos):
                self.paused = True
            elif self.btn_stop.collidepoint(ev.pos):
                self.idx = 0; self.paused = True
            elif self.btn_trail.collidepoint(ev.pos):
                self.trail_on = not self.trail_on
            elif self.btn_smooth.collidepoint(ev.pos):
                self.smoothing_on = not self.smoothing_on
            elif self.slider_tl.collidepoint(ev.pos):
                self.dragging_tl = True
                self._seek(mx=ev.pos[0])
            elif self.slide_spd.collidepoint(ev.pos):
                rel = (ev.pos[0] - self.slide_spd.x) / self.slide_spd.w
                self.speed = round(1 + max(0, min(1, rel)) * 19, 1)

        elif ev.type == MOUSEBUTTONUP and ev.button == 1:
            self.dragging_tl = False

        elif ev.type == MOUSEMOTION and self.dragging_tl:
            self._seek(mx=ev.pos[0])

    def _seek(self, mx: int):
        rel = (mx - self.slider_tl.x) / self.slider_tl.w
        rel = max(0.0, min(1.0, rel))
        self.idx = int(rel * max(len(self.data) - 1, 0))

    # ───────────────────────── main loop ─────────────────────────────
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

        # shutdown
        self.worker_alive = False
        pygame.time.wait(200)

    # ───────────────────────── drawing helpers ───────────────────────
    def _draw_selection(self):
        w = self.screen.get_width()
        title = HDR_FONT.render("Load Recorded Track", True, C.GREEN)
        self.screen.blit(title, (w // 2 - title.get_width() // 2, 40))

        y0 = 120
        hint = C.SMALL_FONT.render("Recent (newest first)", True, C.DIM)
        self.screen.blit(hint, (80, y0 - 26))
        self.list_rects.clear()

        for i, p in enumerate(self.recent_csv):
            surf = SEL_FONT.render(p.name, True, C.GREEN)
            rect = surf.get_rect(topleft=(80, y0 + i * 36))
            self.screen.blit(surf, rect)
            self.list_rects.append(rect)

        # textbox
        entry_r = pygame.Rect(40, 320, w - 80, 34)
        pygame.draw.rect(self.screen, C.DIM, entry_r)
        pygame.draw.rect(self.screen, C.GREEN, entry_r, 2)
        txt = SEL_FONT.render(self.text_path + (" ▌" if self.text_active else ""),
                              True, C.GREEN)
        self.screen.blit(txt, (entry_r.x + 8, entry_r.y + 5))

        hint2 = C.SMALL_FONT.render("Type path + ↵  •  drag .csv  •  click recent",
                                    True, C.DIM)
        self.screen.blit(hint2, (40, entry_r.bottom + 12))

    def _draw_playback(self):
        # map
        self.screen.blit(self.svg_surf, (self.off_x, self.off_y))

        # current frame → draw target & trail
        if self.latest_frame:
            try:
                ser  = self.latest_frame[0]
                x_mm = float(self.latest_frame[1])
                y_mm = float(self.latest_frame[2])
                px, py = self._mm_to_px(*self._local_to_world(x_mm, y_mm))
                now = time.monotonic()
                tr = self.trails.setdefault(ser, collections.deque(maxlen=200))
                tr.append((px, py, now))
                # trail dots
                if self.trail_on:
                    for tx, ty, tt in tr:
                        fade = 1 - (now - tt) / 5.0
                        if fade <= 0: continue
                        dot = pygame.Surface((8, 8), pygame.SRCALPHA)
                        pygame.draw.circle(dot, (0, 255, 0, int(255 * fade)), (4, 4), 4)
                        self.screen.blit(dot, (tx - 4, ty - 4))
                # head
                pygame.draw.circle(self.screen, C.GREEN, (px, py), 6)
            except Exception:
                pass

        self._draw_hud()

    def _draw_hud(self):
        # buttons
        for rect, lbl in ((self.btn_play,  "Play"),
                          (self.btn_pause, "Pause"),
                          (self.btn_stop,  "Stop"),
                          (self.btn_exit,  "Exit")):
            pygame.draw.rect(self.screen, C.DIM, rect, 2)
            t = C.FONT.render(lbl, True, C.GREEN)
            self.screen.blit(t, (rect.x + 8, rect.y + 6))

        # trail / smoothing toggles
        for rect, lbl, on in ((self.btn_trail,  "Trail",  self.trail_on),
                              (self.btn_smooth, "Smooth", self.smoothing_on)):
            pygame.draw.rect(self.screen, C.DIM if not on else C.GREEN, rect, 2)
            t = C.FONT.render(lbl, True, C.GREEN)
            self.screen.blit(t, (rect.x + 8, rect.y + 6))

        # timeline slider
        if self.data:
            pct = self.idx / (len(self.data) - 1)
        else:
            pct = 0
        hx = self.slider_tl.x + int(pct * self.slider_tl.w)
        pygame.draw.rect(self.screen, C.DIM, self.slider_tl, 2)
        pygame.draw.circle(self.screen, C.GREEN,
                           (hx, self.slider_tl.centery), 6)

        # speed slider
        rel = (self.speed - 1) / 19
        sx = self.slide_spd.x + int(rel * self.slide_spd.w)
        pygame.draw.rect(self.screen, C.DIM, self.slide_spd, 2)
        pygame.draw.circle(self.screen, C.GREEN,
                           (sx, self.slide_spd.centery), 6)
        spd_txt = C.FONT.render(f"{self.speed:.1f}×", True, C.GREEN)
        self.screen.blit(spd_txt, (self.slide_spd.right + 10,
                                   self.slide_spd.centery - 10))

        # playback clock
        if self.data:
            t_sec = self.data[self.idx][0]
            clk = C.BIG_FONT.render(
                str(dt.timedelta(seconds=int(t_sec))), True, C.GREEN)
            self.screen.blit(clk, (self.screen.get_width() - clk.get_width() - 20,
                                   self.btn_play.y - 12))
