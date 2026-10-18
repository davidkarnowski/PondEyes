# radar.fusion
# ============
#
# WORLD-frame multi-sensor fusion on top of the per-sensor Tracker.
#
# Why this exists (the 4-corner rig problem):
#   The per-sensor Tracker gives every (sensor, slot) its OWN serial. With 4 radars whose
#   coverage overlaps in the middle, ONE person walking through the overlap is reported by
#   2-4 sensors at once -> 2-4 markers with 4 different serials for the SAME human. This
#   module associates those detections in WORLD-mm and maintains ONE global "person" track
#   per human: id "P1", "P2", ... The map draws these instead of the raw per-sensor dots.
#
# Ghost handling:
#   1. MUTUAL-INTERFERENCE FLASHES: short-lived single-sensor detection -> must survive
#      CONFIRM_HITS updates before drawn. Two-sensor corroboration = instant confirm.
#   2. MULTIPATH GHOSTS: same-bearing, behind a closer det -> born "shadow", needs extra hits.
#   3. UNCORROBORATED TRACKS: in multi-sensor coverage but fed by only one sensor -> "suspect".
#
# Smoothness:
#   Alpha-beta filter with ADAPTIVE gains: residuals within sensor noise floor get reduced
#   gains to avoid injecting jitter. Stationary targets have explicit velocity damping.
#   Render-time prediction (snapshot()) interpolates at display FPS.
#
# ID management:
#   Recycled via min-heap: when P3 dies, "3" goes back to the pool and the next birth
#   reuses it. IDs stay small and human-readable (P1..P12, not P847).

from __future__ import annotations

import heapq
import math
import time
from typing import Dict, List, Optional, Tuple

from radar.logging_setup import get_logger

log = get_logger("fusion")

# ── association ─────────────────────────────────────────────────────────────────────────
GATE_BASE_MM        = 600.0    # base association gate (was 900 — too wide for 12-person density)
GATE_SPEED_S        = 0.40     # gate grows by speed * this (compensates inter-frame motion)
GATE_MAX_MM         = 1800.0   # hard cap
SAME_SENSOR_PENALTY = 1.15     # same-sensor re-association gets a distance penalty to prefer
                               # cross-sensor matches (helps ID stability in overlap zones)

# ── alpha-beta filter ───────────────────────────────────────────────────────────────────
ALPHA               = 0.18     # position gain  (was 0.45 — caused visible jitter)
BETA                = 0.04     # velocity gain   (was 0.20 — amplified noise into velocity)
NOISE_FLOOR_MM      = 250.0    # LD2450/RD-03D typical per-frame noise radius
NOISE_ALPHA_MIN     = 0.30     # within noise floor, alpha scales down to ALPHA * this
NOISE_BETA_MIN      = 0.10     # within noise floor, beta  scales down to BETA  * this
V_MAX_MM_S          = 3500.0   # clamp to plausible human sprint
V_DAMP_THRESH_MM_S  = 300.0    # below this, actively damp velocity (stops noise random walk)
V_DAMP_FACTOR       = 0.65     # per-update velocity decay when "stationary"

# ── coasting ────────────────────────────────────────────────────────────────────────────
COAST_S             = 1.4      # drop track after this long without measurement
COAST_V_DECAY       = 0.82     # per-update velocity bleed while coasting

# ── confirmation (anti ghost) ───────────────────────────────────────────────────────────
CONFIRM_HITS        = 3        # single-sensor births: updates needed before drawn
CONFIRM_HITS_SHADOW = 6        # multipath-suspect births need double evidence
CONFIRM_WINDOW_S    = 1.2      # tentative track dies if not confirmed in this window

# ── multipath shadow test ───────────────────────────────────────────────────────────────
SHADOW_BEARING_DEG  = 6.0      # same bearing threshold
SHADOW_MIN_GAP_MM   = 700.0    # must be this much farther than the closer det

# ── suspect demotion ────────────────────────────────────────────────────────────────────
SUSPECT_AFTER_S     = 2.0
COVER_RANGE_SHRINK  = 0.9
COVER_FOV_DEG       = 110.0

# ── sensor-seen expiry ──────────────────────────────────────────────────────────────────
SENSOR_SEEN_EXPIRY_S = 2.0     # forget a sensor association after this long without update


