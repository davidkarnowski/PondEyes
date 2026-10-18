# tests.test_stability
# ====================
#
# The stability upgrades from the radar characterisation report, each tested at its seam:
#
#   1. N-of-M new-track confirmation (tracker): a single-frame ghost (mutual-interference /
#      multipath flash) must NOT open a track, hit the DB, or render; a real target that
#      persists one more frame must open exactly one track.
#   2. Serial reconnect (reader): a missing/failed port must keep retrying in the background
#      (async startup — the app never blocks or gives up), and the health counters must say so.
#   3. MQTT async startup (reader): start() must return immediately even when the broker is
#      unreachable — the old blocking connect() was the app's startup hang.

import time
import datetime as dt

from radar.store import TrackStore
from radar.tracking import Tracker, CONFIRM_RADIUS_MM


def _sensor(sid):
    return {"id": sid, "label": sid, "input_mode": "serial", "position": [0, 0], "heading": 0}


def _tracker(tmp_path):
    st = TrackStore(tmp_path / "t.db")
    st.upsert_sensor(_sensor("S1"))
    return st, Tracker(st)


# ── 1. N-of-M confirmation ───────────────────────────────────────────────────────────────

def test_single_frame_ghost_opens_no_track(tmp_path):
    # A target seen ONCE and never again (the interference-ghost signature) must leave no
    # trace: no serial, no active track, no DB row, and after the window not even a candidate.
    st, tr = _tracker(tmp_path)
    tr.update("S1", (0, 0, 0), [(1, 500, 500, "")], frame_mono=10.0, confirm_frames=2)
    assert tr.serial_for("S1", 1) is None          # no serial handed out
    assert len(tr.active) == 0                     # no live track
    assert ("S1", 1) in tr.provisional             # held as a candidate only

    # The slot goes quiet; later frames (other slots) age the candidate out.
    tr.update("S1", (0, 0, 0), [], frame_mono=11.0, confirm_frames=2)
    assert tr.provisional == {}                    # ghost fully forgotten
    assert st.list_tracks_on_day(dt.date.today().isoformat()) == []   # DB untouched
    st.close()


def test_persistent_target_confirms_into_one_track(tmp_path):
    # A real person: seen at ~the same spot on the next frame -> exactly one track opens,
    # and the confirmation frame's point is the track's first recorded point.
    st, tr = _tracker(tmp_path)
    tr.update("S1", (0, 0, 0), [(1, 500, 500, "")], frame_mono=10.0, confirm_frames=2)
    tr.update("S1", (0, 0, 0), [(1, 560, 520, "")], frame_mono=10.1, confirm_frames=2)
    ser = tr.serial_for("S1", 1)
    assert ser is not None and ser in tr.active
    assert ("S1", 1) not in tr.provisional
    tracks = st.list_tracks_on_day(dt.date.today().isoformat())
    assert len(tracks) == 1 and tracks[0]["point_count"] == 1
    st.close()


def test_far_jump_restarts_candidate_not_confirms(tmp_path):
    # Two flashes at unrelated positions are two DIFFERENT ghosts, not a confirmation:
    # the second sighting outside CONFIRM_RADIUS_MM restarts the candidate.
    st, tr = _tracker(tmp_path)
    tr.update("S1", (0, 0, 0), [(1, 500, 500, "")], frame_mono=10.0, confirm_frames=2)
    far = 500 + CONFIRM_RADIUS_MM + 200
    tr.update("S1", (0, 0, 0), [(1, far, far, "")], frame_mono=10.1, confirm_frames=2)
    assert tr.serial_for("S1", 1) is None          # still nothing confirmed
    assert tr.provisional[("S1", 1)][0] == far     # candidate restarted at the new spot
    st.close()


def test_confirm_frames_1_keeps_legacy_behaviour(tmp_path):
    # confirm_frames=1 (the default) is the historical contract: first sighting opens a track.
    st, tr = _tracker(tmp_path)
    tr.update("S1", (0, 0, 0), [(1, 500, 500, "")], frame_mono=10.0)
    assert tr.serial_for("S1", 1) is not None
    st.close()


def test_engine_hides_provisional_slot_until_confirmed(tmp_path, monkeypatch):
    # The render path: a provisional slot must not be drawn (latest_by_sensor) or projected
    # (world_snapshot) until confirmed.
    import os
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    from radar.engine import RadarEngine
    from radar import constants as C
    monkeypatch.setattr(C, "ROOT", tmp_path)
    eng = RadarEngine({"schema_version": 2, "map": "x.svg", "smoothing_on": False,
                       "smooth_level": 0,
                       "sensors": [{"id": "S1", "label": "S1", "input_mode": "sim",
                                    "position": [0, 0], "heading": 0.0, "max_range_mm": 8000.0}]})
    try:
        eng._on_frame("S1", [(0, 0, 1000, "")], 10.0)
        assert eng.latest_by_sensor["S1"] == []                 # provisional: hidden
        assert eng.world_snapshot()["targets"] == []
        eng._on_frame("S1", [(0, 0, 1000, "")], 10.1)
        assert eng.latest_by_sensor["S1"] == [(0, 0, 1000)]     # confirmed: visible
        assert len(eng.world_snapshot()["targets"]) == 1
    finally:
        eng.shutdown()


# ── 2. Serial reconnect ──────────────────────────────────────────────────────────────────

def test_serial_reader_retries_missing_port_without_dying():
    # A port that doesn't exist: the reader thread must stay alive, keep retrying, and report
    # the failure via health — never crash, never give up, never block the caller.
    from radar.serial_reader import RadarSerial
    r = RadarSerial("/dev/does-not-exist-xyz", 256000, lambda lst, t: None)
    r.RETRY_MIN_S = 0.05                            # fast retries for the test
    t0 = time.monotonic()
    r.start()                                       # must return immediately
    assert time.monotonic() - t0 < 0.5
    time.sleep(0.3)                                 # let it fail a few open attempts
    assert r._thread.is_alive()                     # still trying, not dead
    assert r.health["connected"] is False
    assert r.health["last_error"] != ""             # the failure is surfaced, not swallowed
    r.stop()
    assert not r._thread.is_alive()                 # stop() halts the retry loop promptly


# ── 3. MQTT async startup ────────────────────────────────────────────────────────────────

def test_mqtt_start_returns_immediately_when_broker_unreachable():
    # The startup-hang fix: start() must NOT block on TCP connect. 203.0.113.0/24 is
    # TEST-NET-3 (RFC 5737) — guaranteed unroutable, so a blocking connect would hang for
    # the full TCP timeout while connect_async returns instantly.
    from radar.mqtt_client import RadarMQTT
    r = RadarMQTT("203.0.113.1", 1883, "PondEyes/raw", lambda lst, t: None)
    t0 = time.monotonic()
    r.start()
    assert time.monotonic() - t0 < 1.0              # async: no TCP-timeout stall
    r.stop()
