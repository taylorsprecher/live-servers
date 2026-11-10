#!/usr/bin/env python3
"""Stage-1 AWS-activation test suite for USB-SOVEREIGN.

Run:  python3 backend/tests_stage1.py     (from the usb-sovereign/ dir)

Covers, in order:
  * sigv4.py — AWS published known-answer vector; byte-identical second
    copy in aws-activation/; env + IMDS credential parsing (stubbed);
    credential cache.
  * mqtt_stdlib.py — CONNECT/PUBLISH wire encoding, remaining-length
    encoding, a full publish round-trip against a fake in-process
    broker (socketpair), and the PUBACK-timeout failure path.
  * aws_sinks.py + config.py + main.build_sinks — the activation gate:
    master flag required, per-sink flags required, fail-closed fallback
    to the stubs, S3 flush signing + failure retention, IoT publish via
    a fake client factory incl. the dropped-frame path.
  * existing bridge behavior (smoke subset of the increments 1/2 +
    LH/FSH verification suites, which were run ad hoc and never saved
    as files): envelope integrity, signature classification incl. the
    LH/FSH prefix rules and the BT non-collision, sim-mode ingest +
    COM3 mirroring with byte purity, the 5 s cellular failover gate,
    a socat PTY plant pass, and the /api/status sink fields.

No real AWS call is made anywhere in this file.
"""

import asyncio
import hashlib
import importlib.util
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sigv4  # noqa: E402
from sigv4 import Credentials, sign_request  # noqa: E402

RESULTS = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        import traceback
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        traceback.print_exc()
    else:
        RESULTS.append((name, True, ""))


class patched_env:
    def __init__(self, **kw):
        self.kw = kw
        self.saved = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ----------------------------------------------------------------------
# sigv4
# ----------------------------------------------------------------------

def t_sigv4_known_answer():
    creds = Credentials("AKIDEXAMPLE",
                        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    h = sign_request(
        "GET", "iam.amazonaws.com", "us-east-1", "iam", creds, path="/",
        query="Action=ListUsers&Version=2010-05-08",
        headers={"content-type":
                 "application/x-www-form-urlencoded; charset=utf-8"},
        when=datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc))
    assert "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7" \
        in h["authorization"], h["authorization"]