class _Track:
    __slots__ = ("tid", "x", "y", "vx", "vy", "t", "hits", "born", "shadow",
                 "confirmed", "sensors_seen", "suspect", "single_since",
                 "_last_sid")

    def __init__(self, tid: str, x: float, y: float, t: float, shadow: bool):
        self.tid = tid
        self.x, self.y = x, y
        self.vx = self.vy = 0.0
        self.t = t
        self.hits = 1
        self.born = t
        self.shadow = shadow
        self.confirmed = False
        self.sensors_seen: Dict[str, float] = {}
        self.suspect = False
        self.single_since: Optional[float] = None
        self._last_sid: Optional[str] = None        # last sensor that fed this track

    def predict(self, t: float) -> Tuple[float, float]:
        dt = max(0.0, t - self.t)
        return self.x + self.vx * dt, self.y + self.vy * dt

    def update(self, zx: float, zy: float, t: float):
        dt = min(max(t - self.t, 1e-3), 0.5)
        px, py = self.x + self.vx * dt, self.y + self.vy * dt
        rx, ry = zx - px, zy - py
        r_mag = math.hypot(rx, ry)

        # ── Adaptive gain: reduce within sensor noise floor to stop jitter ──
        a, b = ALPHA, BETA
        if r_mag < NOISE_FLOOR_MM and r_mag > 0.01:
            k = r_mag / NOISE_FLOOR_MM                        # 0..1
            a *= NOISE_ALPHA_MIN + (1.0 - NOISE_ALPHA_MIN) * k
            b *= NOISE_BETA_MIN  + (1.0 - NOISE_BETA_MIN)  * k

        self.x = px + a * rx
        self.y = py + a * ry
        self.vx += b * rx / dt
        self.vy += b * ry / dt

        # ── Velocity management ──
        sp = math.hypot(self.vx, self.vy)
        if sp < V_DAMP_THRESH_MM_S:
            # Stationary: actively damp velocity to prevent noise random-walk
            self.vx *= V_DAMP_FACTOR
            self.vy *= V_DAMP_FACTOR
        elif sp > V_MAX_MM_S:
            k = V_MAX_MM_S / sp
            self.vx *= k
            self.vy *= k

        self.t = t
        self.hits += 1

    def speed(self) -> float:
        return math.hypot(self.vx, self.vy)

    def expire_sensors(self, now: float):
        """Remove stale sensor associations."""
        stale = [sid for sid, ts in self.sensors_seen.items()
                 if now - ts > SENSOR_SEEN_EXPIRY_S]
        for sid in stale:
            del self.sensors_seen[sid]


