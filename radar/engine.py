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
from dataclasses import replace
from typing import Dict, List, Tuple

from radar import constants as C
from radar import config
from radar import geometry
from radar.sensors import Sensor
from radar.reader_base import make_reader, stop_all_readers
from radar.store import TrackStore
from radar.tracking import Tracker, split_speed_for
from radar.logging_setup import get_logger

log = get_logger("engine")


class RadarEngine:
    MAX_V, FOV_DEG = 4000, 120        # speed cap (colour/beep norm); radar field-of-view degrees
    DATA_TIMEOUT_SEC = 1.0            # gap (ALL sensors quiet) that flags global data loss
    MARKER_STALE_SEC = 1.5            # per-sensor: drop a quiet sensor's live markers after this gap

    def __init__(self, cfg: dict, store_path=None, sim: bool = False) -> None:
        # cfg is the loaded radar_config.json (schema v2). We never touch pygame here.
        # sim=True forces every sensor's reader to a FakeReader (simulated targets) regardless of its
        # configured transport, so the app runs with NO hardware connected. Sensors keep their real
        # pose/colour/gates AND their stored input_mode — sim is a runtime override, NOT persisted —
        # so the map looks exactly like the live rig, just with simulated motion.
        self.cfg = cfg
        self.force_sim = bool(sim)

        # ―― Sensors (the multi-sensor data model)
        self.sensors: List[Sensor] = [Sensor.from_dict(s) for s in cfg.get("sensors", [])]
        if self.force_sim and not self.sensors:
            # Nothing configured + sim: give the sim a default sensor so the app is still usable.
            self.sensors = [Sensor(id="SIM", label="Sim", color="#00ff88", input_mode="sim",
                                   position=(2000.0, 2000.0), heading=0.0, max_range_mm=8000.0)]
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
        # Per-target flags live on the ENGINE (backend truth) so they're shared across UIs: a target
        # ghosted/silenced in one view shows the same in the other.
        self.ghosted: set = set()       # serials marked false (greyed/translucent + silent)
        self.silenced: set = set()      # serials muted from the proximity beep

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
            # In sim mode, build a FakeReader from a sim VIEW of the sensor (its real input_mode is
            # left untouched, so config isn't corrupted); frames still attribute to the real s.id.
            reader_sensor = replace(s, input_mode="sim") if self.force_sim else s
            try:
                r = make_reader(reader_sensor, cb)
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
            self.motion_hist.clear()        # under the lock — _avg_motion also locks, so no race
            self.latest = []
            self.latest_by_sensor = {sid: [] for sid in self.latest_by_sensor}

    def set_target_flag(self, serial: str, ghost=None, silence=None):
        # Toggle a target's ghost/silence flag on the BACKEND so BOTH UIs share it. Pass a bool to
        # set, None to leave unchanged. Returns the resulting flags.
        if ghost is not None:
            (self.ghosted.add if ghost else self.ghosted.discard)(serial)
        if silence is not None:
            (self.silenced.add if silence else self.silenced.discard)(serial)
        return {"serial": serial, "ghost": serial in self.ghosted, "silence": serial in self.silenced}

    # ───────────────────────────────────────── data-loss watchdog + per-sensor staleness
    # Shared by EVERY frontend: the pygame run loop and the web world_snapshot() both call these,
    # so data_lost / marker staleness behave identically regardless of how PondEyes is rendered.
    def _cull_stale_markers(self, now=None):
        # Hide the live markers of any sensor that has individually gone quiet longer than
        # MARKER_STALE_SEC (independent of the others). Clears MARKERS only; the tracks expire on
        # the Tracker's own timeout, so a brief dropout keeps continuity.
        now = time.monotonic() if now is None else now
        for s in self.sensors:
            st = self.stats_by_sid.get(s.id)
            if (st and st["count"] and self.latest_by_sensor.get(s.id)
                    and (now - st["t_last"]) > self.MARKER_STALE_SEC):
                with self._lock:
                    self.latest_by_sensor[s.id] = []

    def check_watchdog(self, now=None):
        # Global DATA-LOSS: when EVERY sensor has been quiet past DATA_TIMEOUT_SEC, flip data_lost
        # (reset to False by _on_frame on any frame) and end the live tracks. Returns data_lost.
        now = time.monotonic() if now is None else now
        if not self.data_lost and (now - self.t_last_frame) > self.DATA_TIMEOUT_SEC:
            self.data_lost = True
            ages = ", ".join(
                (f"{s.id}={now - self.stats_by_sid[s.id]['t_last']:.2f}s"
                 if self.stats_by_sid[s.id]['count'] else f"{s.id}=never")
                for s in self.sensors)
            log.warning("DATA STREAM LOST after %.2fs gap (per-sensor last-seen: %s)",
                        now - self.t_last_frame, ages)
            self.end_all_targets()
        return self.data_lost

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
        # NOTE: this is a PURE read — the caller's loop drives staleness + the watchdog
        # (pygame run loop / web WS loop call _cull_stale_markers() + check_watchdog()); data_lost
        # below just reflects the current flag.
        snap = self._snapshot_for_render()
        now = time.monotonic()
        def _stale(sid):                          # was streaming, now individually quiet -> NO DATA
            st = snap["stats"].get(sid)
            return bool(st and st[0] and (now - st[1]) > self.DATA_TIMEOUT_SEC)
        sensors = [{"id": s.id, "label": s.label, "color": s.color,
                    "x": s.position[0], "y": s.position[1], "heading": s.heading,
                    "min_range_mm": s.min_range_mm, "max_range_mm": s.max_range_mm,
                    "trail_on": s.trail_on, "fov_deg": self.FOV_DEG, "enabled": s.enabled,
                    "stale": _stale(s.id)}
                   for s in self.sensors]
        targets = []
        for s in self.sensors:
            for (slot, x, y) in snap["latest"].get(s.id, []):
                ser = snap["slot2ser"].get((s.id, slot))
                info = snap["active"].get(ser)
                if ser is None or info is None:
                    continue
                # RAW world-mm (no smoothing here): smoothing buffers are owned by the pygame render
                # loop, and touching them from the web push thread races it (a crash under --serve).
                if not self._in_range(s, x, y):
                    continue                          # display gate (backend still tracks/logs all)
                wx, wy = geometry.local_to_world(x, y, (s.position[0], s.position[1], s.heading))
                targets.append({"sensor_id": s.id, "serial": ser,
                                "x": round(wx, 1), "y": round(wy, 1), "v": round(info["hist"][2], 1),
                                "ghost": ser in self.ghosted, "silence": ser in self.silenced})
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
        # Lock-guarded: the pygame render loop calls this while reader threads (and, under --serve,
        # the web push thread via end_all_targets) may touch motion_hist — without the lock the
        # deque could be mutated mid-iteration (a crash).
        with self._lock:
            win = self._win()
            hist = self.motion_hist.setdefault(ser, collections.deque(maxlen=win))
            if hist.maxlen != win:
                hist = collections.deque(hist, maxlen=win); self.motion_hist[ser] = hist
            hist.append((x, y, v))
            return (sum(h[0] for h in hist) / len(hist),
                    sum(h[1] for h in hist) / len(hist),
                    sum(h[2] for h in hist) / len(hist))

    def _in_range(self, sensor, x_local, y_local):
        # Display filter: True if a sensor-local target (mm) is within the sensor's gate, i.e.
        # min_range <= range <= max_range. A 0 on either side disables that side.
        r = math.hypot(x_local, y_local)
        if sensor.max_range_mm > 0 and r > sensor.max_range_mm:
            return False
        if sensor.min_range_mm > 0 and r < sensor.min_range_mm:
            return False
        return True

    # ───────────────────────────────────────── config persistence (backend = source of truth)
    def save_config(self):
        # Persist the current engine state to radar_config.json. The backend OWNS persistence, so
        # BOTH UIs route their config writes through here — settings are stored durably (survive a
        # restart) regardless of which UI made the change.
        self.cfg["sensors"] = [s.to_dict() for s in self.sensors]
        config.save(self.cfg)

    # ───────────────────────────────────────── config mutation (the web POST /api/config uses these)
    def update_sensor(self, sensor_id: str, updates: dict, restart: bool = True):
        # Apply field updates to one sensor (pose, gates, transport, colour, ...), persist to the DB
        # + config file, and restart readers if a transport-affecting field changed.
        s = self.sensors_by_id.get(sensor_id)
        if s is None:
            return None
        for k, v in updates.items():
            if hasattr(s, k):
                setattr(s, k, v)
        self.store.upsert_sensor(s.to_dict())
        self.save_config()
        if restart:
            self._open_inputs()
        return s

    def autobaud(self, sensor_id: str, port: str = None):
        # Probe a serial sensor's port for the right baud — SERVER-SIDE, where the port lives (so it
        # works for a remote web client too). Frees the port, scans every candidate, applies +
        # persists the found baud, then reopens. `port` (optional) lets the UI probe an edited-but-
        # unsaved port. Returns the detected baud or None.
        from radar.serial_reader import probe_baud
        s = self.sensors_by_id.get(sensor_id)
        if s is None:
            return None
        if port:
            s.serial_port = port
        self._close_inputs()                       # release the port so the probe can open it
        found = None
        try:
            found = probe_baud(s.serial_port)
        except Exception as exc:                   # pragma: no cover - hardware/probe error
            self.cfg_error = f"auto-baud: {exc}"
        if found:
            s.serial_baud = found
            self.store.upsert_sensor(s.to_dict()); self.save_config()
            self.cfg_error = f"Auto-baud: {found} on {s.serial_port}"
        elif not self.cfg_error:
            self.cfg_error = f"Auto-baud: no LD2450 frames on {s.serial_port}"
        self._open_inputs()
        return found

    def apply_sensors(self, sensor_dicts: list):
        # Replace the whole sensor set from a list of dicts (a config form on EITHER UI), re-upsert,
        # and restart readers. Safe under concurrent UIs (pygame main thread + web request thread):
        # stop the readers first (so no _on_frame races the swap), swap the live state under the
        # lock, then reopen. Keeps sensors_by_id + per-sensor live-state in sync.
        self._close_inputs()
        with self._lock:
            self.sensors = [Sensor.from_dict(d) for d in sensor_dicts]
            self.sensors_by_id = {s.id: s for s in self.sensors}
            self.latest_by_sensor = {s.id: [] for s in self.sensors}
            self.stats_by_sid = {
                s.id: {"count": 0, "times": collections.deque(maxlen=30), "last_hex": "", "t_last": 0.0}
                for s in self.sensors}
        for s in self.sensors:
            self.store.upsert_sensor(s.to_dict())
        self.save_config()          # durable: the backend stores the new sensor set
        self._open_inputs()
