# webserver.py
# ============
#
# Run the PondEyes WEB backend: build the same RadarEngine the pygame app uses (same config, same
# pondeyes.db), start the per-sensor readers, and serve the FastAPI app (live WebSocket + config +
# playback REST + the webapp/ frontend) with uvicorn. Then open http://127.0.0.1:8000 in a browser.
#
#   .venv/bin/python webserver.py            # default 127.0.0.1:8000
#   .venv/bin/python webserver.py --port 9000 --host 0.0.0.0
#
# NOTE: run EITHER this or the pygame app against the same hardware — both own the serial ports.
# Multiple browser clients off one web backend are fine.

import argparse
import uvicorn

from radar import config
from radar.engine import RadarEngine
from radar.web.server import create_app
from radar.logging_setup import get_logger

log = get_logger("webserver")


def main():
    ap = argparse.ArgumentParser(description="PondEyes web backend")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--sim", action="store_true",
                    help="run all sensors as SIMULATED (no hardware needed; saved config untouched)")
    args = ap.parse_args()

    cfg = config.load()
    engine = RadarEngine(cfg, sim=args.sim)
    engine.start()                       # spawn the per-sensor readers (simulated if --sim)
    log.info("PondEyes web backend on http://%s:%d  (%d sensor(s), sim=%s)",
             args.host, args.port, len(engine.sensors), args.sim)
    app = create_app(engine)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        engine.shutdown()                # stop readers + close the store cleanly


if __name__ == "__main__":
    main()
