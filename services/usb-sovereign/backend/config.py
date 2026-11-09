"""Runtime configuration for USB-SOVEREIGN (Increment 2).

Resolution order: environment variables > JSON config file > defaults.
The JSON file is selected with ``USB_SOV_CONFIG`` (default:
``config.local.json`` next to this module, if present).

Defaults keep the bridge fully local:

* ports mode ``sim`` (increment-1 simulated plant); ``pty`` selects the
  socat PTY hardware plane,
* live AWS sinks DISABLED (``aws.iot_enabled`` / ``aws.s3_enabled`` false),
* local serial_events store enabled.

Environment overrides:
    USB_SOV_PORTS=pty|sim          USB_SOV_AWS_IOT=1
    USB_SOV_AWS_S3=1               USB_SOV_STORE=0
    USB_SOV_PTY_DIR=/tmp/...       USB_SOV_HOST / USB_SOV_PORT (server)
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
        "s3_bucket": "911transedu-telemetry-bucket",
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
    if "USB_SOV_AWS_IOT" in os.environ:
        cfg["aws"]["iot_enabled"] = _truthy(os.environ["USB_SOV_AWS_IOT"])
    if "USB_SOV_AWS_S3" in os.environ:
        cfg["aws"]["s3_enabled"] = _truthy(os.environ["USB_SOV_AWS_S3"])
    if "USB_SOV_STORE" in os.environ:
        cfg["store"]["enabled"] = _truthy(os.environ["USB_SOV_STORE"])
    return cfg
