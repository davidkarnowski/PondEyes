# radar.web
# =========
#
# The FastAPI/WebSocket frontend for PondEyes — an alternative to the pygame GUI that renders in a
# browser. Both sit on the SAME headless radar.engine.RadarEngine; this package adds only the HTTP
# surface (a live WebSocket feed of world_snapshot(), config + playback REST, and the static
# webapp/ assets). See radar.web.server.create_app.

from radar.web.server import create_app

__all__ = ["create_app"]
