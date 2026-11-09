"""Real hardware plane for USB-SOVEREIGN (Increment 2): socat PTY pairs.

Increment 1 ran the controller against in-process simulated ports. This
module gives the controller *real serial endpoints*: for every interface
name, a ``socat`` process bridges two PTY devices —

    device endpoint  /tmp/usb-sovereign-pty/<NAME>.dev
          ⇅  (socat, byte-pure)
    controller port  /tmp/usb-sovereign-pty/<NAME>.ctl

The device plant (talkers, cellular pattern) writes to the ``.dev`` end and
drains what the controller forwards back; the ``UnifiedSerialController``
opens the ``.ctl`` end through ``PtySerialPort``, which exposes exactly the
pyserial-shaped interface the controller already uses (``in_waiting``,
``read(n)``, ``write(data)``, ``close()``, byte counters, ``baudrate``).
Swapping in physical hardware later means opening a real ``/dev/ttyUSB*``
(or a pyserial object) in place of the ``.ctl`` end — nothing in the
controller changes.

Byte purity (R1): both PTYs are created ``raw,echo=0`` and both fds are put
in raw termios mode, so no line discipline mangles the stream.

Standard library + the system ``socat`` binary only.
"""

import asyncio
import fcntl
import os
import random
import struct
import subprocess
import termios
import time
import tty

from simulated_ports import FRAME_HEADERS, CellularPattern, _frame


def _open_raw(path: str) -> int:
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
    tty.setraw(fd)
    return fd


def _available(fd: int) -> int:
    return struct.unpack("I", fcntl.ioctl(fd, termios.FIONREAD,
                                          struct.pack("I", 0)))[0]


class PtySerialPort:
    """Controller-side end of a socat PTY pair (pyserial-shaped)."""

    def __init__(self, name: str, ctl_path: str, baudrate: int = 9600):
        self.name = name
        self.baudrate = baudrate
        self.timeout = 0  # R3/R4 non-blocking, same as spec serial setup
        self.device = ctl_path
        self._fd = _open_raw(ctl_path)
        self.bytes_received = 0      # bytes read from the device side
        self.bytes_transmitted = 0   # bytes written toward the device side
        self._open = True

    @property
    def in_waiting(self) -> int:
        if not self._open:
            return 0
        try:
            return _available(self._fd)
        except OSError:
            return 0

    @property
    def is_open(self) -> bool:
        return self._open

    def read(self, n: int) -> bytes:
        if not self._open or n <= 0:
            return b""
        try:
            data = os.read(self._fd, n)
        except (BlockingIOError, OSError):
            return b""
        self.bytes_received += len(data)
        return data

    def write(self, data: bytes) -> int:
        if not self._open:
            return 0
        written = 0
        view = memoryview(data)
        for _ in range(8):  # PTY buffers are large vs. our small frames
            try:
                written += os.write(self._fd, view[written:])
            except (BlockingIOError, OSError):
                break
            if written >= len(data):
                break
        self.bytes_transmitted += written
        return written

    def close(self):
        self._open = False
        try:
            os.close(self._fd)
        except OSError:
            pass


class PtyDeviceEndpoint:
    """Device-side end of the pair: where the external plant lives.

    ``feed()`` pushes device traffic toward the controller; ``drain()``
    collects whatever the controller forwarded back (pair traffic), which
    makes end-to-end forwarding through real serial endpoints observable.
    """

    def __init__(self, name: str, dev_path: str):
        self.name = name
        self.device = dev_path
        self._fd = _open_raw(dev_path)
        self.bytes_fed = 0
        self.received = bytearray()   # bytes arriving from the controller
        self._open = True

    def feed(self, data: bytes):
        if not self._open:
            return
        try:
            self.bytes_fed += os.write(self._fd, data)
        except (BlockingIOError, OSError):
            pass

    def drain(self) -> int:
        if not self._open:
            return 0
        n = 0
        try:
            waiting = _available(self._fd)
            while waiting > 0:
                chunk = os.read(self._fd, waiting)
                if not chunk:
                    break
                self.received.extend(chunk)
                n += len(chunk)
                waiting = _available(self._fd)
        except (BlockingIOError, OSError):
            pass
        return n

    def close(self):
        self._open = False
        try:
            os.close(self._fd)
        except OSError:
            pass


