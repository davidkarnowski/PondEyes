# radar.store
# ===========
#
# SQLite-backed persistence for tracks. One TrackStore per process.
#
# Why SQLite instead of the old per-track CSV files?
#   - N sensors means N reader threads writing concurrently. The CSV approach rewrote the
#     whole TrackIndex.csv on every track close — a race waiting to happen. SQLite in WAL mode
#     gives us safe concurrent access with a single writer lock and lock-free readers.
#   - Real indexed queries (tracks on a day, points of a track) for playback, instead of
#     globbing files and parsing CSV.
#   - A clean path to future features (e.g. embeddings) as an additive migration.
#
# Threading model: one short-lived writer Lock serialises all writes (SQLite itself also
# serialises, but the lock keeps our multi-statement updates atomic). The connection is opened
# with check_same_thread=False so reader threads and the GUI thread can share it; WAL mode lets
# readers proceed while a writer holds the lock.

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import List, Optional, Tuple

# Bumped via PRAGMA user_version when the schema changes; _migrate applies steps in order.
SCHEMA_VERSION = 2

# The full v1 schema. Kept as one string so _apply_v1 creates everything atomically.
_SCHEMA_V1 = """
CREATE TABLE sensors (
    id           TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    color        TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    input_mode   TEXT NOT NULL,
    serial_port  TEXT, serial_baud INTEGER,
    broker TEXT, port INTEGER, topic TEXT,
    position_x   REAL NOT NULL DEFAULT 0,
    position_y   REAL NOT NULL DEFAULT 0,
    heading      REAL NOT NULL DEFAULT 0
);

CREATE TABLE tracks (
    id              INTEGER PRIMARY KEY,
    serial          TEXT NOT NULL,                -- "T1" user-facing id
    sensor_id       TEXT NOT NULL REFERENCES sensors(id),
    first_seen      TEXT NOT NULL,                -- ISO8601 with ms
    last_seen       TEXT NOT NULL,
    duration_sec    REAL NOT NULL DEFAULT 0,
    point_count     INTEGER NOT NULL DEFAULT 0,
    sensor_x        REAL NOT NULL,                -- pose SNAPSHOT at track open, so a later
    sensor_y        REAL NOT NULL,                -- config edit doesn't move historical tracks
    sensor_heading  REAL NOT NULL,
    x_min INTEGER, x_max INTEGER, y_min INTEGER, y_max INTEGER   -- world-mm bounding box
);
CREATE INDEX idx_tracks_first_seen ON tracks(first_seen);
CREATE INDEX idx_tracks_sensor     ON tracks(sensor_id);

CREATE TABLE track_points (
    track_id     INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    t_iso        TEXT NOT NULL,
    x_mm         INTEGER NOT NULL,
    y_mm         INTEGER NOT NULL,
    range_mm     INTEGER,
    speed_mm_s   REAL,
    accel_mm_s2  REAL,
    raw_hex      TEXT
);
CREATE INDEX idx_points_track   ON track_points(track_id);
CREATE INDEX idx_points_track_t ON track_points(track_id, t_iso);
"""

# v2 (additive): snapshot the FULL sensor config per track (so playback reconstructs the scene
# as-recorded even after the sensor is moved/recoloured) + high-res relative frame timing on
# points (monotonic seconds since track open) for an accurate playback timeline. ADD COLUMN with
# a default backfills existing rows, so this is safe and reversible.
_SCHEMA_V2 = """
ALTER TABLE tracks ADD COLUMN sensor_color TEXT;
ALTER TABLE tracks ADD COLUMN sensor_min_range_mm REAL NOT NULL DEFAULT 0;
ALTER TABLE tracks ADD COLUMN sensor_max_range_mm REAL NOT NULL DEFAULT 0;
ALTER TABLE tracks ADD COLUMN sensor_trail_on INTEGER NOT NULL DEFAULT 1;
ALTER TABLE tracks ADD COLUMN sensor_speed_sensitivity REAL NOT NULL DEFAULT 0.25;
ALTER TABLE track_points ADD COLUMN t_rel_s REAL;
"""


