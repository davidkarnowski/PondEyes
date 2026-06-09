# radar.reader_base
# =================
#
# The input-source abstraction that makes the whole app testable.
#
# Every way of getting LD2450 frames into PondEyes — a real USB/serial bridge, a real MQTT
# broker, or an in-process FAKE source for tests/demos — implements the same tiny `Reader`
# contract: construct with a callback, start(), stop(). The GUI never names a concrete reader
# class; it asks the make_reader() factory for one based on a Sensor's input_mode. That single
# indirection is what lets an automated test (or a hardware-free "sim" run) substitute a
# FakeReader for real hardware with zero changes to the rest of the app.
#
# The callback contract (unchanged from the historical readers): a reader delivers a list of
# per-target tuples (slot, x_mm, y_mm, raw_hex) to `on_frame`. The GUI attributes them to a
# sensor by wrapping the callback in a closure (lambda lst, t, sid=...: self._on_frame(sid, lst, t)).

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Callable, List, Tuple

from radar import frames, trajectories
from radar.logging_setup import get_logger

log = get_logger("reader")

# Registry of every reader handed out by make_reader(). macOS broadcasts serial input to EVERY
# open handle of a /dev/cu.* port, so even ONE leaked reader thread re-reads the whole stream and
# double-counts frames — inflating the debug Hz and doing duplicate tracker/SQLite work. The GUI
# tracks readers in its own dict, but this registry is the backstop: stop_all_readers() guarantees
# a clean slate (no surviving reader thread) before new readers open, regardless of bookkeeping.
_ACTIVE_READERS: List["Reader"] = []


def stop_all_readers() -> None:
    # Stop and forget EVERY reader ever created by make_reader(). Idempotent: stopping an already
    # -stopped reader is a no-op. Called by the GUI before (re)opening inputs.
    n = len(_ACTIVE_READERS)
    if n:
        log.debug("stop_all_readers: stopping %d reader(s)", n)
    for r in list(_ACTIVE_READERS):
        try:
            r.stop()
        except Exception:
            pass
    _ACTIVE_READERS.clear()

# The callback receives (frame, t_mono): a list of (slot, x, y, raw_hex) tuples, plus the
# MONOTONIC capture time (time.monotonic()) stamped by the reader the instant the frame is parsed
# — the closest host-side proxy for arrival (the LD2450 embeds no timestamp). Stamping at the
# reader (not after the GUI lock + SQLite write) is what makes the recorded timeline accurate.
# Typed loosely as `list` because raw_hex makes the tuples heterogeneous.
FrameCallback = Callable[[list, float], None]


class Reader(ABC):
    # Base class for every input source. Holds the callback; subclasses implement start/stop.
    def __init__(self, on_frame: FrameCallback):
        self._on_frame = on_frame

    @abstractmethod
    def start(self) -> None:
        # Begin delivering frames to on_frame (spawn a thread, connect a broker, etc.).
        ...

    @abstractmethod
    def stop(self) -> None:
        # Stop delivering and release resources (close port, disconnect, join thread).
        ...


class FakeReader(Reader):
    # An in-process synthetic source — NO serial port, NO broker. It runs a trajectory and
    # emits real LD2450 frames (built with frames.build_frame, so they are byte-identical to
    # hardware output). Two modes:
    #
    #   threaded (default) : a daemon thread ticks the trajectory at `hz` and emits frames.
    #                        Use this for hardware-free "sim" runs of the whole app.
    #   manual-clock       : pass manual=True. No thread starts; the test drives time by
    #                        calling tick(t) to emit EXACTLY one frame. Deterministic, no
    #                        sleeps -> fast, non-flaky pipeline tests.
    def __init__(
        self,
        on_frame: FrameCallback,
        trajectory: trajectories.Trajectory,
        hz: float = 10.0,
        manual: bool = False,
        distance_gate: int = 0x0168,
    ):
        super().__init__(on_frame)
        self._traj = trajectory          # callable f(t_seconds) -> [(x, y, v), ...]
        self._hz = hz
        self._manual = manual
        self._gate = distance_gate
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # In manual mode there is no background thread — frames come only from tick().
        if self._manual:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)

    def tick(self, t: float) -> None:
        # Manual-clock entry point: emit one frame for simulated time `t` (seconds). `t` is both
        # the trajectory time AND the capture stamp, so tests get fully deterministic timing.
        self._emit_at(t, t)

    def _loop(self) -> None:
        # Threaded mode: walk wall-clock time from a monotonic baseline and emit at `hz`.
        t0 = time.monotonic()
        period = 1.0 / self._hz if self._hz > 0 else 0.1
        while not self._stop.is_set():
            now = time.monotonic()
            self._emit_at(now - t0, now)         # trajectory time is relative; STAMP is absolute
            # Interruptible sleep: stop() sets the event and this returns immediately, so the
            # thread halts promptly (no emit lands after stop()/store close). Falls through on timeout.
            if self._stop.wait(period):
                break

    def _emit_at(self, traj_t: float, stamp: float) -> None:
        # Build a frame from the trajectory at `traj_t`, then deliver it in the SAME shape a real
        # reader would: (slot, x, y, raw_hex). `stamp` is the capture time handed to the callback.
        # Threaded mode passes an ABSOLUTE time.monotonic() (so it matches the real readers and the
        # GUI's data-loss watchdog); manual tick(t) passes the test's controlled clock for
        # deterministic timing. Round-tripping through build_frame+parse keeps the fake output
        # byte-identical to hardware.
        targets = self._traj(traj_t)
        frame = frames.build_frame(targets, self._gate)
        hex_str = frame.hex()
        tracks = [tpl + (hex_str,) for tpl in frames.parse(frame)]
        self._on_frame(tracks, stamp)


def make_reader(sensor, on_frame: FrameCallback) -> Reader:
    # Factory: build the right Reader for a Sensor's transport. This is the ONLY place that
    # knows which concrete reader class maps to which input_mode, so the GUI stays decoupled.
    #
    # The real readers are imported LAZILY (inside this function) on purpose: serial_reader
    # and mqtt_client import Reader from this module, so importing them at module top-level
    # would create a circular import. Importing them here, at call time, breaks the cycle.
    mode = sensor.input_mode.lower()

    if mode == "mqtt":
        from radar.mqtt_client import RadarMQTT
        reader = RadarMQTT(sensor.broker, sensor.port, sensor.topic, on_frame)
    elif mode == "sim":
        traj = trajectories.make(sensor.sim_pattern)
        reader = FakeReader(on_frame, traj, hz=sensor.sim_hz)
    else:                                   # default: real serial/UART bridge
        from radar.serial_reader import RadarSerial
        reader = RadarSerial(sensor.serial_port, sensor.serial_baud, on_frame)

    _ACTIVE_READERS.append(reader)          # track for the stop_all_readers() backstop
    return reader
