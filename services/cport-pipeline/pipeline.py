#!/usr/bin/env python3
"""
C-Port CRC16 Serial Pipeline — merged pure-Python implementation.

Assembled from Taylor's five filed pipeline documents (2026-11-10):

  1. CPort_CRC16_Python_Pipeline.md  — >HHffH framing (sync 0xAA55), CRC16-CCITT
     (poly 0x1021, init 0xFFFF) over sync+payload, corrupt frames dropped,
     SAS XPORT export.
  2. CPort_CRC16_Periodic_Flush.md   — non-blocking time-based flush every
     N minutes to timestamped sensor_batch_YYYYMMDD_HHMMSS.xpt files, final
     flush on shutdown.
  3. CPort_CRC16_Async_Writer.md     — producer-consumer: a daemon writer
     thread performs all disk I/O off the serial loop via queue.Queue;
     None-sentinel shutdown drains pending writes.
  4. CPort_CRC16_Backpressure.md     — bounded queue (maxsize 10), 80%
     high-water warning, put_nowait + queue.Full -> batch dropped + counted.
  5. CPort_CRC16_Monitoring.md       — Prometheus metrics on :8000/metrics
     (pipeline_queue_depth gauge; pipeline_packets_processed_total,
     pipeline_packets_dropped_total, pipeline_batches_dropped_total
     counters) and MQTT JSON status frames every 10 s on pipeline/metrics.

This is the pure-Python line of the C-Port family; the CPython C-extension
sibling lives in CPort_Python_System_Specification.md and is NOT built here.

Optional dependencies degrade gracefully:
  pyserial          — only needed for real-port mode; --simulate works without.
  pyreadstat        — if missing, batch files fall back to CSV with the same
                      timestamped naming (sensor_batch_<ts>.csv).
  pandas            — if missing, records are handled as plain dicts and the
                      CSV fallback is written with the csv module.
  prometheus_client — if missing, metrics are tracked internally only.
  paho-mqtt         — if missing (or the broker is unreachable), MQTT status
                      publishing is disabled and the pipeline runs on.
"""

import argparse
import csv
import json
import os
import queue
import struct
import sys
import threading
import time
from datetime import datetime

# --------------------------------------------------------------------------
# Optional dependencies (guarded — see module docstring)
# --------------------------------------------------------------------------
try:
    import pandas as pd
except ImportError:  # pragma: no cover - exercised only without pandas
    pd = None

try:
    import pyreadstat
except ImportError:
    pyreadstat = None

try:
    import serial
except ImportError:
    serial = None

try:
    from prometheus_client import Counter, Gauge, start_http_server

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

try:
    import paho.mqtt.client as mqtt

    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False

# --------------------------------------------------------------------------
# Framing constants (CPort_CRC16_Python_Pipeline.md)
# --------------------------------------------------------------------------
PACKET_FORMAT = ">HHffH"  # sync H, sensor_id H, val1 f, val2 f, CRC16 H
SYNC_MARKER = b"\xAA\x55"
SYNC_WORD = 0xAA55
PACKET_SIZE = struct.calcsize(PACKET_FORMAT)  # 14 bytes


def calculate_crc16(data: bytes) -> int:
    """Standard CRC16-CCITT (poly 0x1021, init 0xFFFF) — verbatim from spec."""
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc = crc << 1
        crc &= 0xFFFF
    return crc


