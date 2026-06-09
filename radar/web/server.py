# radar.web.server
# ================
#
# A FastAPI app over a headless RadarEngine. The browser frontend (webapp/, Phase 3) consumes:
#
#   WS  /ws/live          — pushes engine.world_snapshot() ~25x/s (the latest state, coalesced;
#                           a slow client never backs up the reader threads). Targets arrive in
#                           WORLD-mm (authoritative geometry done server-side), so the browser only
#                           scales mm->px + pans/zooms.
#   GET /api/config        — sensors + global toggles + map path
#   POST /api/config       — replace sensors / set globals (restarts readers)
#   POST /api/sensors/{id} — update one sensor's fields (restarts readers)
#   GET /api/map.svg       — the configured SVG map
#   GET /api/map/meta      — the map's physical extent in mm (so the client can fit world-mm -> px)
#   GET /api/tracks[?day=] — recorded tracks (recent, or for a given YYYY-MM-DD)
#   GET /api/tracks/{id}   — one track's header + points (t_rel_s timeline) for playback
#
# create_app(engine) takes the engine by injection so tests can pass a sim engine; the launcher
# (webserver.py) builds the engine from config and serves it with uvicorn.

from __future__ import annotations
import asyncio, json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from radar import svg_utils
from radar.engine import RadarEngine
from radar.logging_setup import get_logger

log = get_logger("web")

WEBAPP_DIR = Path(__file__).resolve().parent.parent.parent / "webapp"   # the vanilla-JS frontend
PUSH_HZ = 25                                                            # live WS push rate cap
_GLOBAL_KEYS = ("sound", "night", "trail_on", "smoothing_on", "smooth_level",
                "trail_duration", "debug", "map")


def create_app(engine: RadarEngine) -> FastAPI:
    app = FastAPI(title="PondEyes")

    # ── live feed ──────────────────────────────────────────────────────────────────────────
    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                # Drive the shared staleness + data-loss watchdog at the push cadence (same logic
                # the pygame run loop runs), then serialise the LATEST state. world_snapshot() is
                # fast + lock-safe; we never queue per-frame, so a slow client can't stall readers.
                engine._cull_stale_markers()
                engine.check_watchdog()
                await ws.send_text(json.dumps(engine.world_snapshot()))
                await asyncio.sleep(1.0 / PUSH_HZ)
        except WebSocketDisconnect:
            pass
        except Exception as exc:                       # pragma: no cover - defensive
            log.warning("ws_live closed: %s", exc)

    # ── config ─────────────────────────────────────────────────────────────────────────────
    @app.get("/api/config")
    def get_config():
        return {"sensors": [s.to_dict() for s in engine.sensors],
                "map": engine.cfg.get("map"),
                "globals": {k: engine.cfg.get(k) for k in _GLOBAL_KEYS if k != "map"}}

    @app.post("/api/config")
    def post_config(body: dict = Body(...)):
        if "sensors" in body:
            engine.apply_sensors(body["sensors"])      # replace sensor set + restart readers
        for k in _GLOBAL_KEYS:
            if k in body:
                engine.cfg[k] = body[k]
        # smoothing affects the engine's _avg_motion live
        engine.smoothing_on = bool(engine.cfg.get("smoothing_on", True))
        engine.smooth_level = int(engine.cfg.get("smooth_level", 0))
        engine.save_config()        # persist: the backend stores the truth (durable across restarts)
        return {"ok": True, "cfg_error": engine.cfg_error}

    @app.post("/api/sensors/{sensor_id}")
    def update_sensor(sensor_id: str, body: dict = Body(...)):
        s = engine.update_sensor(sensor_id, body)
        if s is None:
            raise HTTPException(404, f"no sensor {sensor_id}")
        return {"ok": True, "sensor": s.to_dict(), "cfg_error": engine.cfg_error}

    @app.post("/api/sensors/{sensor_id}/autobaud")
    def autobaud(sensor_id: str, body: dict = Body(default={})):
        if sensor_id not in engine.sensors_by_id:
            raise HTTPException(404, f"no sensor {sensor_id}")
        found = engine.autobaud(sensor_id, port=body.get("port"))   # synchronous probe (a few seconds)
        return {"ok": bool(found), "baud": found, "cfg_error": engine.cfg_error}

    # ── map ────────────────────────────────────────────────────────────────────────────────
    @app.get("/api/map.svg")
    def map_svg():
        p = engine.cfg.get("map")
        if not p or not Path(p).exists():
            raise HTTPException(404, "map not found")
        return FileResponse(p, media_type="image/svg+xml")

    @app.get("/api/map/meta")
    def map_meta():
        # The SVG's declared physical size in mm; the browser fits this to its canvas to derive
        # pixels-per-mm, then places the WORLD-mm targets exactly where the frames put them.
        try:
            w_mm, h_mm = svg_utils._svg_mm(engine.cfg.get("map"))
        except Exception as exc:
            raise HTTPException(404, f"map meta: {exc}")
        return {"width_mm": w_mm, "height_mm": h_mm}

    # ── playback (read-only) ─────────────────────────────────────────────────────────────────
    @app.get("/api/tracks")
    def tracks(day: str = None):
        rows = engine.store.list_tracks_on_day(day) if day else engine.store.list_recent_tracks(50)
        return {"tracks": [dict(r) for r in rows]}

    @app.get("/api/tracks/{track_id}")
    def track_detail(track_id: int):
        pts = engine.store.fetch_points(track_id)      # sets conn.row_factory = Row
        hdr = engine.store.conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        if hdr is None:
            raise HTTPException(404, f"no track {track_id}")
        return {"track": dict(hdr), "points": [dict(p) for p in pts]}

    # ── static frontend (Phase 3 lives in webapp/) ──────────────────────────────────────────
    if WEBAPP_DIR.is_dir() and (WEBAPP_DIR / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(WEBAPP_DIR), html=True), name="webapp")
    else:
        @app.get("/")
        def _root():
            return {"status": "PondEyes web backend running",
                    "note": "frontend (webapp/) not built yet — see /docs for the API",
                    "live": "/ws/live"}

    return app
