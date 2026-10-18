# radar.tracking
# ==============
#
# Book-keeping for live & recent radar targets. Persistence is delegated to a TrackStore
# (SQLite); this class only owns the in-memory state and the per-frame kinematics.
#
# Multi-sensor change: a "slot" (the LD2450's target index 1..3) is only unique WITHIN one
# sensor. Two sensors each reporting a target in slot 1 are two different people, so the live
# slot->serial map is keyed by (sensor_id, slot). Serials ("T1", "T2", ...) stay a single
# global, monotonically-increasing counter so logs remain easy to read across sensors.
#
# update() is called once per incoming frame, on the reader's thread. The GUI wraps the call
# in a lock (see RadarGUI), so this class itself does not lock — it assumes single-threaded
# access to its dicts, which the GUI guarantees.

from __future__ import annotations

import datetime as dt
import math
import time
from itertools import count
from typing import Dict, List, Tuple

from radar.store import TrackStore
from radar.logging_setup import get_logger

log = get_logger("tracking")

# ── Speed-sensitivity / slot-reuse split ─────────────────────────────────────────────────
# The LD2450 reuses a target "slot" when one person leaves and another enters within our
# END_TIMEOUT. Without a guard, the old track's serial gets dragged onto the new person and the
# marker "teleports" across the map. We detect that by displacement-per-frame: if a target jumps
# faster than a human/runner plausibly could for the frame interval, we SPLIT the track (close
# the old one, start a new serial). The threshold is set per-sensor by a "speed sensitivity"
# 0..1, mapped here to a max plausible speed (mm/s). Logs showed organic steps < ~600 mm/frame
# vs teleports > 1500 mm/frame, so there is a wide, safe separation.
SPLIT_SPEED_PERMISSIVE_MM_S = 15000.0   # gate at the lowest non-off sensitivity (~15 m/s)
SPLIT_SPEED_STRICT_MM_S     = 3000.0    # gate at sensitivity 1.0 (~3 m/s, indoor)
SPLIT_JITTER_MM             = 350.0     # noise margin added to the per-frame allowance

# ── New-track confirmation (N-of-M) ──────────────────────────────────────────────────────
# The radar characterisation report showed single-frame GHOSTS (mutual interference between
# 24 GHz modules, multipath flashes) that a naive tracker turns into real tracks: a DB row, a
# marker on the map, a beep. The fix is to treat a NEW (sensor, slot) as PROVISIONAL until it
# is seen again close to the same spot — one extra frame (100 ms at 10 Hz) is enough to kill
# one-frame flashes while being imperceptible for a real person walking in.
CONFIRM_RADIUS_MM  = 700.0    # a real target's next frame lands within this of the candidate
                              # (7 m/s at 10 Hz covers a sprinting person with margin)
CONFIRM_WINDOW_S   = 0.6      # a candidate not re-seen within this window is forgotten


def split_speed_for(sensitivity: float):
    # Map a per-sensor speed-sensitivity (0..1) to a max plausible speed in mm/s, or None when
    # splitting is OFF (sensitivity <= 0). Higher sensitivity -> lower allowed speed -> splits
    # sooner. Lower sensitivity -> permissive (keeps fast runners as one track).
    if sensitivity is None or sensitivity <= 0:
        return None
    s = min(1.0, sensitivity)
    return SPLIT_SPEED_PERMISSIVE_MM_S - s * (SPLIT_SPEED_PERMISSIVE_MM_S - SPLIT_SPEED_STRICT_MM_S)


