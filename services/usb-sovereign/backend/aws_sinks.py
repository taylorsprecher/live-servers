"""Live AWS sink classes from the USB-SOVEREIGN spec — GATED OFF by default.

The live counterparts of the local stubs in ``sinks.py``:

* ``AwsIoTClient``     — MQTT publisher to AWS IoT Core: mutual TLS to the
                         account endpoint on port 8883, client ID
                         ``911TransEdu_SovereignBridge``, topic
                         ``device/serial/v2/data``, QoS 1. Implemented on
                         the standard-library MQTT client in
                         ``mqtt_stdlib.py`` — no third-party SDK needed
                         (the deployment image is stdlib-only).
* ``AwsS3BatchLogger`` — the spec's S3 batch logger: RAM buffer of
                         ``SOURCE: hex`` lines, 30 s flush of
                         ``logs/serial_batch_<ts>.txt`` to the telemetry
                         bucket, signed with the stdlib SigV4 signer in
                         ``sigv4.py`` using instance-profile credentials
                         from the EC2 metadata service.

DynamoDB is deliberately NOT written from the host: envelopes published
to IoT Core are routed by the IoT topic rule to the
``serial-events-writer`` Lambda, which writes the ``serial_events``
table (see ``aws-activation/template.yaml``). The local
``DynamoDbStubSink`` still records envelopes in-process for the
dashboard, fed by the same ``on_publish`` callback the stub sink uses.

SAFETY GATE — read before enabling:

* Both classes are constructed DISABLED. ``config.py`` only lets
  ``main.py`` arm them when the master gate ``USB_SOV_AWS_ENABLED=1`` is
  set AND the per-sink flags (``USB_SOV_AWS_IOT=1`` / ``USB_SOV_AWS_S3=1``)
  are set AND their configuration validates (IoT endpoint + certificate
  files present; region + resolvable credentials for S3).
* Arming is probed at startup (IoT connect / credential fetch). Any
  failure falls back to the local stubs with a logged reason — the
  bridge never crashes and never silently talks to AWS.
* At runtime, a lost MQTT connection triggers a throttled reconnect;
  frames that cannot be published are counted (``publish_errors``) and
  dropped from the cloud plane only — the local plane (store, S3 batch
  buffer, dashboard) is unaffected. A failed S3 flush keeps its buffer
  for the next cycle instead of losing the batch.
"""

import http.client
import json
import os
import time
import uuid

from mqtt_stdlib import MqttError, MqttTlsClient
from sigv4 import CredentialCache, default_credentials, sign_request
from sinks import BUCKET_NAME, CLIENT_ID, TOPIC, build_envelope


class AwsSinkNotReady(RuntimeError):
    """Raised when a gated live sink is asked to run without its config
    or credentials — always before any unexpected network activity."""


class AwsIoTClient:
    """Spec AwsIoTClient: publishes v2.0 envelopes to AWS IoT Core (MQTT).

    Disabled unless constructed with ``enabled=True`` AND a complete
    ``iot_config`` (endpoint, cert_path, key_path, ca_path). Builds the
    exact same envelope as the local stub via ``sinks.build_envelope``.
    """

    def __init__(self, iot_config: dict = None, enabled: bool = False,
                 on_publish=None, client_factory=None):
        cfg = iot_config or {}
        self.enabled = bool(enabled)
        self.endpoint = cfg.get("endpoint")
        self.cert_path = cfg.get("cert_path")
        self.key_path = cfg.get("key_path")
        self.ca_path = cfg.get("ca_path")
        self.bridge_id = cfg.get("client_id", CLIENT_ID)
        self.topic = cfg.get("topic", TOPIC)
        self.session_id = f"usb-sov-{uuid.uuid4()}"
        self.sequence_counter = 0
        self.total_published = 0
        self.publish_errors = 0
        self.connected = False
        self._on_publish = on_publish
        self._client = None
        self._client_factory = client_factory or MqttTlsClient
        self._last_reconnect = 0.0

    # -- gate ------------------------------------------------------------
    def _check_ready(self):
        if not self.enabled:
            raise AwsSinkNotReady(
                "AwsIoTClient is disabled (aws.iot_enabled is false). "
                "Live AWS IoT publishing stays off in this build.")
        missing = [p for p in (self.endpoint, self.cert_path,
                               self.key_path, self.ca_path) if not p]
        if missing:
            raise AwsSinkNotReady(
                "AwsIoTClient enabled but iot config is incomplete "
                "(endpoint/cert_path/key_path/ca_path required).")
        for path in (self.cert_path, self.key_path, self.ca_path):
            if not os.path.isfile(path):
                raise AwsSinkNotReady(
                    f"AwsIoTClient certificate file not found: {path}")

    # -- TelemetrySink interface ----------------------------------------
    def connect(self):
        self._check_ready()
        if self.connected and self._client is not None:
            return
        client = self._client_factory(
            self.endpoint, self.bridge_id, self.cert_path,
            self.key_path, self.ca_path,
            on_disconnect=self._note_disconnect)
        client.connect()
        self._client = client
        self.connected = True
        print(f"[MQTT-AWS] Connected client_id={self.bridge_id} "
              f"endpoint={self.endpoint} topic={self.topic}")

    def _note_disconnect(self):
        self.connected = False
        print("[MQTT-AWS] Connection lost — will retry on next publish.")

    def disconnect(self):
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self.connected = False

    def _reconnect_throttled(self) -> bool:
        now = time.time()
        if now - self._last_reconnect < 5.0:
            return False
        self._last_reconnect = now
        try:
            if self._client is not None:
                try:
                    self._client.disconnect()
                except Exception:
                    pass
                self._client = None
            self.connect()
            print("[MQTT-AWS] Reconnected.")
            return True
        except Exception as exc:
            print(f"[MQTT-AWS] Reconnect failed: {exc}")
            return False

    def publish(self, source: str, raw: bytes):
        self._check_ready()
        self.sequence_counter += 1
        payload = build_envelope(source, raw, self.bridge_id,
                                 self.session_id, self.sequence_counter)
        body = json.dumps(payload).encode()
        sent = False
        try:
            if not self.connected:
                raise MqttError("not connected")
            self._client.publish(self.topic, body, qos=1)
            sent = True
        except Exception:
            # One throttled reconnect + single retry; beyond that the
            # frame's cloud copy is dropped (counted) and the bridge —
            # including the local recording below — carries on.
            if self._reconnect_throttled():
                try:
                    self._client.publish(self.topic, body, qos=1)
                    sent = True
                except Exception:
                    sent = False
        if sent:
            self.total_published += 1
        else:
            self.publish_errors += 1
        if self._on_publish is not None:
            self._on_publish(payload)
        return payload


