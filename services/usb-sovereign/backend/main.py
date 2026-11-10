#!/usr/bin/env python3
"""USB-SOVEREIGN v1 — local full-stack entry point (Increment 2).

Wires the serial plant, the UnifiedSerialController (adapted from the spec
in 'Cport to Coort.docx'), the telemetry sinks, the local serial_events
store, and the dashboard HTTP/WebSocket server together, then runs until
Ctrl+C / SIGTERM triggers the spec's graceful shutdown (R21-R25).

Increment 2 wiring, driven by ``config.py`` (env > JSON file > defaults):

* Ports: ``USB_SOV_PORTS=sim`` (default, increment-1 simulated plant) or
  ``USB_SOV_PORTS=pty`` — the real hardware plane: socat PTY pairs, the
  controller on the ``.ctl`` ends, the device plant on the ``.dev`` ends.
* Live AWS sinks stay DISABLED unless the master gate
  ``USB_SOV_AWS_ENABLED=1`` AND the per-sink flags
  (``USB_SOV_AWS_IOT=1`` / ``USB_SOV_AWS_S3=1``) are set AND their
  config/credentials validate (IoT: endpoint + cert files + a probe
  connect; S3: region + instance credentials); on any failure the
  wiring logs it and falls back to the local stubs. No AWS resource is
  touched by default. See ``aws-activation/README.md`` for the staged
  activation runbook.
* The local serial_events store (ClickHouse stand-in) records every
  published envelope to ``data/serial_events.jsonl``.

Run from the usb-sovereign/ directory:

    python3 backend/main.py              # simulated plant
    USB_SOV_PORTS=pty python3 backend/main.py   # socat PTY hardware plane

Dashboard + API + WS: http://127.0.0.1:8765/

Hosting knobs (env): ``USB_SOV_HOST`` / ``USB_SOV_PORT`` pick the bind
address and port; ``BASE_PATH`` (e.g. ``/usb``) mounts the whole app —
page, API and WebSocket — under a path prefix for gateway hosting that
forwards the prefix (see DEPLOY.md). Unset, it serves at the root.
"""

import asyncio
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(line_buffering=True)  # live logs when redirected
except Exception:
    pass

from aws_sinks import AwsIoTClient, AwsSinkNotReady, AwsS3BatchLogger  # noqa: E402
from config import load_config                                        # noqa: E402
from controller import EventLog, UnifiedSerialController              # noqa: E402
from event_store import SerialEventStore, SignatureRegistry           # noqa: E402
from pty_ports import (SocatPtyPlant, pty_bio_talker,                 # noqa: E402
                       pty_cellular_talker, pty_drainer, pty_talker,
                       pty_ziltb_talker)
from server import StatusServer                                       # noqa: E402
from simulated_ports import (CellularPattern, SimulatedSerialPort,    # noqa: E402
                             bio_talker, cellular_talker, talker,
                             ziltb_talker)
from sinks import DynamoDbStubSink, MqttIoTStubSink, S3BatchLoggerStub  # noqa: E402

HOST = os.environ.get("USB_SOV_HOST", "127.0.0.1")
PORT = int(os.environ.get("USB_SOV_PORT", "8765"))
BASE_PATH = os.environ.get("BASE_PATH", "")
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "frontend")

# Baud rates from the spec's controller. COM3/COM4 are the bio-telemetry
# mirror targets (LH surge -> COM3, FSH pulse -> COM4): they carry no
# device talker of their own, only frames the controller mirrors to them.
BAUD = {
    "COM1": 9600, "COM2": 9600, "Sport": 115200, "Coort": 115200,
    "LH": 9600, "FSH": 9600, "CELL": 115200, "BT": 115200,
    "WIFI": 115200, "LAN": 115200, "ZILTB": 115200,
    "COM3": 9600, "COM4": 9600,
}

TALKERS = ("COM1", "COM2", "Sport", "Coort", "LH", "FSH", "BT", "WIFI", "LAN")


