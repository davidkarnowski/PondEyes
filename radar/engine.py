# radar.engine
# ============
#
# The headless PondEyes core — everything that owns DATA and STATE, with NO pygame and no
# rendering. One reader per sensor delivers frames to `_on_frame`, which drives the Tracker
# (kinematics + SQLite persistence) and a lock-guarded per-sensor live snapshot. Both frontends
# sit on top of this:
#
#   - radar.gui.RadarGUI(RadarEngine)  — the pygame live view (inherits this engine, adds drawing)
#   - radar.web.server                 — a FastAPI/WebSocket server (instantiates RadarEngine directly)
#
# The seam between them is two snapshot methods, both produced under the same lock:
#   - snapshot_for_render()  — sensor-LOCAL latest + tracker dicts; what the pygame view consumes
#                              (its EXACT historical shape, so the pygame path is unchanged).
#   - world_snapshot()       — a serializable, view-agnostic state for the web: each visible target
#                              already projected to WORLD-mm via geometry.local_to_world (the
#                              authoritative, tested projection), plus sensor descriptors. The
#                              browser only scales world-mm -> px + pans/zooms, so what it draws is
#                              exactly what the frames represent.
#
# Smoothing (_avg_motion) lives here too, so BOTH frontends render identical positions.

from __future__ import annotations
import math, time, collections, threading
from typing import Dict, List, Tuple

from radar import constants as C
from radar import geometry
from radar.sensors import Sensor
from radar.reader_base import make_reader, stop_all_readers
from radar.store import TrackStore
from radar.tracking import Tracker, split_speed_for
from radar.logging_setup import get_logger

log = get_logger("engine")