class TrackStore:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        # autocommit (isolation_level=None) keeps each statement durable immediately, which is
        # what we want for a continuously-appending logger; our own Lock handles atomicity of
        # the few multi-statement updates.
        self.conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None, timeout=5.0
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        self._closed = False        # set under the lock in close() so a late reader-thread write
                                    # can't touch (and C-level crash) a freed sqlite connection
        self._migrate()

    # ── schema migration framework ───────────────────────────────────────────────────────
    def _migrate(self):
        # Apply schema steps based on the DB's stored user_version. Adding a v2 later is just
        # another `if v < 2: self._apply_v2()` block — no data movement for additive changes.
        v = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if v < 1:
            self._apply_v1()
        if v < 2:
            self._apply_v2()

    def _apply_v1(self):
        with self._lock:
            self.conn.executescript("BEGIN;" + _SCHEMA_V1 + "PRAGMA user_version=1; COMMIT;")

    def _apply_v2(self):
        with self._lock:
            self.conn.executescript("BEGIN;" + _SCHEMA_V2 + "PRAGMA user_version=2; COMMIT;")

    # ── sensors ──────────────────────────────────────────────────────────────────────────
    def upsert_sensor(self, sensor: dict):
        # Insert or update a sensor row from a Sensor.to_dict(). The DB keeps a snapshot of
        # sensor records so tracks' sensor_id foreign keys stay valid even if the user later
        # edits config; radar_config.json remains the authoritative config.
        row = {
            "id": sensor["id"], "label": sensor.get("label", sensor["id"]),
            "color": sensor.get("color"), "enabled": int(bool(sensor.get("enabled", True))),
            "input_mode": sensor.get("input_mode", "serial"),
            "serial_port": sensor.get("serial_port"), "serial_baud": sensor.get("serial_baud"),
            "broker": sensor.get("broker"), "port": sensor.get("port"),
            "topic": sensor.get("topic"),
            "position_x": sensor.get("position", [0, 0])[0],
            "position_y": sensor.get("position", [0, 0])[1],
            "heading": sensor.get("heading", 0.0),
        }
        with self._lock:
            if self._closed:
                return
            self.conn.execute(
                """
                INSERT INTO sensors(id, label, color, enabled, input_mode, serial_port,
                                    serial_baud, broker, port, topic, position_x, position_y,
                                    heading)
                VALUES (:id,:label,:color,:enabled,:input_mode,:serial_port,:serial_baud,
                        :broker,:port,:topic,:position_x,:position_y,:heading)
                ON CONFLICT(id) DO UPDATE SET
                    label=excluded.label, color=excluded.color, enabled=excluded.enabled,
                    input_mode=excluded.input_mode, serial_port=excluded.serial_port,
                    serial_baud=excluded.serial_baud, broker=excluded.broker,
                    port=excluded.port, topic=excluded.topic, position_x=excluded.position_x,
                    position_y=excluded.position_y, heading=excluded.heading
                """,
                row,
            )

    # ── tracks ───────────────────────────────────────────────────────────────────────────
    def open_track(self, sensor_id: str, serial: str,
                   pose: Tuple[float, float, float], first_seen: str,
                   snapshot: dict = None) -> int:
        # Create a track row and return its integer primary key (used to append points). The pose
        # (x, y, heading) AND the rest of the sensor config (`snapshot`: color, min/max range,
        # trail_on, speed_sensitivity) are snapshotted here so playback reconstructs the scene
        # exactly as-recorded, even if the sensor is later moved or reconfigured. snapshot=None
        # falls back to defaults (keeps older callers/tests working).
        snap = snapshot or {}
        with self._lock:
            if self._closed:
                return None
            cur = self.conn.execute(
                """
                INSERT INTO tracks(serial, sensor_id, first_seen, last_seen,
                                   sensor_x, sensor_y, sensor_heading,
                                   sensor_color, sensor_min_range_mm, sensor_max_range_mm,
                                   sensor_trail_on, sensor_speed_sensitivity)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (serial, sensor_id, first_seen, first_seen, pose[0], pose[1], pose[2],
                 snap.get("color"),
                 float(snap.get("min_range_mm", 0.0)),
                 float(snap.get("max_range_mm", 0.0)),
                 int(bool(snap.get("trail_on", True))),
                 float(snap.get("speed_sensitivity", 0.25))),
            )
            return cur.lastrowid

    def append_point(self, track_id: int, t_iso: str, x: int, y: int, rng: int,
                     v: float, a: float, raw_hex: str, t_rel_s: float = None):
        # Insert one sample and roll the track's last_seen, point_count, and bounding box
        # forward. `t_rel_s` is high-res seconds since track open (monotonic-derived) for an
        # accurate playback timeline; `t_iso` stays the absolute wall-clock anchor. Maintaining
        # the bbox incrementally gives cheap "tracks in a region" queries without scanning every
        # point. COALESCE handles the first point (NULL bounds).
        with self._lock:
            if self._closed:
                return                       # store is shutting down; drop this late frame
            self.conn.execute(
                "INSERT INTO track_points(track_id, t_iso, x_mm, y_mm, range_mm, speed_mm_s, "
                "accel_mm_s2, raw_hex, t_rel_s) VALUES (?,?,?,?,?,?,?,?,?)",
                (track_id, t_iso, x, y, rng, v, a, raw_hex, t_rel_s),
            )
            self.conn.execute(
                """
                UPDATE tracks SET
                    last_seen=?, point_count=point_count+1,
                    x_min=MIN(COALESCE(x_min, ?), ?), x_max=MAX(COALESCE(x_max, ?), ?),
                    y_min=MIN(COALESCE(y_min, ?), ?), y_max=MAX(COALESCE(y_max, ?), ?)
                WHERE id=?
                """,
                (t_iso, x, x, x, x, y, y, y, y, track_id),
            )

    def close_track(self, track_id: int, last_seen: str, duration_sec: float):
        with self._lock:
            if self._closed:
                return
            self.conn.execute(
                "UPDATE tracks SET last_seen=?, duration_sec=? WHERE id=?",
                (last_seen, duration_sec, track_id),
            )

    # ── read helpers (playback / future API) ─────────────────────────────────────────────
    def max_serial(self) -> int:
        # Highest numeric suffix among "T<N>" serials, so the Tracker can keep numbering
        # monotonically across runs. Returns 0 when there are no tracks yet.
        rows = self.conn.execute(
            "SELECT serial FROM tracks WHERE serial LIKE 'T%'"
        ).fetchall()
        best = 0
        for (serial,) in rows:
            try:
                best = max(best, int(serial[1:]))
            except (ValueError, IndexError):
                continue
        return best

    def list_tracks_on_day(self, iso_day: str) -> List[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute(
            "SELECT * FROM tracks WHERE first_seen LIKE ? ORDER BY first_seen",
            (iso_day + "%",),
        ).fetchall()

    def fetch_points(self, track_id: int) -> List[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute(
            "SELECT * FROM track_points WHERE track_id=? ORDER BY t_iso", (track_id,)
        ).fetchall()

    def list_recent_tracks(self, limit: int = 50) -> List[sqlite3.Row]:
        # Newest tracks first, across all days — drives the playback selection list.
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute(
            "SELECT * FROM tracks ORDER BY first_seen DESC LIMIT ?", (limit,)
        ).fetchall()

    def sensor_color(self, sensor_id: str) -> str:
        # The configured colour for a sensor, defaulting to green if unknown. Used by playback
        # to render each track in the colour of the sensor that recorded it.
        row = self.conn.execute(
            "SELECT color FROM sensors WHERE id=?", (sensor_id,)
        ).fetchone()
        return (row[0] if row and row[0] else "#00ff80")

    def close(self):
        # Take the write lock so we wait for any in-flight append/open/close to finish before
        # freeing the connection — otherwise closing mid-write (the store is shared across reader
        # threads via check_same_thread=False) is a C-level use-after-free / segfault.
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.conn.close()