# --------------------------------------------------------------------------
# Configuration — spec defaults, environment overrides, then CLI overrides
# --------------------------------------------------------------------------
class PipelineConfig:
    """Runtime configuration. Defaults are the spec values from the docs."""

    def __init__(self):
        self.port = "COM3"                    # base + backpressure docs
        self.baud = 9600
        self.flush_interval_seconds = 5 * 60  # periodic-flush doc (5 min)
        self.max_queue_size = 10              # backpressure doc
        self.output_dir = "."
        self.metrics_port = 8000              # monitoring doc
        self.prometheus = True
        self.mqtt_broker = "localhost"        # monitoring doc
        self.mqtt_port = 1883
        self.mqtt_topic = "pipeline/metrics"
        self.mqtt_interval_seconds = 10       # monitoring doc (every 10 s)
        self.mqtt_enabled = True

    @property
    def warn_threshold(self) -> int:
        """High-water mark: 80% of queue capacity (backpressure doc)."""
        return int(self.max_queue_size * 0.8)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        cfg = cls()

        def _str(key, attr):
            if env.get(key):
                setattr(cfg, attr, env[key])

        def _int(key, attr):
            if env.get(key):
                setattr(cfg, attr, int(env[key]))

        def _float(key, attr):
            if env.get(key):
                setattr(cfg, attr, float(env[key]))

        def _bool(key, attr):
            if env.get(key) is not None:
                setattr(cfg, attr, env[key].strip().lower() not in ("0", "false", "no", "off"))

        _str("CPORT_PORT", "port")
        _int("CPORT_BAUD", "baud")
        _float("CPORT_FLUSH_INTERVAL", "flush_interval_seconds")
        _int("CPORT_MAX_QUEUE", "max_queue_size")
        _str("CPORT_OUTPUT_DIR", "output_dir")
        _int("CPORT_METRICS_PORT", "metrics_port")
        _bool("CPORT_PROMETHEUS", "prometheus")
        _str("CPORT_MQTT_BROKER", "mqtt_broker")
        _int("CPORT_MQTT_PORT", "mqtt_port")
        _str("CPORT_MQTT_TOPIC", "mqtt_topic")
        _float("CPORT_MQTT_INTERVAL", "mqtt_interval_seconds")
        _bool("CPORT_MQTT", "mqtt_enabled")
        return cfg


# --------------------------------------------------------------------------
# Metrics (monitoring doc) — internal counters always kept; Prometheus and
# MQTT are optional exporters layered on top.
# --------------------------------------------------------------------------
class Metrics:
    def __init__(self, enable_prometheus=False, port=8000):
        self._lock = threading.Lock()
        self._processed = 0
        self._dropped = 0
        self._batches_dropped = 0
        self._queue_depth = 0
        self._prom = {}  # name -> prometheus metric object
        if enable_prometheus:
            self.start_prometheus(port)

    def start_prometheus(self, port):
        if not PROMETHEUS_AVAILABLE:
            print("[Metrics] prometheus_client not installed — "
                  "internal counters only, no HTTP exporter.")
            return False
        try:
            self._prom = {
                "queue_depth": Gauge("pipeline_queue_depth",
                                     "Current number of batches in write queue"),
                "processed": Counter("pipeline_packets_processed_total",
                                     "Total valid packets ingested"),
                "dropped": Counter("pipeline_packets_dropped_total",
                                   "Total packets dropped due to CRC or framing errors"),
                "batches_dropped": Counter("pipeline_batches_dropped_total",
                                           "Total batch flushes dropped due to queue overflow"),
            }
            start_http_server(port)
            print(f"[Metrics] Prometheus metrics exposed at "
                  f"http://localhost:{port}/metrics")
            return True
        except ValueError:
            # Metrics already registered in this process (e.g. a second
            # pipeline in one test session) — keep internal counters only.
            print("[Metrics] Prometheus metrics already registered in this "
                  "process — internal counters only for this instance.")
            self._prom = {}
            return False

    def inc_processed(self):
        with self._lock:
            self._processed += 1
        if "processed" in self._prom:
            self._prom["processed"].inc()

    def inc_dropped(self):
        with self._lock:
            self._dropped += 1
        if "dropped" in self._prom:
            self._prom["dropped"].inc()

    def inc_batches_dropped(self):
        with self._lock:
            self._batches_dropped += 1
        if "batches_dropped" in self._prom:
            self._prom["batches_dropped"].inc()

    def set_queue_depth(self, depth):
        with self._lock:
            self._queue_depth = depth
        if "queue_depth" in self._prom:
            self._prom["queue_depth"].set(depth)

    def snapshot(self):
        with self._lock:
            return {
                "queue_depth": self._queue_depth,
                "packets_processed": self._processed,
                "packets_dropped": self._dropped,
                "batches_dropped": self._batches_dropped,
            }


