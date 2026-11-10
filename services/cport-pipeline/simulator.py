#!/usr/bin/env python3
"""
Virtual sensor source for the C-Port CRC16 pipeline.

Emits correctly framed CRC16 packets in exactly the pipeline's wire format
(>HHffH: sync 0xAA55, sensor_id, val1, val2, CRC16-CCITT over sync+payload):

  * configurable sensor IDs and frame rate,
  * configurable corruption rate — a payload byte is flipped AFTER the CRC
    is computed, so the frame's checksum no longer matches and the pipeline
    must drop it (this models electrical noise on the COM port),
  * a burst mode that emits many frames back-to-back to pressure the
    pipeline's write queue / backpressure path.

Two ways to use it:

  (a) In-process: SimulatedSource wraps a SensorSimulator and exposes the
      read(size) -> bytes interface the pipeline loop consumes
      (pipeline.py --simulate uses this; no hardware, no PTY needed).

  (b) Standalone: writes the byte stream to a PTY (or a given serial path)
      so a separately-running pipeline process can read it as if a sensor
      were attached:

          python3 simulator.py --pty                 # prints the PTY path
          python3 simulator.py --pty /tmp/vsensor    # symlinks that path
          python3 simulator.py --port /dev/ttyUSB0   # real serial device
"""

import argparse
import os
import random
import struct
import sys
import threading
import time

from pipeline import PACKET_SIZE, SYNC_WORD, calculate_crc16


def build_frame(sensor_id, val1, val2, corrupt=False, rng=None):
    """Build one 14-byte wire frame. With corrupt=True, one payload byte
    (sensor-id/value region — never the sync marker or the CRC field) is
    flipped after checksumming, so CRC validation must reject the frame."""
    body = struct.pack(">HHff", SYNC_WORD, int(sensor_id), float(val1), float(val2))
    frame = bytearray(body + struct.pack(">H", calculate_crc16(body)))
    if corrupt:
        r = rng if rng is not None else random
        idx = r.randrange(2, PACKET_SIZE - 2)  # payload bytes only
        frame[idx] ^= 0xFF
    return bytes(frame)


class SensorSimulator:
    """Generates frames one call at a time; each next_frame() returns the
    bytes for one frame — or, on a burst boundary, for burst_size frames
    concatenated. Counters (frames_emitted / frames_corrupted) let callers
    reconcile exactly what was put on the wire."""

    def __init__(self, sensor_ids=(1, 2, 3), rate_hz=10.0, corruption_rate=0.1,
                 burst_every=None, burst_size=50, seed=None):
        if not sensor_ids:
            raise ValueError("at least one sensor id is required")
        if not 0.0 <= corruption_rate <= 1.0:
            raise ValueError("corruption_rate must be within [0, 1]")
        self.sensor_ids = tuple(sensor_ids)
        self.rate_hz = float(rate_hz)
        self.corruption_rate = float(corruption_rate)
        self.burst_every = burst_every
        self.burst_size = int(burst_size)
        self._rng = random.Random(seed)
        self.frames_emitted = 0
        self.frames_corrupted = 0
        self._since_burst = 0
        self._phase = self._rng.uniform(0.0, 6.283)

    def _one_frame(self):
        self._phase += 0.11
        sensor_id = self._rng.choice(self.sensor_ids)
        # Smooth-ish wandering values so output looks like real telemetry.
        val1 = 36.5 + 4.0 * self._rng.uniform(-1.0, 1.0) + (self._phase % 3.0)
        val2 = 98.0 + 10.0 * self._rng.uniform(-1.0, 1.0)
        corrupt = self._rng.random() < self.corruption_rate
        frame = build_frame(sensor_id, val1, val2, corrupt=corrupt, rng=self._rng)
        self.frames_emitted += 1
        if corrupt:
            self.frames_corrupted += 1
        return frame

    def next_frame(self) -> bytes:
        """Return the next chunk of wire bytes: a single frame normally, or
        a whole burst when the burst counter trips."""
        self._since_burst += 1
        if self.burst_every and self._since_burst >= self.burst_every:
            self._since_burst = 0
            return b"".join(self._one_frame() for _ in range(self.burst_size))
        return self._one_frame()


class SimulatedSource:
    """Pipeline-facing source (usage (a)): a background producer thread
    streams simulator frames into a buffer at the configured rate; read()
    hands out buffered bytes, briefly waiting when the buffer is empty so
    the pipeline loop doesn't busy-spin."""

    def __init__(self, simulator: SensorSimulator):
        self.simulator = simulator
        self._pending = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._produce, daemon=True)
        self._thread.start()

    def _produce(self):
        interval = 1.0 / self.simulator.rate_hz if self.simulator.rate_hz > 0 else 0.0
        while not self._closed:
            chunk = self.simulator.next_frame()
            with self._lock:
                self._pending += chunk
            if interval:
                time.sleep(interval)

    def read(self, size):
        deadline = time.time() + 0.05
        while True:
            with self._lock:
                if self._pending:
                    n = min(size, len(self._pending))
                    data = bytes(self._pending[:n])
                    del self._pending[:n]
                    return data
            if self._closed or time.time() >= deadline:
                return b""
            time.sleep(0.002)

    def close(self):
        self._closed = True


