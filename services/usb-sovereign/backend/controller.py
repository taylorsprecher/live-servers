"""UnifiedSerialController — adapted from the USB-SOVEREIGN v1 specification
in Drive's 'Cport to Coort.docx'.

The control flow, source names, baud rates, failover gate, 10 ms yield cycle,
and shutdown sequence follow the document's production implementation. The
differences are deliberate and confined to the edges:

* pyserial ports are replaced by injected port objects (SimulatedSerialPort
  in this increment; real serial.Serial ports drop in unchanged later),
* AwsIoTClient / AwsS3BatchLogger are replaced by the stub sinks in
  sinks.py, which reproduce their behavior locally,
* per-source statistics and an event log are maintained for the monitoring
  dashboard's /api/status endpoint.

Governing rules honored: R1 byte purity (forwarded bytes are never mutated),
R3/R4/R16 non-blocking 10 ms yield, failover gate (>5.0 s cellular silence
binds ZILTB until cellular returns), R21-R25 graceful shutdown (flush S3
buffer, close ports, disconnect MQTT).
"""

import asyncio
import time
from collections import deque

FAILOVER_THRESHOLD_S = 5.0  # spec: cellular silence > 5.0 s binds ZILTB

PAIRS = [("COM1", "COM2"), ("Sport", "Coort")]
INGEST_SOURCES = ["LH", "FSH", "BT", "WIFI", "LAN"]
ALL_SOURCES = ["COM1", "COM2", "Sport", "Coort", "CELL", "ZILTB"] + INGEST_SOURCES


class EventLog:
    def __init__(self, capacity: int = 200):
        self._events = deque(maxlen=capacity)

    def add(self, level: str, message: str):
        entry = {"ts": time.time(), "level": level, "message": message}
        self._events.append(entry)
        print(f"[{level}] {message}")
        return entry

    def recent(self, n: int = 50):
        return list(self._events)[-n:]


