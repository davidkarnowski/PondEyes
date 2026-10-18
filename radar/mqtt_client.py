# radar.mqtt_client
# =================
#
# Receives LD2450 frames over MQTT (each message payload is one 30-byte frame as a hex
# string), parses them with the shared codec, and delivers them to a callback.
#
# Like serial_reader, this is now a thin transport over radar.frames + radar.reader_base:
# all wire-format knowledge lives in frames, and the start/stop lifecycle comes from Reader.
# The MQTT-specific job is: connect, subscribe, and hand decoded frames off a worker thread
# (so the paho network thread is never blocked by the GUI callback).
#
# Back-compat note: the GUI historically called `reader.connect()` and `reader.cli.loop_stop()`
# directly. We keep `connect()` as an alias of start() and keep the `.cli` attribute so the
# existing GUI keeps working until it is switched to the uniform start()/stop() in Phase 1.

from __future__ import annotations

import time
import uuid
import threading
from queue import Queue, Empty

import paho.mqtt.client as mqtt

from radar import frames
from radar.reader_base import Reader, FrameCallback


class RadarMQTT(Reader):
    HDR = frames.HDR
    FTR = frames.FTR
    FLEN = frames.FLEN

    def __init__(self, host, port, topic, on_frame: FrameCallback):
        super().__init__(on_frame)
        self.host, self.port, self.topic = host, port, topic
        self.last_pkt = time.monotonic()

        # A random client id avoids the broker kicking us off when several instances connect.
        # Pin the v1 callback API explicitly: paho 2.x warns otherwise, and our on_connect /
        # on_message signatures follow the v1 shape.
        random_id = f"gui-{uuid.uuid4().hex[:8]}"
        try:
            self.cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=random_id)
        except (AttributeError, TypeError):
            self.cli = mqtt.Client(client_id=random_id)   # paho 1.x fallback
        self.cli.on_connect = self._on_connect
        self.cli.on_message = self._on_msg

        # Decoded frames are queued here and drained by a worker thread, so the paho callback
        # (_on_msg, on the network thread) stays fast and never runs GUI/tracker code.
        self.q: Queue = Queue()
        self._stop = threading.Event()
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def start(self) -> None:
        # NON-BLOCKING connect: connect_async() only records the target; the actual TCP connect
        # happens on paho's network thread after loop_start(). This is what keeps app startup
        # instant when the broker is down/unreachable (a plain connect() would block the caller
        # for the full TCP timeout). paho's loop also owns RECONNECTION: if the broker drops or
        # was never up, it retries forever with the backoff configured below, and on_connect
        # re-subscribes on every (re)connect — so a sensor that comes online later just appears.
        self.cli.reconnect_delay_set(min_delay=1, max_delay=8)
        self.cli.connect_async(self.host, self.port, 60)
        self.cli.loop_start()

    # Back-compat alias: the current GUI calls connect(); new code should call start().
    connect = start

    def stop(self) -> None:
        self._stop.set()
        try:
            self.cli.loop_stop()
            self.cli.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, *_):
        client.subscribe(self.topic)

    def _on_msg(self, _cli, _userdata, msg):
        # Decode one payload. Anything malformed is silently ignored — a noisy topic must not
        # crash the reader.
        try:
            hex_str = msg.payload.decode().strip()
            buf = bytes.fromhex(hex_str)
            if frames.is_complete_frame(buf):
                tracks = [t + (hex_str,) for t in frames.parse(buf)]
                self.q.put_nowait(tracks)
                self.last_pkt = time.monotonic()
        except Exception:
            pass

    def _worker_loop(self):
        # Drain decoded frames and deliver them to the callback. The 1s timeout lets the loop
        # notice a stop() request promptly instead of blocking forever on an idle topic.
        while not self._stop.is_set():
            try:
                tracks = self.q.get(timeout=1)
                self._on_frame(tracks, time.monotonic())   # stamp at delivery
            except Empty:
                continue