class MQTTMetricsPublisher:
    """Verbatim-in-spirit from the monitoring doc (§3): JSON status frames
    published with QoS 1 to the metrics topic."""

    def __init__(self, broker="localhost", port=1883, topic="pipeline/metrics"):
        self.topic = topic
        self.client = None
        if not MQTT_AVAILABLE:
            print("[MQTT] paho-mqtt not installed — MQTT publishing disabled.")
            return
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            self.client.connect(broker, port, keepalive=60)
            self.client.loop_start()
            print(f"[MQTT] Metrics client connected to {broker}:{port}")
        except Exception as e:
            print(f"[MQTT] Connection failed ({e}) — MQTT publishing disabled.")
            self.client = None

    @property
    def active(self):
        return self.client is not None

    def publish_metrics(self, queue_depth, processed, dropped_packets, dropped_batches):
        if self.client is None:
            return
        payload = {
            "timestamp": time.time(),
            "queue_depth": queue_depth,
            "packets_processed": processed,
            "packets_dropped": dropped_packets,
            "batches_dropped": dropped_batches,
        }
        self.client.publish(self.topic, json.dumps(payload), qos=1)

    def stop(self):
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
            self.client = None


# --------------------------------------------------------------------------
# Frame parsing — sync hunt over a byte stream, then CRC validation
# --------------------------------------------------------------------------
class FrameParser:
    """Incremental parser: feed arbitrary byte chunks, get whole frames.

    Hunts for the 0xAA55 sync marker (discarding noise bytes before it),
    tolerates frames split across reads, and keeps a trailing lone 0xAA
    that may be half of a split sync marker.
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes):
        self._buf += data
        frames = []
        while True:
            idx = self._buf.find(SYNC_MARKER)
            if idx < 0:
                # No sync in buffer; keep only a possible trailing 0xAA.
                if len(self._buf) > 1:
                    del self._buf[:-1]
                break
            if idx > 0:
                del self._buf[:idx]  # drop noise before the sync marker
            if len(self._buf) < PACKET_SIZE:
                break  # partial frame — wait for more bytes
            frames.append(bytes(self._buf[:PACKET_SIZE]))
            del self._buf[:PACKET_SIZE]
        return frames


def parse_frame(frame: bytes):
    """Unpack + CRC-check one 14-byte frame.

    Returns a record dict {sensor_id, val1, val2} on success, None when the
    frame is malformed or its CRC16 does not match (corrupt — drop it).
    CRC is computed over sync+payload (everything except the last 2 bytes),
    exactly as the base document specifies.
    """
    if len(frame) != PACKET_SIZE or frame[:2] != SYNC_MARKER:
        return None
    _, sensor_id, val1, val2, received_crc = struct.unpack(PACKET_FORMAT, frame)
    if calculate_crc16(frame[:-2]) != received_crc:
        return None
    return {"sensor_id": int(sensor_id), "val1": float(val1), "val2": float(val2)}


# --------------------------------------------------------------------------
# Batch file writing — SAS XPORT via pyreadstat, CSV fallback (same naming)
# --------------------------------------------------------------------------
def batch_extension():
    """.xpt when pyreadstat is available, .csv otherwise (logged at startup)."""
    return ".xpt" if pyreadstat is not None else ".csv"


def write_batch(records, filepath, batch_id):
    """Write one batch of record dicts to filepath. Runs on the writer
    thread only — never on the serial/producer loop."""
    if pyreadstat is not None and pd is not None:
        df = pd.DataFrame(records)
        pyreadstat.write_xport(df, filepath, table_name="SENSORS",
                               file_label=f"Batch {batch_id}")
        return
    # Portability fallback: CSV with the identical column set.
    fieldnames = ["sensor_id", "timestamp", "val1", "val2"]
    with open(filepath, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            row = dict(rec)
            ts = row.get("timestamp")
            if not isinstance(ts, str):
                row["timestamp"] = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            writer.writerow({k: row.get(k) for k in fieldnames})


def writer_worker(write_queue, metrics=None, write_delay=0.0):
    """Background consumer thread (async-writer doc): performs all disk I/O
    off the serial loop. A None item is the shutdown sentinel — the worker
    drains everything queued before it, then exits."""
    while True:
        item = write_queue.get()
        if item is None:  # Sentinel object to signal thread termination
            write_queue.task_done()
            break
        records, filepath, batch_id = item
        try:
            if write_delay:
                time.sleep(write_delay)  # test hook: simulate a slow disk
            write_batch(records, filepath, batch_id)
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] [Worker] "
                  f"Wrote {len(records)} records to {filepath}")
        except Exception as e:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] [Worker Error] "
                  f"Failed to write {filepath}: {e}")
        finally:
            write_queue.task_done()
            if metrics is not None:
                metrics.set_queue_depth(write_queue.qsize())


# --------------------------------------------------------------------------
# Serial sources — a real pyserial port, or anything with read()/close()
# --------------------------------------------------------------------------
class SerialSource:
    """Thin wrapper over pyserial so the pipeline loop works against a
    uniform read(size) -> bytes interface."""

    def __init__(self, port, baud):
        if serial is None:
            raise RuntimeError(
                "pyserial is not installed — real-port mode is unavailable. "
                "Install it (pip install pyserial) or run with --simulate.")
        self._ser = serial.Serial(port, baud, timeout=0.2)

    def read(self, size):
        try:
            return self._ser.read(size)
        except Exception:
            return b""

    def close(self):
        try:
            self._ser.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# The merged pipeline
# --------------------------------------------------------------------------
class SerialPipeline:
    """Producer (serial/simulated ingestion loop) + consumer (writer thread),
    with periodic flush, bounded-queue backpressure, metrics, and a clean
    sentinel shutdown with a final flush — all five source documents in one
    coherent program."""

    def __init__(self, config, source=None, metrics=None, writer_delay=0.0,
                 mqtt_publisher=None):
        self.config = config
        self.source = source
        self.metrics = metrics if metrics is not None else Metrics()
        self.writer_delay = writer_delay
        self.mqtt = mqtt_publisher
        self.write_queue = queue.Queue(maxsize=config.max_queue_size)
        self.data_buffer = []
        self.batch_count = 1
        self.total_dropped_batches = 0
        self.parser = FrameParser()
        self._used_filenames = set()
        self.worker_thread = None
        self._stop = threading.Event()
        self._last_flush_time = None
        self._last_mqtt_time = None
        self._started = False

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self._started:
            return
        os.makedirs(self.config.output_dir, exist_ok=True)
        self.worker_thread = threading.Thread(
            target=writer_worker,
            args=(self.write_queue, self.metrics, self.writer_delay),
            daemon=True)
        self.worker_thread.start()
        now = time.time()
        self._last_flush_time = now
        self._last_mqtt_time = now
        self._started = True

    def shutdown(self):
        """Final flush, then the None sentinel; join waits until every
        pending write has drained (async-writer doc §2)."""
        if not self._started:
            return
        self.flush_buffer(final=True)
        self.write_queue.put(None)  # Signal worker thread to terminate
        if self.worker_thread is not None:
            self.worker_thread.join()  # Block until all pending writes finish
            self.worker_thread = None
        if self.mqtt is not None:
            self.mqtt.stop()
        if self.source is not None:
            try:
                self.source.close()
            except Exception:
                pass
        self._started = False
        print("Pipeline shut down cleanly.")

    def stop(self):
        self._stop.set()

    # -- ingestion ---------------------------------------------------------
    def feed_bytes(self, data: bytes) -> int:
        """Parse a chunk of the byte stream; buffer valid records, count
        CRC/framing drops. Returns the number of valid records added."""
        accepted = 0
        for frame in self.parser.feed(data):
            record = parse_frame(frame)
            if record is None:
                self.metrics.inc_dropped()
                print("CRC mismatch - packet dropped.")
            else:
                record = {
                    "sensor_id": record["sensor_id"],
                    "timestamp": pd.Timestamp.now() if pd is not None else datetime.now(),
                    "val1": record["val1"],
                    "val2": record["val2"],
                }
                self.data_buffer.append(record)
                self.metrics.inc_processed()
                accepted += 1
        return accepted

    # -- flushing (periodic-flush + async-writer + backpressure docs) -------
    def flush_buffer(self, final=False) -> bool:
        """Snapshot the buffer and hand it to the writer thread without
        blocking (put_nowait). On a full queue the batch is dropped and
        counted — the serial loop never freezes (backpressure doc §2).
        Returns True if the batch was enqueued."""
        if not self.data_buffer:
            if not final:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                      "Flush interval reached, but buffer is empty.")
            return False
        records = list(self.data_buffer)  # snapshot — buffer resets below
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = "_final" if final else ""
        filename = f"sensor_batch_{timestamp_str}{suffix}{batch_extension()}"
        filepath = os.path.join(self.config.output_dir, filename)
        if filepath in self._used_filenames or os.path.exists(filepath):
            # Same-second flush collision: the earlier batch may still be
            # queued (its file not yet written by the worker thread), so
            # track issued names ourselves — never let a later batch
            # overwrite an earlier one. Tag the colliding name with its
            # batch id; the plain timestamped name is kept whenever free.
            stem, ext = os.path.splitext(filepath)
            filepath = f"{stem}_b{self.batch_count}{ext}"
            n = 1
            while filepath in self._used_filenames or os.path.exists(filepath):
                n += 1
                filepath = f"{stem}_b{self.batch_count}_{n}{ext}"
        self._used_filenames.add(filepath)

        current_qsize = self.write_queue.qsize()
        # High-water mark warning check (backpressure doc)
        if current_qsize >= self.config.warn_threshold:
            print(f"\n[WARNING] Disk I/O lag detected! Queue capacity at "
                  f"{current_qsize}/{self.config.max_queue_size}.")

        try:
            self.write_queue.put_nowait((records, filepath, self.batch_count))
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Enqueued batch "
                  f"{self.batch_count} (Queue: {self.write_queue.qsize()}/"
                  f"{self.config.max_queue_size})")
            enqueued = True
        except queue.Full:
            self.total_dropped_batches += 1
            self.metrics.inc_batches_dropped()
            print(f"\n[ALERT] QUEUE OVERFLOW! Disk write thread is blocked. "
                  f"Dropped batch {self.batch_count} ({len(records)} rows "
                  f"lost). Total dropped: {self.total_dropped_batches}")
            enqueued = False

        self.data_buffer.clear()  # Clear memory buffer after dispatch
        self.batch_count += 1
        self.metrics.set_queue_depth(self.write_queue.qsize())
        return enqueued

    # -- main loop -----------------------------------------------------------
    def run(self, duration=None):
        """Run the ingestion loop until Ctrl+C, stop(), or `duration`
        seconds elapse. Always shuts down cleanly (final flush + drain)."""
        if self.source is None:
            raise RuntimeError("No byte source configured for this pipeline.")
        self.start()
        started_at = time.time()
        try:
            while not self._stop.is_set():
                current_time = time.time()
                if duration is not None and current_time - started_at >= duration:
                    break

                # 1. Non-blocking periodic flush check (periodic-flush doc)
                if current_time - self._last_flush_time >= self.config.flush_interval_seconds:
                    self.flush_buffer()
                    self._last_flush_time = current_time

                # 2. Queue-depth metric (monitoring doc)
                self.metrics.set_queue_depth(self.write_queue.qsize())

                # 3. Periodic MQTT status publish (monitoring doc)
                if (self.mqtt is not None and self.mqtt.active
                        and current_time - self._last_mqtt_time >= self.config.mqtt_interval_seconds):
                    snap = self.metrics.snapshot()
                    self.mqtt.publish_metrics(
                        queue_depth=snap["queue_depth"],
                        processed=snap["packets_processed"],
                        dropped_packets=snap["packets_dropped"],
                        dropped_batches=snap["batches_dropped"])
                    self._last_mqtt_time = current_time

                # 4. Read + parse whatever the source has
                chunk = self.source.read(4096)
                if chunk:
                    self.feed_bytes(chunk)
        except KeyboardInterrupt:
            print("\nStopping capture. Flushing remaining buffer...")
        finally:
            self.shutdown()
        return self.metrics.snapshot()


# --------------------------------------------------------------------------
# Command-line entry point
# --------------------------------------------------------------------------
def build_arg_parser():
    p = argparse.ArgumentParser(
        description="C-Port CRC16 serial pipeline (merged pure-Python build)")
    p.add_argument("--port", help="Serial port (default COM3; env CPORT_PORT)")
    p.add_argument("--baud", type=int, help="Baud rate (default 9600)")
    p.add_argument("--simulate", action="store_true",
                   help="Run with the virtual sensor simulator — no hardware")
    p.add_argument("--duration", type=float, default=None,
                   help="Stop after N seconds (default: run until Ctrl+C)")
    p.add_argument("--flush-interval", type=float, default=None,
                   help="Seconds between batch flushes (default 300)")
    p.add_argument("--max-queue", type=int, default=None,
                   help="Max pending batch writes (default 10)")
    p.add_argument("--output-dir", help="Where batch files are written")
    p.add_argument("--metrics-port", type=int, default=None,
                   help="Prometheus HTTP port (default 8000)")
    p.add_argument("--no-prometheus", action="store_true",
                   help="Disable the Prometheus HTTP exporter")
    p.add_argument("--no-mqtt", action="store_true",
                   help="Disable MQTT status publishing")
    p.add_argument("--mqtt-broker", help="MQTT broker host (default localhost)")
    p.add_argument("--mqtt-port", type=int, help="MQTT broker port (default 1883)")
    p.add_argument("--mqtt-topic", help="MQTT topic (default pipeline/metrics)")
    # Simulator passthrough (used with --simulate)
    p.add_argument("--sim-rate", type=float, default=10.0,
                   help="Simulated frames per second (default 10)")
    p.add_argument("--sim-sensors", default="1,2,3",
                   help="Comma-separated simulated sensor IDs (default 1,2,3)")
    p.add_argument("--sim-corruption", type=float, default=0.1,
                   help="Fraction of simulated frames corrupted (default 0.1)")
    p.add_argument("--sim-burst-every", type=int, default=None,
                   help="Emit a burst every N simulated frames")
    p.add_argument("--sim-burst-size", type=int, default=50,
                   help="Frames per burst (default 50)")
    p.add_argument("--sim-seed", type=int, default=None,
                   help="Seed the simulator RNG for repeatable runs")
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    config = PipelineConfig.from_env()
    # CLI overrides env overrides spec defaults.
    if args.port is not None:
        config.port = args.port
    if args.baud is not None:
        config.baud = args.baud
    if args.flush_interval is not None:
        config.flush_interval_seconds = args.flush_interval
    if args.max_queue is not None:
        config.max_queue_size = args.max_queue
    if args.output_dir is not None:
        config.output_dir = args.output_dir
    if args.metrics_port is not None:
        config.metrics_port = args.metrics_port
    if args.no_prometheus:
        config.prometheus = False
    if args.no_mqtt:
        config.mqtt_enabled = False
    if args.mqtt_broker is not None:
        config.mqtt_broker = args.mqtt_broker
    if args.mqtt_port is not None:
        config.mqtt_port = args.mqtt_port
    if args.mqtt_topic is not None:
        config.mqtt_topic = args.mqtt_topic

    if pyreadstat is None:
        print("[Startup] pyreadstat not installed — batch files will be "
              "written as CSV (same timestamped naming, .csv extension).")
    if pd is None:
        print("[Startup] pandas not installed — records handled as plain "
              "dicts; CSV fallback in effect.")

    metrics = Metrics(enable_prometheus=config.prometheus,
                      port=config.metrics_port)
    mqtt_publisher = None
    if config.mqtt_enabled:
        mqtt_publisher = MQTTMetricsPublisher(
            broker=config.mqtt_broker, port=config.mqtt_port,
            topic=config.mqtt_topic)

    if args.simulate:
        from simulator import SensorSimulator, SimulatedSource
        sensor_ids = tuple(int(s) for s in args.sim_sensors.split(",") if s.strip())
        simulator = SensorSimulator(
            sensor_ids=sensor_ids, rate_hz=args.sim_rate,
            corruption_rate=args.sim_corruption,
            burst_every=args.sim_burst_every, burst_size=args.sim_burst_size,
            seed=args.sim_seed)
        source = SimulatedSource(simulator)
        print(f"[Startup] SIMULATION mode — sensors {sensor_ids} at "
              f"{args.sim_rate} fps, corruption rate {args.sim_corruption}. "
              "No serial hardware involved.")
    else:
        if serial is None:
            print("ERROR: pyserial is not installed, so a real serial port "
                  "cannot be opened.\nInstall it with: pip install pyserial"
                  "\nOr run without hardware: python3 pipeline.py --simulate")
            return 2
        try:
            source = SerialSource(config.port, config.baud)
        except Exception as e:
            print(f"ERROR: could not open serial port {config.port} "
                  f"at {config.baud} baud: {e}")
            return 2
        print(f"[Startup] Listening on {config.port} at {config.baud} baud. "
              f"Flushing every {config.flush_interval_seconds:.0f}s; "
              f"max write queue {config.max_queue_size} batches.")

    pipeline = SerialPipeline(config, source=source, metrics=metrics,
                              mqtt_publisher=mqtt_publisher)
    snapshot = pipeline.run(duration=args.duration)
    print(f"[Done] packets_processed={snapshot['packets_processed']} "
          f"packets_dropped={snapshot['packets_dropped']} "
          f"batches_dropped={snapshot['batches_dropped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
