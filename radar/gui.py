# radar.gui
# =========
#
# The PondEyes live GUI (pygame). Renders N radar sensors on one shared SVG map.
#
# Per sensor: pose (position + heading), colour (marker + FOV cone + targets), input transport
# (serial / mqtt / sim), a trail toggle, and a distance GATE (min/max range) that hides near/far
# targets on the map + silences the beep while the backend still tracks + logs them.
#
# Interaction:
#  - CONFIG dialog: sensor list (add / remove / duplicate, inline SET, click-to-rename),
#    per-sensor transport + collapsed colour picker; global Map + Smoothing. Distance gates are
#    NOT in this modal (it covers the map) — set them in the wizard or by dragging the arcs.
#  - Set-Sensor wizard: click to place, then a combined screen with heading + min/max range
#    sliders and the live cone / arcs / fill. Double-clicking a marker jumps straight to it.
#  - Live map: drag a gate arc to resize it; hover shows hand / resize cursors; blinking carets
#    on editable fields.
#  - Master TRAIL button OVERRIDES the per-sensor trail toggle (render = master AND per-sensor).
#    NIGHT / SOUND / SMOOTH toggles; DEBUG line (per-sensor Hz + age); a flashing DATA-STREAM-LOST
#    banner when ALL sensors are quiet, plus per-sensor marker staleness so a quiet sensor's
#    markers don't ghost while others stream.
#
# Concurrency: one reader thread per sensor; a single lock guards the tracker + per-sensor live
# state; the render thread snapshots under the lock then draws lock-free. Persistence is SQLite
# (radar.store). Verbose logging via radar.logging_setup -> run-logs/ (git-ignored).

from __future__ import annotations
import math, time, datetime as dt, collections, threading, pygame, pygame.cursors
from typing import Dict, List, Tuple

from radar import constants as C
from radar import colors
from radar import geometry
from radar import render
from radar.svg_utils import fit_svg
from radar.sensors import Sensor, DEFAULT_PALETTE
from radar.serial_reader import probe_baud
from radar.engine import RadarEngine
from radar.widgets import ColorPicker
from radar.sound import beep
from radar.logging_setup import get_logger

log = get_logger("gui")