class Tracker:
    END_TIMEOUT = 3.0        # seconds of silence -> a track is considered ended

    def __init__(self, store: TrackStore) -> None:
        self.store = store
        # Live slot ownership, keyed by (sensor_id, slot_no) so sensors don't collide.
        self.slot2ser: Dict[Tuple[str, int], str] = {}
        # serial -> per-track in-memory state (db track_id, motion history, timestamps).
        self.active: Dict[str, Dict] = {}
        # Most recent few completed tracks, for the GUI's "recent" panel.
        self.recent: List[Dict] = []
        # Slots awaiting a 2nd-frame split confirmation:
        #   (sensor_id, slot) -> (candidate_x, candidate_y, old_serial).
        # While a slot is pending we don't move/record/render it; next frame decides
        # CONFIRM (real teleport -> new track) or CANCEL (single-frame noise -> resume). The GUI
        # reads this to suppress drawing the slot for the one pending frame.
        self.pending: Dict[Tuple[str, int], Tuple[float, float, str]] = {}
        # NEW-track candidates awaiting N-of-M confirmation (see CONFIRM_* above):
        #   (sensor_id, slot) -> (cand_x, cand_y, times_seen, last_seen_mono).
        # A provisional slot has NO serial, NO DB track, and is NOT rendered — it either gets
        # confirmed (re-seen nearby) and becomes a real track, or it ages out silently.
        self.provisional: Dict[Tuple[str, int], Tuple[float, float, int, float]] = {}
        # Continue serial numbering from whatever is already in the DB.
        self.serial_iter = count(self.store.max_serial() + 1)

    # ── live target lookup helper (used by the GUI render loop) ──────────────────────────
    def serial_for(self, sensor_id: str, slot: int):
        # Return the live serial owning (sensor_id, slot), or None. Lets the renderer find a
        # target's track without poking at the internal dict shape.
        return self.slot2ser.get((sensor_id, slot))

    # ── public API ───────────────────────────────────────────────────────────────────────
    def update(self, sensor_id: str,
               sensor_pose: Tuple[float, float, float],
               latest: List[Tuple],
               split_speed_mm_s: float = None,
               frame_mono: float = None,
               sensor_snapshot: dict = None,
               confirm_frames: int = 1) -> float:
        # Process one frame's worth of targets from `sensor_id`. `sensor_pose` is (x, y, heading)
        # and `sensor_snapshot` the full sensor config — both snapshotted into newly-opened tracks
        # for faithful playback. `frame_mono` is the reader's monotonic capture time (used for the
        # kinematics dt AND the per-point t_rel_s, so the recorded timeline is accurate); falls
        # back to now if absent. `split_speed_mm_s` is the slot-reuse split gate (None disables).
        # Returns the fastest speed seen this frame (the GUI uses it for the proximity beep).
        fastest = 0.0
        now_mon = time.monotonic() if frame_mono is None else frame_mono
        now_iso = dt.datetime.now().isoformat()                  # absolute anchor (microseconds)

        for item in latest:
            slot, x_mm, y_mm, *rest = item
            raw_hex: str = rest[0] if rest else ""
            key = (sensor_id, slot)

            # (a) A slot that jumped last frame is judged NOW: did the target stay at the new
            #     spot (real teleport -> split) or come back (single-frame noise -> resume)?
            if key in self.pending:
                cand_x, cand_y, old_ser = self.pending.pop(key)
                old = self.active.get(old_ser)
                if old is not None:
                    ax, ay = old["hist"][0], old["hist"][1]
                    if math.hypot(x_mm - cand_x, y_mm - cand_y) < math.hypot(x_mm - ax, y_mm - ay):
                        # CONFIRM: it persisted away from A. Close the old track at A and open a
                        # fresh serial seeded here — no teleport drawn, fresh trail/smoothing.
                        self._expire(old_ser)
                        ser = f"T{next(self.serial_iter)}"
                        self.slot2ser[key] = ser
                        self._open_track(sensor_id, sensor_pose, ser, sensor_snapshot, now_mon)
                        info = self.active[ser]
                        self.store.append_point(info["track_id"], now_iso, x_mm, y_mm,
                                                int(math.hypot(x_mm, y_mm)), 0.0, 0.0, raw_hex,
                                                t_rel_s=0.0)
                        info["hist"] = (x_mm, y_mm, 0.0, now_mon)
                        info["last_ts"] = now_mon
                        log.debug("slot-reuse split confirmed: %s slot %d -> %s", sensor_id, slot, ser)
                        continue
                    # CANCEL: came back toward A — fall through and resume the old track.

            # (b) Ensure this (sensor, slot) owns a LIVE serial; open a new track if not.
            ser = self.slot2ser.get(key)
            if ser is None or ser not in self.active:
                # N-of-M confirmation gate for BRAND-NEW slots (confirm_frames > 1): a first
                # sighting only registers a provisional candidate; the track opens when the
                # target is re-seen within CONFIRM_RADIUS_MM. One-frame interference ghosts
                # never come back at the same spot, so they die here — no serial, no DB row,
                # no marker, no beep.
                if confirm_frames > 1:
                    prov = self.provisional.get(key)
                    if prov is not None and math.hypot(x_mm - prov[0], y_mm - prov[1]) <= CONFIRM_RADIUS_MM:
                        seen = prov[2] + 1
                        if seen < confirm_frames:      # still gathering confirmations
                            self.provisional[key] = (x_mm, y_mm, seen, now_mon)
                            continue
                        self.provisional.pop(key, None)   # CONFIRMED -> fall through, open track
                    else:
                        # First sighting (or the candidate moved implausibly): (re)start the
                        # candidate here and wait for the next frame.
                        self.provisional[key] = (x_mm, y_mm, 1, now_mon)
                        continue
                ser = f"T{next(self.serial_iter)}"
                self.slot2ser[key] = ser
                self._open_track(sensor_id, sensor_pose, ser, sensor_snapshot, now_mon)

            info = self.active[ser]

            # Kinematics from the previous point. The very first point of a track has no
            # history, so velocity/accel are 0 (avoids a huge spike from the (0,0,0) seed).
            px, py, pv, pt = info["hist"]
            first_point = (px == py == pv == 0.0)
            dt_s = max(now_mon - pt, 1e-3)
            disp = 0.0 if first_point else math.hypot(x_mm - px, y_mm - py)

            # (c) Jump detection: a step too far to be the same target. Don't move/record/render
            #     this frame — mark it pending and let the NEXT frame confirm or cancel.
            if (not first_point and split_speed_mm_s
                    and disp > split_speed_mm_s * dt_s + SPLIT_JITTER_MM):
                self.pending[key] = (x_mm, y_mm, ser)
                info["last_ts"] = now_mon            # keep the old track alive while we wait
                continue

            v = 0.0 if first_point else disp / dt_s
            accel = 0.0 if first_point else (v - pv) / dt_s
            rng = math.hypot(x_mm, y_mm)
            fastest = max(fastest, v)

            # Persist the sample with accurate relative timing (seconds since this track opened).
            self.store.append_point(info["track_id"], now_iso, x_mm, y_mm, int(rng),
                                    v, accel, raw_hex, t_rel_s=now_mon - info["open_mono"])

            info["hist"] = (x_mm, y_mm, v, now_mon)
            info["last_ts"] = now_mon

        # Expire tracks that have gone silent past the timeout.
        for ser in list(self.active):
            if now_mon - self.active[ser]["last_ts"] > self.END_TIMEOUT:
                self._expire(ser)

        # Drop pending entries whose old track has since expired (slot went quiet mid-wait).
        if self.pending:
            self.pending = {k: v for k, v in self.pending.items() if v[2] in self.active}

        # Age out provisional candidates that were never re-seen (the one-frame ghosts).
        if self.provisional:
            self.provisional = {k: v for k, v in self.provisional.items()
                                if now_mon - v[3] <= CONFIRM_WINDOW_S}

        return fastest

    def end_all(self) -> None:
        # Close every live track (e.g. when the data stream drops). Each is finalised in the
        # DB and moved to `recent`, exactly as a normal timeout expiry would do.
        for ser in list(self.active):
            self._expire(ser)

    # ── internals ────────────────────────────────────────────────────────────────────────
    def _open_track(self, sensor_id: str, sensor_pose, ser: str,
                    snapshot: dict = None, open_mono: float = None) -> None:
        # Open a DB track and seed its in-memory state. `snapshot` is the sensor config persisted
        # for faithful playback; `open_mono` is the track's monotonic time origin (so per-point
        # t_rel_s = frame_mono - open_mono). hist starts at the (0,0,0) sentinel so the first real
        # point is detected as `first_point` above.
        ts = dt.datetime.now()
        om = time.monotonic() if open_mono is None else open_mono
        track_id = self.store.open_track(
            sensor_id, ser, sensor_pose, ts.isoformat(), snapshot=snapshot
        )
        self.active[ser] = dict(
            track_id=track_id,
            sensor_id=sensor_id,
            first=ts,
            open_mono=om,
            hist=(0.0, 0.0, 0.0, om),
            last_ts=om,
        )

    def _expire(self, ser: str) -> None:
        # Finalise a track: stamp its duration in the DB, push it onto the recent list, and
        # drop it from the live dicts (freeing its (sensor_id, slot) for reuse).
        info = self.active[ser]
        first = info["first"]
        last = dt.datetime.now()
        dur = last - first

        self.store.close_track(
            info["track_id"], last.isoformat(timespec="milliseconds"), dur.total_seconds()
        )

        self.recent.insert(0, dict(serial=ser, first=first, last=last, dur=dur,
                                   sensor_id=info["sensor_id"]))
        if len(self.recent) > 3:
            self.recent.pop()

        del self.active[ser]
        # Remove every slot mapping that pointed at this now-dead serial.
        self.slot2ser = {k: t for k, t in self.slot2ser.items() if t != ser}
