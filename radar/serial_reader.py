# radar.serial_reader
# ===================
#
# Reads LD2450 frames from a local serial port (a UART->USB bridge, e.g. the FTDI converter
# or the ESP32 bridge) on a background thread and delivers each frame to a callback.
#
# This is now a thin transport: ALL wire-format knowledge lives in radar.frames, and the
# start/stop lifecycle comes from the radar.reader_base.Reader base class. The class only
# owns the serial-specific job of resyncing a byte STREAM into discrete 30-byte frames.
#
# Callback contract (shared by every Reader): on each complete frame, deliver a list of
# (slot, x_mm, y_mm, raw_hex) tuples. raw_hex is the full 30-byte frame as lowercase hex.

from __future__ import annotations

import threading
import time

import serial

from radar import frames
from radar.reader_base import Reader, FrameCallback
from radar.logging_setup import get_logger

log = get_logger("serial")

# Every baud the LD2450 supports, plus 921600 for the ESP32 bridge after the throughput fix.
# Ordered by likelihood so the common cases (bridge 921600, direct module 256000) lock fast.
LD2450_BAUDS = (921600, 256000, 460800, 230400, 115200, 57600, 38400, 19200, 9600)


def probe_baud(port: str, bauds=LD2450_BAUDS, read_s: float = 0.5, need: int = 2):
    # Auto-baud discovery: open `port` at each candidate baud, read up to `read_s` seconds, and
    # return the FIRST baud that yields >= `need` complete LD2450 frames (valid AA FF 03 00 …
    # 55 CC, 30 bytes). A wrong baud produces unframeable garbage, so false positives are nil.
    # Returns the baud (int) or None if nothing frames at any rate. CALLER MUST free the port
    # first (stop the live reader) — this opens the port exclusively for each trial.
    import time
    for baud in bauds:
        try:
            with serial.Serial(port, baud, timeout=0.05) as ser:
                buf = bytearray(); valid = 0; t0 = time.monotonic()
                while time.monotonic() - t0 < read_s and valid < need:
                    buf += ser.read(ser.in_waiting or 64)
                    while True:
                        idx = buf.find(frames.HDR)
                        if idx == -1:
                            if len(buf) > 3:
                                del buf[:-3]
                            break
                        if len(buf) < idx + frames.FLEN:
                            break
                        if buf[idx:idx + frames.FLEN].endswith(frames.FTR):
                            valid += 1; del buf[:idx + frames.FLEN]
                        else:
                            del buf[idx]
                if valid >= need:
                    return baud
        except serial.SerialException:
            continue
    return None


class RadarSerial(Reader):
    # Convenience aliases so existing references keep working; the source of truth is frames.
    HDR = frames.HDR
    FTR = frames.FTR
    FLEN = frames.FLEN

    # Reconnect backoff: first retry fast (a replug is usually quick), then ease off so a
    # permanently-absent port doesn't spin the CPU on a Pi. Capped, never gives up: the radar
    # characterisation report's top reliability ask is that a sensor that comes back is
    # picked up WITHOUT restarting the app.
    RETRY_MIN_S = 1.0
    RETRY_MAX_S = 8.0

    def __init__(self, port: str, baud: int, on_frame: FrameCallback):
        super().__init__(on_frame)
        self.port, self.baud = port, baud
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        # Frame-health telemetry (report §"ESP32/üst katman: ölçüm altyapısı"): without these
        # counters "sensör bozuk mu, hat mı bozuk?" cannot be answered. Written only by the
        # reader thread; read by the engine's stats snapshot (int reads are atomic in CPython).
        self.health = {
            "frames_ok": 0,        # complete, footer-valid frames delivered
            "resync_drops": 0,     # bytes dropped hunting for a valid frame (bad footer/garbage)
            "reconnects": 0,       # successful port (re)opens after the first
            "connected": False,    # port currently open
            "last_error": "",      # most recent open/read error (shown in CONFIG/web)
        }

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)

    def _loop(self) -> None:
        # Outer RECONNECT loop: open the port; if the open fails or the port dies mid-read
        # (USB unplug, ESP32 reboot, brownout), close, back off, and try again until stop().
        # Startup is therefore fully async — a missing sensor never blocks or kills the app,
        # and plugging it in later Just Works.
        opens = 0
        delay = self.RETRY_MIN_S
        while not self._stop.is_set():
            try:
                with serial.Serial(self.port, self.baud, timeout=0.05) as ser:
                    opens += 1
                    if opens > 1:
                        self.health["reconnects"] += 1
                        log.info("serial RECONNECTED: %s @ %d (attempt ok)", self.port, self.baud)
                    self.health["connected"] = True
                    self.health["last_error"] = ""
                    delay = self.RETRY_MIN_S           # a good open resets the backoff
                    self._read_frames(ser)
            except serial.SerialException as exc:
                self.health["connected"] = False
                self.health["last_error"] = str(exc)
            if self._stop.is_set():
                break
            # Interruptible backoff: stop() returns immediately; otherwise wait and retry.
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self.RETRY_MAX_S)
        self.health["connected"] = False

    def _read_frames(self, ser) -> None:
        # Inner READ loop (unchanged framing logic): pull bytes, find the header, and once a
        # full 30-byte frame is buffered, validate its footer and hand it off. The buffer
        # carries leftover bytes between reads so a frame split across two reads reassembles.
        # Raises SerialException to the outer loop when the port dies.
        buf = bytearray()
        while not self._stop.is_set():
            buf += ser.read(ser.in_waiting or 1)

            idx = buf.find(self.HDR)
            if idx == -1:
                # No header in view yet — keep only the last few bytes in case a
                # header straddles the boundary, and wait for more data.
                if len(buf) > 3:
                    self.health["resync_drops"] += len(buf) - 3
                    del buf[:-3]
                continue
            if idx > 0:
                self.health["resync_drops"] += idx     # garbage before the header

            if len(buf) < idx + self.FLEN:
                continue                       # header found but frame incomplete

            frame = bytes(buf[idx : idx + self.FLEN])
            if frame.endswith(self.FTR):
                hex_str = frame.hex()
                # Same delivery shape as every reader: (slot, x, y, raw_hex).
                tracks = [t + (hex_str,) for t in frames.parse(frame)]
                self.health["frames_ok"] += 1
                self._on_frame(tracks, time.monotonic())   # stamp at frame arrival
                del buf[: idx + self.FLEN]     # consume the frame we just handled
            else:
                self.health["resync_drops"] += 1
                del buf[idx]                   # bad footer -> drop one byte and resync
