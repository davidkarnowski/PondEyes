# radar package
# =============
# Utility modules for the PondEyes project. v4.0 adds multi-sensor support.

__all__ = [
    "constants",
    "config",
    "frames",         # shared LD2450 wire-format codec
    "sensors",        # Sensor dataclass
    "colors",         # per-sensor colour model
    "trajectories",   # simulated motion generators
    "reader_base",    # Reader ABC + make_reader + FakeReader
    "serial_reader",
    "mqtt_client",
    "store",          # SQLite TrackStore
    "tracking",
    "svg_utils",
    "sound",
    "widgets",        # ColorPicker
    "gui",
]

__version__ = "4.0-dev"
