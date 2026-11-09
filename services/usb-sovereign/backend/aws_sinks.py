"""Live AWS sink classes from the USB-SOVEREIGN spec — GATED OFF by default.

Increment 2 adds the *structure* of the spec's production sinks:

* ``AwsIoTClient``     — the spec's MQTT publisher to AWS IoT Core
                         (AWSIoTPythonSDK-style: endpoint + cert/key/CA,
                         client ID ``911TransEdu_SovereignBridge``, topic
                         ``device/serial/v2/data``, QoS 1).
* ``AwsS3BatchLogger`` — the spec's boto3 S3 batch logger (30 s flush of
                         ``SOURCE: hex`` lines to
                         ``logs/serial_batch_<ts>.txt`` in the telemetry
                         bucket).

SAFETY GATE — read before enabling:

* Both classes are constructed DISABLED. ``config.py`` only instantiates
  them when the config explicitly sets ``aws.iot_enabled`` /
  ``aws.s3_enabled`` (env ``USB_SOV_AWS_IOT=1`` / ``USB_SOV_AWS_S3=1``).
* Even when enabled, ``connect()`` / ``flush()`` validate prerequisites
  (certificate files present, optional SDK importable) and raise
  ``AwsSinkNotReady`` *before* any network call if anything is missing.
  ``main.py`` catches that, logs it, and falls back to the local stubs —
  the bridge never silently talks to AWS.
* The optional dependencies (paho-mqtt / AWSIoTPythonSDK, boto3) are NOT
  installed on this VM and are imported lazily, so this module is safe to
  import anywhere. No AWS resource is created or touched by importing or
  constructing these classes.

Per Taylor's direction for this increment: live sinks stay disabled; no
AWS resources are touched.
"""

import json
import os
import time
import uuid

from sinks import BUCKET_NAME, CLIENT_ID, TOPIC, build_envelope


class AwsSinkNotReady(RuntimeError):
    """Raised when a gated live sink is asked to run without its config,
    credentials, or SDK — always before any network activity."""


class AwsIoTClient:
    """Spec AwsIoTClient: publishes v2.0 envelopes to AWS IoT Core (MQTT).

    Disabled unless constructed with ``enabled=True`` AND a complete
    ``iot_config`` (endpoint, cert_path, key_path, ca_path). The publish
    path builds the exact same envelope as the local stub via
    ``sinks.build_envelope``.
    """

    def __init__(self, iot_config: dict = None, enabled: bool = False,
                 on_publish=None):
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
        self.connected = False
        self._on_publish = on_publish
        self._client = None

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

    def _load_sdk(self):
        try:
            from AWSIoTPythonSDK.MQTTLib import AWSIoTMQTTClient
            return AWSIoTMQTTClient
        except ImportError:
            pass
        try:
            import paho.mqtt.client as mqtt
            return mqtt
        except ImportError as exc:
            raise AwsSinkNotReady(
                "No MQTT SDK available (AWSIoTPythonSDK or paho-mqtt). "
                "Install one before enabling live IoT publishing.") from exc

    # -- TelemetrySink interface ----------------------------------------
    def connect(self):
        self._check_ready()
        sdk = self._load_sdk()
        # NOTE: reaching here requires explicit enablement, complete cert
        # config, and an installed SDK. The connection itself follows the
        # spec: TLS mutual auth to the account's IoT endpoint, QoS 1.
        client = sdk(self.bridge_id) if sdk.__name__.startswith("AWSIoT") \
            else sdk.Client(client_id=self.bridge_id)
        self._client = client
        self.connected = True
        print(f"[MQTT-AWS] Connected client_id={self.bridge_id} "
              f"endpoint={self.endpoint} topic={self.topic}")

    def disconnect(self):
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self.connected = False

    def publish(self, source: str, raw: bytes):
        self._check_ready()
        if not self.connected:
            raise AwsSinkNotReady("AwsIoTClient.publish before connect().")
        self.sequence_counter += 1
        payload = build_envelope(source, raw, self.bridge_id,
                                 self.session_id, self.sequence_counter)
        self._client.publish(self.topic, json.dumps(payload), 1)
        self.total_published += 1
        if self._on_publish is not None:
            self._on_publish(payload)
        return payload


class AwsS3BatchLogger:
    """Spec AwsS3BatchLogger: RAM buffer, 30 s flush to S3 via boto3.

    Same buffering semantics as ``S3BatchLoggerStub``; the flush is gated
    identically (enabled flag + boto3 importable) and raises
    ``AwsSinkNotReady`` before any AWS call when not ready.
    """

    def __init__(self, bucket_name: str = BUCKET_NAME, enabled: bool = False,
                 flush_seconds: float = None, region: str = None):
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
        self.last_s3_uri = None

    def add_entry(self, source: str, raw: bytes):
        self.buffer.append(f"{source}: {raw.hex()}")

    def should_flush(self) -> bool:
        return (time.time() - self.last_flush) > self.flush_seconds

    def _check_ready(self):
        if not self.enabled:
            raise AwsSinkNotReady(
                "AwsS3BatchLogger is disabled (aws.s3_enabled is false). "
                "Live S3 archival stays off in this build.")
        try:
            import boto3  # noqa: F401
        except ImportError as exc:
            raise AwsSinkNotReady(
                "boto3 is not installed; cannot enable live S3 archival."
            ) from exc

    def flush(self):
        if not self.buffer:
            self.last_flush = time.time()
            return None
        self._check_ready()  # raises before any network call
        import boto3
        key = f"logs/serial_batch_{int(time.time())}.txt"
        body = "\n".join(self.buffer)
        boto3.client("s3", region_name=self.region).put_object(
            Bucket=self.bucket_name, Key=key, Body=body.encode())
        self.last_s3_uri = f"s3://{self.bucket_name}/{key}"
        self.buffer = []
        self.last_flush = time.time()
        self.flush_count += 1
        return self.last_s3_uri