class FusionTracker:
    """Associates world-frame detections from N sensors into global person tracks."""

    def __init__(self, sensors: list):
        self.set_sensors(sensors)
        self.tracks: Dict[str, _Track] = {}
        # ID recycling: min-heap of freed numeric IDs
        self._next_id = 1
        self._free_ids: List[int] = []
        # per-sensor bookkeeping
        self._last_count: Dict[str, int] = {}
        self._last_frame: Dict[str, float] = {}

    def _alloc_id(self) -> str:
        if self._free_ids:
            n = heapq.heappop(self._free_ids)
        else:
            n = self._next_id
            self._next_id += 1
        return f"P{n}"

    def _free_id(self, tid: str):
        try:
            n = int(tid[1:])
            heapq.heappush(self._free_ids, n)
        except ValueError:
            pass

    def set_sensors(self, sensors: list):
        self._sensors = [
            dict(id=s.id, x=s.position[0], y=s.position[1], hd=s.heading,
                 min_r=s.min_range_mm, max_r=s.max_range_mm or 8000.0, enabled=s.enabled)
            for s in sensors
        ]

    # ── main entry ──────────────────────────────────────────────────────────────────────
    def on_sensor_frame(self, sensor_id: str,
                        dets_world: List[Tuple[float, float]],
                        sensor_pos: Tuple[float, float],
                        t: float,
                        reported_count: int):
        self._last_count[sensor_id] = reported_count
        self._last_frame[sensor_id] = t

        # 1. multipath shadow test
        shadow_flags = [False] * len(dets_world)
        sx, sy = sensor_pos
        polar = [(math.hypot(x - sx, y - sy), math.atan2(y - sy, x - sx))
                 for x, y in dets_world]
        for i in range(len(dets_world)):
            for j in range(len(dets_world)):
                if i == j:
                    continue
                ri, ai = polar[i]
                rj, aj = polar[j]
                dang = math.degrees(abs((ai - aj + math.pi) % (2 * math.pi) - math.pi))
                if dang <= SHADOW_BEARING_DEG and ri - rj >= SHADOW_MIN_GAP_MM:
                    shadow_flags[i] = True

        # 2. gated greedy association — prefer cross-sensor matches for ID stability
        preds = {tid: tr.predict(t) for tid, tr in self.tracks.items()}
        pairs = []
        for tid, (px, py) in preds.items():
            tr = self.tracks[tid]
            gate = min(GATE_MAX_MM, GATE_BASE_MM + tr.speed() * GATE_SPEED_S)
            for di, (dx, dy) in enumerate(dets_world):
                d = math.hypot(dx - px, dy - py)
                # Penalize same-sensor re-association to prefer cross-sensor corroboration
                if tr._last_sid == sensor_id:
                    d *= SAME_SENSOR_PENALTY
                if d <= gate:
                    pairs.append((d, tid, di))
        pairs.sort(key=lambda p: p[0])
        used_t, used_d = set(), set()
        for d, tid, di in pairs:
            if tid in used_t or di in used_d:
                continue
            used_t.add(tid)
            used_d.add(di)
            tr = self.tracks[tid]
            tr.update(dets_world[di][0], dets_world[di][1], t)
            tr.sensors_seen[sensor_id] = t
            tr._last_sid = sensor_id
            if not tr.confirmed:
                need = CONFIRM_HITS_SHADOW if tr.shadow else CONFIRM_HITS
                two_sensors = len([1 for ts in tr.sensors_seen.values()
                                   if t - ts < 0.6]) >= 2
                if tr.hits >= need or two_sensors:
                    tr.confirmed = True
                    log.debug("confirmed %s (hits=%d sensors=%s shadow=%s)",
                              tid, tr.hits, list(tr.sensors_seen), tr.shadow)

        # 3. unmatched detections -> tentative new tracks
        for di, (dx, dy) in enumerate(dets_world):
            if di in used_d:
                continue
            tid = self._alloc_id()
            tr = _Track(tid, dx, dy, t, shadow=shadow_flags[di])
            tr.sensors_seen[sensor_id] = t
            tr._last_sid = sensor_id
            self.tracks[tid] = tr

    # ── periodic maintenance ────────────────────────────────────────────────────────────
    def tick(self, now: Optional[float] = None):
        now = time.monotonic() if now is None else now
        dead = []
        for tid, tr in self.tracks.items():
            age = now - tr.t
            if tr.confirmed:
                if age > COAST_S:
                    dead.append(tid)
                    continue
                if age > 0.25:
                    tr.vx *= COAST_V_DECAY
                    tr.vy *= COAST_V_DECAY
                tr.expire_sensors(now)
                self._update_suspect(tr, now)
            else:
                if now - tr.born > CONFIRM_WINDOW_S:
                    dead.append(tid)
        for tid in dead:
            self._free_id(tid)
            del self.tracks[tid]

    def _covering_sensors(self, x: float, y: float) -> List[str]:
        out = []
        for s in self._sensors:
            if not s["enabled"]:
                continue
            dx, dy = x - s["x"], y - s["y"]
            c = math.cos(math.radians(s["hd"]))
            si = math.sin(math.radians(s["hd"]))
            xl = c * dx - si * dy
            yl = si * dx + c * dy
            r = math.hypot(xl, yl)
            if r < max(s["min_r"], 1.0) or r > s["max_r"] * COVER_RANGE_SHRINK:
                continue
            if yl <= 0:
                continue
            if math.degrees(abs(math.atan2(xl, yl))) > COVER_FOV_DEG / 2:
                continue
            out.append(s["id"])
        return out

    def _update_suspect(self, tr: _Track, now: float):
        cover = self._covering_sensors(tr.x, tr.y)
        fresh = [sid for sid, ts in tr.sensors_seen.items() if now - ts < COAST_S]
        if len(cover) >= 2 and len(fresh) == 1:
            silent_with_capacity = [
                sid for sid in cover
                if sid not in fresh
                and now - self._last_frame.get(sid, 0.0) < 0.6
                and self._last_count.get(sid, 3) < 3
            ]
            if silent_with_capacity:
                if tr.single_since is None:
                    tr.single_since = now
                tr.suspect = (now - tr.single_since) >= SUSPECT_AFTER_S
                return
        tr.single_since = None
        tr.suspect = False

    # ── render snapshot ─────────────────────────────────────────────────────────────────
    def snapshot(self, now: Optional[float] = None) -> List[dict]:
        now = time.monotonic() if now is None else now
        out = []
        for tr in self.tracks.values():
            if not tr.confirmed:
                continue
            px, py = tr.predict(min(now, tr.t + COAST_S))
            out.append(dict(id=tr.tid, x=px, y=py,
                            vx=tr.vx, vy=tr.vy, v=tr.speed(),
                            sensors=sorted(tr.sensors_seen),
                            n_sensors=len([1 for ts in tr.sensors_seen.values()
                                           if now - ts < COAST_S]),
                            suspect=tr.suspect,
                            age=now - tr.born))
        return out

    def end_all(self):
        self.tracks.clear()
        self._free_ids.clear()
        self._next_id = 1
