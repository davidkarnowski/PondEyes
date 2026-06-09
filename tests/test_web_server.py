# tests.test_web_server
# =====================
#
# The FastAPI backend (radar/web/server.py) over a headless sim engine. We feed frames in directly
# (no reader threads), then exercise the REST endpoints and the live WebSocket via FastAPI's
# TestClient — confirming the browser gets WORLD-mm targets, map metadata, config writes, and the
# playback queries.

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import time
from pathlib import Path

from fastapi.testclient import TestClient

from radar.engine import RadarEngine
from radar.web.server import create_app

REPO = Path(__file__).resolve().parent.parent
MAP = str(REPO / "map.svg")


def _engine(tmp_path, monkeypatch):
    from radar import constants as C
    monkeypatch.setattr(C, "ROOT", tmp_path)
    cfg = {"schema_version": 2, "map": MAP, "sound": False, "night": False, "trail_on": True,
           "smoothing_on": False, "smooth_level": 0, "trail_duration": 5.0, "debug": False,
           "sensors": [{"id": "S1", "label": "S1", "color": "#00ff88", "input_mode": "sim",
                        "position": [2000, 3000], "heading": 90.0, "max_range_mm": 8000.0}]}
    return RadarEngine(cfg)        # readers NOT started; we drive _on_frame directly


def test_config_and_map_endpoints(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch)
    client = TestClient(create_app(eng))
    try:
        r = client.get("/api/config")
        assert r.status_code == 200 and [s["id"] for s in r.json()["sensors"]] == ["S1"]
        m = client.get("/api/map/meta").json()
        assert m["width_mm"] > 0 and m["height_mm"] > 0
        svg = client.get("/api/map.svg")
        assert svg.status_code == 200 and "svg" in svg.headers["content-type"]
    finally:
        eng.shutdown()


def test_update_sensor(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch)
    client = TestClient(create_app(eng))
    try:
        r = client.post("/api/sensors/S1", json={"max_range_mm": 1234.0})
        assert r.status_code == 200 and r.json()["sensor"]["max_range_mm"] == 1234.0
        assert eng.sensors_by_id["S1"].max_range_mm == 1234.0
        assert client.post("/api/sensors/NOPE", json={}).status_code == 404
    finally:
        eng.shutdown()


def test_ws_live_pushes_world_mm_targets(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch)
    eng._on_frame("S1", [(0, 1000, 0, "")], time.monotonic())   # a FRESH frame (the WS loop runs
                                                                # the data-loss watchdog each push)
    client = TestClient(create_app(eng))
    try:
        with client.websocket_connect("/ws/live") as ws:
            data = ws.receive_json()
            assert [s["id"] for s in data["sensors"]] == ["S1"]
            assert len(data["targets"]) == 1
            t = data["targets"][0]
            assert "x" in t and "y" in t and t["sensor_id"] == "S1"
    finally:
        eng.shutdown()


def test_tracks_endpoints(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch)
    eng._on_frame("S1", [(0, 1000, 0, "")], 10.0)      # opens a track in the store
    client = TestClient(create_app(eng))
    try:
        tracks = client.get("/api/tracks").json()["tracks"]
        assert len(tracks) >= 1
        tid = tracks[0]["id"]
        d = client.get(f"/api/tracks/{tid}").json()
        assert d["track"]["id"] == tid and len(d["points"]) >= 1
        assert client.get("/api/tracks/999999").status_code == 404
    finally:
        eng.shutdown()
