#!/usr/bin/env python3
"""Zillion Storage — shared helpers: the manifest store on
ZILLION_DATA, streaming SHA-256 of a file, and the retry-with-backoff
wrapper. Moved verbatim from backend/server.py."""
import hashlib
import json
import os
import threading
import time  # noqa: F401  (kept from the original import set)

from config import CHUNK, DATA, MANIFEST_PATH

_lock = threading.Lock()

def load_manifest():
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_manifest(manifest):
    os.makedirs(DATA, exist_ok=True)
    tmp = MANIFEST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    os.replace(tmp, MANIFEST_PATH)


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def with_retries(fn, attempts=3, base_wait=0.5, cap=8):
    """Exponential backoff, tenacity-style: 3 attempts, waits grow
    from base_wait (local profile 0.5s -> 1s -> 2s; the document's
    network profile for cloud backends is 2-10s)."""
    wait = base_wait
    last = None
    for _ in range(attempts):
        try:
            return fn()
        except Exception as ex:  # noqa: BLE001 - isolated per backend by design
            last = ex
            time.sleep(wait)
            wait = min(wait * 2, cap)
    raise last

