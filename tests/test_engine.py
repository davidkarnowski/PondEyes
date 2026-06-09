# tests.test_engine
# =================
#
# The headless RadarEngine (radar/engine.py) — the pygame-free core that both the pygame GUI and
# the (coming) web server sit on. We drive frames in directly (as a reader thread would) so the
# test is deterministic and needs no real readers/threads, and assert the two things the web
# frontend depends on: world_snapshot() projects targets to WORLD-mm with the AUTHORITATIVE
# geometry.local_to_world, applies the distance gate, and config ops re-sync state.

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pytest

from radar.engine import RadarEngine
from radar import geometry


def _cfg(sensors):
    return {"schema_version": 2, "sensors": sensors, "map": "x.svg",
            "sound": False, "night": False, "trail_duration": 5.0, "trail_on": True,
            "smoothing_on": False, "smooth_level": 0, "debug": False}   # smoothing OFF -> exact coords


def _engine(tmp_path, monkeypatch, sensors):
    from radar import constants as C
    monkeypatch.setattr(C, "ROOT", tmp_path)
    return RadarEngine(_cfg(sensors))     # does NOT start reader threads; we feed _on_frame directly


def test_world_snapshot_projects_to_authoritative_world_mm(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch, [
        {"id": "S1", "label": "S1", "color": "#00ff88", "input_mode": "sim",
         "position": [2000, 3000], "heading": 90.0, "max_range_mm": 8000.0}])
    try:
        eng._on_frame("S1", [(0, 1000, 0, "")], 10.0)     # one frame: local x=1000 (right), y=0
        snap = eng.world_snapshot()
        assert [s["id"] for s in snap["sensors"]] == ["S1"]
        assert len(snap["targets"]) == 1
        t = snap["targets"][0]
        wx, wy = geometry.local_to_world(1000, 0, (2000, 3000, 90.0))   # the authoritative projection
        assert t["x"] == pytest.approx(wx, abs=0.5)
        assert t["y"] == pytest.approx(wy, abs=0.5)
        assert t["sensor_id"] == "S1"
        assert snap["data_lost"] is False
    finally:
        eng.shutdown()


def test_world_snapshot_applies_distance_gate(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch, [
        {"id": "S1", "label": "S1", "color": "#00ff88", "input_mode": "sim",
         "position": [0, 0], "heading": 0.0, "max_range_mm": 2000.0}])
    try:
        eng._on_frame("S1", [(0, 0, 1000, ""), (1, 0, 5000, "")], 10.0)   # 1 m in-range, 5 m out
        snap = eng.world_snapshot()
        assert len(snap["targets"]) == 1                  # the 5 m target is gated out (max 2 m)
    finally:
        eng.shutdown()


def test_data_loss_watchdog_flips_and_recovers(tmp_path, monkeypatch):
    import time
    eng = _engine(tmp_path, monkeypatch, [
        {"id": "S1", "label": "S1", "color": "#00ff88", "input_mode": "sim",
         "position": [0, 0], "heading": 0.0, "max_range_mm": 8000.0}])
    try:
        eng._on_frame("S1", [(0, 0, 1000, "")], time.monotonic())   # fresh frame
        assert eng.check_watchdog() is False                        # not stale yet
        assert len(eng.world_snapshot()["targets"]) == 1
        eng.t_last_frame = time.monotonic() - 5.0                   # force an all-quiet gap
        assert eng.check_watchdog() is True                        # flips data_lost + ends tracks
        assert eng.data_lost is True
        assert eng.world_snapshot()["targets"] == []
        eng._on_frame("S1", [(0, 0, 1000, "")], time.monotonic())   # a fresh frame recovers
        assert eng.data_lost is False
        assert len(eng.world_snapshot()["targets"]) == 1
    finally:
        eng.shutdown()


def test_config_persists_to_disk_on_change(tmp_path, monkeypatch):
    import json
    from radar import constants as C
    monkeypatch.setattr(C, "ROOT", tmp_path)
    eng = RadarEngine(_cfg([
        {"id": "S1", "label": "S1", "color": "#00ff88", "input_mode": "sim",
         "position": [0, 0], "heading": 0.0}]))
    try:
        eng.update_sensor("S1", {"max_range_mm": 1234.0}, restart=False)   # backend persists it
        saved = json.loads((tmp_path / "radar_config.json").read_text())
        assert saved["sensors"][0]["max_range_mm"] == 1234.0               # durable across a restart
    finally:
        eng.shutdown()


def test_world_snapshot_has_per_sensor_stale_flag(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch, [
        {"id": "S1", "label": "S1", "color": "#00ff88", "input_mode": "sim",
         "position": [0, 0], "heading": 0.0}])
    try:
        assert "stale" in eng.world_snapshot()["sensors"][0]
    finally:
        eng.shutdown()


def test_sim_mode_runs_serial_sensor_without_hardware(tmp_path, monkeypatch):
    from radar import constants as C
    monkeypatch.setattr(C, "ROOT", tmp_path)
    eng = RadarEngine(_cfg([
        {"id": "S1", "label": "S1", "color": "#00ff88", "input_mode": "serial",
         "serial_port": "/dev/cu.does-not-exist", "serial_baud": 256000,
         "position": [0, 0], "heading": 0.0}]), sim=True)
    try:
        eng.start()                       # would bind a (missing) serial port if sim weren't forcing it
        assert type(eng.readers["S1"]).__name__ == "FakeReader"   # simulated despite serial config
        assert eng.sensors_by_id["S1"].input_mode == "serial"     # stored mode UNCHANGED (sim not persisted)
    finally:
        eng.shutdown()


def test_sim_mode_injects_demo_when_no_sensors(tmp_path, monkeypatch):
    from radar import constants as C
    monkeypatch.setattr(C, "ROOT", tmp_path)
    eng = RadarEngine(_cfg([]), sim=True)            # no sensors configured + sim
    try:
        assert len(eng.sensors) == 1 and eng.sensors[0].input_mode == "sim"
    finally:
        eng.shutdown()


def test_update_sensor_persists_and_resyncs(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch, [
        {"id": "S1", "label": "S1", "color": "#00ff88", "input_mode": "sim",
         "position": [0, 0], "heading": 0.0}])
    try:
        eng.update_sensor("S1", {"max_range_mm": 1234.0, "color": "#ff0000"}, restart=False)
        assert eng.sensors_by_id["S1"].max_range_mm == 1234.0       # applied in memory
        row = eng.store.conn.execute("SELECT color FROM sensors WHERE id='S1'").fetchone()
        assert row[0] == "#ff0000"                                  # and persisted (upsert_sensor)
    finally:
        eng.shutdown()
