"""Runtime configuration for USB-SOVEREIGN (Increment 2).

Resolution order: environment variables > JSON config file > defaults.
The JSON file is selected with ``USB_SOV_CONFIG`` (default:
``config.local.json`` next to this module, if present).

Defaults keep the bridge fully local:

* ports mode ``sim`` (increment-1 simulated plant); ``pty`` selects the
  socat PTY hardware plane,
* live AWS sinks DISABLED — arming them needs the master gate
  ``aws.enabled`` AND the per-sink flags (``aws.iot_enabled`` /
  ``aws.s3_enabled``) AND valid sink config (see aws_sinks.py),
* local serial_events store enabled.

Environment overrides:
    USB_SOV_PORTS=pty|sim          USB_SOV_AWS_ENABLED=1 (master gate)
    USB_SOV_AWS_IOT=1              USB_SOV_AWS_S3=1
    USB_SOV_STORE=0                USB_SOV_PTY_DIR=/tmp/...
    USB_SOV_HOST / USB_SOV_PORT (server)
    USB_SOV_IOT_ENDPOINT / USB_SOV_IOT_CERT / USB_SOV_IOT_KEY /
    USB_SOV_IOT_CA / USB_SOV_S3_BUCKET / USB_SOV_AWS_REGION (sink config)
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

DEFAULTS = {
    "ports": {
        "mode": "sim",  # "sim" | "pty"
        "pty_dir": os.path.join("/tmp", "usb-sovereign-pty"),
    },
    "aws": {
        # Master gate (USB_SOV_AWS_ENABLED): no live sink arms without
        # it, even if a per-sink flag is set. Activation plan of record:
        # aws-activation/README.md.
        "enabled": False,
        "iot_enabled": False,
        "s3_enabled": False,
        "iot": {
            "endpoint": None,
            "cert_path": None,
            "key_path": None,
            "ca_path": None,
            "client_id": "911TransEdu_SovereignBridge",
            "topic": "device/serial/v2/data",
        },
        # Bucket from the approved activation plan (the stub-era default
        # "911transedu-telemetry-bucket" in sinks.BUCKET_NAME is only
        # the local stubs' label; the live bucket is this one).
        "s3_bucket": "911transedu-serial-batches",
        "region": None,
    },
    "store": {
        "enabled": True,
        "dir": os.path.join(_ROOT, "data"),
    },
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    path = os.environ.get(
        "USB_SOV_CONFIG", os.path.join(_HERE, "config.local.json"))
    if os.path.isfile(path):
        with open(path) as fh:
            cfg = _deep_merge(cfg, json.load(fh))
    if os.environ.get("USB_SOV_PORTS"):
        cfg["ports"]["mode"] = os.environ["USB_SOV_PORTS"].strip().lower()
    if os.environ.get("USB_SOV_PTY_DIR"):
        cfg["ports"]["pty_dir"] = os.environ["USB_SOV_PTY_DIR"]
    if "USB_SOV_AWS_ENABLED" in os.environ:
        cfg["aws"]["enabled"] = _truthy(os.environ["USB_SOV_AWS_ENABLED"])
    if "USB_SOV_AWS_IOT" in os.environ:
        cfg["aws"]["iot_enabled"] = _truthy(os.environ["USB_SOV_AWS_IOT"])
    if "USB_SOV_AWS_S3" in os.environ:
        cfg["aws"]["s3_enabled"] = _truthy(os.environ["USB_SOV_AWS_S3"])
    # Live-sink config plumbing (host .env at activation time; values
    # are paths/hostnames/bucket names — never key material).
    if os.environ.get("USB_SOV_IOT_ENDPOINT"):
        cfg["aws"]["iot"]["endpoint"] = os.environ["USB_SOV_IOT_ENDPOINT"]
    if os.environ.get("USB_SOV_IOT_CERT"):
        cfg["aws"]["iot"]["cert_path"] = os.environ["USB_SOV_IOT_CERT"]
    if os.environ.get("USB_SOV_IOT_KEY"):
        cfg["aws"]["iot"]["key_path"] = os.environ["USB_SOV_IOT_KEY"]
    if os.environ.get("USB_SOV_IOT_CA"):
        cfg["aws"]["iot"]["ca_path"] = os.environ["USB_SOV_IOT_CA"]
    if os.environ.get("USB_SOV_S3_BUCKET"):
        cfg["aws"]["s3_bucket"] = os.environ["USB_SOV_S3_BUCKET"]
    if os.environ.get("USB_SOV_AWS_REGION"):
        cfg["aws"]["region"] = os.environ["USB_SOV_AWS_REGION"]
    if "USB_SOV_STORE" in os.environ:
        cfg["store"]["enabled"] = _truthy(os.environ["USB_SOV_STORE"])
    return cfg
