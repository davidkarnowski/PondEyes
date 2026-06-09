# Entry-point. Keeps the top-level script tiny.
import argparse
import pygame

from radar import config, gui
from radar.logging_setup import setup_logging


def main():
    ap = argparse.ArgumentParser(description="PondEyes live radar GUI")
    ap.add_argument("--sim", action="store_true",
                    help="run all sensors as SIMULATED (no hardware needed; saved config untouched)")
    args = ap.parse_args()

    log = setup_logging()            # rotating file (run-logs/) + stderr; PONDEYES_LOG=DEBUG for detail
    pygame.init()
    cfg = config.load()
    log.info("PondEyes start: %d sensor(s), map=%s, sim=%s",
             len(cfg.get("sensors", [])), cfg.get("map"), args.sim)
    app = gui.RadarGUI(cfg, sim=args.sim)
    try:
        app.run()
    finally:
        config.save(cfg)
        log.info("PondEyes exit")


if __name__ == "__main__":
    main()
