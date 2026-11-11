"""
Save storage — LOCAL STUB for Amazon S3.

!!! STUB — LOCAL FILESYSTEM INSTEAD OF S3 !!!
Production (Phase 3 of the plan) stores saves in S3 buckets
(xsave-dev / xsave-stage / xsave-prod, versioning + encryption on).
This stub stores the exact same key layout on the local filesystem:

    {STORAGE_DIR}/users/{xuid}/savefiles/{save_name}
    {STORAGE_DIR}/users/{xuid}/_index.json     <- per-user metadata index

so swapping in boto3 later changes this module only.

Metadata follows the plan's Save API model and extends it:
    {"xuid", "name", "saveVersion", "timestamp", "size", "sha256"}

saveVersion increments on every upload, matching the plan's
monotonic-version model.

Save names are restricted to an allowlist pattern. The plan's own
review section lists "raw filename input" as a launch blocker; the
allowlist (no slashes, no dot-dot, bounded length) is enforced here
from the first increment.
"""

import hashlib
import json
import os
import re
import time

STORAGE_DIR = os.environ.get(
    "XSAVE_STORAGE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage_data"),
)

# Allowlisted save namespace: letters, digits, dot, underscore, hyphen.
SAVE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class StorageError(Exception):
    pass


class InvalidSaveName(StorageError):
    pass


class SaveNotFound(StorageError):
    pass


def validate_save_name(name: str) -> str:
    if not SAVE_NAME_RE.match(name or "") or ".." in name:
        raise InvalidSaveName(f"invalid save name: {name!r}")
    return name


def key_for(xuid: str, name: str) -> str:
    """Canonical storage key, per the Xbox AWS S3 plan: save objects
    live under users/{xuid}/savefiles/{saveId}. Paths are ALWAYS
    derived from the XUID — never from a client-supplied user id
    (Phase 2/3 rule)."""
    return f"users/{xuid}/savefiles/{name}"


def _parse_key(key: str) -> tuple[str, str]:
    """Split a canonical save key into (xuid, name)."""
    parts = key.split("/", 3)
    return parts[1], parts[3]


def _user_dir(xuid: str) -> str:
    return os.path.join(STORAGE_DIR, "users", xuid)


def _index_path(xuid: str) -> str:
    return os.path.join(_user_dir(xuid), "_index.json")


def _load_index(xuid: str) -> dict:
    try:
        with open(_index_path(xuid), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_index(xuid: str, index: dict) -> None:
    os.makedirs(_user_dir(xuid), exist_ok=True)
    with open(_index_path(xuid), "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)


def _path_for_key(key: str) -> str:
    # Keys are internally generated (users/{xuid}/savefiles/{name});
    # join safely.
    parts = key.split("/")
    return os.path.join(STORAGE_DIR, *parts)


def put_bytes(key: str, data: bytes) -> dict:
    xuid, name = _parse_key(key)
    path = _path_for_key(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    index = _load_index(xuid)
    previous = index.get(name, {})
    meta = {
        "xuid": xuid,
        "name": name,
        "saveVersion": int(previous.get("saveVersion", 0)) + 1,
        "timestamp": int(time.time()),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    index[name] = meta
    _save_index(xuid, index)
    return meta


def get_bytes(key: str) -> bytes:
    path = _path_for_key(key)
    if not os.path.isfile(path):
        raise SaveNotFound(key)
    with open(path, "rb") as fh:
        return fh.read()


def list_saves(xuid: str) -> list[dict]:
    index = _load_index(xuid)
    return [index[name] for name in sorted(index)]


def delete_save(xuid: str, name: str) -> dict:
    index = _load_index(xuid)
    if name not in index:
        raise SaveNotFound(name)
    meta = index.pop(name)
    path = _path_for_key(key_for(xuid, name))
    if os.path.isfile(path):
        os.remove(path)
    _save_index(xuid, index)
    return meta