class RadarEngine:
    MAX_V, FOV_DEG = 4000, 120        # speed cap (colour/beep norm); radar field-of-view degrees

    def __init__(self, cfg: dict, store_path=None) -> None:
        # cfg is the loaded radar_config.json (schema v2). We never touch pygame here.
        self.cfg = cfg

        # ―― Sensors (the multi-sensor data model)
        self.sensors: List[Sensor] = [Sensor.from_dict(s) for s in cfg["sensors"]]
        self.sensors_by_id: Dict[str, Sensor] = {s.id: s for s in self.sensors}

        # ―― Smoothing parameters (used by _avg_motion / _win); render-side toggles live in the GUI
        self.smoothing_on = bool(cfg.get("smoothing_on", True))
        self.smooth_level = int(cfg.get("smooth_level", 0))    # 0–9
        self.motion_hist: Dict[str, collections.deque] = {}    # ser -> deque[(x,y,v)] smoothing window

        # ―― Per-sensor diagnostic stats: rx count, recent timestamps (Hz), last hex, last arrival.
        self.stats_by_sid = {
            s.id: {"count": 0, "times": collections.deque(maxlen=30),
                   "last_hex": "", "t_last": 0.0}
            for s in self.sensors
        }

        # ―― Concurrency + per-sensor live state. One reader thread per sensor delivers frames;
        #    _lock guards the tracker + per-sensor latest during update().
        self._lock = threading.Lock()
        self.latest_by_sensor: Dict[str, List[Tuple[int, int, int]]] = {s.id: [] for s in self.sensors}
        self.readers: Dict[str, object] = {}
        self.latest: List[Tuple[int, int, int]] = []   # primary sensor's targets (pygame render path)
        self.fastest = 0.0

        # ―― Watchdog state must exist BEFORE readers start: a reader thread can call _on_frame
        #    immediately, and _on_frame reads/writes these.
        self.t_last_frame = time.monotonic()
        self.data_lost = False

        # ―― Persistence (SQLite) + sensor-aware tracker. Sensors are upserted FIRST so the
        #    tracks.sensor_id foreign key is valid before any reader opens a track.
        self.store = TrackStore((store_path if store_path is not None else C.ROOT / "pondeyes.db"))
        for s in self.sensors:
            self.store.upsert_sensor(s.to_dict())
        self.tracker = Tracker(self.store)

        # Last reader-open error (a sensor's port/broker failed). The pygame CONFIG dialog and the
        # web API both surface this; "" means all readers opened cleanly.
        self.cfg_error = ""

    # ───────────────────────────────────────── lifecycle (the web server uses start/shutdown)
    def start(self):
        self._open_inputs()

    def shutdown(self):
        self._close_inputs()
        self.store.close()

    # ───────────────────────────────────────── open / close data sources
    def _open_inputs(self):
        # Spawn one reader per enabled sensor. Each reader's callback is wrapped in a closure that
        # tags frames with that sensor's id, so _on_frame knows which sensor they came from.
        self._close_inputs()
        self.cfg_error = ""
        for s in self.sensors:
            if not s.enabled:
                continue
            cb = (lambda lst, t, sid=s.id: self._on_frame(sid, lst, t))   # tag with sensor id + capture time
            try:
                r = make_reader(s, cb)
                r.start()
            except OSError as exc:
                # e.g. MQTT broker unreachable — leave this sensor without a reader; the data-loss
                # watchdog surfaces the gap. The error is also exposed to the CONFIG dialog / web API.
                self.cfg_error = f"{s.id}: {exc}"
                log.warning("reader open FAILED for %s (%s): %s", s.id, s.input_mode, exc)
                continue
            self.readers[s.id] = r
            log.info("reader open: %s (%s)", s.id, s.input_mode)
        log.info("inputs open: %d reader(s) live", len(self.readers))

    def _close_inputs(self):
        # Stop and forget every reader (shutdown, before playback, on re-config).
        for r in self.readers.values():
            try:
                r.stop()
            except Exception:
                pass
        self.readers.clear()
        # Backstop: macOS broadcasts serial input to EVERY open handle, so a single surviving
        # reader thread would double-count frames. This guarantees a clean slate.
        stop_all_readers()

    # ───────────────────────────────────────── frame callback (per sensor)
    def _on_frame(self, sensor_id, lst, t_mono):
        # Called on a reader thread for `sensor_id`. `lst` is (slot, x, y, raw_hex) tuples; `t_mono`
        # is the reader's monotonic capture time (frame arrival) — used everywhere instead of a
        # post-lock clock read so the recorded timing stays accurate. The lock makes the tracker +
        # per-sensor latest update atomic w.r.t. the other reader threads.
        sensor = self.sensors_by_id.get(sensor_id)
        if sensor is None:
            return                                   # sensor was removed mid-flight
        pose = (sensor.position[0], sensor.position[1], sensor.heading)
        now = t_mono
        gap = now - self.t_last_frame
        was_lost = self.data_lost
        # Snapshot the sensor's config so the recorded track replays as-recorded (faithful playback).
        snap = {"color": sensor.color, "min_range_mm": sensor.min_range_mm,
                "max_range_mm": sensor.max_range_mm, "trail_on": sensor.trail_on,
                "speed_sensitivity": sensor.speed_sensitivity}
        with self._lock:
            t0 = time.monotonic()
            split = split_speed_for(sensor.speed_sensitivity)   # per-sensor teleport gate
            self.fastest = self.tracker.update(sensor_id, pose, lst, split,
                                               frame_mono=t_mono, sensor_snapshot=snap)
            dur = time.monotonic() - t0
            if dur > 0.10:
                log.warning("slow frame handling for %s: %.0f ms (tracker.update/store write)",
                            sensor_id, dur * 1000)
            # Exclude slots the tracker holds PENDING a split-confirmation (don't draw a teleporting
            # target at the jumped position for the one confirmation frame).
            self.latest_by_sensor[sensor_id] = [
                t[:3] for t in lst if (sensor_id, t[0]) not in self.tracker.pending]
            self.latest = self.latest_by_sensor.get(self.sensors[0].id, []) if self.sensors else []
            self.t_last_frame = now                  # global watchdog (any sensor = "we have data")
            self.data_lost = False
            st = self.stats_by_sid.get(sensor_id)
            if st is not None:
                st["count"] += 1
                st["times"].append(now)
                st["t_last"] = now
                if lst and len(lst[0]) >= 4:
                    st["last_hex"] = lst[0][3]
        if was_lost:
            log.info("data stream RECOVERED on %s after %.2fs", sensor_id, gap)

    def end_all_targets(self):
        # Close every active track (the data stream dropped). The tracker finalises each track in
        # the DB and moves it to `recent`; we clear the engine-side per-target state. (Render-side
        # visuals like trails are cleared by the GUI override.)
        with self._lock:
            self.tracker.end_all()
        for ser in list(self.motion_hist):
            self.motion_hist.pop(ser, None)
        self.latest = []
        self.latest_by_sensor = {sid: [] for sid in self.latest_by_sensor}

    # ───────────────────────────────────────── snapshots (both produced under the lock)
    def _snapshot_for_render(self):
        # Lock-safe copy of the shared live state, then released before any drawing. EXACT historical
        # shape — the pygame render loop iterates this, never the tracker dicts directly.
        with self._lock:
            latest = {sid: list(v) for sid, v in self.latest_by_sensor.items()}
            slot2ser = dict(self.tracker.slot2ser)
            active = {ser: {"hist": info["hist"], "first": info["first"]}
                      for ser, info in self.tracker.active.items()}
            recent = list(self.tracker.recent)
            stats = {sid: (st["count"], st["t_last"], list(st["times"]), st["last_hex"])
                     for sid, st in self.stats_by_sid.items()}
        return dict(latest=latest, slot2ser=slot2ser, active=active, recent=recent, stats=stats)

    def world_snapshot(self):
        # A serializable, view-agnostic state for the web frontend. Each visible target is projected
        # to WORLD-mm here (authoritative geometry.local_to_world), after the SAME smoothing + gate
        # filter the pygame view applies — so both frontends agree. The browser only scales world-mm
        # to pixels and pans/zooms (a cosmetic transform), never re-deriving geometry.
        snap = self._snapshot_for_render()
        sensors = [{"id": s.id, "label": s.label, "color": s.color,
                    "x": s.position[0], "y": s.position[1], "heading": s.heading,
                    "min_range_mm": s.min_range_mm, "max_range_mm": s.max_range_mm,
                    "trail_on": s.trail_on, "fov_deg": self.FOV_DEG, "enabled": s.enabled}
                   for s in self.sensors]
        targets = []
        for s in self.sensors:
            for (slot, x, y) in snap["latest"].get(s.id, []):
                ser = snap["slot2ser"].get((s.id, slot))
                info = snap["active"].get(ser)
                if ser is None or info is None:
                    continue
                ax, ay, av = self._avg_motion(ser, x, y, info["hist"][2])   # smoothing (engine-owned)
                if not self._in_range(s, ax, ay):
                    continue                          # display gate (backend still tracks/logs all)
                wx, wy = geometry.local_to_world(ax, ay, (s.position[0], s.position[1], s.heading))
                targets.append({"sensor_id": s.id, "serial": ser,
                                "x": round(wx, 1), "y": round(wy, 1), "v": round(av, 1)})
        recent = [{"serial": r.get("serial"), "sensor_id": r.get("sensor_id"),
                   "first": r["first"].isoformat(), "last": r["last"].isoformat(),
                   "dur": str(r["dur"]).split(".")[0]} for r in snap["recent"]]
        stats = {}
        for sid, (count, t_last, times, _hex) in snap["stats"].items():
            hz = (len(times) - 1) / (times[-1] - times[0]) if len(times) >= 2 and times[-1] > times[0] else 0.0
            stats[sid] = {"count": count, "t_last": t_last, "hz": round(hz, 1)}
        return {"sensors": sensors, "targets": targets, "recent": recent,
                "stats": stats, "data_lost": self.data_lost}

    # ───────────────────────────────────────── smoothing + gate helpers (shared by both frontends)
    def _win(self) -> int:
        return 1 if not self.smoothing_on else 3 + round(self.smooth_level * 47 / 9)

    def _avg_motion(self, ser, x, y, v):
        win = self._win()
        hist = self.motion_hist.setdefault(ser, collections.deque(maxlen=win))
        if hist.maxlen != win:
            hist = collections.deque(hist, maxlen=win); self.motion_hist[ser] = hist
        hist.append((x, y, v))
        ax = sum(h[0] for h in hist) / len(hist)
        ay = sum(h[1] for h in hist) / len(hist)
        av = sum(h[2] for h in hist) / len(hist)
        return ax, ay, av

    def _in_range(self, sensor, x_local, y_local):
        # Display filter: True if a sensor-local target (mm) is within the sensor's gate, i.e.
        # min_range <= range <= max_range. A 0 on either side disables that side.
        r = math.hypot(x_local, y_local)
        if sensor.max_range_mm > 0 and r > sensor.max_range_mm:
            return False
        if sensor.min_range_mm > 0 and r < sensor.min_range_mm:
            return False
        return True

    # ───────────────────────────────────────── config mutation (the web POST /api/config uses these)
    def update_sensor(self, sensor_id: str, updates: dict, restart: bool = True):
        # Apply field updates to one sensor (pose, gates, transport, colour, ...), persist to the DB,
        # and restart readers if a transport-affecting field changed.
        s = self.sensors_by_id.get(sensor_id)
        if s is None:
            return None
        for k, v in updates.items():
            if hasattr(s, k):
                setattr(s, k, v)
        self.store.upsert_sensor(s.to_dict())
        if restart:
            self._open_inputs()
        return s

    def apply_sensors(self, sensor_dicts: list):
        # Replace the whole sensor set from a list of dicts (the web config form), re-upsert, and
        # restart readers. Keeps sensors_by_id + per-sensor live-state in sync.
        self.sensors = [Sensor.from_dict(d) for d in sensor_dicts]
        self.sensors_by_id = {s.id: s for s in self.sensors}
        self.latest_by_sensor = {s.id: [] for s in self.sensors}
        self.stats_by_sid = {
            s.id: {"count": 0, "times": collections.deque(maxlen=30), "last_hex": "", "t_last": 0.0}
            for s in self.sensors}
        for s in self.sensors:
            self.store.upsert_sensor(s.to_dict())
        self.cfg["sensors"] = [s.to_dict() for s in self.sensors]
        self._open_inputs()
