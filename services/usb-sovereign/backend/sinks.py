"""Telemetry sinks for USB-SOVEREIGN v1 — AWS components stubbed behind an
interface.

The spec's controller talks to three cloud sinks:

* ``AwsIoTClient``      — publishes canonical v2.0 envelopes to AWS IoT Core
                          over MQTT (topic ``device/serial/v2/data``, QoS 1).
* Lambda -> DynamoDB    — envelopes are routed into DynamoDB under partition
                          keys formatted ``source#timestamp``.
* ``AwsS3BatchLogger``  — buffers hex payloads in RAM and flushes to S3 every
                          30 s as ``logs/serial_batch_<timestamp>.txt``.

This increment runs fully local, so each sink is a stub with spec-faithful
behavior: identical envelope contents, identical DynamoDB key format, and an
identical 30 s batching cadence (the stub writes the batch to a local file
and records the s3:// key it *would* have used). Swapping in the real
boto3 / AWSIoTPythonSDK implementations only requires replacing these classes
— the controller depends on the TelemetrySink interface, not on AWS.
"""

import hashlib
import json
import os
import time
import uuid

CLIENT_ID = "911TransEdu_SovereignBridge"
TOPIC = "device/serial/v2/data"
BUCKET_NAME = "911transedu-telemetry-bucket"


def build_envelope(source: str, raw: bytes, bridge_id: str,
                   session_id: str, sequence: int) -> dict:
    """The canonical v2.0 telemetry envelope, exactly as the spec builds it.

    Shared by the local stub sink and the gated AwsIoTClient so both planes
    emit byte-identical envelopes.
    """
    return {
        "version": "2.0",
        "timestamp": time.time(),
        "source": source,  # COM1, COM2, Sport, Coort, LH, FSH, CELL, BT, WIFI, LAN, ZILTB
        "session_id": session_id,
        "sequence": sequence,
        "data_hex": raw.hex(),
        "bridge_id": bridge_id,
        "integrity": {
            "length_bytes": len(raw),
            "hash_sha256": hashlib.sha256(raw).hexdigest(),
        },
    }


class TelemetrySink:
    """Interface every telemetry sink implements."""

    def connect(self):
        pass

    def disconnect(self):
        pass

    def publish(self, source: str, raw: bytes):
        raise NotImplementedError


class MqttIoTStubSink(TelemetrySink):
    """Local stand-in for the spec's AwsIoTClient.

    Builds the canonical v2.0 envelope exactly as the specification's
    implementation does (same fields, lowercase hex, SHA-256 integrity block)
    and records it in memory instead of publishing to AWS IoT Core. An
    optional ``on_publish`` callback models the IoT topic rule / Lambda route
    into DynamoDB.
    """

    def __init__(self, bridge_id: str = CLIENT_ID, on_publish=None):
        self.bridge_id = bridge_id
        self.session_id = f"usb-sov-{uuid.uuid4()}"
        self.sequence_counter = 0
        self.envelopes = []          # most recent envelopes (capped)
        self.total_published = 0
        self.connected = False
        self._on_publish = on_publish

    def connect(self):
        self.connected = True
        print(f"[MQTT-STUB] Connected (local stub) client_id={self.bridge_id} "
              f"topic={TOPIC} session={self.session_id}")

    def disconnect(self):
        self.connected = False
        print("[MQTT-STUB] Disconnected (local stub).")

    def publish(self, source: str, raw: bytes):
        self.sequence_counter += 1
        payload = build_envelope(source, raw, self.bridge_id,
                                 self.session_id, self.sequence_counter)
        self.total_published += 1
        self.envelopes.append(payload)
        if len(self.envelopes) > 500:
            del self.envelopes[:250]
        if self._on_publish is not None:
            self._on_publish(payload)
        return payload


class DynamoDbStubSink:
    """Models the Lambda -> DynamoDB route: items keyed ``source#timestamp``."""

    def __init__(self):
        self.items = {}
        self.total_written = 0

    def put(self, envelope: dict):
        key = f"{envelope['source']}#{envelope['timestamp']}"
        self.items[key] = envelope
        self.total_written += 1

    def __len__(self):
        return len(self.items)


class S3BatchLoggerStub:
    """Local stand-in for the spec's AwsS3BatchLogger (same semantics).

    Buffers ``"SOURCE: hex"`` entries in RAM; ``should_flush()`` turns true
    after the flush interval (30 s per spec R11-R15, overridable via the
    USB_SOV_S3_FLUSH_SECONDS env var for demos/tests). ``flush()`` writes the
    batch to a local ``logs/`` directory using the spec's key format and
    records the s3:// URI it would have uploaded to.
    """

    def __init__(self, bucket_name: str = BUCKET_NAME, log_dir: str = "logs",
                 flush_seconds: float = None):
        self.bucket_name = bucket_name
        self.log_dir = log_dir
        self.flush_seconds = (
            float(os.environ.get("USB_SOV_S3_FLUSH_SECONDS", "30"))
            if flush_seconds is None else flush_seconds
        )
        self.buffer = []
        self.last_flush = time.time()
        self.flush_count = 0
        self.last_s3_uri = None
        os.makedirs(self.log_dir, exist_ok=True)

    def add_entry(self, source: str, raw: bytes):
        self.buffer.append(f"{source}: {raw.hex()}")

    def should_flush(self) -> bool:
        return (time.time() - self.last_flush) > self.flush_seconds

    def flush(self):
        if not self.buffer:
            self.last_flush = time.time()
            return None
        file_name = f"logs/serial_batch_{int(time.time())}.txt"
        body = "\n".join(self.buffer)
        # Write under <cwd>/logs/ using the spec's key layout.
        local_path = os.path.join(os.getcwd(), file_name)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "w") as fh:
            fh.write(body)
        self.last_s3_uri = f"s3://{self.bucket_name}/{file_name}"
        print(f"[S3-STUB] Batch flushed -> {self.last_s3_uri} "
              f"(local copy: {local_path}, {len(self.buffer)} entries)")
        self.buffer = []
        self.last_flush = time.time()
        self.flush_count += 1
        return self.last_s3_uri