class SocatPtyPlant:
    """Owns the socat processes and both ends of every PTY pair."""

    def __init__(self, pty_dir: str, baud: dict):
        self.pty_dir = pty_dir
        self.baud = baud
        self._procs = []
        self.ports = {}      # name -> PtySerialPort (controller side)
        self.endpoints = {}  # name -> PtyDeviceEndpoint (device side)

    def start(self):
        os.makedirs(self.pty_dir, exist_ok=True)
        for name, baudrate in self.baud.items():
            dev = os.path.join(self.pty_dir, f"{name}.dev")
            ctl = os.path.join(self.pty_dir, f"{name}.ctl")
            for link in (dev, ctl):
                if os.path.islink(link) or os.path.exists(link):
                    os.unlink(link)
            proc = subprocess.Popen(
                ["socat", "-d", "-d",
                 f"PTY,link={dev},raw,echo=0",
                 f"PTY,link={ctl},raw,echo=0"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._procs.append(proc)
            deadline = time.time() + 3.0
            while not (os.path.exists(dev) and os.path.exists(ctl)):
                if time.time() > deadline:
                    raise RuntimeError(f"socat PTY links for {name} "
                                       f"did not appear in {self.pty_dir}")
                time.sleep(0.02)
            self.ports[name] = PtySerialPort(name, ctl, baudrate=baudrate)
            self.endpoints[name] = PtyDeviceEndpoint(name, dev)
        return self.ports

    def stats(self) -> dict:
        return {
            "mode": "pty",
            "pty_dir": self.pty_dir,
            "device_endpoints": {
                name: {"fed_bytes": ep.bytes_fed,
                       "received_from_controller": len(ep.received),
                       "device": ep.device}
                for name, ep in self.endpoints.items()
            },
        }

    def close(self):
        for port in self.ports.values():
            port.close()
        for ep in self.endpoints.values():
            ep.close()
        for proc in self._procs:
            proc.terminate()
        for proc in self._procs:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------- Device plant traffic over PTY endpoints ----------------

async def pty_talker(endpoint: PtyDeviceEndpoint, source: str,
                     interval=(0.3, 1.4), stop: asyncio.Event = None):
    """Same traffic shape as simulated_ports.talker, over a real PTY."""
    while stop is None or not stop.is_set():
        endpoint.feed(_frame(FRAME_HEADERS[source], random.randint(8, 48)))
        await asyncio.sleep(random.uniform(*interval))


async def pty_cellular_talker(endpoint: PtyDeviceEndpoint,
                              pattern: CellularPattern,
                              stop: asyncio.Event):
    while not stop.is_set():
        if pattern.is_up():
            endpoint.feed(_frame(FRAME_HEADERS["CELL"],
                                 random.randint(16, 64)))
            await asyncio.sleep(random.uniform(0.4, 0.9))
        else:
            await asyncio.sleep(0.2)


async def pty_ziltb_talker(endpoint: PtyDeviceEndpoint,
                           pattern: CellularPattern,
                           stop: asyncio.Event):
    while not stop.is_set():
        if not pattern.is_up():
            endpoint.feed(_frame(FRAME_HEADERS["ZILTB"],
                                 random.randint(16, 64)))
            await asyncio.sleep(random.uniform(0.4, 0.9))
        else:
            await asyncio.sleep(0.2)


async def pty_drainer(endpoints: dict, stop: asyncio.Event):
    """Continuously drains device endpoints so forwarded pair traffic is
    collected (and PTY buffers never back up)."""
    while not stop.is_set():
        for ep in endpoints.values():
            ep.drain()
        await asyncio.sleep(0.02)
