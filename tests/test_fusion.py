# tests.test_fusion
# =================
# Fusion contract tests — updated for v2 (adaptive gains, ID recycling, 12-person capacity).

import math

from radar.fusion import FusionTracker, CONFIRM_HITS
from radar.sensors import Sensor


def _rig():
    s1 = Sensor(id="S1", position=(0.0, 0.0), heading=-45.0, max_range_mm=8000.0)
    s2 = Sensor(id="S2", position=(6000.0, 6000.0), heading=135.0, max_range_mm=8000.0)
    return FusionTracker([s1, s2]), s1, s2


def test_two_sensors_one_person_one_id():
    fus, s1, s2 = _rig()
    t = 100.0
    for i in range(6):
        fus.on_sensor_frame("S1", [(3000.0 + i * 5, 3000.0)], (0, 0), t, 1)
        fus.on_sensor_frame("S2", [(3050.0 + i * 5, 2960.0)], (6000, 6000), t + 0.02, 1)
        t += 0.1
        fus.tick(t)
    people = fus.snapshot(t)
    assert len(people) == 1
    assert people[0]["n_sensors"] == 2


def test_one_frame_ghost_never_confirmed():
    fus, *_ = _rig()
    t = 200.0
    fus.on_sensor_frame("S1", [(1500.0, 1500.0)], (0, 0), t, 1)
    fus.tick(t)
    assert fus.snapshot(t) == []
    t += 2.0
    fus.tick(t)
    assert fus.tracks == {}


def test_single_sensor_confirms_after_n_hits():
    fus, *_ = _rig()
    t = 300.0
    for i in range(CONFIRM_HITS):
        fus.on_sensor_frame("S1", [(2000.0, 2000.0 + i * 60)], (0, 0), t, 1)
        t += 0.1
    fus.tick(t)
    people = fus.snapshot(t)
    assert len(people) == 1 and not people[0]["suspect"]


def test_multipath_shadow_needs_more_evidence():
    fus, *_ = _rig()
    t = 400.0
    for i in range(CONFIRM_HITS):
        fus.on_sensor_frame("S1", [(1414.0, 1414.0), (2475.0, 2475.0)], (0, 0), t, 2)
        t += 0.1
    fus.tick(t)
    ids = fus.snapshot(t)
    assert len(ids) == 1


def test_snapshot_predicts_between_frames():
    fus, *_ = _rig()
    t = 500.0
    for i in range(8):
        fus.on_sensor_frame("S1", [(1000.0 + i * 100.0, 3000.0)], (0, 0), t + i * 0.1, 1)
    t_last = t + 0.7
    p0 = fus.snapshot(t_last)[0]
    p1 = fus.snapshot(t_last + 0.05)[0]
    assert p1["x"] > p0["x"]
    assert math.isclose(p1["y"], p0["y"], abs_tol=50)


def test_id_recycling():
    """When a track dies, its numeric ID goes back to the pool and gets reused."""
    fus, *_ = _rig()
    t = 600.0
    # Birth P1
    for i in range(CONFIRM_HITS):
        fus.on_sensor_frame("S1", [(2000.0, 2000.0)], (0, 0), t, 1)
        t += 0.1
    assert any(tr.tid == "P1" for tr in fus.tracks.values())
    # Let P1 die (coast timeout)
    t += 5.0
    fus.tick(t)
    assert "P1" not in fus.tracks
    # Next birth should reuse "P1", not "P2"
    fus.on_sensor_frame("S1", [(4000.0, 4000.0)], (0, 0), t, 1)
    new_tids = list(fus.tracks.keys())
    assert "P1" in new_tids


def test_stationary_target_no_jitter():
    """A stationary target with noisy measurements should have near-zero velocity."""
    import random
    random.seed(42)
    fus, *_ = _rig()
    t = 700.0
    base_x, base_y = 3000.0, 3000.0
    for i in range(30):  # 3 seconds of noisy stationary data
        nx = base_x + random.gauss(0, 200)  # ±200mm noise
        ny = base_y + random.gauss(0, 200)
        fus.on_sensor_frame("S1", [(nx, ny)], (0, 0), t, 1)
        t += 0.1
    fus.tick(t)
    people = fus.snapshot(t)
    assert len(people) == 1
    # Velocity should be well-damped, not hundreds of mm/s from noise
    assert people[0]["v"] < 200.0, f"velocity too high for stationary: {people[0]['v']:.0f} mm/s"


def test_nearby_people_stay_separate():
    """Two people 1.5m apart should not merge into one track."""
    fus, *_ = _rig()
    t = 800.0
    for i in range(8):
        fus.on_sensor_frame("S1", [(2000.0, 3000.0), (3500.0, 3000.0)], (0, 0), t, 2)
        t += 0.1
    fus.tick(t)
    people = fus.snapshot(t)
    assert len(people) == 2, f"expected 2 people, got {len(people)}"


def test_end_all_resets_ids():
    """end_all() should reset the ID counter so next birth starts at P1."""
    fus, *_ = _rig()
    t = 900.0
    for i in range(CONFIRM_HITS):
        fus.on_sensor_frame("S1", [(2000.0, 2000.0)], (0, 0), t, 1)
        t += 0.1
    fus.end_all()
    fus.on_sensor_frame("S1", [(4000.0, 4000.0)], (0, 0), t + 1, 1)
    assert "P1" in fus.tracks