# --------------------------------------------------------------------------
# Standalone mode (usage (b)): stream frames onto a PTY or serial device
# --------------------------------------------------------------------------
def _stream_to_fd(fd, simulator, duration):
    """Write simulator frames to a file descriptor at the configured rate.
    Non-blocking: if the reader falls behind and the buffer fills, frames
    are skipped (and reported) rather than stalling the stream."""
    os.set_blocking(fd, False)
    interval = 1.0 / simulator.rate_hz if simulator.rate_hz > 0 else 0.0
    started = time.time()
    skipped = 0
    try:
        while duration is None or time.time() - started < duration:
            chunk = simulator.next_frame()
            try:
                os.write(fd, chunk)
            except BlockingIOError:
                skipped += simulator.burst_size if len(chunk) > PACKET_SIZE else 1
            except OSError as e:
                print(f"[Simulator] write failed: {e}")
                break
            if interval:
                time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[Simulator] interrupted.")
    print(f"[Simulator] done: {simulator.frames_emitted} frames emitted "
          f"({simulator.frames_corrupted} corrupted), {skipped} skipped "
          "because the reader fell behind.")


def main(argv=None):
    p = argparse.ArgumentParser(description="Virtual sensor source for the "
                                            "C-Port CRC16 pipeline")
    p.add_argument("--pty", nargs="?", const="", default=None, metavar="LINK",
                   help="Open a PTY and stream to it; optionally symlink the "
                        "given path to the PTY slave")
    p.add_argument("--port", help="Write to this serial device instead "
                                  "(requires pyserial)")
    p.add_argument("--rate", type=float, default=10.0, help="Frames/second")
    p.add_argument("--sensors", default="1,2,3",
                   help="Comma-separated sensor IDs")
    p.add_argument("--corruption", type=float, default=0.1,
                   help="Fraction of frames corrupted (0..1)")
    p.add_argument("--burst-every", type=int, default=None,
                   help="Emit a burst every N frames")
    p.add_argument("--burst-size", type=int, default=50, help="Frames per burst")
    p.add_argument("--duration", type=float, default=None,
                   help="Stop after N seconds (default: until Ctrl+C)")
    p.add_argument("--seed", type=int, default=None, help="Seed the RNG")
    args = p.parse_args(argv)

    sensor_ids = tuple(int(s) for s in args.sensors.split(",") if s.strip())
    simulator = SensorSimulator(sensor_ids=sensor_ids, rate_hz=args.rate,
                                corruption_rate=args.corruption,
                                burst_every=args.burst_every,
                                burst_size=args.burst_size, seed=args.seed)

    if args.port:
        try:
            import serial
        except ImportError:
            print("ERROR: pyserial is required for --port mode "
                  "(pip install pyserial).")
            return 2
        ser = serial.Serial(args.port, 9600, timeout=1)
        print(f"[Simulator] streaming to serial device {args.port} — Ctrl+C to stop.")
        started = time.time()
        try:
            while args.duration is None or time.time() - started < args.duration:
                ser.write(simulator.next_frame())
                if simulator.rate_hz > 0:
                    time.sleep(1.0 / simulator.rate_hz)
        except KeyboardInterrupt:
            print("\n[Simulator] interrupted.")
        finally:
            ser.close()
        print(f"[Simulator] done: {simulator.frames_emitted} frames emitted "
              f"({simulator.frames_corrupted} corrupted).")
        return 0

    if args.pty is not None:
        import pty
        import tty
        master_fd, slave_fd = pty.openpty()
        # Raw mode: a PTY defaults to cooked/canonical line discipline,
        # which buffers and mangles binary frames. A serial link is raw —
        # set it so any reader sees the exact byte stream (pyserial would
        # do this itself; raw os.open readers would otherwise lose data).
        tty.setraw(slave_fd)
        slave_name = os.ttyname(slave_fd)
        print(f"[Simulator] PTY slave device: {slave_name}")
        print(f"[Simulator] point the pipeline at it: "
              f"python3 pipeline.py --port {slave_name}")
        if args.pty:
            try:
                if os.path.islink(args.pty) or os.path.exists(args.pty):
                    os.unlink(args.pty)
                os.symlink(slave_name, args.pty)
                print(f"[Simulator] symlinked {args.pty} -> {slave_name}")
            except OSError as e:
                print(f"[Simulator] could not create symlink {args.pty}: {e}")
        print("[Simulator] streaming — Ctrl+C to stop.")
        _stream_to_fd(master_fd, simulator, args.duration)
        return 0

    p.print_help()
    print("\nERROR: choose --pty or --port (or use pipeline.py --simulate "
          "for in-process simulation).")
    return 2


if __name__ == "__main__":
    sys.exit(main())