def _https_put(host: str, path: str, headers: dict, body: bytes,
               timeout: float = 15.0) -> int:
    conn = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        conn.request("PUT", path, body=body, headers=headers)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


class AwsS3BatchLogger:
    """Spec AwsS3BatchLogger: RAM buffer, 30 s flush to S3 (SigV4/IMDS).

    Same buffering semantics as ``S3BatchLoggerStub``. Credentials come
    from the instance profile via IMDS (or env vars off-host) through
    ``sigv4.default_credentials`` — no boto3, no static keys in config.
    A failed flush keeps the buffer for the next cycle (bounded: oldest
    entries are dropped past ``MAX_BUFFER_ENTRIES`` with a log line).
    """

    MAX_BUFFER_ENTRIES = 10_000

    def __init__(self, bucket_name: str = BUCKET_NAME, enabled: bool = False,
                 flush_seconds: float = None, region: str = None,
                 credentials_provider=None, transport=None):
        self.bucket_name = bucket_name
        self.enabled = bool(enabled)
        self.region = region
        self.flush_seconds = (
            float(os.environ.get("USB_SOV_S3_FLUSH_SECONDS", "30"))
            if flush_seconds is None else flush_seconds
        )
        self.buffer = []
        self.last_flush = time.time()
        self.flush_count = 0
        self.flush_errors = 0
        self.last_s3_uri = None
        self._creds = CredentialCache(
            credentials_provider or default_credentials)
        self._transport = transport or _https_put

    def add_entry(self, source: str, raw: bytes):
        self.buffer.append(f"{source}: {raw.hex()}")
        if len(self.buffer) > self.MAX_BUFFER_ENTRIES:
            dropped = len(self.buffer) - self.MAX_BUFFER_ENTRIES
            del self.buffer[:dropped]
            print(f"[S3-AWS] Buffer cap hit — dropped {dropped} oldest "
                  "entries (flush is failing).")

    def should_flush(self) -> bool:
        return (time.time() - self.last_flush) > self.flush_seconds

    def _check_ready(self):
        if not self.enabled:
            raise AwsSinkNotReady(
                "AwsS3BatchLogger is disabled (aws.s3_enabled is false). "
                "Live S3 archival stays off in this build.")
        if not self.region:
            raise AwsSinkNotReady(
                "AwsS3BatchLogger enabled but no AWS region configured "
                "(aws.region / USB_SOV_AWS_REGION).")
        # Resolving credentials here is what lets main.py arm or fall
        # back at startup, before the first flush is due.
        try:
            self._creds.get()
        except Exception as exc:
            raise AwsSinkNotReady(
                f"AwsS3BatchLogger cannot resolve credentials: {exc}"
            ) from exc

    def flush(self):
        if not self.buffer:
            self.last_flush = time.time()
            return None
        self._check_ready()  # raises before any network call
        key = f"logs/serial_batch_{int(time.time())}.txt"
        body = "\n".join(self.buffer).encode()
        host = f"{self.bucket_name}.s3.{self.region}.amazonaws.com"
        creds = self._creds.get()
        headers = sign_request(
            "PUT", host, self.region, "s3", creds, path="/" + key,
            headers={"x-amz-content-sha256": "UNSIGNED-PAYLOAD"},
            body=body, payload_hash="UNSIGNED-PAYLOAD")
        try:
            status = self._transport(host, "/" + key, headers, body)
        except Exception as exc:
            self.flush_errors += 1
            print(f"[S3-AWS] Flush failed ({exc}) — buffer kept for "
                  "the next cycle.")
            return None
        if status != 200:
            self.flush_errors += 1
            print(f"[S3-AWS] Flush failed (HTTP {status}) — buffer kept "
                  "for the next cycle.")
            return None
        self.last_s3_uri = f"s3://{self.bucket_name}/{key}"
        print(f"[S3-AWS] Batch flushed -> {self.last_s3_uri} "
              f"({len(self.buffer)} entries)")
        self.buffer = []
        self.last_flush = time.time()
        self.flush_count += 1
        return self.last_s3_uri