class RadarGUI(RadarEngine):
    # Inherits the headless RadarEngine (readers, tracker, store, live snapshot, smoothing, gates);
    # this class adds ONLY the pygame window, rendering, and interaction. MAX_V / FOV_DEG /
    # DATA_TIMEOUT_SEC / MARKER_STALE_SEC come from the engine.
    RANGE_MAX_M      = 8.0             # max settable distance limit (metres); slider full-scale
    MAX_ZOOM         = 8.0             # viewport zoom ceiling (1.0 = fit-to-window)

    # ────────────────────────────────────────────────── INIT
    def __init__(self, cfg: dict, sim: bool = False) -> None:
        # Engine first: cfg, sensors, store, tracker, readers dict, per-sensor live state, smoothing,
        # stats, lock, watchdog flags. Readers are NOT started here (we start them at the end of
        # __init__, once the render state is ready). sim=True runs simulated readers (no hardware).
        super().__init__(cfg, sim=sim)

        # ―― Pygame window
        self.screen = pygame.display.set_mode((1100, 750), pygame.RESIZABLE)
        pygame.display.set_caption("Mini-Radar")
        self.clock = pygame.time.Clock()

        # ―― Layout & sensor pose
        self.full_screen = False
        self.map_mode    = False
        self.playback_mode = False
        self.top_pad, self.bottom_pad = C.TOP_PAD_N, C.BOTTOM_PAD_N
        self.svg_surf = None
        self.ppm = 1.0; self.off_x = self.off_y = 0
        self.proj = geometry.Projector(0, 0, 1.0, 0)   # rebuilt in refresh_map()
        # ── viewport zoom (a pure VIEW transform; world mm + geometry are untouched) ──
        self.zoom = 1.0                                # 1.0 = fit-to-window
        self._view_surf = None                         # cached zoom-scaled map (rebuilt on change)
        self.base_ppm = 1.0; self.base_off_x = 0; self.base_off_y = 0; self.base_svg_h = 0
        self.map_rect = pygame.Rect(0, 0, 0, 0)        # map viewport (clip region for map+geometry)
        self.zoom_rect = pygame.Rect(0, 0, 0, 0)       # zoom slider track
        self.reset_zoom_rect = pygame.Rect(0, 0, 0, 0)
        self.drag_zoom = False
        self._pan_drag = None                          # last pos while click-dragging the map (pan)
        # ―― Sensors (multi-sensor data model). The GUI keeps an editing MIRROR of the primary
        #    sensor (sensor_mm / sensor_hd / input_mode / serial_*) so the existing single-
        #    sensor wizard, CONFIG dialog and render path keep working unchanged in this phase.
        #    Phase 3 replaces those mirrors with a full per-sensor editor. (self.sensors /
        #    self.sensors_by_id are owned by the engine, set in super().__init__.)
        # The CONFIG dialog + wizard edit the SELECTED sensor; the mirrors below reflect it.
        self.selected_idx = 0
        self.editing_label = False        # True while inline-renaming the selected sensor
        primary = self.sensors[0]
        self.sensor_mm = list(primary.position)   # [x_mm, y_mm] of the selected sensor
        self.sensor_hd = primary.heading          # degrees ±180
        self.color_picker = ColorPicker(primary.color)

        # ―― Toggles
        self.sound_on   = cfg["sound"]
        self.night_mode = cfg["night"]
        self.trail_on   = bool(cfg.get("trail_on", True))
        self.debug_on   = bool(cfg.get("debug", False))

        # ―― Per-sensor diagnostic stats + smoothing params (smoothing_on / smooth_level /
        #    stats_by_sid / motion_hist) are owned by the engine (super().__init__).

        # ―― Trails (render-side, px coords) — motion_hist (smoothing) lives in the engine.
        self.trail_duration = float(cfg.get("trail_duration", 5.0))
        self.trails:  dict[str, collections.deque] = {}       # ser → deque[(px,py,t,col)]
        self.pulse_phase: dict[str, float] = {}

        # ―― Interaction & wizard
        self.sensor_stage = None          # None | 'intro' | 'placing' | 'heading'
        self.placing_sensor = self.rotating_sensor = False
        self.drag_slider = self.drag_level = False
        self.drag_heading = False                      # dragging the grabbable heading handle
        self.heading_handle = (0, 0)                   # current heading-handle px (set during draw)
        self.knob_rect = self.level_rect = pygame.Rect(0,0,0,0)
        self.heading_btn_rect = pygame.Rect(0,0,0,0)
        self.setpos_btn_rect = pygame.Rect(0,0,0,0)   # "SET POSITION" in the heading sub-modal
        self.menu_rects, self.exit_rect = {}, pygame.Rect(0,0,0,0)
        pygame.mouse.set_cursor(*pygame.cursors.arrow)
        self._cur_cursor = None            # last system cursor set (avoid re-setting each motion)

        # ―― Input mode mirror (primary sensor's transport, edited via the CONFIG dialog)
        self.input_mode  = primary.input_mode.lower()
        self.serial_port = primary.serial_port
        self.serial_baud = int(primary.serial_baud)

        # ―― Concurrency, per-sensor live state, persistence (SQLite) + tracker, and watchdog
        #    flags are all owned by the engine (super().__init__). Readers are started at the END
        #    of __init__ (self._open_inputs(), below), once the render state is ready.

        # ―― Timers & watchdog (render-side)
        self.flash=True; self.t_flash=time.monotonic()
        self._last_beep = 0.0

        # ―― CONFIG dialog
        self.show_cfg=False
        self.fields = ["Broker IP", "Port", "Topic", "Serial Port",
                       "Trail Duration", "Trail ON", "Map", "Max Range", "Min Range",
                       "Serial Baud"]
        # CONFIG dialog edits the PRIMARY sensor's transport (Phase 1 keeps the single-sensor
        # dialog; broker/port/topic/serial come from sensors[0]).
        self.cfg_input = [str(primary.broker), str(primary.port), primary.topic,
                          self.serial_port, str(self.trail_duration), str(primary.trail_on),
                          str(cfg["map"]), f"{primary.max_range_mm/1000:.1f}",
                          f"{primary.min_range_mm/1000:.1f}", str(self.serial_baud)]
        self.MAP_FIELD = 6
        self.MAXRANGE_FIELD = 7
        self.MINRANGE_FIELD = 8
        self.SERIALBAUD_FIELD = 9
        self.drag_range_field = None                       # field idx of the range slider in drag
        self.range_rects: Dict[int, pygame.Rect] = {}      # field idx -> slider track rect
        self.drag_arc = None                               # field idx of the gate arc dragged on the map
        self._click_marker = None                          # last-clicked sensor marker (double-click detect)
        self._click_t = 0.0
        self._min_drag_sid = None                          # marker that may start a min-gate drag
        self._min_drag_start = (0, 0)
        # self.silenced / self.ghosted are engine-owned (shared across UIs) — set in super().__init__.
        self.target_hits: list = []                        # (px, py, serial) of targets drawn this frame
        self.target_menu = None                            # open right-click menu: {serial, pos, rects}
        self.sens_rect = pygame.Rect(0, 0, 0, 0)           # speed-sensitivity slider track (CONFIG)
        self.drag_sens = False
        self.cfg_error = ""
        self.banner_msg = ""
        self.banner_until = 0.0
        self.visible_idx: List[int] = []
        self.cur_vis = self.cur_field = 0
        self._update_visible()
        self.cfg_buttons = {}
        self.field_rects: Dict[int, pygame.Rect] = {}   # editable-field hit areas (click to focus)

        self.refresh_map()
        self._open_inputs()        # start the per-sensor readers now that all state is ready


    # ───────────────────────────────────────── launch playback viewer
    def _launch_playback(self, preset_track_id: int | None = None):
        # Pause the live readers, open the SQLite-backed playback window (its selection screen
        # when preset_track_id is None; otherwise jump straight to that track id), then resume
        # live streaming when the user exits playback.
        self._close_inputs()
        from radar.playback_gui import RadarPlaybackGUI      # lazy import
        self.playback_mode = True
        RadarPlaybackGUI(self.cfg, preset_track_id).run()    # blocks until the user exits
        self.playback_mode = False
        pygame.display.set_caption("Mini-Radar")             # playback reset the window title
        self._open_inputs()



    # _open_inputs / _close_inputs are inherited from RadarEngine.

    # ───────────────────────────────────────── sensor list management (CONFIG dialog)
    @property
    def selected_sensor(self):
        # The Sensor currently selected for editing, or None when the list is empty.
        if 0 <= self.selected_idx < len(self.sensors):
            return self.sensors[self.selected_idx]
        return None

    def _toggle_all_trails(self):
        # Main-window TRAIL button = a global OVERRIDE. It flips self.trail_on, which gates
        # every sensor's trails at render time WITHOUT changing each sensor's own trail_on, so
        # turning the master back on restores the per-sensor choices. Render uses
        # (self.trail_on AND sensor.trail_on).
        self.trail_on = not self.trail_on
        self._sync_cfg()

    def _set_sensor_wizard(self, idx):
        # Run the Set-Sensor place/heading wizard for a SPECIFIC sensor (inline SET button).
        if not (0 <= idx < len(self.sensors)):
            return
        self._select_sensor(idx)
        self.editing_label = False
        self.show_cfg = False
        self.sensor_stage = "intro"

    def _reindex_sensors(self):
        # Rebuild id->Sensor and ensure per-sensor live dicts exist for current ids; prune
        # entries belonging to removed sensors.
        self.sensors_by_id = {s.id: s for s in self.sensors}
        ids = set(self.sensors_by_id)
        for s in self.sensors:
            self.latest_by_sensor.setdefault(s.id, [])
            self.stats_by_sid.setdefault(
                s.id, {"count": 0, "times": collections.deque(maxlen=30),
                       "last_hex": "", "t_last": 0.0})
        self.latest_by_sensor = {k: v for k, v in self.latest_by_sensor.items() if k in ids}
        self.stats_by_sid = {k: v for k, v in self.stats_by_sid.items() if k in ids}

    def _map_center(self):
        # Default pose for a new sensor: the middle of the current map (so it lands on-screen).
        from radar.svg_utils import _svg_mm
        try:
            w_mm, h_mm = _svg_mm(self.cfg["map"])
            return (w_mm / 2.0, h_mm / 2.0)
        except Exception:
            return (0.0, 0.0)

    def _next_sensor_id(self):
        # Lowest free "S<n>" id and its number.
        existing = {s.id for s in self.sensors}
        n = 1
        while f"S{n}" in existing:
            n += 1
        return f"S{n}", n

    def _select_sensor(self, idx):
        # Make `idx` the selected sensor and reload every editing mirror from it. No-op (and
        # mirrors left as-is) when the list is empty.
        self.editing_label = False
        if not self.sensors:
            self.selected_idx = 0
            return
        self.selected_idx = max(0, min(idx, len(self.sensors) - 1))
        s = self.sensors[self.selected_idx]
        self.sensor_mm = list(s.position)
        self.sensor_hd = s.heading
        self.input_mode = s.input_mode.lower()
        self.serial_port = s.serial_port
        self.cfg_input[0] = str(s.broker)
        self.cfg_input[1] = str(s.port)
        self.cfg_input[2] = s.topic
        self.cfg_input[3] = s.serial_port
        self.cfg_input[5] = str(s.trail_on)        # per-sensor trail toggle field
        self.cfg_input[self.MAXRANGE_FIELD] = f"{s.max_range_mm/1000:.1f}"   # metres
        self.cfg_input[self.MINRANGE_FIELD] = f"{s.min_range_mm/1000:.1f}"
        self.cfg_input[self.SERIALBAUD_FIELD] = str(s.serial_baud)
        self.color_picker.set_hex(s.color)
        self._update_visible()

    def _add_sensor(self):
        # Append a fresh sim sensor (so it needs no hardware until configured) at map centre,
        # with the next palette colour, and select it.
        sid, n = self._next_sensor_id()
        color = DEFAULT_PALETTE[len(self.sensors) % len(DEFAULT_PALETTE)]
        s = Sensor(id=sid, label=f"Sensor {n}", color=color, input_mode="sim",
                   position=self._map_center(), heading=0.0)
        self.sensors.append(s)
        self._reindex_sensors()
        self._select_sensor(len(self.sensors) - 1)

    def _duplicate_sensor(self):
        src = self.sensors[self.selected_idx]
        d = src.to_dict()
        sid, _n = self._next_sensor_id()
        d["id"] = sid
        d["label"] = f"{src.label} copy"
        self.sensors.append(Sensor.from_dict(d))
        self._reindex_sensors()
        self._select_sensor(len(self.sensors) - 1)

    def _remove_sensor(self):
        # Remove the selected sensor — allowed even down to ZERO (empty map, no readers).
        if not self.sensors:
            return
        del self.sensors[self.selected_idx]
        self._reindex_sensors()
        if self.sensors:
            self._select_sensor(min(self.selected_idx, len(self.sensors) - 1))
        else:
            self.selected_idx = 0
            self.editing_label = False

    # ───────────────────────────────────────── helper – visible CFG fields
    def _update_visible(self):
        # Which CONFIG fields are relevant depends on the transport:
        #   mqtt -> broker/port/topic (+ trail), serial -> serial port (+ trail),
        #   sim  -> no transport fields (+ trail) since the simulator needs no connection.
        # Editable TEXT fields for the current transport, plus the global trail-length field (4).
        # Trail ON (5) is a checkbox now, not a text field, so it's not in this edit cycle.
        if self.input_mode == "mqtt":
            transport = [0, 1, 2]
        elif self.input_mode == "serial":
            transport = [3, self.SERIALBAUD_FIELD]   # serial PORT + BAUD
        else:  # sim — no transport text fields
            transport = []
        # Range gates (7, 8) are NOT in the config text-field cycle — they're set on the map
        # (wizard sliders / draggable arcs). Only transport fields + trail length are here.
        self.visible_idx = transport + [4]
        self.cur_vis = min(self.cur_vis, len(self.visible_idx) - 1)
        self.cur_field = self.visible_idx[self.cur_vis]

    # ───────────────────────────────────────── smoothing window (3→50)
    # _win is inherited from RadarEngine.

    # ───────────────────────────────────────── sync runtime→cfg
    def _auto_baud(self):
        # AUTO-baud button (serial mode): take the thread, free the port, probe every candidate
        # baud for intelligible LD2450 frames, then alert the user with the result. Synchronous
        # by design — the GUI pauses while it scans, then reports detected / not-found.
        sel = self.selected_sensor
        if sel is None or self.input_mode != "serial":
            return
        port = self.cfg_input[3].strip()
        self._close_inputs()                       # release the port so the probe can open it
        try:
            found = probe_baud(port)
        except Exception as exc:                   # pragma: no cover - defensive
            found = None
            log.warning("auto-baud probe error on %s: %s", port, exc)
        if found:
            sel.serial_baud = found
            self.serial_baud = found
            self.cfg_input[self.SERIALBAUD_FIELD] = str(found)
            self.cfg_error = f"Auto-baud: detected {found} baud on {port}"
            log.info("auto-baud locked %s @ %d", port, found)
        else:
            self.cfg_error = (f"Auto-baud: no LD2450 frames on {port} at any baud "
                              f"— check wiring / power / port")
            log.warning("auto-baud found nothing on %s", port)
        self._open_inputs()                        # restart readers (at the new baud if found)

    def _sync_cfg(self):
        # Push the in-memory editing mirrors back into the SELECTED sensor (if any), then
        # serialise all sensors into cfg["sensors"]. Global settings stay at the top level.
        sel = self.selected_sensor
        if sel is not None:
            sel.position = (self.sensor_mm[0], self.sensor_mm[1])
            sel.heading = self.sensor_hd
            sel.input_mode = self.input_mode
            sel.serial_port = self.serial_port
            sel.color = self.color_picker.hex
            sel.max_range_mm = self._range_m(self.MAXRANGE_FIELD) * 1000.0
            sel.min_range_mm = self._range_m(self.MINRANGE_FIELD) * 1000.0
        self.cfg["sensors"] = [s.to_dict() for s in self.sensors]
        self.cfg.update(sound=self.sound_on, night=self.night_mode,
                        trail_duration=self.trail_duration, trail_on=self.trail_on,
                        smoothing_on=self.smoothing_on, smooth_level=self.smooth_level,
                        debug=self.debug_on)

    # _on_frame and _snapshot_for_render are inherited from RadarEngine.

    # end_all_targets is inherited from RadarEngine (engine-only state). The render-side trails are
    # cleared by the run loop on the pygame thread when data_lost flips — NOT here, so the watchdog
    # called from the web thread (under --serve) never touches pygame-owned visuals.

    # ───────────────────────────────────────── geometry helpers
    def mm_to_px(self, mx, my):
        return self.proj.mm_to_px(mx, my)
    def px_to_mm(self, px, py):
        return self.proj.px_to_mm(px, py)
    def local_to_world(self, xl, yl):
        # Projects through the EDITING mirror (the sensor being placed/rotated).
        return geometry.local_to_world(xl, yl, (self.sensor_mm[0], self.sensor_mm[1], self.sensor_hd))

    # ── distance-gate helpers ────────────────────────────────────────────────────────────
    # _in_range is inherited from RadarEngine.

    def _range_m(self, field_idx):
        # Parse a range edit field (metres) -> clamped float metres.
        try:
            m = float(self.cfg_input[field_idx])
        except (ValueError, IndexError):
            m = 0.0
        return max(0.0, min(self.RANGE_MAX_M, m))

    def _set_range_from_x(self, field_idx, mx):
        # Map a slider x-position to metres and write it to `field_idx` (slider->field link);
        # field->slider is just re-reading the field. Uses that field's registered slider rect.
        r = self.range_rects.get(field_idx)
        if not r or r.width <= 0:
            return
        mx = max(r.left, min(r.right, mx))
        m = (mx - r.left) / r.width * self.RANGE_MAX_M
        self.cfg_input[field_idx] = f"{m:.1f}"

    def _set_sens_from_x(self, mx):
        # Slider -> selected sensor's speed_sensitivity (0..1). Written straight onto the sensor;
        # to_dict persistence picks it up on save.
        s = self.selected_sensor
        if s is None or self.sens_rect.width <= 0:
            return
        s.speed_sensitivity = max(0.0, min(1.0, (mx - self.sens_rect.left) / self.sens_rect.width))

    def _marker_at(self, pos):
        # Sensor index whose marker (the small circle at its position) is under `pos`, else None.
        for i, s in enumerate(self.sensors):
            sx, sy = self.mm_to_px(*s.position)
            if math.hypot(pos[0] - sx, pos[1] - sy) <= 10:
                return i
        return None

    @staticmethod
    def _pt_seg_dist(p, a, b):
        # Distance from point p to segment a-b (px).
        (px, py), (ax, ay), (bx, by) = p, a, b
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    def _cone_line_at(self, pos, tol=6):
        # Sensor index whose FOV cone EDGE (fence) line is under `pos`, else None. Lets the user
        # grab a sensor whose marker is stacked under a neighbour's by clicking its cone line.
        for i, s in enumerate(self.sensors):
            sx, sy = self.mm_to_px(*s.position)
            R = (s.max_range_mm if s.max_range_mm > 0 else 5000) * self.ppm
            edge = geometry.fov_arc_pts(sx, sy, s.heading, R, self.FOV_DEG, steps=2)  # [left, mid, right]
            if (self._pt_seg_dist(pos, (sx, sy), edge[0]) <= tol or
                    self._pt_seg_dist(pos, (sx, sy), edge[-1]) <= tol):
                return i
        return None

    # ── target right-click context menu (Silence, ...) ───────────────────────────────────
    def _target_at(self, pos):
        # Serial of the target marker nearest `pos` (within a small grab radius), or None.
        # target_hits is rebuilt each render frame as (px, py, serial).
        best, best_d = None, 16
        for px, py, ser in self.target_hits:
            d = math.hypot(pos[0] - px, pos[1] - py)
            if d < best_d:
                best, best_d = ser, d
        return best

    def _target_menu_items(self, ser):
        # (label, key) rows for the context menu — easy to extend with more actions later.
        return [("Unsilence" if ser in self.silenced else "Silence", "silence"),
                ("Unghost" if ser in self.ghosted else "Ghost", "ghost")]

    def _target_menu_click(self, pos):
        # Apply a click on the open menu. Returns True if it hit an item.
        for key, r in (self.target_menu or {}).get("rects", {}).items():
            if r.collidepoint(pos):
                ser = self.target_menu["serial"]
                if key == "silence":
                    self.silenced.discard(ser) if ser in self.silenced else self.silenced.add(ser)
                elif key == "ghost":                       # false reading: greyed + silent
                    self.ghosted.discard(ser) if ser in self.ghosted else self.ghosted.add(ser)
                return True
        return False

    def _draw_target_menu(self):
        m = self.target_menu
        ser = m["serial"]
        items = self._target_menu_items(ser)
        w, rh = 132, 24
        h = rh * (len(items) + 1)                                  # +1 for the serial header row
        x = min(max(0, m["pos"][0]), self.screen.get_width() - w)  # clamp on-screen
        y = min(max(0, m["pos"][1]), self.screen.get_height() - h)
        pygame.draw.rect(self.screen, C.BLACK, (x, y, w, h))
        pygame.draw.rect(self.screen, C.GREEN, (x, y, w, h), 1)
        self.screen.blit(C.SMALL_FONT.render(f"Target {ser}", True, C.DIM), (x + 8, y + 4))
        m["rects"] = {}
        yy = y + rh
        for label, key in items:
            r = pygame.Rect(x, yy, w, rh)
            if r.collidepoint(pygame.mouse.get_pos()):
                pygame.draw.rect(self.screen, (0, 60, 0), r)        # hover highlight
            self.screen.blit(C.SMALL_FONT.render(label, True, C.GREEN), (x + 10, yy + 4))
            m["rects"][key] = r
            yy += rh

    def _arc_grab_at(self, pos):
        # If `pos` (screen px) is near a sensor's distance-gate arc AND inside that sensor's FOV
        # cone, return (sensor_index, field_idx) for the nearest such arc, else None. Lets the
        # user drag the min/max arcs directly on the live map.
        mx, my = pos
        best, best_d = None, 14                      # px grab tolerance
        for i, s in enumerate(self.sensors):
            sx, sy = self.mm_to_px(*s.position)
            dx, dy = mx - sx, my - sy
            click_px = math.hypot(dx, dy)
            # angle of the click vs the heading (screen convention: x=sin, -y=cos)
            a = (math.degrees(math.atan2(dx, -dy)) - s.heading + 180) % 360 - 180
            if abs(a) > self.FOV_DEG / 2:
                continue                              # outside the cone -> not on an arc
            for fidx, rng_mm in ((self.MAXRANGE_FIELD, s.max_range_mm),
                                 (self.MINRANGE_FIELD, s.min_range_mm)):
                if rng_mm <= 0:
                    continue
                d = abs(click_px - rng_mm * self.ppm)
                if d < best_d:
                    best, best_d = (i, fidx), d
        return best

    def _set_range_field_from_point(self, fidx, pos):
        # Set a gate's distance from a screen point = its distance from the selected sensor.
        s = self.selected_sensor
        if s is None:
            return
        sx, sy = self.mm_to_px(*s.position)
        m = (math.hypot(pos[0] - sx, pos[1] - sy) / self.ppm) / 1000.0   # px -> mm -> metres
        self.cfg_input[fidx] = f"{max(0.0, min(self.RANGE_MAX_M, m)):.1f}"

    # ───────────────────────────────────────── SVG raster
    def refresh_map(self):
        if self.map_mode:
            box = (self.screen.get_width() - 2 * C.MAP_BORDER,
                   self.screen.get_height() - 2 * C.MAP_BORDER)
            self.svg_surf, self.ppm = fit_svg(self.cfg["map"], box)
            self.off_x = C.MAP_BORDER + (box[0] - self.svg_surf.get_width()) // 2
            self.off_y = C.MAP_BORDER
            self.map_rect = pygame.Rect(C.MAP_BORDER, C.MAP_BORDER, box[0], box[1])
        else:
            box = (self.screen.get_width(),
                   self.screen.get_height() - self.top_pad - self.bottom_pad)
            self.svg_surf, self.ppm = fit_svg(self.cfg["map"], box)
            self.off_x = (box[0] - self.svg_surf.get_width()) // 2
            self.off_y = self.top_pad + (box[1] - self.svg_surf.get_height()) // 2
            self.map_rect = pygame.Rect(0, self.top_pad, box[0], box[1])
        # This fit is the zoom=1 baseline; reset the view to it on any (re)fit.
        self.base_ppm = self.ppm
        self.base_off_x, self.base_off_y = self.off_x, self.off_y
        self.base_svg_h = self.svg_surf.get_height()
        self.zoom = 1.0
        self._view_surf = None
        self.proj = geometry.Projector(self.off_x, self.off_y, self.ppm, self.base_svg_h)

    # ── viewport zoom (pure VIEW transform; the shared Projector carries it, so all geometry
    #    + hit-tests follow automatically — only the map background needs scaling) ───────────
    def _map_surface(self):
        # The map background scaled to the current zoom; cached and rebuilt only on zoom change.
        if self._view_surf is None:
            if abs(self.zoom - 1.0) < 1e-6:
                self._view_surf = self.svg_surf
            else:
                w = max(1, int(self.svg_surf.get_width() * self.zoom))
                h = max(1, int(self.svg_surf.get_height() * self.zoom))
                self._view_surf = pygame.transform.smoothscale(self.svg_surf, (w, h))
        return self._view_surf

    def _apply_zoom(self, new_zoom, focus):
        # Zoom about `focus` (screen px) keeping the world point under it fixed. Rebuilds self.proj
        # with the effective (scaled) params; ALL draws + hit-tests go through it, so geometry and
        # clicks stay correct. World mm are never changed.
        new_zoom = max(1.0, min(self.MAX_ZOOM, new_zoom))
        if abs(new_zoom - self.zoom) < 1e-9:
            return
        fx = max(self.map_rect.left, min(self.map_rect.right, focus[0]))
        fy = max(self.map_rect.top, min(self.map_rect.bottom, focus[1]))
        wx, wy = self.px_to_mm(fx, fy)                 # world point under the focus (current proj)
        self.zoom = new_zoom
        self.ppm = self.base_ppm * new_zoom
        svg_h = self.base_svg_h * new_zoom
        self.off_x = fx - wx * self.ppm
        self.off_y = fy - svg_h + wy * self.ppm
        self.proj = geometry.Projector(self.off_x, self.off_y, self.ppm, svg_h)
        self._view_surf = None

    def _reset_zoom(self):
        self.zoom = 1.0
        self.ppm = self.base_ppm
        self.off_x, self.off_y = self.base_off_x, self.base_off_y
        self.proj = geometry.Projector(self.off_x, self.off_y, self.ppm, self.base_svg_h)
        self._view_surf = None

    def _pan(self, dx, dy):
        # Shift the view by (dx, dy) screen px — a pure pan (zoom unchanged), like dragging a map.
        self.off_x += dx; self.off_y += dy
        self.proj = geometry.Projector(self.off_x, self.off_y, self.ppm, self.base_svg_h * self.zoom)

    def _set_zoom_from_y(self, my):
        # Vertical zoom slider -> zoom about the map centre (top = MAX, bottom = 1.0x).
        if self.zoom_rect.height <= 0:
            return
        frac = 1.0 - max(0.0, min(1.0, (my - self.zoom_rect.top) / self.zoom_rect.height))
        self._apply_zoom(1.0 + frac * (self.MAX_ZOOM - 1.0), self.map_rect.center)

    def _draw_zoom_control(self):
        # Vertical zoom slider on the RIGHT EDGE, overlaid on the map (map-app style) with a 1:1
        # reset below — clear of the occupied corners (logo / menu / stats / clock).
        w = self.screen.get_width(); h = self.screen.get_height()
        track = pygame.Rect(w - 26, int(h * 0.30), 8, int(h * 0.34))
        bg = pygame.Rect(track.x - 16, track.top - 22, track.width + 30, track.height + 54)
        ov = pygame.Surface(bg.size, pygame.SRCALPHA); ov.fill((0, 0, 0, 110))
        self.screen.blit(ov, bg.topleft)               # subtle backing for readability over the map
        pygame.draw.rect(self.screen, C.DIM, track, 1)
        knoby = track.bottom - int((self.zoom - 1.0) / (self.MAX_ZOOM - 1.0) * track.height)
        k = pygame.Rect(0, 0, 22, 14); k.center = (track.centerx, knoby)   # big, easy-to-grab knob
        pygame.draw.rect(self.screen, C.GREEN, k)
        self.zoom_rect = track
        lab = C.SMALL_FONT.render(f"{self.zoom:.1f}x", True, C.GREEN)
        self.screen.blit(lab, (track.centerx - lab.get_width() // 2, track.top - 18))
        rb = pygame.Rect(0, 0, 32, 18); rb.midtop = (track.centerx, track.bottom + 8)
        pygame.draw.rect(self.screen, C.GREEN, rb, 1)
        self.screen.blit(C.SMALL_FONT.render("1:1", True, C.GREEN), (rb.x + 6, rb.y + 2))
        self.reset_zoom_rect = rb

    # ───────────────────────────────────────── menu row
    def _menu_row(self):
        r,x,y={},self.screen.get_width()-10,10
        def add(label,key,col=C.GREEN):
            nonlocal x
            surf=C.FONT.render(label,True,col); rr=surf.get_rect(); rr.topright=(x,y)
            self.screen.blit(surf,rr); r[key]=rr; x=rr.left-20
        add("EXIT_FULL" if self.full_screen else "FULL_SCREEN","full")
        if not self.map_mode: add("MAP","map")
        add("CONFIG","config")
        add("TRAIL","trail",C.GREEN if self.trail_on else C.DIM)
        add("SMOOTH","smooth",C.GREEN if self.smoothing_on else C.DIM)
        add("NIGHT","night",C.RED if self.night_mode else C.GREEN)
        add("SOUND","sound",C.GREEN if self.sound_on else C.DIM)
        add("PLAYBACK","playback")          # an ACTION (not a toggle) -> always green
        r["h"]=C.FONT.get_height(); self.menu_rects=r

    # ───────────────────────────────────────── rotation slider
    def _draw_slider(self, angle):
        # Heading control is a centered vertical stack: [angle label] / [slider] / [SET HEADING]
        # — placed above the clock (bottom-right) and dashboard (bottom-left) so nothing overlaps.
        # Slider stays 220px wide centered on screen-x so the drag math (±110) is unchanged.
        cx = self.screen.get_width() // 2
        cy = self.screen.get_height() - 80
        slid = pygame.Rect(0, 0, 220, 12); slid.center = (cx, cy)
        pygame.draw.rect(self.screen, C.DIM, slid, 1)
        knobx = slid.left + int((angle + 180) / 360 * slid.width)
        knob = pygame.Rect(0, 0, 10, 22); knob.center = (knobx, slid.centery)
        pygame.draw.rect(self.screen, C.GREEN, knob)
        self.knob_rect = knob
        lbl = C.FONT.render(f"Heading  {angle:+.0f}°", True, C.GREEN)
        self.screen.blit(lbl, (cx - lbl.get_width() // 2, slid.top - 28))   # centered ABOVE
        return slid

    # ───────────────────────────────────────── CONFIG pop-up
    def _draw_cfg_popup(self):
        # The dialog has a running vertical cursor `y` so sections stack without hardcoded
        # collisions. Layout: sensor list -> add/remove/dup -> selected-sensor editor (input
        # mode + fields + colour picker) -> map -> smoothing -> action buttons.
        from os.path import basename
        w, h = 660, 640
        rect = pygame.Rect((self.screen.get_width()-w)//2,
                           (self.screen.get_height()-h)//2, w, h)
        pygame.draw.rect(self.screen, C.BLACK, rect)
        pygame.draw.rect(self.screen, C.GREEN, rect, 2)
        # X close button (top-right) — same effect as CANCEL
        close_x = pygame.Rect(rect.right - 34, rect.y + 8, 26, 26)
        pygame.draw.rect(self.screen, C.GREEN, close_x, 1)
        self.screen.blit(C.FONT.render("X", True, C.GREEN),
                         C.FONT.render("X", True, C.GREEN).get_rect(center=close_x.center))
        self.cfg_buttons["close_x"] = close_x
        x = rect.x + 20
        y = rect.y + 14

        # ── sensor list ── (headers use FONT=18; rows stay readable green, ▶ marks selection)
        self.screen.blit(C.FONT.render("Sensors:", True, C.GREEN), (x, y)); y += 26
        sensor_rows = []
        set_btns = []
        if not self.sensors:
            self.screen.blit(C.SMALL_FONT.render("(none — click + ADD to create a sensor)",
                                                 True, C.GREEN), (x+22, y+1))
            y += 22
        for i, s in enumerate(self.sensors):
            is_sel = (i == self.selected_idx)
            pygame.draw.rect(self.screen, colors.hex_to_rgb(s.color), pygame.Rect(x, y+2, 14, 14))
            marker = "▶ " if is_sel else "   "
            editing_this = (is_sel and self.editing_label)
            name = s.label + ("▏" if (editing_this and self.flash) else "")   # BLINKING rename caret
            label = f"{marker}{s.id}  {name}  ({s.input_mode})"
            self.screen.blit(C.SMALL_FONT.render(label, True, C.GREEN), (x+22, y+1))
            # inline SET button — runs the place/heading wizard for THIS sensor
            setb = pygame.Rect(rect.right - 84, y - 1, 64, 22)
            pygame.draw.rect(self.screen, C.GREEN, setb, 1)
            self.screen.blit(C.SMALL_FONT.render("SET", True, C.GREEN),
                             C.SMALL_FONT.render("SET", True, C.GREEN).get_rect(center=setb.center))
            # the name/select+rename area is everything left of the SET button
            sensor_rows.append(pygame.Rect(x, y, setb.left - x - 6, 20))
            set_btns.append(setb)
            y += 24
        addb = pygame.Rect(x, y+4, 70, 26)
        remb = pygame.Rect(addb.right+10, y+4, 95, 26)
        dupb = pygame.Rect(remb.right+10, y+4, 115, 26)
        for b, lbl in [(addb, "+ ADD"), (remb, "- REMOVE"), (dupb, "⊕ DUPLICATE")]:
            pygame.draw.rect(self.screen, C.GREEN, b, 2)
            self.screen.blit(C.SMALL_FONT.render(lbl, True, C.GREEN), (b.x+8, b.y+5))
        y = dupb.bottom + 10
        pygame.draw.line(self.screen, C.DIM, (rect.x+10, y), (rect.right-10, y)); y += 10

        # ── selected-sensor editor (only when a sensor is selected) ──
        cur = self.selected_sensor
        _zero = pygame.Rect(0, 0, 0, 0)
        if cur is not None:
            self.screen.blit(C.FONT.render(f"Editing: {cur.id}  {cur.label}", True, C.GREEN),
                             (x, y)); y += 28

            self.screen.blit(C.FONT.render("Input:", True, C.GREEN), (x, y+4))
            mqtt_r = pygame.Rect(x+80, y, 64, 26)
            ser_r = pygame.Rect(mqtt_r.right+8, y, 70, 26)
            sim_r = pygame.Rect(ser_r.right+8, y, 54, 26)
            for r, lbl, on in [(mqtt_r, "MQTT", self.input_mode == "mqtt"),
                               (ser_r, "SERIAL", self.input_mode == "serial"),
                               (sim_r, "SIM", self.input_mode == "sim")]:
                pygame.draw.rect(self.screen, C.GREEN if on else C.DIM, r, 2)
                self.screen.blit(C.SMALL_FONT.render(lbl, True, C.GREEN), (r.x+7, r.y+5))
            self.cfg_buttons.update({"mode_mqtt": mqtt_r, "mode_serial": ser_r, "mode_sim": sim_r})
            y += 34

            # Transport text fields — clickable to focus, BLINKING caret on the focused one.
            self.field_rects = {}
            for f in self.visible_idx:
                if f in (4, self.MAXRANGE_FIELD, self.MINRANGE_FIELD):
                    continue                       # trail length + range gates rendered specially
                caret = "▏" if (f == self.cur_field and self.flash) else ""
                surf = C.SMALL_FONT.render(f"{self.fields[f]}: {self.cfg_input[f]}{caret}",
                                           True, C.GREEN)
                self.screen.blit(surf, (x, y))
                self.field_rects[f] = pygame.Rect(x, y, max(surf.get_width(), 220) + 10, 22)
                y += 24

            # AUTO-baud button, inline with the Serial Baud field (serial mode only). Probes the
            # port for a baud that yields intelligible LD2450 frames.
            if self.input_mode == "serial" and self.SERIALBAUD_FIELD in self.field_rects:
                br = self.field_rects[self.SERIALBAUD_FIELD]
                auto_r = pygame.Rect(br.x + 240, br.y - 1, 76, 22)
                pygame.draw.rect(self.screen, C.GREEN, auto_r, 2)
                self.screen.blit(C.SMALL_FONT.render("AUTO", True, C.GREEN), (auto_r.x + 12, auto_r.y + 3))
                self.cfg_buttons["auto_baud"] = auto_r
            else:
                self.cfg_buttons["auto_baud"] = _zero

            # Trail line: [checkbox] Trail   Length: <n> s   (checkbox = THIS sensor's trail_on)
            chk = pygame.Rect(x, y, 16, 16)
            pygame.draw.rect(self.screen, C.GREEN, chk, 2)
            if cur.trail_on:
                pygame.draw.rect(self.screen, C.GREEN, chk.inflate(-6, -6))   # filled = on
            self.screen.blit(C.SMALL_FONT.render("Trail", True, C.GREEN), (chk.right+8, y-1))
            lx = chk.right + 90
            lcaret = "▏" if (4 == self.cur_field and self.flash) else ""
            lsurf = C.SMALL_FONT.render(f"Length: {self.cfg_input[4]}{lcaret} s", True, C.GREEN)
            self.screen.blit(lsurf, (lx, y-1))
            self.field_rects[4] = pygame.Rect(lx, y-1, lsurf.get_width()+10, 22)
            self.cfg_buttons["trail_chk"] = chk
            y += 28

            # Distance gates (min/max) are NOT edited here — this modal covers the map. They're
            # set on the live map: SET SENSOR (wizard) or by dragging the arcs directly. We clear
            # range_rects so no stale slider hit-areas remain from a previous draw.
            self.range_rects = {}
            cur_max = self._range_m(self.MAXRANGE_FIELD); cur_min = self._range_m(self.MINRANGE_FIELD)
            gate_txt = (f"Distance gates  min {('off' if cur_min<=0 else f'{cur_min:.1f}m')}"
                        f" / max {('off' if cur_max<=0 else f'{cur_max:.1f}m')}"
                        f"   — set via SET SENSOR or drag the arcs on the map")
            self.screen.blit(C.SMALL_FONT.render(gate_txt, True, C.DIM), (x, y)); y += 24

            # Speed sensitivity slider: how readily a "teleporting" target (LD2450 slot reuse)
            # is split into a new track. Off = never; High = strict (indoor). Lower it for fast
            # runners outdoors so they stay one track.
            sv = cur.speed_sensitivity
            lvl = "Off" if sv <= 0 else ("Low" if sv < 0.34 else ("Med" if sv < 0.67 else "High"))
            self.screen.blit(C.SMALL_FONT.render(f"Speed sensitivity: {lvl}", True, C.GREEN), (x, y))
            y += 20
            sslide = pygame.Rect(x+20, y+4, 300, 10)
            pygame.draw.rect(self.screen, C.DIM, sslide, 1)
            sknob = sslide.left + int(max(0.0, min(1.0, sv)) * sslide.width)
            sk = pygame.Rect(0, 0, 10, 16); sk.center = (sknob, sslide.centery)
            pygame.draw.rect(self.screen, C.GREEN, sk)
            self.sens_rect = sslide
            y += 24

            # colour picker (collapsed box; expands a swatch row on click)
            y += self.color_picker.draw(self.screen, x, y, w-60, C.SMALL_FONT) + 6
        else:
            # No sensor selected: keep the keys defined so the event handler is safe.
            self.field_rects = {}
            self.range_rects = {}
            self.sens_rect = _zero
            self.cfg_buttons.update({"mode_mqtt": _zero, "mode_serial": _zero, "mode_sim": _zero,
                                     "trail_chk": _zero, "auto_baud": _zero})

        # ── Display / Map section (GLOBAL — separated from per-sensor settings) ──
        pygame.draw.line(self.screen, C.DIM, (rect.x+10, y), (rect.right-10, y)); y += 10
        self.screen.blit(C.FONT.render("Display / Map:", True, C.GREEN), (x, y)); y += 28
        path = self.cfg_input[self.MAP_FIELD]
        disp = basename(path) if path else "(none)"
        if len(disp) > 34:
            disp = "…" + disp[-33:]
        self.screen.blit(C.SMALL_FONT.render(f"Map: {disp}", True, C.GREEN), (x, y))
        browse = pygame.Rect(rect.right-130, y-2, 110, 24)
        pygame.draw.rect(self.screen, C.GREEN, browse, 2)
        self.screen.blit(C.SMALL_FONT.render("BROWSE…", True, C.GREEN), (browse.x+16, browse.y+4))
        self.cfg_buttons.update({"browse_map": browse})
        y += 30

        # ── smoothing slider (global) ──
        self.screen.blit(C.SMALL_FONT.render("Smoothing", True, C.GREEN), (x, y+2))
        slide = pygame.Rect(x+110, y+4, 300, 10)
        pygame.draw.rect(self.screen, C.DIM, slide, 1)
        for t in range(11):
            tx = slide.left + int(t*slide.width/10)
            pygame.draw.line(self.screen, C.DIM, (tx, slide.bottom), (tx, slide.bottom+4))
        knobx = slide.left + int(self.smooth_level*slide.width/9)
        k = pygame.Rect(0, 0, 10, 16); k.midbottom = (knobx, slide.bottom)
        pygame.draw.rect(self.screen, C.GREEN, k); self.level_rect = slide

        # ── action buttons (anchored to the dialog bottom). SET SENSOR is now inline per row.
        dbg = pygame.Rect(rect.left+20, rect.bottom-46, 90, 30)
        save = pygame.Rect(rect.right-190, rect.bottom-46, 80, 30)
        cancel = pygame.Rect(rect.right-100, rect.bottom-46, 80, 30)
        for b, lbl in [(save, "SAVE"), (cancel, "CANCEL")]:
            pygame.draw.rect(self.screen, C.GREEN, b, 2)
            self.screen.blit(C.SMALL_FONT.render(lbl, True, C.GREEN), (b.x+7, b.y+7))
        dbg_col = C.GREEN if self.debug_on else C.DIM
        pygame.draw.rect(self.screen, dbg_col, dbg, 2)
        self.screen.blit(C.SMALL_FONT.render("DEBUG", True, dbg_col), (dbg.x+18, dbg.y+7))
        self.cfg_buttons.update({"save": save, "cancel": cancel, "debug": dbg,
                                 "add": addb, "remove": remb, "dup": dupb,
                                 "sensor_rows": sensor_rows, "set_btns": set_btns})

        if self.cfg_error:
            err = C.SMALL_FONT.render(self.cfg_error, True, C.RED)
            self.screen.blit(err, (rect.x+20, rect.bottom-72))

    # ───────────────────────────────────────── wizard intro overlay
    def _draw_sensor_intro(self):
        w,h=520,230
        r=pygame.Rect((self.screen.get_width()-w)//2,
                      (self.screen.get_height()-h)//2,w,h)
        pygame.draw.rect(self.screen,C.BLACK,r); pygame.draw.rect(self.screen,C.GREEN,r,2)
        for i,line in enumerate(("SET SENSOR",
                                 "1) Click map where sensor is located",
                                 "2) Rotate slider to set heading")):
            self.screen.blit(C.MID_FONT.render(line,True,C.GREEN),
                             (r.x+20,r.y+25+i*45))
        setb=pygame.Rect(r.x+40,r.bottom-60,180,40)
        canc=pygame.Rect(r.right-180,r.bottom-60,120,40)
        pygame.draw.rect(self.screen,C.GREEN,setb,2)
        pygame.draw.rect(self.screen,C.GREEN,canc,2)
        self.screen.blit(C.FONT.render("SET POSITION",True,C.GREEN),setb.move(10,8))
        self.screen.blit(C.FONT.render("CANCEL",True,C.GREEN),canc.move(25,8))
        self.sensor_buttons={"set":setb,"cancel":canc}

    # ───────────────────────────────────────── heading finish button
    def _draw_heading_btn(self, slid_rect):
        # Two buttons centered BELOW the slider: CONFIRM finalises heading + range; SET POSITION
        # jumps back to re-placing the sensor on the map (when the marker is in the wrong spot).
        cx = slid_rect.centerx
        confirm = pygame.Rect(0, 0, 150, 32); confirm.midtop = (cx - 80, slid_rect.bottom + 12)
        setpos  = pygame.Rect(0, 0, 150, 32); setpos.midtop  = (cx + 80, slid_rect.bottom + 12)
        for b, lbl in ((confirm, "CONFIRM"), (setpos, "SET POSITION")):
            pygame.draw.rect(self.screen, C.GREEN, b, 2)
            t = C.FONT.render(lbl, True, C.GREEN); self.screen.blit(t, t.get_rect(center=b.center))
        self.heading_btn_rect = confirm
        self.setpos_btn_rect = setpos

    def _draw_wizard_range_slider(self, fidx, name, cy):
        # One wizard range slider (centered, at screen-y `cy`), sharing the SAME linked cfg
        # field as the CONFIG menu so the two stay in sync. Registers range_rects[fidx].
        cx = self.screen.get_width() // 2
        slid = pygame.Rect(0, 0, 220, 12); slid.center = (cx, cy)
        pygame.draw.rect(self.screen, C.DIM, slid, 1)
        rng_m = self._range_m(fidx)
        knobx = slid.left + int((rng_m / self.RANGE_MAX_M) * slid.width)
        knob = pygame.Rect(0, 0, 10, 22); knob.center = (knobx, slid.centery)
        pygame.draw.rect(self.screen, C.GREEN, knob)
        self.range_rects[fidx] = slid
        label = "off" if rng_m <= 0 else f"{rng_m:.1f} m"
        lbl = C.FONT.render(f"{name}  {label}", True, C.GREEN)
        self.screen.blit(lbl, (cx - lbl.get_width() // 2, slid.top - 26))

    # ───────────────────────────────────────── save CONFIG
    def _cfg_save(self):
        self.cfg_error = ""
        # Apply the dialog fields to the SELECTED sensor (if any). trail ON is per-sensor now.
        sel = self.selected_sensor
        if sel is not None:
            sel.input_mode = self.input_mode
            sel.broker = self.cfg_input[0]
            sel.port = int(self.cfg_input[1] or 1883)
            sel.topic = self.cfg_input[2]
            sel.serial_port = self.cfg_input[3]
            try:
                baud = int(self.cfg_input[self.SERIALBAUD_FIELD])
                if baud <= 0:
                    raise ValueError
                sel.serial_baud = baud
            except (ValueError, TypeError):
                self.cfg_error = "Bad serial baud (need a positive integer, e.g. 921600)"
                return
            sel.color = self.color_picker.hex
            sel.trail_on = self.cfg_input[5].strip().lower() in ("true", "1", "yes", "on")
            sel.max_range_mm = self._range_m(self.MAXRANGE_FIELD) * 1000.0
            sel.min_range_mm = self._range_m(self.MINRANGE_FIELD) * 1000.0
            self.serial_port = sel.serial_port
            self.serial_baud = sel.serial_baud
        # Global visuals (trail DURATION stays global; trail ON is per-sensor above)
        self.trail_duration = float(self.cfg_input[4] or 5.0)
        self.cfg.update(trail_duration=self.trail_duration, trail_on=self.trail_on,
                        smoothing_on=self.smoothing_on, smooth_level=self.smooth_level)
        # Persist ALL sensor records (new ones need rows before their reader opens a track),
        # sync cfg, and re-open readers across the (possibly changed) sensor set.
        self._sync_cfg()
        for s in self.sensors:
            self.store.upsert_sensor(s.to_dict())
        self.save_config()          # persist immediately (not just on exit) so the backend stores it
        self._open_inputs()

        old_map = self.cfg.get("map")
        new_map = self.cfg_input[self.MAP_FIELD].strip()
        if not new_map:
            self.cfg_input[self.MAP_FIELD] = old_map or ""
        elif new_map != old_map:
            self.cfg["map"] = new_map
            try:
                self.refresh_map()
                self._check_sensor_bounds()
            except Exception as exc:
                self.cfg["map"] = old_map
                self.cfg_input[self.MAP_FIELD] = old_map or ""
                self.cfg_error = f"Map load failed: {exc}"
                try:
                    self.refresh_map()
                except Exception:
                    pass
                self._sync_cfg()
                return

        self._sync_cfg()

    # ───────────────────────────────────────── native file picker
    def _pick_map_file(self):
        """
        macOS: AppleScript via `osascript` (Tk in-process crashes against
        pygame's SDLApplication).  Linux: subprocess-isolated tkinter.
        """
        import sys, subprocess
        initial = str(C.ROOT)
        try:
            if sys.platform == "darwin":
                script = (
                    'set f to choose file with prompt "Select map SVG" '
                    f'default location (POSIX file "{initial}")\n'
                    'return POSIX path of f'
                )
                r = subprocess.run(["osascript", "-e", script],
                                   capture_output=True, text=True, timeout=300)
                if r.returncode != 0:
                    if "User canceled" in r.stderr or "-128" in r.stderr:
                        return
                    self.cfg_error = f"Browse failed: {r.stderr.strip() or 'osascript error'}"
                    return
                path = r.stdout.strip()
            else:
                code = (
                    "import tkinter as tk\n"
                    "from tkinter import filedialog\n"
                    "r = tk.Tk(); r.withdraw()\n"
                    f"p = filedialog.askopenfilename(initialdir={initial!r}, "
                    "filetypes=[('SVG','*.svg'),('All','*.*')])\n"
                    "print(p)\n"
                )
                r = subprocess.run([sys.executable, "-c", code],
                                   capture_output=True, text=True, timeout=300)
                if r.returncode != 0:
                    self.cfg_error = f"Browse failed: {r.stderr.strip() or 'tkinter error'}"
                    return
                path = r.stdout.strip()
        except Exception as exc:
            self.cfg_error = f"Browse failed: {exc}"
            return
        if path:
            self.cfg_input[self.MAP_FIELD] = path
            self.cfg_error = ""

    # ───────────────────────────────────────── sensor bounds check
    def _check_sensor_bounds(self):
        from radar.svg_utils import _svg_mm
        try:
            w_mm, h_mm = _svg_mm(self.cfg["map"])
        except Exception:
            return
        sx, sy = self.sensor_mm
        if not (0 <= sx <= w_mm and 0 <= sy <= h_mm):
            self.banner_msg = "SENSOR OFF-MAP — re-run SET SENSOR"
            self.banner_until = time.monotonic() + 5.0

    # ───────────────────────────────────────── avg motion helper
    # _avg_motion is inherited from RadarEngine.

    # ───────────────────────────────────────── per-sensor marker staleness (ghost fix)
    # _cull_stale_markers and check_watchdog are inherited from RadarEngine.

    # ───────────────────────────────────────── hover cursor feedback
    def _update_hover_cursor(self, pos):
        # Hand over clickable things, I-beam over editable text, arrow otherwise. Skipped while
        # the Set-Sensor wizard owns the cursor; only calls set_cursor when the cursor changes.
        if self.sensor_stage is not None or self.placing_sensor:
            return
        want = pygame.SYSTEM_CURSOR_ARROW

        def hit(r):
            return isinstance(r, pygame.Rect) and r.width and r.height and r.collidepoint(pos)

        if self.show_cfg:
            # editable text: sensor-name rows (rename) + config text fields
            if (any(hit(r) for r in self.cfg_buttons.get("sensor_rows", [])) or
                    any(hit(r) for r in self.field_rects.values())):
                want = pygame.SYSTEM_CURSOR_IBEAM
            # clickable buttons take priority -> hand
            clickable = list(self.cfg_buttons.get("set_btns", []))
            for k, r in self.cfg_buttons.items():
                if k in ("sensor_rows", "set_btns"):
                    continue
                if isinstance(r, pygame.Rect):
                    clickable.append(r)
            cp = self.color_picker
            if cp._box_rect:
                clickable.append(cp._box_rect)
            clickable.extend(cp._swatch_rects)
            if any(hit(r) for r in clickable):
                want = pygame.SYSTEM_CURSOR_HAND
        else:
            if self.zoom_rect.width and self.zoom_rect.inflate(28, 16).collidepoint(pos):
                want = pygame.SYSTEM_CURSOR_SIZENS        # zoom slider knob -> drag up/down
            elif self.reset_zoom_rect.width and self.reset_zoom_rect.collidepoint(pos):
                want = pygame.SYSTEM_CURSOR_HAND          # 1:1 reset
            elif any(hit(r) for r in self.menu_rects.values() if isinstance(r, pygame.Rect)):
                want = pygame.SYSTEM_CURSOR_HAND
            elif self._marker_at(pos) is not None:
                want = pygame.SYSTEM_CURSOR_HAND          # sensor marker (click / double-click)
            elif self._arc_grab_at(pos) is not None:
                want = pygame.SYSTEM_CURSOR_SIZENESW      # draggable gate arc -> resize cursor

        if want != self._cur_cursor:
            self._cur_cursor = want
            try:
                pygame.mouse.set_cursor(want)
            except Exception:
                pass   # some platforms/headless drivers lack system cursors

    # ───────────────────────────────────────── MAIN LOOP
    def run(self):
        running=True
        while running:
            dt_frame=self.clock.tick(30)/1000
            if time.monotonic()-self.t_flash>0.5:
                self.flash=not self.flash; self.t_flash=time.monotonic()

            # ――― EVENTS ―――――――――――――――――――――――――――――――――――――――――――
            for e in pygame.event.get():
                if e.type==pygame.QUIT:
                    running=False

                elif e.type==pygame.VIDEORESIZE and not self.full_screen:
                    self.screen=pygame.display.set_mode(e.size,pygame.RESIZABLE)
                    self.refresh_map()

                elif e.type==pygame.DROPFILE:
                    if self.show_cfg and e.file.lower().endswith(".svg"):
                        self.cfg_input[self.MAP_FIELD] = e.file
                    continue

                # Hover-cursor feedback (hand over buttons, I-beam over editable text)
                if e.type==pygame.MOUSEMOTION:
                    self._update_hover_cursor(e.pos)

                # Wizard intro buttons
                if self.sensor_stage=="intro" and e.type==pygame.MOUSEBUTTONDOWN and e.button==1:
                    if self.sensor_buttons["cancel"].collidepoint(e.pos):
                        self.sensor_stage=None
                    elif self.sensor_buttons["set"].collidepoint(e.pos):
                        self.sensor_stage="placing"; self.placing_sensor=True
                        pygame.mouse.set_cursor(*pygame.cursors.broken_x)
                    continue

                # Heading finish
                if self.sensor_stage=="heading" and e.type==pygame.MOUSEBUTTONDOWN and e.button==1:
                    if self.heading_btn_rect.collidepoint(e.pos):
                        self.rotating_sensor=False; self.sensor_stage=None; self._sync_cfg()
                        continue
                    if self.setpos_btn_rect.collidepoint(e.pos):   # re-place this sensor on the map
                        self.sensor_stage="placing"; self.placing_sensor=True; continue

                # CONFIG dialog events
                if self.show_cfg:
                    if "save" not in self.cfg_buttons: continue
                    if e.type==pygame.KEYDOWN:
                        if self.editing_label and self.selected_sensor is not None:
                            # inline rename of the selected sensor's label
                            s = self.selected_sensor
                            if e.key in (pygame.K_RETURN, pygame.K_ESCAPE):
                                self.editing_label = False
                            elif e.key == pygame.K_BACKSPACE:
                                s.label = s.label[:-1]
                            elif e.unicode and 32 <= ord(e.unicode) < 127:
                                s.label += e.unicode
                        elif e.key==pygame.K_ESCAPE:
                            self.show_cfg=False
                        elif self.selected_sensor is not None:
                            if e.key in (pygame.K_TAB,pygame.K_RETURN):
                                self.cur_vis=(self.cur_vis+1)%len(self.visible_idx)
                                self.cur_field=self.visible_idx[self.cur_vis]
                            elif e.key==pygame.K_BACKSPACE:
                                self.cfg_input[self.cur_field]=self.cfg_input[self.cur_field][:-1]
                            elif e.unicode and 32<=ord(e.unicode)<127:
                                self.cfg_input[self.cur_field]+=e.unicode
                    elif e.type==pygame.MOUSEBUTTONDOWN and e.button==1:
                        rows = self.cfg_buttons.get("sensor_rows", [])
                        setbs = self.cfg_buttons.get("set_btns", [])
                        row_hit = next((i for i, r in enumerate(rows) if r.collidepoint(e.pos)), None)
                        set_hit = next((i for i, r in enumerate(setbs) if r.collidepoint(e.pos)), None)
                        if set_hit is not None:
                            self._set_sensor_wizard(set_hit)       # inline SET -> wizard for that sensor
                        elif self.color_picker.handle_mousedown(e.pos):
                            self.editing_label = False             # picker consumed the click
                        elif row_hit is not None:
                            if row_hit == self.selected_idx:
                                self.editing_label = True          # click the selected row -> rename it
                            else:
                                self._select_sensor(row_hit)       # select another (clears rename)
                        elif self.cfg_buttons.get("trail_chk") and self.cfg_buttons["trail_chk"].collidepoint(e.pos):
                            if self.selected_sensor is not None:   # toggle THIS sensor's trail
                                self.selected_sensor.trail_on = not self.selected_sensor.trail_on
                                self.cfg_input[5] = str(self.selected_sensor.trail_on)
                            self.editing_label = False
                        elif self.cfg_buttons["auto_baud"].collidepoint(e.pos):
                            self.editing_label = False; self._auto_baud()   # probe the serial baud
                        elif any(r.collidepoint(e.pos) for r in self.field_rects.values()):
                            # click a text field -> focus it for editing
                            self.cur_field = next(f for f, r in self.field_rects.items()
                                                  if r.collidepoint(e.pos))
                            if self.cur_field in self.visible_idx:
                                self.cur_vis = self.visible_idx.index(self.cur_field)
                            self.editing_label = False
                        elif self.cfg_buttons["add"].collidepoint(e.pos):
                            self.editing_label=False; self._add_sensor()
                        elif self.cfg_buttons["remove"].collidepoint(e.pos):
                            self.editing_label=False; self._remove_sensor()
                        elif self.cfg_buttons["dup"].collidepoint(e.pos):
                            self.editing_label=False; self._duplicate_sensor()
                        elif self.cfg_buttons["save"].collidepoint(e.pos):
                            self.editing_label=False; self._cfg_save()
                            if not self.cfg_error:
                                self.show_cfg=False
                        elif (self.cfg_buttons["cancel"].collidepoint(e.pos)
                              or (self.cfg_buttons.get("close_x") and
                                  self.cfg_buttons["close_x"].collidepoint(e.pos))):
                            self.editing_label=False; self.show_cfg=False   # CANCEL or the X close
                        elif "browse_map" in self.cfg_buttons and self.cfg_buttons["browse_map"].collidepoint(e.pos):
                            self._pick_map_file()
                        elif "debug" in self.cfg_buttons and self.cfg_buttons["debug"].collidepoint(e.pos):
                            self.debug_on = not self.debug_on; self._sync_cfg()
                        elif self.level_rect.collidepoint(e.pos):
                            self.drag_level=True
                        elif self.sens_rect.collidepoint(e.pos):
                            self.drag_sens=True; self._set_sens_from_x(e.pos[0])
                        elif any(r.collidepoint(e.pos) for r in self.range_rects.values()):
                            self.drag_range_field = next(f for f, r in self.range_rects.items()
                                                         if r.collidepoint(e.pos))
                            self._set_range_from_x(self.drag_range_field, e.pos[0])
                        elif self.cfg_buttons["mode_mqtt"].collidepoint(e.pos):
                            self.input_mode="mqtt"; self._update_visible()
                        elif self.cfg_buttons["mode_serial"].collidepoint(e.pos):
                            self.input_mode="serial"; self._update_visible()
                        elif "mode_sim" in self.cfg_buttons and self.cfg_buttons["mode_sim"].collidepoint(e.pos):
                            self.input_mode="sim"; self._update_visible()
                        else:
                            self.editing_label=False               # clicked empty space -> commit rename
                    elif e.type==pygame.MOUSEBUTTONUP and e.button==1:
                        self.drag_level=False
                        self.drag_range_field=None
                        self.drag_sens=False
                        self.color_picker.handle_mouseup()
                    elif e.type==pygame.MOUSEMOTION:
                        if self.drag_level:
                            pos=e.pos[0]; left,right=self.level_rect.left,self.level_rect.right
                            pos=max(left,min(right,pos))
                            self.smooth_level=round((pos-left)*9/(right-left))
                            self._sync_cfg()
                        elif self.drag_sens:
                            self._set_sens_from_x(e.pos[0])
                        elif self.drag_range_field is not None:
                            self._set_range_from_x(self.drag_range_field, e.pos[0])
                        else:
                            self.color_picker.handle_mousemotion(e.pos)
                    continue  # dialog eats events

                # Hot-keys (no dialog - These are keys that can be pressed by the user to call menu or program actions)
                if self.sensor_stage is None and e.type==pygame.KEYDOWN:
                    # Escape Key will quit the running program
                    if self.target_menu is not None and e.key==pygame.K_ESCAPE:
                        self.target_menu=None             # Esc closes the target menu first
                    elif e.key in (pygame.K_q,pygame.K_ESCAPE):
                        running=False
                    # 'f'-key will toggle the program in and out of full-screen or window mode
                    elif e.key==pygame.K_f:
                        pygame.display.toggle_fullscreen()
                        self.full_screen=not self.full_screen; self.refresh_map()
                    # 'n'-key will toggle "Night Mode" on and off  
                    elif e.key==pygame.K_n:
                        self.night_mode=not self.night_mode; self._sync_cfg()
                    # 's'-key will toggle "Sound" on and off  
                    elif e.key==pygame.K_s:
                        self.sound_on=not self.sound_on; self._sync_cfg()
                    # 'p'-key will toggle the playback mode
                    elif e.key==pygame.K_p:
                        self._launch_playback()
                    # zoom keys: + / - to zoom (about the map centre), 0 to reset to fit
                    elif e.key in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
                        self._apply_zoom(self.zoom*1.25, self.map_rect.center)
                    elif e.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                        self._apply_zoom(self.zoom/1.25, self.map_rect.center)
                    elif e.key==pygame.K_0:
                        self._reset_zoom()

                # viewport gestures (native-map style): two-finger scroll = PAN, pinch = ZOOM,
                # Ctrl/Cmd+scroll = zoom-to-cursor (fallback if the trackpad sends no pinch event).
                if not self.show_cfg and self.sensor_stage is None:
                    if e.type==pygame.MOUSEWHEEL:
                        if pygame.key.get_mods() & (pygame.KMOD_CTRL | pygame.KMOD_GUI):
                            self._apply_zoom(self.zoom * (1.1 ** e.y), pygame.mouse.get_pos())
                        else:
                            dx = getattr(e, "precise_x", e.x); dy = getattr(e, "precise_y", e.y)
                            self._pan(dx * 30, dy * 30)        # two-finger scroll -> pan the map
                        continue
                    if hasattr(pygame, "MULTIGESTURE") and e.type==pygame.MULTIGESTURE:
                        ctr = (int(e.x * self.screen.get_width()), int(e.y * self.screen.get_height()))
                        self._apply_zoom(self.zoom * (1.0 + e.ddist * 6.0), ctr)   # pinch -> zoom
                        continue

                # target right-click context menu (Silence, ...)
                if self.sensor_stage is None and not self.show_cfg:
                    if self.target_menu is not None and e.type==pygame.MOUSEBUTTONDOWN:
                        self._target_menu_click(e.pos)     # toggle if an item was hit
                        self.target_menu=None              # any click closes the menu
                        continue
                    if e.type==pygame.MOUSEBUTTONDOWN and e.button==3:
                        ser=self._target_at(e.pos)         # right-click a target marker -> menu
                        if ser is not None:
                            self.target_menu={"serial": ser, "pos": list(e.pos), "rects": {}}
                            continue

                # Menu clicks & general mouse
                if e.type==pygame.MOUSEBUTTONDOWN and e.button==1:
                    # zoom control (vertical slider + 1:1 reset, right edge) — generous grab area
                    if not self.show_cfg and self.sensor_stage is None:
                        if self.zoom_rect.inflate(28, 16).collidepoint(e.pos):
                            self.drag_zoom=True; self._set_zoom_from_y(e.pos[1]); continue
                        if self.reset_zoom_rect.collidepoint(e.pos):
                            self._reset_zoom(); continue
                    # map exit
                    if self.map_mode and self.exit_rect.collidepoint(e.pos):
                        self.map_mode=False
                        self.top_pad=C.TOP_PAD_N; self.bottom_pad=C.BOTTOM_PAD_N
                        self.refresh_map(); continue

                    m=self.menu_rects
                    if self.sensor_stage is None and m:
                        if not self.map_mode and m["full"].collidepoint(e.pos):
                            pygame.display.toggle_fullscreen()
                            self.full_screen=not self.full_screen; self.refresh_map(); continue
                        if not self.map_mode and "map" in m and m["map"].collidepoint(e.pos):
                            self.map_mode=True; self.top_pad=self.bottom_pad=C.MAP_BORDER
                            self.refresh_map(); continue
                        if not self.map_mode and m["config"].collidepoint(e.pos):
                            self.show_cfg=True; self.cur_vis=0; self.cur_field=self.visible_idx[0]; self.cfg_error=""; continue
                        if not self.map_mode and m["trail"].collidepoint(e.pos):
                            self._toggle_all_trails(); continue   # master: trails for ALL sensors
                        if not self.map_mode and m["smooth"].collidepoint(e.pos):
                            self.smoothing_on=not self.smoothing_on; self._sync_cfg(); continue
                        if not self.map_mode and m["night"].collidepoint(e.pos):
                            self.night_mode=not self.night_mode; self._sync_cfg(); continue
                        if not self.map_mode and m["sound"].collidepoint(e.pos):
                            self.sound_on=not self.sound_on; self._sync_cfg(); continue
                        if not self.map_mode and m["playback"].collidepoint(e.pos):
                                self._launch_playback()
                                continue                            

                    # sensor marker: single click selects it, DOUBLE click opens the SET SENSOR
                    # wizard for it.
                    if self.sensor_stage is None:
                        midx = self._marker_at(e.pos)
                        if midx is not None:
                            # A click-and-DRAG from the marker sets the near (min) gate; a plain
                            # click selects; a double-click edits. The drag activates in MOUSEMOTION.
                            self._min_drag_sid = midx; self._min_drag_start = e.pos
                            now = time.monotonic()
                            if midx == self._click_marker and (now - self._click_t) < 0.4:
                                # double-click: edit heading + gates IN PLACE (skip re-placing).
                                self._click_marker = None
                                self._select_sensor(midx)
                                self.rotating_sensor = True
                                self.sensor_stage = "heading"
                                continue
                            self._click_marker = midx; self._click_t = now
                            self._select_sensor(midx); continue

                    # live-map distance-gate arc drag (no wizard active): grab a min/max arc
                    if self.sensor_stage is None:
                        grab = self._arc_grab_at(e.pos)
                        if grab is not None:
                            sidx, fidx = grab
                            self._select_sensor(sidx)        # mirror follows the dragged sensor
                            self.drag_arc = fidx
                            self._set_range_field_from_point(fidx, e.pos)
                            continue

                    # FOV fence line: single click selects, double-click opens the heading wizard —
                    # the way to reach a sensor whose marker is stacked under a close neighbour's.
                    if self.sensor_stage is None:
                        cidx = self._cone_line_at(e.pos)
                        if cidx is not None:
                            now = time.monotonic()
                            if cidx == self._click_marker and (now - self._click_t) < 0.4:
                                self._click_marker = None
                                self._select_sensor(cidx)
                                self.rotating_sensor = True; self.sensor_stage = "heading"; continue
                            self._click_marker = cidx; self._click_t = now
                            self._select_sensor(cidx); continue

                    # placing click
                    if self.sensor_stage=="placing":
                        self.sensor_mm[:]=self.px_to_mm(*e.pos)
                        self.placing_sensor=False; self.rotating_sensor=True
                        self.sensor_stage="heading"; pygame.mouse.set_cursor(*pygame.cursors.arrow)
                        continue

                    # grabbable heading handle (the dot at the end of the heading arm on the map)
                    if (self.sensor_stage=="heading" and
                            math.hypot(e.pos[0]-self.heading_handle[0], e.pos[1]-self.heading_handle[1]) <= 12):
                        self.drag_heading=True; continue
                    # heading knob drag
                    if self.sensor_stage=="heading" and self.knob_rect.collidepoint(e.pos):
                        self.drag_slider=True; continue
                    # range slider drag (combined wizard screen): grab whichever range slider
                    if self.sensor_stage=="heading":
                        hit = next((f for f, r in self.range_rects.items()
                                    if r.collidepoint(e.pos)), None)
                        if hit is not None:
                            self.drag_range_field=hit
                            self._set_range_from_x(hit, e.pos[0]); continue

                    # empty-map click -> start a pan drag (mapping-program style). Only reached when
                    # no interactive element (marker / gate arc / menu / slider) consumed the click.
                    if self.sensor_stage is None:
                        self._pan_drag = e.pos

                elif e.type==pygame.MOUSEBUTTONUP and e.button==1:
                    self.drag_slider=False
                    self.drag_heading=False
                    self.drag_range_field=None
                    self._min_drag_sid=None
                    self.drag_zoom=False
                    self._pan_drag=None
                    if self.drag_arc is not None:
                        self.drag_arc=None; self._sync_cfg()   # persist the dragged gate

                elif e.type==pygame.MOUSEMOTION and self.drag_zoom:
                    self._set_zoom_from_y(e.pos[1])

                elif e.type==pygame.MOUSEMOTION and self._pan_drag is not None:
                    self._pan(e.pos[0]-self._pan_drag[0], e.pos[1]-self._pan_drag[1])
                    self._pan_drag = e.pos

                elif e.type==pygame.MOUSEMOTION and self.drag_heading:
                    # aim by dragging the handle: heading = angle from the sensor marker to the cursor
                    sx, sy = self.mm_to_px(*self.sensor_mm)
                    self.sensor_hd = (math.degrees(math.atan2(e.pos[0]-sx, -(e.pos[1]-sy))) + 540) % 360 - 180

                elif e.type==pygame.MOUSEMOTION and self.drag_slider:
                    left=(self.screen.get_width()//2)-110; right=left+220
                    x=max(left,min(right,e.pos[0]))
                    self.sensor_hd=((x-left)/220)*360-180

                elif e.type==pygame.MOUSEMOTION and self.drag_range_field is not None:
                    self._set_range_from_x(self.drag_range_field, e.pos[0])

                elif (e.type==pygame.MOUSEMOTION and self._min_drag_sid is not None
                      and self.drag_arc is None and self.sensor_stage is None):
                    # Dragged far enough off the marker -> set the NEAR gate from the cursor.
                    dx=e.pos[0]-self._min_drag_start[0]; dy=e.pos[1]-self._min_drag_start[1]
                    if dx*dx + dy*dy > 64:                  # > 8 px = a drag, not a click
                        self._select_sensor(self._min_drag_sid)
                        self.drag_arc = self.MINRANGE_FIELD
                        self._set_range_field_from_point(self.MINRANGE_FIELD, e.pos)
                        self._min_drag_sid = None           # now a normal gate-arc drag

                elif e.type==pygame.MOUSEMOTION and self.drag_arc is not None:
                    self._set_range_field_from_point(self.drag_arc, e.pos)

            # ――― STALENESS + DATA-LOSS WATCHDOG (shared engine logic) ―――
            # Per-sensor staleness hides a quiet sensor's markers even while OTHERS stream; the
            # global watchdog flips data_lost + ends tracks only when ALL sensors are quiet. Both
            # live in RadarEngine so the web frontend behaves identically.
            now_m = time.monotonic()
            self._cull_stale_markers(now_m)
            was_lost = self.data_lost
            self.check_watchdog(now_m)
            if self.data_lost and not was_lost:        # data just dropped: clear trails on THIS thread
                self.trails.clear(); self.pulse_phase.clear()

            # ――― DRAWING ――――――――――――――――――――――――――――――
            self.screen.fill(C.BLACK)
            self.screen.blit(self._map_surface(), (self.off_x, self.off_y))   # zoom-scaled map (full-bleed)

            # header / menu / clock
            if not self.map_mode:
                y=10
                for surf in C.ASCII_SURFS:
                    self.screen.blit(surf,(10,y)); y+=surf.get_height()
                self._menu_row()
                clk=C.BIG_FONT.render(dt.datetime.now().strftime("%H:%M:%S"),True,C.GREEN)
                self.screen.blit(clk,(self.screen.get_width()-clk.get_width()-10,
                                       self.screen.get_height()-clk.get_height()-10))

            # slider / wizard
            if self.rotating_sensor:
                slid=self._draw_slider(self.sensor_hd)
                if self.sensor_stage=="heading":
                    # combined heading + range gates screen (max + min sliders, live arcs+fill)
                    self.range_rects = {}
                    H = self.screen.get_height()
                    self._draw_wizard_range_slider(self.MAXRANGE_FIELD, "Max range", H-150)
                    self._draw_wizard_range_slider(self.MINRANGE_FIELD, "Min range", H-210)
                    self._draw_heading_btn(slid)
            elif self.sensor_stage=="placing":
                msg=C.FONT.render("SET POSITION NOW",True,C.GREEN)
                self.screen.blit(msg,(self.screen.get_width()//2-msg.get_width()//2,
                                      self.screen.get_height()-40))

            # sensor icons & FOV cones — one per sensor, drawn in that sensor's colour. Keep
            # the primary sensor synced to the live editing mirror so the wizard shows moves
            # as they happen.
            sel = self.selected_sensor
            if sel is not None:
                sel.position = (self.sensor_mm[0], self.sensor_mm[1])
                sel.heading = self.sensor_hd
                sel.color = self.color_picker.hex
                sel.max_range_mm = self._range_m(self.MAXRANGE_FIELD) * 1000.0   # live arc preview
                sel.min_range_mm = self._range_m(self.MINRANGE_FIELD) * 1000.0

            # ── distance-gate FILLS: faint sensor-colour wedges (between the FOV edges, from
            #    min_range to max_range), batched onto ONE translucent overlay so they sit under
            #    the cones / markers / targets. Cones/arcs/markers come from render.draw_sensor.
            fill_overlay = None
            for s in self.sensors:
                if s.max_range_mm <= 0:
                    continue                       # only shade when a far gate bounds the area
                sx, sy = self.mm_to_px(*s.position)
                pts = geometry.sensor_fill_pts(
                    sx, sy, s.heading, s.max_range_mm * self.ppm,
                    s.min_range_mm * self.ppm if s.min_range_mm > 0 else 0.0)
                if pts is None:
                    continue
                if fill_overlay is None:
                    fill_overlay = pygame.Surface(self.screen.get_size(), pygame.SRCALPHA)
                pygame.draw.polygon(fill_overlay, colors.hex_to_rgb(s.color) + (render.FILL_ALPHA,), pts)
            if fill_overlay is not None:
                self.screen.blit(fill_overlay, (0, 0))

            for s in self.sensors:
                base = colors.hex_to_rgb(s.color)
                editing = (s is sel)
                label = s.id if (len(self.sensors) > 1 and not self.map_mode) else None
                # Cone/arcs/marker via the SHARED renderer; fill already drawn above (draw_fill=False).
                sx, sy = render.draw_sensor(
                    self.screen, self.proj, (s.position[0], s.position[1], s.heading), base,
                    min_range_mm=s.min_range_mm, max_range_mm=s.max_range_mm,
                    draw_cone=not (self.placing_sensor and editing), draw_fill=False,
                    label=label, label_font=C.SMALL_FONT)
                # editing affordances only for the sensor being placed/rotated (live-view only)
                if editing and (self.placing_sensor or
                                (self.rotating_sensor and self.sensor_stage == "heading")):
                    pygame.draw.rect(self.screen, base, (sx-3, sy-3, 6, 6))
                if editing and self.sensor_stage == "heading":
                    th = math.radians(s.heading)
                    ex = int(sx + math.sin(th)*60); ey = int(sy - math.cos(th)*60)
                    pygame.draw.line(self.screen, base, (sx, sy), (ex, ey), 2)
                    pygame.draw.circle(self.screen, base, (ex, ey), 6)   # GRABBABLE heading handle
                    self.heading_handle = (ex, ey)
                # PER-SENSOR data-loss: a sensor that WAS streaming but has gone silent past the
                # timeout gets a flashing red ring + "NO DATA", so unplugging ONE sensor is visible
                # even while OTHERS keep streaming (the big banner only fires when ALL go quiet).
                st = self.stats_by_sid.get(s.id)
                if (st and st["count"] and not (self.placing_sensor and editing)
                        and (now_m - st["t_last"]) > self.DATA_TIMEOUT_SEC):
                    if self.flash:
                        pygame.draw.circle(self.screen, C.RED, (sx, sy), 12, 2)
                    if not self.map_mode:
                        self.screen.blit(C.SMALL_FONT.render(f"{s.id} NO DATA", True, C.RED),
                                         (sx + 10, sy - 18))

            # distance readout while a gate is being dragged on the map (metres, 3 decimals) so
            # the user can see how far out the gate is as they drag it.
            if self.drag_arc is not None and self.selected_sensor is not None:
                tag = "max" if self.drag_arc == self.MAXRANGE_FIELD else "min"
                col = colors.hex_to_rgb(self.selected_sensor.color)
                mx, my = pygame.mouse.get_pos()
                self.screen.blit(C.FONT.render(f"{tag} {self._range_m(self.drag_arc):.3f} m", True, col),
                                 (mx + 14, my - 6))

            # live targets & trails — for EVERY sensor: project each target through that
            # sensor's own pose, and colour it by the sensor (hue) + speed (brightness) via
            # colors.target_color. Work off a lock-safe snapshot of the shared live state.
            now=time.monotonic()
            dash_y=self.base_off_y+self.base_svg_h+10   # below the map (FIXED; stays put under zoom/pan)
            if not self.debug_on:                       # debug OFF -> reclaim its bottom row: drop the
                dash_y += C.SMALL_FONT.get_height()+10   # target details/footer down into that space
            snap=self._snapshot_for_render()
            dash_row=0
            any_audible=False; visible_fastest=0.0     # NON-silenced in-range targets drive the beep
            self.target_hits=[]                        # rebuilt each frame for the right-click menu
            for s in self.sensors:
                for (slot,x,y) in snap["latest"].get(s.id, []):
                    ser=snap["slot2ser"].get((s.id, slot))
                    info=snap["active"].get(ser)
                    if ser is None or info is None: continue
                    ax,ay,av=self._avg_motion(ser,x,y,info['hist'][2])
                    if not self._in_range(s, ax, ay):
                        continue                         # beyond this sensor's distance limit -> hide
                    muted = ser in self.silenced
                    ghost = ser in self.ghosted
                    if not muted and not ghost:          # silenced/ghosted targets don't drive the beep
                        any_audible=True; visible_fastest=max(visible_fastest, av)
                    px,py=self.mm_to_px(*s.local_to_world(ax,ay))   # this sensor's projection
                    self.target_hits.append((px, py, ser))
                    norm=min(1.0,av/self.MAX_V)                     # speed -> pulse-ring size only
                    col=colors.hex_to_rgb(s.color)                  # EXACT sensor colour (no tint)
                    trail=self.trails.setdefault(ser,collections.deque())
                    trail.append((px,py,now,col))
                    while trail and now-trail[0][2]>self.trail_duration:
                        trail.popleft()
                    if self.trail_on and s.trail_on and not ghost:  # ghosted = false reading, no trail
                        render.draw_trail(self.screen, trail, now, self.trail_duration, skip_pos=(px,py))
                    ph=self.pulse_phase.get(ser,0.0)+dt_frame
                    self.pulse_phase[ser]=ph%1.0
                    render.draw_target(self.screen, px, py, col, pulse=ph, speed_norm=norm,
                                       label=ser, font=C.FONT, ghost=ghost)
                    if muted and not ghost:              # mute cue (a ghost's translucency is its own cue)
                        pygame.draw.circle(self.screen, C.DIM, (px-12, py-12), 5, 1)
                        pygame.draw.line(self.screen, C.DIM, (px-15, py-9), (px-9, py-15), 1)
                    if not self.map_mode:
                        rng=math.hypot(ax,ay)
                        self.screen.blit(C.FONT.render(
                            f"{s.id}/{ser}: X={ax/1000:+.2f} Y={ay/1000:+.2f} "
                            f"D={rng/1000:.2f}m v={av/10:.1f}",True,col),(10,dash_y+dash_row*22))
                        dash_row+=1

            # footer: recent targets across all sensors (prefixed with the source sensor id)
            if not self.map_mode:
                next_y=dash_y+dash_row*22+5
                self.screen.blit(C.SMALL_FONT.render("Recent Targets:",True,C.GREEN),
                                 (10,next_y))
                for i,tr in enumerate(snap["recent"]):
                    sid=tr.get("serial","—"); src=tr.get("sensor_id","")
                    pfx=f"{src}/" if src else ""
                    txt=(f"{pfx}{sid}: {tr['first'].strftime('%H:%M:%S')}–"
                         f"{tr['last'].strftime('%H:%M:%S')} "
                         f"({str(tr['dur']).split('.')[0]})")
                    self.screen.blit(C.SMALL_FONT.render(txt,True,C.GREEN),
                                     (10,next_y+20+i*18))

            # map exit button
            if self.map_mode:
                xs=C.MID_FONT.render("X",True,C.GREEN)
                self.exit_rect=xs.get_rect()
                self.exit_rect.topright=(self.screen.get_width()-10,10)
                pygame.draw.rect(self.screen,C.BLACK,self.exit_rect.inflate(8,4))
                self.screen.blit(xs,self.exit_rect)

            # debug status line (bottom-left): per-sensor Hz + age, then global active + fps
            if self.debug_on and not self.map_mode:
                now2 = time.monotonic()
                parts = []
                for s in self.sensors:
                    count, t_last, times, _hex = snap["stats"][s.id]
                    hz = (len(times)-1)/(times[-1]-times[0]) if len(times) >= 2 and times[-1] > times[0] else 0.0
                    age = now2 - t_last if count else float("inf")
                    age_s = f"{age:4.1f}s" if age != float("inf") else "—"
                    parts.append(f"{s.id}:{hz:4.1f}Hz a={age_s}")
                txt = ("DEBUG  " + "  ".join(parts) +
                       f"  active={len(snap['active'])}  fps={self.clock.get_fps():4.1f}")
                surf = C.SMALL_FONT.render(txt, True, C.GREEN)
                self.screen.blit(surf, (10, self.screen.get_height()-surf.get_height()-10))

            # data-loss banner
            if self.data_lost and self.flash:
                alert=C.BIG_FONT.render("DATA STREAM CONNECTION LOST",True,C.RED)
                ar=alert.get_rect(center=(self.screen.get_width()//2,
                                          self.screen.get_height()//2))
                self.screen.blit(alert,ar)

            # transient sensor-bounds banner
            if self.banner_msg and time.monotonic() < self.banner_until:
                msg=C.FONT.render(self.banner_msg,True,C.GREEN)
                mr=msg.get_rect(center=(self.screen.get_width()//2,
                                        self.screen.get_height()-40))
                self.screen.blit(msg,mr)

            # zoom control (slider + 1:1), unless a modal/wizard is up
            if not self.show_cfg and self.sensor_stage is None:
                self._draw_zoom_control()

            # overlays
            if self.show_cfg: self._draw_cfg_popup()
            if self.sensor_stage=="intro": self._draw_sensor_intro()
            if self.target_menu is not None: self._draw_target_menu()   # on top of the map
            if self.night_mode:
                ov=pygame.Surface(self.screen.get_size(),pygame.SRCALPHA)
                ov.fill((255,0,0,120)); self.screen.blit(ov,(0,0))
            pygame.display.flip()

            # beep — proximity tone driven by the fastest IN-RANGE, NON-silenced target (so a
            # target the user right-click->Silenced, and out-of-range targets, don't beep).
            if any_audible and self.sound_on:
                n=min(1.0,visible_fastest/self.MAX_V)
                freq=int(700+(1400-700)*n)
                if time.monotonic()-self._last_beep>=0.6-n*0.45:
                    beep(freq).play(); self._last_beep=time.monotonic()

        # graceful shutdown
        self._sync_cfg()
        self._close_inputs()
        self.store.close()
        pygame.quit()
