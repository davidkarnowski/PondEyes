"""
radar.config
============

Tiny helper that loads / saves *radar_config.json* and injects sensible
defaults for any missing keys.
"""

from __future__ import annotations
import json
from radar.constants import CFG_PATH

_DEFAULT = {
    # display / map
    "map": "map.svg",
    "sensor": [0.0, 0.0],
    "heading": 0.0,
    "sound": True,
    "night": False,

    # input selection
    "input_mode": "mqtt",             # "mqtt"  or  "serial"
    "serial_port": "/dev/ttyUSB0",
    "serial_baud": 256000,

    # MQTT (only used when input_mode == "mqtt")
    "broker": "127.0.0.1",
    "port": 1883,
    "topic": "PondEyes/raw",

    # visuals
    "trail_duration": 5.0,            # seconds
    "trail_on": True,
    "smoothing_on": True,
    "smooth_level": 0,
}


def load() -> dict:
    try:
        with open(CFG_PATH) as fh:
            return {**_DEFAULT, **json.load(fh)}
    except FileNotFoundError:
        save(_DEFAULT)
        return dict(_DEFAULT)


def save(cfg: dict) -> None:
    CFG_PATH.write_text(json.dumps(cfg, indent=2))
