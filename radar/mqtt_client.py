import time
import uuid
import threading
from queue import Queue, Empty
import paho.mqtt.client as mqtt


class RadarMQTT:
    """
    Connects to the broker, parses length-30 radar frames, and sends them to the
    GUI callback using a background worker thread.

    The callback receives a list of 4-tuples:
        (slot, x_mm, y_mm, raw_hex)

    • `raw_hex` is the original MQTT payload as a hex string.
    """

    HDR  = bytes.fromhex("AAFF0300")
    FTR  = bytes.fromhex("55CC")
    FLEN = 30

    def __init__(self, host, port, topic, on_frame):
        self.host, self.port, self.topic = host, port, topic
        self.on_frame = on_frame
        self.last_pkt = time.monotonic()

        random_id = f"gui-{uuid.uuid4().hex[:8]}"
        self.cli = mqtt.Client(client_id=random_id)
        self.cli.on_connect = self._on_connect
        self.cli.on_message = self._on_msg

        self.q = Queue()
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def connect(self):
        self.cli.connect(self.host, self.port, 60)
        self.cli.loop_start()

    def _on_connect(self, client, *_):
        client.subscribe(self.topic)

    def _on_msg(self, _cli, _userdata, msg):
        try:
            hex_str = msg.payload.decode().strip()
            buf = bytes.fromhex(hex_str)

            if (
                len(buf) == self.FLEN and
                buf.startswith(self.HDR) and
                buf.endswith(self.FTR)
            ):
                tracks = self._parse(buf)
                tracks = [t + (hex_str,) for t in tracks]
                self.q.put_nowait(tracks)
                self.last_pkt = time.monotonic()
        except Exception:
            pass  # ignore malformed payloads

    def _worker_loop(self):
        while True:
            try:
                tracks = self.q.get(timeout=1)
                self.on_frame(tracks)
            except Empty:
                continue

    @staticmethod
    def _s15(u: int) -> int:
        """16-bit signed little-endian → Python int."""
        return u & 0x7FFF if u & 0x8000 else -(u & 0x7FFF)

    def _parse(self, b: bytes):
        """
        Extract up to 3 target blobs from the 30-byte frame.

        Returns
        -------
        list[(slot, x_mm, y_mm)]
        """
        out = []
        for i in range(3):
            chunk = b[4 + i * 8 : 8 + i * 8]
            if any(chunk):
                x = self._s15(int.from_bytes(chunk[:2], "little"))
                y = self._s15(int.from_bytes(chunk[2:], "little"))
                out.append((i + 1, x, y))
        return out