def build_sinks(cfg, events, dynamodb):
    """Telemetry sinks behind the AWS activation gate.

    A live class is used only when ALL of these hold: the master gate
    (``USB_SOV_AWS_ENABLED=1``), its per-sink flag (``USB_SOV_AWS_IOT=1``
    / ``USB_SOV_AWS_S3=1``), and a successful readiness probe (config +
    cert files for IoT, incl. a live connect; region + credentials for
    S3). Anything else falls back to the local stubs with a logged
    reason — the bridge never crashes and never silently talks to AWS.
    """
    aws_cfg = cfg["aws"]
    master = bool(aws_cfg.get("enabled"))

    iot = MqttIoTStubSink(on_publish=dynamodb.put)
    if master and aws_cfg.get("iot_enabled"):
        candidate = AwsIoTClient(aws_cfg.get("iot"), enabled=True,
                                 on_publish=dynamodb.put)
        try:
            candidate._check_ready()
            candidate.connect()  # probe: TLS + CONNACK, or no arming
            iot = candidate
            events.add("AWS", "AwsIoTClient armed — live IoT publishing "
                              "to AWS IoT Core (master gate + flag + "
                              "probe connect all passed).")
        except Exception as exc:
            candidate.disconnect()
            events.add("AWS", f"AwsIoTClient NOT armed ({exc}) — "
                              "falling back to the local MQTT stub.")
    elif aws_cfg.get("iot_enabled"):
        events.add("AWS", "USB_SOV_AWS_IOT=1 set but the master gate "
                          "USB_SOV_AWS_ENABLED is off — live IoT stays "
                          "disabled (local MQTT stub in use).")

    s3 = S3BatchLoggerStub()
    if master and aws_cfg.get("s3_enabled"):
        candidate = AwsS3BatchLogger(bucket_name=aws_cfg.get("s3_bucket"),
                                     enabled=True,
                                     region=aws_cfg.get("region"))
        try:
            candidate._check_ready()
            s3 = candidate
            events.add("AWS", "AwsS3BatchLogger armed — live S3 archival "
                              f"to s3://{aws_cfg.get('s3_bucket')}/ "
                              "(master gate + flag + credentials passed).")
        except AwsSinkNotReady as exc:
            events.add("AWS", f"AwsS3BatchLogger NOT armed ({exc}) — "
                              "falling back to the local S3 stub.")
    elif aws_cfg.get("s3_enabled"):
        events.add("AWS", "USB_SOV_AWS_S3=1 set but the master gate "
                          "USB_SOV_AWS_ENABLED is off — live S3 stays "
                          "disabled (local S3 stub in use).")
    return iot, s3


async def amain():
    cfg = load_config()
    events = EventLog()

    dynamodb = DynamoDbStubSink()
    iot, s3 = build_sinks(cfg, events, dynamodb)

    # Local serial_events store + signature registry (ClickHouse stand-in).
    store = None
    if cfg["store"]["enabled"]:
        store_dir = cfg["store"]["dir"]
        registry = SignatureRegistry(
            os.path.join(store_dir, "signature_registry.json"))
        store = SerialEventStore(
            os.path.join(store_dir, "serial_events.jsonl"), registry)
        events.add("INFO", f"serial_events store open: {store.path} "
                           f"(registry: {len(registry)} signatures)")

    # Serial plant: simulated (default) or socat PTY hardware plane.
    mode = cfg["ports"]["mode"]
    plant = None
    if mode == "pty":
        plant = SocatPtyPlant(cfg["ports"]["pty_dir"], BAUD)
        ports = plant.start()
        events.add("INFO", f"PTY hardware plane up: {len(ports)} socat "
                           f"pairs in {plant.pty_dir} (controller on .ctl, "
                           "devices on .dev)")
    else:
        ports = {name: SimulatedSerialPort(name, baudrate=baud)
                 for name, baud in BAUD.items()}

    controller = UnifiedSerialController(ports, iot, s3, dynamodb, events)
    controller.store = store
    if plant is not None:
        controller.plant_label = "socat PTY hardware plane"
        controller.extra_status = plant.stats
    server = StatusServer(controller, FRONTEND_DIR, HOST, PORT,
                          base_path=BASE_PATH)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    # Device plant traffic (same shapes on both planes).
    tasks = []
    pattern = CellularPattern(up_s=14.0, down_s=12.0)
    if plant is not None:
        eps = plant.endpoints
        for name in TALKERS:
            interval = (0.5, 1.6) if name in ("LH", "FSH") else (0.3, 1.2)
            talk = pty_bio_talker if name in ("LH", "FSH") else pty_talker
            tasks.append(asyncio.create_task(
                talk(eps[name], name, interval=interval, stop=stop)))
        tasks.append(asyncio.create_task(
            pty_cellular_talker(eps["CELL"], pattern, stop)))
        tasks.append(asyncio.create_task(
            pty_ziltb_talker(eps["ZILTB"], pattern, stop)))
        tasks.append(asyncio.create_task(pty_drainer(eps, stop)))
    else:
        for name in TALKERS:
            interval = (0.5, 1.6) if name in ("LH", "FSH") else (0.3, 1.2)
            talk = bio_talker if name in ("LH", "FSH") else talker
            tasks.append(asyncio.create_task(
                talk(ports[name], name, interval=interval, stop=stop)))
        tasks.append(asyncio.create_task(
            cellular_talker(ports["CELL"], pattern, stop)))
        tasks.append(asyncio.create_task(
            ziltb_talker(ports["ZILTB"], pattern, stop)))

    await server.start()
    controller_task = asyncio.create_task(controller.run())
    events.add("INFO", f"Plant '{mode}' running: cellular pattern "
                       "14 s up / 12 s down (failover gate fires each cycle).")

    await stop.wait()
    events.add("INFO", "Stop signal received — graceful shutdown (R21-R25).")
    controller.stop()
    try:
        await asyncio.wait_for(controller_task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        controller_task.cancel()
    for t in tasks:
        t.cancel()
    await server.close()
    if plant is not None:
        plant.close()
    if store is not None:
        store.close()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
