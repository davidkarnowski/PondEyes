"""
radar.serial_reader
===================

Non-blocking LD2450 frame reader for local serial port connection.

Callback signature
------------------
The callback now receives *4-tuples* per target:

    (slot, x_mm, y_mm, raw_hex)

• `raw_hex` is the full 30-byte frame in lowercase hex.  
• Code that still expects 3-tuples can simply ignore the 4th element.

Usage
-----
    reader = RadarSerial("/dev/ttyUSB0", 256000, on_frame)
    reader.start()     # spawns a background thread
    reader.stop()      # clean shutdown
"""
from __future__ import annotations

import threading
import serial
import time


class RadarSerial:
    HDR  = bytes.fromhex("AAFF0300")   # frame header
    FTR  = bytes.fromhex("55CC")       # frame footer
    FLEN = 30                          # full frame length (bytes)

    def __init__(self, port: str, baud: int, on_frame):
        self.port, self.baud = port, baud
        self._cb      = on_frame                 # callback(frame_list)
        self._stop    = threading.Event()
        self._thread  = threading.Thread(target=self._loop, daemon=True)

    # ───────────────────────── public API
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)

    # ───────────────────────── helpers
    @staticmethod
    def _s15(u: int) -> int:
        """
        Convert LD2450 signed-15-bit word to Python int (mm).

        Rule: MSB=1 → positive, MSB=0 → negative.
        """
        return (u & 0x7FFF) if (u & 0x8000) else -(u & 0x7FFF)

    def _parse(self, buf: bytes):
        """
        Extract (slot, x_mm, y_mm) tuples from one 30-byte frame.
        Slots are 1-based: 1, 2, 3  – matching the MQTT path & Tracker.
        """
        out = []
        for i in range(3):
            off = 4 + i * 8                          # start of target i
            x = self._s15(int.from_bytes(buf[off : off + 2], "little"))
            y = self._s15(int.from_bytes(buf[off + 2 : off + 4], "little"))
            # speed v is in bytes [off+4 : off+6] – decode if you need it
            if x or y:                               # ignore empty slots
                out.append((i + 1, x, y))
        return out

    # ───────────────────────── background reader thread
    def _loop(self):
        buf = bytearray()
        try:
            with serial.Serial(self.port, self.baud, timeout=0.05) as ser:
                while not self._stop.is_set():
                    buf += ser.read(ser.in_waiting or 1)

                    idx = buf.find(self.HDR)
                    if idx == -1:                    # no header yet
                        if len(buf) > 3:
                            del buf[:-3]             # keep last few bytes
                        continue

                    if len(buf) < idx + self.FLEN:   # incomplete frame
                        continue

                    frame = buf[idx : idx + self.FLEN]
                    if frame.endswith(self.FTR):
                        hex_str = frame.hex()        # full packet → hex
                        tracks  = [t + (hex_str,)    # add raw_hex
                                   for t in self._parse(frame)]
                        self._cb(tracks)             # deliver to GUI/tracker
                        del buf[: idx + self.FLEN]   # drop processed bytes
                    else:
                        del buf[idx]                 # bad align → resync
        except serial.SerialException:
            # Silently exit; GUI will show no data until user re-saves CONFIG
            pass
