"""Simulated serial ports and device traffic for USB-SOVEREIGN v1.

Stands in for physical pyserial ports so the UnifiedSerialController can run
(and be verified) without hardware. Each SimulatedSerialPort mirrors the
subset of the pyserial API the controller uses: ``in_waiting``, ``read(n)``,
``write(data)``, ``close()``.

Simulated external devices "feed" bytes into a port's receive buffer via
``feed()``. Bytes the controller writes out are captured as transmitted (TX),
which is how pair forwarding (COM1 -> COM2 etc.) becomes observable.
"""

import asyncio
import random
import time


class SimulatedSerialPort:
    def __init__(self, name: str, baudrate: int = 9600):
        self.name = name
        self.baudrate = baudrate
        self.timeout = 0  # R3/R4: non-blocking, same as the spec's serial setup
        self._rx = bytearray()
        self.tx_capture = bytearray()
        self.bytes_received = 0
        self.bytes_transmitted = 0
        self.frames_received = 0
        self.last_feed_at = None
        self._open = True

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    @property
    def is_open(self) -> bool:
        return self._open

    def feed(self, data: bytes):
        """An external (simulated) device pushes bytes into this port."""
        if not self._open:
            return
        self._rx.extend(data)
        self.bytes_received += len(data)
        self.frames_received += 1
        self.last_feed_at = time.time()

    def read(self, n: int) -> bytes:
        n = min(n, len(self._rx))
        data = bytes(self._rx[:n])
        del self._rx[:n]
        return data

    def write(self, data: bytes) -> int:
        if not self._open:
            return 0
        self.tx_capture.extend(data)
        self.bytes_transmitted += len(data)
        return len(data)

    def close(self):
        self._open = False


# ---------------- Simulated device traffic ----------------

def _frame(header: bytes, payload_len: int) -> bytes:
    body = bytes(random.randint(0, 255) for _ in range(payload_len))
    return header + body


# Protocol-signature-flavoured headers from the spec's ClickHouse registry:
# 41S0 sport_handshake, 41C1 coort_response, 41L0 lan_ethernet_frame.
FRAME_HEADERS = {
    "COM1": b"\x41\x4c\x30",        # 'AL0'
    "COM2": b"\x41\x4c\x31",        # 'AL1'
    "Sport": b"\x41\x53\x30",       # 'AS0' sport_handshake
    "Coort": b"\x41\x43\x31",       # 'AC1' coort_response
    "LH": b"\x41\x48\x30",          # bio-telemetry
    "FSH": b"\x41\x46\x30",
    "BT": b"\x41\x42\x30",
    "WIFI": b"\x41\x57\x30",
    "LAN": b"\x41\x4c\x30",         # lan_ethernet_frame
    "CELL": b"\x41\x54\x30",        # cellular telemetry
    "ZILTB": b"\x41\x5a\x30",       # failover channel
}


async def talker(port: SimulatedSerialPort, source: str,
                 interval=(0.3, 1.4), stop: asyncio.Event = None):
    """Generic simulated device: feeds a small frame every interval."""
    while stop is None or not stop.is_set():
        port.feed(_frame(FRAME_HEADERS[source], random.randint(8, 48)))
        await asyncio.sleep(random.uniform(*interval))


# Bio-telemetry event headers (ClickHouse catalog ids 10/11): the surge /
# pulse frames are keyed by their raw two-byte prefix 0x41A0 / 0x41B0 —
# the same headers the registry's PREFIX_SIGNATURES and the controller's
# COM3/COM4 mirror rules match on. The third byte keeps the plant's
# 3-byte header shape.
BIO_EVENT_HEADERS = {
    "LH": b"\x41\xa0\x30",          # lh_surge
    "FSH": b"\x41\xb0\x30",         # fsh_pulse
}
BIO_EVENT_EVERY = 5  # every 5th bio frame is a surge/pulse event frame


async def bio_talker(port: SimulatedSerialPort, source: str,
                     interval=(0.5, 1.6), stop: asyncio.Event = None):
    """Bio-telemetry device (LH/FSH): baseline frames on the source's
    regular header, with every 5th frame carrying the surge/pulse event
    header so the registry classification and the COM3/COM4 mirroring
    fire regularly."""
    count = 0
    while stop is None or not stop.is_set():
        count += 1
        header = (BIO_EVENT_HEADERS[source] if count % BIO_EVENT_EVERY == 0
                  else FRAME_HEADERS[source])
        port.feed(_frame(header, random.randint(8, 48)))
        await asyncio.sleep(random.uniform(*interval))


class CellularPattern:
    """Repeating cellular availability pattern: UP for up_s, DOWN for down_s.

    Default 14 s up / 12 s down guarantees the controller's >5.0 s silence
    failover gate fires once per cycle and then stands down on restoration.
    """

    def __init__(self, up_s: float = 14.0, down_s: float = 12.0):
        self.up_s = up_s
        self.down_s = down_s
        self.t0 = time.time()

    def is_up(self) -> bool:
        phase = (time.time() - self.t0) % (self.up_s + self.down_s)
        return phase < self.up_s


async def cellular_talker(port: SimulatedSerialPort, pattern: CellularPattern,
                          stop: asyncio.Event):
    """Feeds the CELL port only while the pattern says the link is up."""
    while not stop.is_set():
        if pattern.is_up():
            port.feed(_frame(FRAME_HEADERS["CELL"], random.randint(16, 64)))
            await asyncio.sleep(random.uniform(0.4, 0.9))
        else:
            await asyncio.sleep(0.2)


async def ziltb_talker(port: SimulatedSerialPort, pattern: CellularPattern,
                       stop: asyncio.Event):
    """The ZilTB failover path carries traffic while cellular is down."""
    while not stop.is_set():
        if not pattern.is_up():
            port.feed(_frame(FRAME_HEADERS["ZILTB"], random.randint(16, 64)))
            await asyncio.sleep(random.uniform(0.4, 0.9))
        else:
            await asyncio.sleep(0.2)
