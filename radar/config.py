# radar.config
# ============
#
# Loads / saves radar_config.json and injects defaults for any missing keys.
#
# Schema v2 (multi-sensor): the single-sensor keys (sensor, heading, input_mode, serial_*,
# broker/port/topic) moved INTO a per-sensor dict inside a `sensors` list. Global keys (map,
# visuals, sound, debug) stay at the top level. A `schema_version` marks the shape so we can
# migrate old files automatically.
#
# Migration is the important part: a user who already calibrated a single sensor must lose
# nothing. _migrate() folds their flat v1 keys into sensors[0] (preserving position/heading),
# and we write a one-time radar_config.json.bak first so a downgrade is "restore the .bak".

from __future__ import annotations

import json
import shutil

from radar.constants import CFG_PATH

# Current on-disk schema version. Bump when the shape changes and add a migration step.
SCHEMA_VERSION = 2

_DEFAULT = {
    "schema_version": SCHEMA_VERSION,

    # ── One sensor by default. Each entry matches radar.sensors.Sensor.to_dict(). ─────────
    "sensors": [
        {
            "id": "S1",
            "label": "Sensor 1",
            "color": "#00ff80",
            "enabled": True,
            "input_mode": "mqtt",          # "mqtt" | "serial" | "sim"
            "serial_port": "/dev/ttyUSB0",
            "serial_baud": 256000,
            "broker": "127.0.0.1",
            "port": 1883,
            "topic": "PondEyes/raw",
            "sim_pattern": "circle",
            "sim_hz": 10.0,
            "position": [0.0, 0.0],
            "heading": 0.0,
        },
    ],

    # ── Global display / map ──────────────────────────────────────────────────────────────
    "map": "map.svg",
    "sound": True,
    "night": False,

    # ── Global visuals ────────────────────────────────────────────────────────────────────
    "trail_duration": 5.0,
    "trail_on": True,
    "smoothing_on": True,
    "smooth_level": 0,
    "fusion_on": True,      # fuse per-sensor detections into global person IDs (P1, P2, ...)
    "debug": False,
}

# Keys that lived at the top level in v1 and belong to a sensor in v2. Listed once here so
# the migration and any future audit agree on exactly what moves.
_V1_SENSOR_KEYS = (
    "input_mode", "serial_port", "serial_baud", "broker", "port", "topic",
)


def _migrate(cfg: dict) -> dict:
    # Bring any config up to the current schema. v2 files pass straight through. A v1 file
    # (no schema_version / no sensors list) has its flat single-sensor keys folded into one
    # sensor entry, preserving the calibrated position ("sensor") and "heading".
    if cfg.get("schema_version") == SCHEMA_VERSION and "sensors" in cfg:
        return cfg

    # Build the single migrated sensor by pulling (and removing) the old flat keys.
    sensor = {
        "id": "S1",
        "label": "Sensor 1",
        "color": "#00ff80",
        "enabled": True,
        "sim_pattern": "circle",
        "sim_hz": 10.0,
        "position": cfg.pop("sensor", [0.0, 0.0]),     # v1 stored pose as "sensor"
        "heading": cfg.pop("heading", 0.0),
    }
    for key in _V1_SENSOR_KEYS:
        if key in cfg:
            sensor[key] = cfg.pop(key)

    cfg["sensors"] = [sensor]
    cfg["schema_version"] = SCHEMA_VERSION
    return cfg


def load() -> dict:
    # Read radar_config.json, migrate to the current schema, then merge global defaults for
    # any missing keys. If the file is missing, write out the defaults and return them.
    #
    # ORDER MATTERS: we migrate the RAW file first (while its flat v1 keys are still visible),
    # THEN merge _DEFAULT underneath. Merging defaults first would inject a default `sensors`
    # list and `schema_version: 2`, which would fool _migrate into thinking a v1 file was
    # already v2 and silently drop the user's calibrated single-sensor settings.
    try:
        with open(CFG_PATH) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        save(_DEFAULT)
        return dict(_DEFAULT)

    is_v2 = (raw.get("schema_version") == SCHEMA_VERSION) and ("sensors" in raw)
    if not is_v2:
        # One-time safety net so a downgrade is just "restore the .bak".
        backup = CFG_PATH.with_suffix(CFG_PATH.suffix + ".bak")
        if not backup.exists():
            try:
                shutil.copyfile(CFG_PATH, backup)
            except OSError:
                pass  # a missing backup is not worth failing the launch over
        raw = _migrate(raw)

    # raw is now v2-shaped (its sensors + schema_version win over the defaults); merging
    # _DEFAULT underneath only fills in any absent GLOBAL keys (map, visuals, sound, debug).
    return {**_DEFAULT, **raw}


def save(cfg: dict) -> None:
    # Persist whatever shape we hold in memory (always v2 once the app has run once). Resolve the
    # path against constants.ROOT at CALL time (not the import-time CFG_PATH) so tests that redirect
    # ROOT to a tmp dir don't clobber the real radar_config.json.
    from radar import constants
    (constants.ROOT / "radar_config.json").write_text(json.dumps(cfg, indent=2))