def t_sigv4_copies_identical():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "aws-activation", "sigv4.py")
    spec = importlib.util.spec_from_file_location("sigv4_activation", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    creds = Credentials("AKIDEXAMPLE",
                        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    kw = dict(path="/", query="Action=ListUsers&Version=2010-05-08",
              headers={"content-type":
                       "application/x-www-form-urlencoded; charset=utf-8"},
              when=datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc))
    a = sign_request("GET", "iam.amazonaws.com", "us-east-1", "iam",
                     creds, **kw)
    b = mod.sign_request("GET", "iam.amazonaws.com", "us-east-1", "iam",
                         mod.Credentials(
                             "AKIDEXAMPLE",
                             "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"),
                         **kw)
    assert a["authorization"] == b["authorization"]


def t_sigv4_env_credentials():
    with patched_env(AWS_ACCESS_KEY_ID="AK", AWS_SECRET_ACCESS_KEY="SK",
                     AWS_SESSION_TOKEN="TOK"):
        c = sigv4.env_credentials()
        assert (c.access_key, c.secret_key, c.session_token) == \
            ("AK", "SK", "TOK")
    with patched_env(AWS_ACCESS_KEY_ID=None, AWS_SECRET_ACCESS_KEY=None):
        assert sigv4.env_credentials() is None


def t_sigv4_imds_parsing():
    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        method = req.method if hasattr(req, "method") else "GET"
        calls.append((method, url))
        if url.endswith("/latest/api/token"):
            assert method == "PUT"
            assert req.headers.get(
                "X-aws-ec2-metadata-token-ttl-seconds") == "21600"
            return FakeResp(b"token-123")
        if url.endswith("security-credentials/"):
            assert req.headers.get("X-aws-ec2-metadata-token") == "token-123"
            return FakeResp(b"usb-sovereign-host")
        if url.endswith("security-credentials/usb-sovereign-host"):
            return FakeResp(json.dumps({
                "AccessKeyId": "ASIAFAKE",
                "SecretAccessKey": "fake-secret",
                "Token": "fake-token",
                "Expiration": "2030-01-01T00:00:00Z",
            }).encode())
        raise AssertionError(f"unexpected IMDS URL {url}")

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        creds = sigv4.imds_credentials()
    finally:
        urllib.request.urlopen = orig
    assert creds.access_key == "ASIAFAKE"
    assert creds.secret_key == "fake-secret"
    assert creds.session_token == "fake-token"
    assert len(calls) == 3


def t_sigv4_credential_cache():
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return Credentials("AK", "SK")

    cache = sigv4.CredentialCache(provider=provider)
    cache.get()
    cache.get()
    assert calls["n"] == 1


# ----------------------------------------------------------------------
# mqtt_stdlib
# ----------------------------------------------------------------------

from mqtt_stdlib import (MqttError, MqttTlsClient, _PacketReader,  # noqa: E402
                         build_connect_packet, build_publish_packet,
                         _encode_remaining_length)


def t_mqtt_connect_packet():
    pkt = build_connect_packet("911TransEdu_SovereignBridge",
                               keepalive_s=60)
    assert pkt[0] == 0x10
    reader_body = pkt[1:]
    # remaining length is 1 byte for this size
    ln = reader_body[0]
    body = reader_body[1:1 + ln]
    assert len(body) == ln
    assert body[0:2] == b"\x00\x04" and body[2:6] == b"MQTT"
    assert body[6] == 4           # protocol level 3.1.1
    assert body[7] == 0x02        # clean session flag
    assert struct.unpack("!H", body[8:10])[0] == 60
    cid_len = struct.unpack("!H", body[10:12])[0]
    assert body[12:12 + cid_len] == b"911TransEdu_SovereignBridge"


def t_mqtt_publish_packet():
    pkt = build_publish_packet("device/serial/v2/data", b"{}", 7)
    assert pkt[0] == 0x32  # PUBLISH, QoS 1
    ln = pkt[1]
    body = pkt[2:2 + ln]
    tlen = struct.unpack("!H", body[0:2])[0]
    assert body[2:2 + tlen] == b"device/serial/v2/data"
    assert struct.unpack("!H", body[2 + tlen:4 + tlen])[0] == 7
    assert body[4 + tlen:] == b"{}"


def t_mqtt_remaining_length():
    assert _encode_remaining_length(0) == b"\x00"
    assert _encode_remaining_length(127) == b"\x7f"
    assert _encode_remaining_length(128) == b"\x80\x01"
    assert _encode_remaining_length(16383) == b"\xff\x7f"
    assert _encode_remaining_length(16384) == b"\x80\x80\x01"


class _FakeBroker(threading.Thread):
    """Reads CONNECT, sends CONNACK, records one PUBLISH, PUBACKs it,
    then expects DISCONNECT. Optionally withholds the PUBACK."""

    def __init__(self, sock, withhold_puback=False):
        super().__init__(daemon=True)
        self.sock = sock
        self.withhold = withhold_puback
        self.connect_client_id = None
        self.published = None
        self.disconnected = False
        self.error = None

    def run(self):
        try:
            reader = _PacketReader(self.sock.recv)
            ptype, _f, body = reader.read_packet()
            assert ptype == 1, ptype
            cid_len = struct.unpack("!H", body[10:12])[0]
            self.connect_client_id = body[12:12 + cid_len].decode()
            self.sock.sendall(b"\x20\x02\x00\x00")  # CONNACK accepted
            ptype, _f, body = reader.read_packet()
            assert ptype == 3, ptype
            tlen = struct.unpack("!H", body[0:2])[0]
            topic = body[2:2 + tlen].decode()
            pid = struct.unpack("!H", body[2 + tlen:4 + tlen])[0]
            payload = body[4 + tlen:]
            self.published = (topic, payload, pid)
            if not self.withhold:
                self.sock.sendall(b"\x40\x02" + struct.pack("!H", pid))
                ptype, _f, _b = reader.read_packet()
                self.disconnected = (ptype == 14)
        except Exception as exc:  # noqa: BLE001
            self.error = exc


def t_mqtt_roundtrip_fake_broker():
    a, b = socket.socketpair()
    broker = _FakeBroker(b)
    broker.start()
    client = MqttTlsClient("fake-endpoint", "911TransEdu_SovereignBridge",
                           "unused.pem", "unused.key", "unused.ca",
                           socket_factory=lambda ep, port: a)
    client.connect()
    assert client.connected
    client.publish("device/serial/v2/data", b'{"x": 1}', qos=1)
    client.disconnect()
    broker.join(timeout=5)
    assert broker.error is None, broker.error
    assert broker.connect_client_id == "911TransEdu_SovereignBridge"
    assert broker.published[0] == "device/serial/v2/data"
    assert broker.published[1] == b'{"x": 1}'
    assert broker.disconnected


def t_mqtt_puback_timeout():
    a, b = socket.socketpair()
    broker = _FakeBroker(b, withhold_puback=True)
    broker.start()
    client = MqttTlsClient("fake-endpoint", "cid", "c", "k", "ca",
                           socket_factory=lambda ep, port: a)
    client.connect()
    start = time.time()
    try:
        client.publish("device/serial/v2/data", b"{}", qos=1, timeout=0.6)
    except MqttError:
        pass
    else:
        raise AssertionError("publish should have raised on PUBACK timeout")
    assert time.time() - start < 3.0
    client.disconnect()
    b.close()


# ----------------------------------------------------------------------
# aws_sinks + config + main.build_sinks (the activation gate)
# ----------------------------------------------------------------------

import config as config_mod  # noqa: E402
import main as main_mod  # noqa: E402
from aws_sinks import AwsIoTClient, AwsS3BatchLogger, AwsSinkNotReady  # noqa: E402
from controller import EventLog, UnifiedSerialController  # noqa: E402
from sinks import (DynamoDbStubSink, MqttIoTStubSink,  # noqa: E402
                   S3BatchLoggerStub, build_envelope)

AWS_ENV_OFF = dict(USB_SOV_AWS_ENABLED=None, USB_SOV_AWS_IOT=None,
                   USB_SOV_AWS_S3=None, USB_SOV_IOT_ENDPOINT=None,
                   USB_SOV_IOT_CERT=None, USB_SOV_IOT_KEY=None,
                   USB_SOV_IOT_CA=None, USB_SOV_S3_BUCKET=None,
                   USB_SOV_AWS_REGION=None, USB_SOV_CONFIG="/nonexistent.json")


def _sinks_for(env):
    with patched_env(**env):
        cfg = config_mod.load_config()
    events = EventLog()
    dyn = DynamoDbStubSink()
    iot, s3 = main_mod.build_sinks(cfg, events, dyn)
    return iot, s3, events


def t_gate_defaults_off():
    iot, s3, _events = _sinks_for(AWS_ENV_OFF)
    assert type(iot).__name__ == "MqttIoTStubSink"
    assert type(s3).__name__ == "S3BatchLoggerStub"


def t_gate_flag_without_master():
    env = dict(AWS_ENV_OFF, USB_SOV_AWS_IOT="1", USB_SOV_AWS_S3="1")
    iot, s3, events = _sinks_for(env)
    assert type(iot).__name__ == "MqttIoTStubSink"
    assert type(s3).__name__ == "S3BatchLoggerStub"
    msgs = " ".join(e["message"] for e in events.recent(50))
    assert "master gate" in msgs


def t_gate_master_iot_missing_config_falls_back():
    env = dict(AWS_ENV_OFF, USB_SOV_AWS_ENABLED="1", USB_SOV_AWS_IOT="1")
    iot, _s3, events = _sinks_for(env)
    assert type(iot).__name__ == "MqttIoTStubSink"
    msgs = " ".join(e["message"] for e in events.recent(50))
    assert "NOT armed" in msgs


def t_iot_check_ready_missing_certs():
    client = AwsIoTClient({"endpoint": "x.example.com",
                           "cert_path": "/nope/cert.pem",
                           "key_path": "/nope/key.pem",
                           "ca_path": "/nope/ca.pem"}, enabled=True)
    try:
        client._check_ready()
    except AwsSinkNotReady as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected AwsSinkNotReady")


class _FakeMqttClient:
    def __init__(self, *a, **kw):
        self.published = []
        self.fail = False
        self.connected = False

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def publish(self, topic, body, qos=1):
        if self.fail:
            raise RuntimeError("broker gone")
        self.published.append((topic, body))


def _tmp_cert_files():
    d = tempfile.mkdtemp(prefix="usb-sov-certs-")
    paths = {}
    for name in ("cert.pem", "key.pem", "ca.pem"):
        p = os.path.join(d, name)
        with open(p, "w") as fh:
            fh.write("placeholder")
        paths[name] = p
    return paths


def t_iot_publish_with_fake_client():
    paths = _tmp_cert_files()
    made = {}

    def factory(endpoint, client_id, cert, key, ca, on_disconnect=None):
        made["args"] = (endpoint, client_id)
        made["client"] = _FakeMqttClient()
        return made["client"]

    seen = []
    client = AwsIoTClient(
        {"endpoint": "ep.example.com", "cert_path": paths["cert.pem"],
         "key_path": paths["key.pem"], "ca_path": paths["ca.pem"]},
        enabled=True, on_publish=seen.append, client_factory=factory)
    client.connect()
    assert made["args"][1] == "911TransEdu_SovereignBridge"
    env = client.publish("COM1", b"\x41\x4c\x30hello")
    assert env["version"] == "2.0" and env["sequence"] == 1
    assert env["integrity"]["hash_sha256"] == \
        hashlib.sha256(b"\x41\x4c\x30hello").hexdigest()
    assert client.total_published == 1 and client.publish_errors == 0
    topic, body = made["client"].published[0]
    assert topic == "device/serial/v2/data"
    assert json.loads(body)["bridge_id"] == "911TransEdu_SovereignBridge"
    assert seen and seen[0]["sequence"] == 1  # on_publish still fires


def t_iot_publish_drop_counts_and_survives():
    paths = _tmp_cert_files()

    def factory(*a, **kw):
        c = _FakeMqttClient()
        c.fail = True
        return c

    seen = []
    client = AwsIoTClient(
        {"endpoint": "ep.example.com", "cert_path": paths["cert.pem"],
         "key_path": paths["key.pem"], "ca_path": paths["ca.pem"]},
        enabled=True, on_publish=seen.append, client_factory=factory)
    client.connect()
    env = client.publish("CELL", b"\x41\x54\x30x")
    assert env is not None
    assert client.total_published == 0
    assert client.publish_errors == 1
    assert seen and seen[0]["source"] == "CELL"  # local plane unaffected


def _s3_logger(transport, **kw):
    return AwsS3BatchLogger(
        bucket_name="911transedu-serial-batches", enabled=True,
        region="us-west-2",
        credentials_provider=lambda: Credentials("AKID", "SECRET", "TOK"),
        transport=transport, **kw)


def t_s3_flush_signs_and_clears():
    calls = []

    def transport(host, path, headers, body):
        calls.append((host, path, headers, body))
        return 200

    log = _s3_logger(transport)
    log.add_entry("COM1", b"\x01\x02")
    log.add_entry("LH", b"\x41\xa0\x30")
    uri = log.flush()
    assert uri is not None and uri.startswith(
        "s3://911transedu-serial-batches/logs/serial_batch_")
    assert log.flush_count == 1 and log.buffer == []
    host, path, headers, body = calls[0]
    assert host == "911transedu-serial-batches.s3.us-west-2.amazonaws.com"
    assert path.startswith("/logs/serial_batch_")
    assert headers["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"
    assert headers["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKID/")
    assert body == b"COM1: 0102\nLH: 41a030"


def t_s3_flush_failure_keeps_buffer():
    log = _s3_logger(lambda *a: 500)
    log.add_entry("COM1", b"\xff")
    assert log.flush() is None
    assert len(log.buffer) == 1 and log.flush_errors == 1

    def boom(*a):
        raise OSError("network unreachable")

    log2 = _s3_logger(boom)
    log2.add_entry("COM1", b"\xff")
    assert log2.flush() is None
    assert len(log2.buffer) == 1 and log2.flush_errors == 1


def t_s3_check_ready_requires_region_and_creds():
    log = AwsS3BatchLogger(enabled=True, region=None)
    try:
        log._check_ready()
    except AwsSinkNotReady:
        pass
    else:
        raise AssertionError("expected AwsSinkNotReady without region")


def t_config_env_plumbing():
    env = dict(AWS_ENV_OFF,
               USB_SOV_IOT_ENDPOINT="ep.example.com",
               USB_SOV_IOT_CERT="/s/cert.pem",
               USB_SOV_IOT_KEY="/s/private.key",
               USB_SOV_IOT_CA="/s/AmazonRootCA1.pem",
               USB_SOV_S3_BUCKET="911transedu-serial-batches",
               USB_SOV_AWS_REGION="us-west-2",
               USB_SOV_AWS_ENABLED="1")
    with patched_env(**env):
        cfg = config_mod.load_config()
    assert cfg["aws"]["enabled"] is True
    assert cfg["aws"]["iot"]["endpoint"] == "ep.example.com"
    assert cfg["aws"]["iot"]["cert_path"] == "/s/cert.pem"
    assert cfg["aws"]["iot"]["key_path"] == "/s/private.key"
    assert cfg["aws"]["iot"]["ca_path"] == "/s/AmazonRootCA1.pem"
    assert cfg["aws"]["s3_bucket"] == "911transedu-serial-batches"
    assert cfg["aws"]["region"] == "us-west-2"
    assert cfg["aws"]["iot"]["client_id"] == "911TransEdu_SovereignBridge"
    assert cfg["aws"]["iot"]["topic"] == "device/serial/v2/data"


# ----------------------------------------------------------------------
# Existing bridge behavior — smoke subset
# ----------------------------------------------------------------------

from event_store import SerialEventStore, SignatureRegistry  # noqa: E402
from simulated_ports import SimulatedSerialPort  # noqa: E402


def _sim_controller(with_store=False):
    ports = {name: SimulatedSerialPort(name, baudrate=9600) for name in
             ("COM1", "COM2", "Sport", "Coort", "LH", "FSH", "CELL",
              "BT", "WIFI", "LAN", "ZILTB", "COM3", "COM4")}
    dyn = DynamoDbStubSink()
    iot = MqttIoTStubSink(on_publish=dyn.put)
    s3 = S3BatchLoggerStub(log_dir=tempfile.mkdtemp(prefix="usb-sov-logs-"))
    events = EventLog()
    ctl = UnifiedSerialController(ports, iot, s3, dyn, events)
    store = None
    if with_store:
        d = tempfile.mkdtemp(prefix="usb-sov-store-")
        registry = SignatureRegistry(os.path.join(d, "registry.json"))
        store = SerialEventStore(os.path.join(d, "events.jsonl"), registry)
        ctl.store = store
    return ctl, ports, dyn, store


def t_envelope_integrity():
    env = build_envelope("COM1", b"abc", "911TransEdu_SovereignBridge",
                         "sess", 3)
    assert env["version"] == "2.0" and env["sequence"] == 3
    assert env["data_hex"] == "616263"
    assert env["integrity"]["hash_sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert env["integrity"]["length_bytes"] == 3


def t_classify_prefix_rules():
    d = tempfile.mkdtemp(prefix="usb-sov-reg-")
    reg = SignatureRegistry(os.path.join(d, "registry.json"))
    assert reg.classify(b"\x41\xa0\x30rest") == ("41A0", "lh_surge")
    assert reg.classify(b"\x41\xb0\x30rest") == ("41B0", "fsh_pulse")
    # BT frame: bytes 41 42 30 — notation "41B0" but NOT the fsh prefix.
    assert reg.classify(b"\x41\x42\x30rest") == ("41B0", "bt_telemetry_frame")
    # LH baseline bio frames carry header 'A','H','0' (code 41H0).
    assert reg.classify(b"\x41\x48\x30rest") == ("41H0", "lh_bio_telemetry")
    assert reg.classify(b"\x41\x4c\x30rest") == ("41L0", "lan_ethernet_frame")


def t_sim_ingest_and_mirror():
    ctl, ports, dyn, store = _sim_controller(with_store=True)
    ctl.iot.connect()
    surge = b"\x41\xa0\x30SURGE"
    baseline = b"\x41\x48\x30base"
    # The port is a byte stream: feed and process one frame at a time,
    # exactly as the timed talkers do in the running bridge.
    ports["LH"].feed(surge)
    asyncio.run(ctl._process_serial())
    ports["LH"].feed(baseline)
    asyncio.run(ctl._process_serial())
    # Mirror: only the surge frame lands on COM3, byte-identical.
    assert bytes(ports["COM3"].tx_capture) == surge
    assert ctl.mirror_stats["COM3"]["frames"] == 1
    # Ingest: both frames published + recorded.
    assert len(dyn) == 2
    recs = store.recent(10)
    names = sorted(r["signature_name"] for r in recs)
    assert names == ["lh_bio_telemetry", "lh_surge"], names
    assert ctl.stats["LH"]["frames"] == 2
    snap = ctl.status_snapshot()
    assert snap["sinks"]["iot_sink"] == "MqttIoTStubSink"
    assert snap["sinks"]["s3_sink"] == "S3BatchLoggerStub"
    assert snap["mirrors"]["COM3"]["frames_mirrored"] == 1
    store.close()


def t_failover_gate():
    ctl, ports, dyn, _store = _sim_controller()
    ctl.last_cell_activity = time.time() - 6.0  # past the 5.0 s gate

    async def run_briefly():
        task = asyncio.create_task(ctl.run())
        await asyncio.sleep(0.35)
        ctl.stop()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(run_briefly())
    assert ctl.ziltb_active is True
    assert ctl.failover_engagements == 1


def t_pty_plant_subset():
    from pty_ports import SocatPtyPlant
    d = tempfile.mkdtemp(prefix="usb-sov-pty-")
    plant = SocatPtyPlant(d, main_mod.BAUD)
    try:
        ports = plant.start()
        assert len(ports) == len(main_mod.BAUD)
        dyn = DynamoDbStubSink()
        iot = MqttIoTStubSink(on_publish=dyn.put)
        s3 = S3BatchLoggerStub(
            log_dir=tempfile.mkdtemp(prefix="usb-sov-logs-"))
        ctl = UnifiedSerialController(ports, iot, s3, dyn, EventLog())
        plant.endpoints["COM1"].feed(b"\x41\x4c\x30pty-frame")
        # socat + raw fds settle asynchronously; poll the process step.
        deadline = time.time() + 5
        while time.time() < deadline and len(dyn) == 0:
            asyncio.run(ctl._process_serial())
            time.sleep(0.05)
        assert len(dyn) == 1, "frame never crossed the PTY plane"
        assert ports["COM2"].bytes_transmitted == len(b"\x41\x4c\x30pty-frame")
        st = plant.stats()
        assert isinstance(st, dict)
    finally:
        plant.close()


TESTS = [
    ("sigv4: AWS published known-answer vector", t_sigv4_known_answer),
    ("sigv4: aws-activation copy signs identically", t_sigv4_copies_identical),
    ("sigv4: env credential provider", t_sigv4_env_credentials),
    ("sigv4: IMDSv2 parsing (stubbed)", t_sigv4_imds_parsing),
    ("sigv4: credential cache", t_sigv4_credential_cache),
    ("mqtt: CONNECT packet encoding", t_mqtt_connect_packet),
    ("mqtt: PUBLISH QoS1 packet encoding", t_mqtt_publish_packet),
    ("mqtt: remaining-length encoding", t_mqtt_remaining_length),
    ("mqtt: fake-broker publish round-trip", t_mqtt_roundtrip_fake_broker),
    ("mqtt: PUBACK timeout raises", t_mqtt_puback_timeout),
    ("gate: defaults keep stubs", t_gate_defaults_off),
    ("gate: per-sink flags without master stay off", t_gate_flag_without_master),
    ("gate: master+IoT flag, missing config -> stub", t_gate_master_iot_missing_config_falls_back),
    ("iot: _check_ready rejects missing cert files", t_iot_check_ready_missing_certs),
    ("iot: publish via fake client (envelope intact)", t_iot_publish_with_fake_client),
    ("iot: publish failure counted, bridge survives", t_iot_publish_drop_counts_and_survives),
    ("s3: flush signs (SigV4) and clears buffer", t_s3_flush_signs_and_clears),
    ("s3: flush failure keeps buffer", t_s3_flush_failure_keeps_buffer),
    ("s3: _check_ready requires region", t_s3_check_ready_requires_region_and_creds),
    ("config: env plumbing for sink config", t_config_env_plumbing),
    ("behavior: envelope integrity", t_envelope_integrity),
    ("behavior: signature classification + LH/FSH prefixes", t_classify_prefix_rules),
    ("behavior: sim ingest + COM3 mirror byte purity", t_sim_ingest_and_mirror),
    ("behavior: 5s cellular failover gate", t_failover_gate),
    ("behavior: socat PTY plant subset", t_pty_plant_subset),
]


def main():
    for name, fn in TESTS:
        check(name, fn)
    passed = sum(1 for _n, ok, _e in RESULTS if ok)
    for name, ok, err in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {err}" if err else ""))
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
