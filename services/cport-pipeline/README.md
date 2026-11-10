# C-Port CRC16 Serial Pipeline — Merged Pure-Python Package

One runnable program assembled from Taylor's seven filed C-Port pipeline
documents (2026-11-10). It ingests CRC16-framed binary packets from a serial
port (or a built-in simulator), drops corrupt frames, buffers valid records,
and flushes them periodically to timestamped SAS XPORT batch files via a
background writer thread with bounded-queue backpressure — with Prometheus
and MQTT metrics on top.

## What was merged, from which document

| Source document | What it contributed here |
|---|---|
| `CPort_CRC16_Python_Pipeline.md` | Wire format `>HHffH` (sync `0xAA55`, sensor_id, val1, val2, CRC16 — 14 bytes), CRC16-CCITT (poly `0x1021`, init `0xFFFF`) over sync+payload, corrupt-frame drops, XPORT export |
| `CPort_CRC16_Periodic_Flush.md` | Non-blocking `time.time()` flush check, `sensor_batch_YYYYMMDD_HHMMSS.xpt` naming, final flush on shutdown |
| `CPort_CRC16_Async_Writer.md` | Producer–consumer design: `queue.Queue` + daemon writer thread does all disk I/O; `None`-sentinel shutdown that drains pending writes |
| `CPort_CRC16_Backpressure.md` | Queue `maxsize=10`, 80% high-water warning, `put_nowait` + `queue.Full` → batch dropped and counted, never blocking the reader |
| `CPort_CRC16_Monitoring.md` | Prometheus exporter on `:8000/metrics` (gauge `pipeline_queue_depth`; counters `pipeline_packets_processed_total`, `pipeline_packets_dropped_total`, `pipeline_batches_dropped_total`) and MQTT JSON status frames every 10 s on `pipeline/metrics` |
| `CPort_CRC16_Grafana_Dashboard.md` | Companion only — the importable Grafana dashboard JSON also lives at `~/workspace/your_files/Serial_Pipeline_Grafana_Dashboard.json`; its panels query exactly the metric names above |
| `CPort_CRC16_Grafana_Alerting.md` | Companion only — Grafana contact points, the "High Write Queue Depth (>80%)" alert rule (threshold 8 of 10 — matches this package's defaults), and notification-policy routing |

The CPython C-extension sibling (`CPort_Python_System_Specification.md`,
`cserialproto`) uses the identical framing and CRC; this package is the
pure-Python line and does not build the C extension.

The merge is faithful to the documents, with one deliberate hardening the
documents' snippets imply but don't state: because a queued batch's file may
not exist yet when the next flush is named, the pipeline tracks the filenames
it has issued and tags a same-second collision with its batch id
(`sensor_batch_<ts>_b3.xpt`) instead of letting two batches share one
timestamped name and silently overwrite each other.

## Files

| File | Purpose |
|---|---|
| `pipeline.py` | The merged program (parser, pipeline, metrics, CLI) |
| `simulator.py` | Virtual sensor source: in-process feeder + standalone PTY/serial writer |
| `test_pipeline.py` | Automated tests (pytest, or plain `python3 test_pipeline.py`) |
| `requirements.txt` | Dependencies, with runtime-optionality notes |
| `.venv/` | Local virtualenv used to build/test this package (see Setup) |

## Setup

```bash
cd ~/workspace/builds/cport-pipeline
python3 -m venv --system-site-packages .venv
.venv/bin/pip install pyserial pyreadstat prometheus-client paho-mqtt pytest
```

(Any Python 3.10+ environment with the packages from `requirements.txt`
works; the venv is just how it was built and verified here.)

## Running

Real serial port (defaults are the spec values: COM3 @ 9600, flush every
5 minutes, queue max 10):

```bash
.venv/bin/python pipeline.py --port COM3
.venv/bin/python pipeline.py --port /dev/ttyUSB0 --flush-interval 60
```

Simulation — full pipeline, no hardware:

```bash
.venv/bin/python pipeline.py --simulate --duration 12 --flush-interval 1 \
    --sim-rate 30 --sim-corruption 0.2 --output-dir ./out
```

Simulator knobs: `--sim-sensors 1,2,3`, `--sim-rate` (frames/s),
`--sim-corruption` (0..1 fraction of frames with a flipped payload byte),
`--sim-burst-every N --sim-burst-size M` (burst pressure on the write
queue), `--sim-seed` (repeatable runs).

Standalone simulator (feeds a separately-running pipeline process):

```bash
.venv/bin/python simulator.py --pty /tmp/vsensor --rate 20 --corruption 0.1
# prints the PTY device; then in another shell:
.venv/bin/python pipeline.py --port /tmp/vsensor
```

Stop with Ctrl+C: the pipeline performs a final flush, sends the writer
thread its sentinel, and waits for all pending writes to drain before
exiting.

## Configuration

Spec defaults, overridable by environment variable, overridable again by
CLI flag:

| Setting | Default | Env var | CLI flag |
|---|---|---|---|
| Port / baud | `COM3` / `9600` | `CPORT_PORT` / `CPORT_BAUD` | `--port` / `--baud` |
| Flush interval | 300 s | `CPORT_FLUSH_INTERVAL` | `--flush-interval` (seconds) |
| Max queue size | 10 batches | `CPORT_MAX_QUEUE` | `--max-queue` |
| Output directory | `.` | `CPORT_OUTPUT_DIR` | `--output-dir` |
| Prometheus | on, port 8000 | `CPORT_PROMETHEUS`, `CPORT_METRICS_PORT` | `--no-prometheus`, `--metrics-port` |
| MQTT | on | `CPORT_MQTT`, `CPORT_MQTT_BROKER`, `CPORT_MQTT_PORT`, `CPORT_MQTT_TOPIC`, `CPORT_MQTT_INTERVAL` | `--no-mqtt`, `--mqtt-broker`, `--mqtt-port`, `--mqtt-topic` |

The high-water warning threshold is derived: `int(max_queue_size * 0.8)`,
per the backpressure document.

## Metrics

- **Prometheus:** `GET http://localhost:8000/metrics` — `pipeline_queue_depth`
  (gauge), `pipeline_packets_processed_total`, `pipeline_packets_dropped_total`,
  `pipeline_batches_dropped_total` (counters). Visualize with the Grafana
  dashboard companion on file; alert with the alerting companion on file.
- **MQTT:** JSON status frames every 10 s to topic `pipeline/metrics`
  (QoS 1): `{timestamp, queue_depth, packets_processed, packets_dropped,
  batches_dropped}`. If no broker is reachable, publishing disables itself
  with a log line and the pipeline runs on.

## Optional dependencies — graceful degradation

Every third-party import is guarded:

| Missing package | Behavior |
|---|---|
| `pyreadstat` | Batch files fall back to **CSV** with the same timestamped naming (`sensor_batch_<ts>.csv`). Logged at startup. This is a portability fallback, not a silent change. |
| `pandas` | Records handled as plain dicts; CSV fallback written via the `csv` module. |
| `pyserial` | Real-port mode reports the problem cleanly (exit code 2) and points at `--simulate`, which works without it. |
| `prometheus_client` | Metrics tracked internally only; no HTTP exporter. |
| `paho-mqtt` (or no broker) | MQTT publishing disabled; pipeline unaffected. |

## Tests

```bash
.venv/bin/python -m pytest test_pipeline.py -v
python3 test_pipeline.py        # plain runner — same assertions, no pytest needed
```

Coverage: (1) packet size 14 + CRC known-answer vectors (`"123456789"` →
`0x29B1`, the CRC-16/CCITT-FALSE check value; a full sample frame → `0x421C`);
(2) valid frame parses, corrupted frame drops and counts, noise/split-frame
handling; (3) a flush writes a batch file whose rows match the records fed;
(4) backpressure with a slowed writer and `maxsize=1` — overflow drops are
counted and the producer's flush returns in < 0.5 s; (5) end-to-end simulate
run reconciling `packets_processed` against the total rows across all batch
files, with `packets_dropped > 0`.

Verified 2026-11-10 in this sandbox: **5/5 pass** under pytest (full
dependency set) and **5/5 pass** under the plain runner on the system Python
(degraded mode — CSV fallback, no pytest/pyserial/pyreadstat/prometheus/paho).
A 12 s CLI simulation at 30 fps with 20% corruption produced 285 processed /
75 dropped (= 360 frames emitted, exactly reconciled) and 285 rows across 12
`.xpt` files, with the live Prometheus endpoint serving the counters mid-run.

## Caveats

- **No real serial hardware was tested.** The wire path was exercised via
  the in-process simulator and a PTY pair only. Baud-rate timing behavior,
  electrical noise characteristics, and real device quirks are untested.
- **PTY note:** the standalone simulator puts its PTY slave into raw mode
  (`tty.setraw`); a default cooked-mode PTY buffers/mangles binary frames.
  This was found and fixed during testing (a raw reader went from catching
  4 of ~36 frames to catching the full stream).
- **MQTT** was verified only for graceful failure — no broker exists in this
  sandbox, so no live publish was observed.
- Batch filenames are timestamped to the second (per the source documents);
  same-second collisions are disambiguated with a `_b<batch>` tag as
  described above, and the final flush carries a `_final` suffix.
- The documents' per-packet console logging is retained in spirit but kept
  brief (drop notices and batch events), so high-rate runs stay readable.
