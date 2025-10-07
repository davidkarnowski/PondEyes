"""
Book-keeping for live & recent radar targets plus CSV persistence.

`Tracker.update()` now accepts tuples of either length 3 or 4:

    (slot, x_mm, y_mm)                 ← legacy
    (slot, x_mm, y_mm, raw_hex)        ← preferred

`raw_hex` (the original 30-byte frame as a hex string) is written to the
verbose per-target CSV.  Older senders that omit it still work—the column
is simply left blank.
"""
from __future__ import annotations

import csv
import datetime as dt
import math
import time
from itertools import count
from pathlib import Path
from typing import Dict, List, Tuple

from radar.constants import LOG_DIR


class Tracker:
    END_TIMEOUT = 3.0        # seconds of silence → track ends

    # ─────────────────────────────────────────────────────────── INIT
    def __init__(self) -> None:
        today = dt.date.today().isoformat()

        self.daydir: Path = LOG_DIR / f"{today}_targets_tracked"
        self.daydir.mkdir(parents=True, exist_ok=True)

        self.TrackIndex: Path = LOG_DIR / f"{today}_TrackIndex.csv"
        self.TrackIndex_exists: bool = self.TrackIndex.exists()
        self.max_serial: int = self._scan_TrackIndex()

        self.slot2ser: Dict[int, str] = {}       # slot → serial “T##”
        self.active:   Dict[str, Dict] = {}      # serial → info dict
        self.recent:   List[Dict]     = []       # last three completed tracks
        self.serial_iter = count(self.max_serial + 1)

    # ─────────────────────────────────────────────────── CSV helpers
    def _scan_TrackIndex(self) -> int:
        """Return the highest T-number already present in today's TrackIndex log."""
        if not self.TrackIndex_exists:
            LOG_DIR.mkdir(exist_ok=True)
            with self.TrackIndex.open("w", newline="") as fh:
                csv.writer(fh).writerow(
                    ["first_seen_iso", "serial", "last_seen_iso",
                     "duration", "verbose_file"]
                )
            return 0

        max_ser = 0
        with self.TrackIndex.open() as fh:
            for r in csv.DictReader(fh):
                if r and r["serial"].startswith("T"):
                    max_ser = max(max_ser, int(r["serial"][1:]))
        return max_ser

    # ───────────────────────────────────────────────────── public API
    def update(self, latest: List[Tuple]) -> float:
        """
        Parameters
        ----------
        latest : list of tuples
                 (slot, x_mm, y_mm [, raw_hex])

        Returns
        -------
        fastest_speed_mm_s : float
        """
        fastest  = 0.0
        now_mon  = time.monotonic()
        now_iso  = dt.datetime.now().isoformat(timespec="milliseconds")

        for item in latest:
            slot, x_mm, y_mm, *rest = item
            raw_hex: str = rest[0] if rest else ""    # always defined

            # —— ensure slot has a *live* serial ——
            ser = self.slot2ser.get(slot)
            if ser is None or ser not in self.active:
                ser = f"T{next(self.serial_iter)}"
                self.slot2ser[slot] = ser
                self._open_verbose(ser)               # seeds self.active[ser]

            info = self.active[ser]

            # —— kinematics ——
            px, py, pv, pt = info["hist"]
            first_point = (px == py == pv == 0.0)
            dt_s  = max(now_mon - pt, 1e-3)

            if first_point:
                v = accel = 0.0
            else:
                v     = math.hypot(x_mm - px, y_mm - py) / dt_s
                accel = (v - pv) / dt_s

            rng = math.hypot(x_mm, y_mm)
            fastest = max(fastest, v)

            # —— write line to verbose CSV ——
            csv.writer(info["fh"]).writerow(
                [now_iso, x_mm, y_mm, int(rng),
                 f"{v:.3f}", f"{accel:.3f}", raw_hex]
            )
            info["fh"].flush()

            # —— update history & timestamp ——
            info["hist"]    = (x_mm, y_mm, v, now_mon)
            info["last_ts"] = now_mon

        # —— expire stale tracks ——
        for ser in list(self.active):
            if now_mon - self.active[ser]["last_ts"] > self.END_TIMEOUT:
                self._expire(ser)

        return fastest

    # ───────────────────────────────────────────────── internal helpers
    def _open_verbose(self, ser: str) -> None:
        """Create per-track verbose CSV and seed `self.active[ser]`."""
        ts = dt.datetime.now()
        fname = f"{ser}_{ts.strftime('%H%M%S')}.csv"
        fpath = self.daydir / fname

        fh = fpath.open("w", newline="")
        csv.writer(fh).writerow(
            ["timestamp_iso", "x_mm", "y_mm", "range_mm",
             "speed_mm_s", "accel_mm_s2", "raw_hex"]
        )

        self.active[ser] = dict(
            first=ts,
            fh=fh,
            hist=(0.0, 0.0, 0.0, time.monotonic()),   # (x, y, v, t)
            last_ts=time.monotonic(),
        )

        # provisional row in TrackIndex (duration will be completed on _expire)
        with self.TrackIndex.open("a", newline="") as mfh:
            csv.writer(mfh).writerow(
                [ts.isoformat(timespec="seconds"), ser,
                 ts.isoformat(timespec="seconds"), "00:00:00.000", fname]
            )

    def _expire(self, ser: str) -> None:
        """Close CSV, move track to `recent`, update TrackIndex duration."""
        info = self.active[ser]
        info["fh"].close()

        first = info["first"]
        last  = dt.datetime.now()
        dur   = last - first

        # —— update TrackIndex CSV row ——
        with self.TrackIndex.open() as mfh:
            rows = list(csv.DictReader(mfh))
            headers = rows[0].keys() if rows else (
                "first_seen_iso serial last_seen_iso duration verbose_file".split()
            )

        for r in rows:
            if r["serial"] == ser and r["first_seen_iso"] == first.isoformat(timespec="seconds"):
                r["last_seen_iso"] = last.isoformat(timespec="seconds")
                r["duration"]      = str(dur).split(".")[0]
                break

        with self.TrackIndex.open("w", newline="") as mfh:
            wr = csv.DictWriter(mfh, fieldnames=headers)
            wr.writeheader()
            wr.writerows(rows)

        # —— move to “recent” list for GUI ——
        self.recent.insert(0, dict(serial=ser, first=first, last=last, dur=dur))
        if len(self.recent) > 3:
            self.recent.pop()

        # —— clean up dicts ——
        del self.active[ser]
        self.slot2ser = {s: t for s, t in self.slot2ser.items() if t != ser}