class UnifiedSerialController:
    def __init__(self, ports: dict, iot, s3, dynamodb, events: EventLog):
        self.ports = ports                      # name -> port object
        self.iot = iot                          # TelemetrySink (MQTT stub)
        self.s3 = s3                            # S3BatchLoggerStub
        self.dynamodb = dynamodb                # DynamoDbStubSink
        self.events = events

        self.last_cell_activity = time.time()
        self.ziltb_active = False
        self._running = True
        self.started_at = time.time()
        self.failover_engagements = 0

        # Increment 2 attachments (set by main.py after construction):
        #   store        — SerialEventStore; every published envelope is
        #                  appended to the local serial_events log.
        #   extra_status — optional callable returning a dict merged into
        #                  the status snapshot (e.g. PTY plant stats).
        self.store = None
        self.extra_status = None
        self.plant_label = "simulated ports"

        # stats[source] = {"bytes": int, "frames": int, "last_activity": ts|None}
        self.stats = {s: {"bytes": 0, "frames": 0, "last_activity": None}
                      for s in ALL_SOURCES}
        # rate history: deque of (ts, {source: cumulative_bytes})
        self._history = deque(maxlen=240)

    # ---------------- main loop (spec: run / _process_serial) ----------------

    async def run(self):
        self.events.add("INFO", "Unified serial bridge active with LAN, RF & "
                                f"ZilTB monitoring. ({self.plant_label})")
        self.iot.connect()
        try:
            while self._running:
                await self._process_serial()
                if self.s3.should_flush():
                    uri = self.s3.flush()
                    if uri:
                        self.events.add("S3", f"Batch flushed -> {uri}")
                self._sample_history()
                await asyncio.sleep(0.01)  # 10 ms yield cycle (R16)
        finally:
            self._shutdown()

    def stop(self):
        self._running = False

    def _ingest(self, source: str, data: bytes):
        """Telemetry fan-out shared by every source (spec: publish + buffer)."""
        envelope = self.iot.publish(source, data)
        if self.store is not None and envelope is not None:
            self.store.append(envelope)
        self.s3.add_entry(source, data)
        st = self.stats[source]
        st["bytes"] += len(data)
        st["frames"] += 1
        st["last_activity"] = time.time()

    async def _process_serial(self):
        now = time.time()
        ports = self.ports

        # 1. Paired forwarding loops: COM1 <-> COM2, Sport <-> Coort (R1/R2)
        for a, b in PAIRS:
            pa, pb = ports[a], ports[b]
            if pa.in_waiting > 0:
                data = pa.read(pa.in_waiting)
                pb.write(data)
                self._ingest(a, data)
            if pb.in_waiting > 0:
                data = pb.read(pb.in_waiting)
                pa.write(data)
                self._ingest(b, data)

        # 2. Primary cellular ingestion & ZilTB failover tracking
        cell = ports["CELL"]
        if cell.in_waiting > 0:
            data = cell.read(cell.in_waiting)
            self._ingest("CELL", data)
            self.last_cell_activity = now
            if self.ziltb_active:
                self.events.add("CELL", "Cellular connection restored. "
                                        "Standing down ZilTB.")
                self.ziltb_active = False

        # Failover Gate: bind ZilTB if CELL is inactive for > 5.0 seconds
        if (now - self.last_cell_activity > FAILOVER_THRESHOLD_S) \
                and not self.ziltb_active:
            self.events.add("FAILOVER", "Out of Cell Tower range. Automatically "
                                        "binding connection to ZilTB...")
            self.ziltb_active = True
            self.failover_engagements += 1

        if self.ziltb_active and ports["ZILTB"].in_waiting > 0:
            data = ports["ZILTB"].read(ports["ZILTB"].in_waiting)
            self._ingest("ZILTB", data)

        # 3. Multi-interface ingestion streams (LH, FSH, BT, WIFI, LAN)
        for source in INGEST_SOURCES:
            port = ports[source]
            if port.in_waiting > 0:
                data = port.read(port.in_waiting)
                self._ingest(source, data)

    # ---------------- graceful shutdown (R21-R25) ----------------

    def _shutdown(self):
        self.events.add("INFO", "Shutdown: flushing buffers, closing ports, "
                                "disconnecting MQTT.")
        self.s3.flush()
        for p in self.ports.values():
            p.close()
        self.iot.disconnect()
        self._running = False

    # ---------------- dashboard status ----------------

    def _sample_history(self):
        now = time.time()
        if self._history and now - self._history[-1][0] < 0.25:
            return
        self._history.append(
            (now, {s: self.stats[s]["bytes"] for s in ALL_SOURCES}))

    def _rate(self, source: str, window_s: float = 5.0) -> float:
        if len(self._history) < 2:
            return 0.0
        now, latest = self._history[-1]
        target = now - window_s
        oldest_t, oldest = self._history[0]
        for t, snap in self._history:
            if t >= target:
                oldest_t, oldest = t, snap
                break
        dt = now - oldest_t
        if dt <= 0:
            return 0.0
        return (latest[source] - oldest[source]) / dt

    def status_snapshot(self) -> dict:
        now = time.time()
        sources = {}
        for name in ALL_SOURCES:
            st = self.stats[name]
            port = self.ports[name]
            sources[name] = {
                "bytes_in": st["bytes"],
                "frames": st["frames"],
                "bytes_per_s": round(self._rate(name), 1),
                "last_activity_s_ago": (
                    None if st["last_activity"] is None
                    else round(now - st["last_activity"], 2)),
                "device_bytes_received": port.bytes_received,
                "bytes_transmitted": port.bytes_transmitted,
                "baudrate": port.baudrate,
            }
        pairs = []
        for a, b in PAIRS:
            pairs.append({
                "a": a, "b": b,
                "a_to_b_bytes": self.ports[b].bytes_transmitted,
                "b_to_a_bytes": self.ports[a].bytes_transmitted,
                "a_bytes_per_s": sources[a]["bytes_per_s"],
                "b_bytes_per_s": sources[b]["bytes_per_s"],
            })
        silence = now - self.last_cell_activity
        snap = {
            "platform": "USB-SOVEREIGN v1",
            "bridge_id": self.iot.bridge_id,
            "session_id": self.iot.session_id,
            "uptime_s": round(now - self.started_at, 1),
            "uplink": {
                "active_path": "ZILTB" if self.ziltb_active else "CELL",
                "ziltb_active": self.ziltb_active,
                "cell_silence_s": round(silence, 2),
                "failover_threshold_s": FAILOVER_THRESHOLD_S,
                "failover_engagements": self.failover_engagements,
            },
            "pairs": pairs,
            "sources": sources,
            "sinks": {
                "mqtt_topic": "device/serial/v2/data",
                "mqtt_published": self.iot.total_published,
                "mqtt_connected": self.iot.connected,
                "dynamodb_records": len(self.dynamodb),
                "s3_buffered_entries": len(self.s3.buffer),
                "s3_flushes": self.s3.flush_count,
                "s3_last_uri": self.s3.last_s3_uri,
            },
            "events": self.events.recent(50),
        }
        if self.store is not None:
            snapshot_store = self.store.stats()
            # The full registry map is bulky for a 2 Hz push; keep counts
            # here and serve the map via /api/serial-events.
            snapshot_store.pop("registry", None)
            snap["serial_events"] = snapshot_store
        if self.extra_status is not None:
            try:
                snap["plant"] = self.extra_status()
            except Exception:
                pass
        return snap
