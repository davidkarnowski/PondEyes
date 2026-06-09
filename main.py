# Entry-point. Keeps the top-level script tiny.
import argparse
import pygame

from radar import config, gui
from radar.logging_setup import setup_logging


def main():
    ap = argparse.ArgumentParser(description="PondEyes live radar GUI")
    ap.add_argument("--sim", action="store_true",
                    help="run all sensors as SIMULATED (no hardware needed; saved config untouched)")
    ap.add_argument("--serve", action="store_true",
                    help="also host the web view on the SAME engine (e.g. a Pi running the GUI + a "
                         "browser in another room — both reflect the one backend's state)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default localhost-only; pass 0.0.0.0 to allow LAN clients)")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    log = setup_logging()            # rotating file (run-logs/) + stderr; PONDEYES_LOG=DEBUG for detail
    pygame.init()
    cfg = config.load()
    log.info("PondEyes start: %d sensor(s), map=%s, sim=%s",
             len(cfg.get("sensors", [])), cfg.get("map"), args.sim)
    app = gui.RadarGUI(cfg, sim=args.sim)

    if args.serve:
        # Share THIS RadarGUI (it IS a RadarEngine) with a FastAPI server in a daemon thread, so the
        # web UI and the pygame UI are two views of ONE engine — config/state changes on either show
        # on both. The backend holds truth.
        import threading, uvicorn
        from radar.web.server import create_app
        srv = uvicorn.Server(uvicorn.Config(create_app(app), host=args.host, port=args.port, log_level="warning"))
        threading.Thread(target=srv.run, daemon=True).start()
        log.info("web view shared on http://%s:%d (same engine)", args.host, args.port)

    try:
        app.run()
    finally:
        config.save(cfg)
        log.info("PondEyes exit")


if __name__ == "__main__":
    main()
