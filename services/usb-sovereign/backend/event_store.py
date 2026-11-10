"""Local serial_events store + protocol-signature registry (Increment 2).

The spec's analytics layer targets a ClickHouse ``serial_events`` MergeTree
table fed by the telemetry plane, with a protocol-signature registry
(``41S0`` sport_handshake, ``41C1`` coort_response, ``41L0``
lan_ethernet_frame, ...) classifying frames by their header bytes.

This increment provides the same two structures as a local store:

* ``SignatureRegistry`` — maps a frame's signature code to a protocol name.
  The code follows the spec's notation: first byte as hex + remaining header
  bytes as ASCII (b"\\x41S0" -> "41S0"). The registry is persisted as JSON
  (``data/signature_registry.json``) and seeded from the spec on first run,
  so new signatures can be registered without code changes.
* ``SerialEventStore`` — appends every published v2.0 envelope to a JSONL
  file (``data/serial_events.jsonl``), one record per frame with the
  ClickHouse column set (ts, source, session, sequence, signature, length,
  sha256, hex), and keeps in-memory counters + a recent-events ring for the
  dashboard/API. Append-only, like the MergeTree it stands in for.

Standard library only.
"""

import json
import os
import time
from collections import Counter, deque

# Spec registry seeds (signature code -> protocol name). Codes derive from
# the frame headers used by the device plant in simulated_ports/pty_ports:
# 41S0 = 'A''S''0', 41C1 = 'A''C''1', 41L0 = 'A''L''0', ...
DEFAULT_SIGNATURES = {
    "41S0": "sport_handshake",
    "41C1": "coort_response",
    "41L0": "lan_ethernet_frame",
    "41L1": "com_pair_frame",
    "41H0": "lh_bio_telemetry",
    "41F0": "fsh_bio_telemetry",
    "41A0": "lh_surge",
    "41B0": "bt_telemetry_frame",
    "41W0": "wifi_telemetry_frame",
    "41T0": "cell_telemetry_frame",
    "41Z0": "ziltb_failover_frame",
}


def signature_code(data: bytes) -> str:
    """Spec-notation signature code for a frame: hex byte 0 + ASCII rest."""
    if not data:
        return "EMPTY"
    if len(data) < 3:
        return data.hex().upper()
    return f"{data[0]:02X}" + data[1:3].decode("ascii", errors="replace")


# Bio-telemetry signature additions (ClickHouse catalog ids 10 and 11).
# These two signatures are keyed by their raw two-byte hex prefix, exactly
# as the catalog and the bio_telemetry_routing RDL state them (header
# 0x41A0 / 0x41B0), NOT by the ASCII notation above: byte 0xA0 / 0xB0 is
# not ASCII, so the notation code cannot express them. The prefix check
# runs before the notation lookup, and it cannot collide with the BT
# frame (bytes 41 42 30 -> notation code "41B0", bt_telemetry_frame):
# that frame's second byte is 0x42, not 0xB0.
PREFIX_SIGNATURES = {
    b"\x41\xa0": ("41A0", "lh_surge"),
    b"\x41\xb0": ("41B0", "fsh_pulse"),
}


class SignatureRegistry:
    def __init__(self, path: str):
        self.path = path
        self.signatures = dict(DEFAULT_SIGNATURES)
        if os.path.isfile(path):
            try:
                with open(path) as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self.signatures.update(
                        {str(k): str(v) for k, v in loaded.items()})
            except (ValueError, OSError):
                pass
        else:
            self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump(self.signatures, fh, indent=2, sort_keys=True)

    def register(self, code: str, name: str):
        self.signatures[code] = name
        self.save()

    def classify(self, data: bytes):
        """-> (signature_code, protocol_name); unknown frames are 'unknown'.

        The bio-telemetry prefix signatures (lh_surge / fsh_pulse) are
        matched on the raw two-byte header BEFORE the notation lookup:
        their second byte (0xA0 / 0xB0) is not ASCII, so the notation
        cannot express them. This never touches the BT frame — its bytes
        are 41 42 30, so its two-byte prefix is b"\\x41\\x42", which is
        not a key of PREFIX_SIGNATURES, and it still resolves through the
        notation to "41B0" / bt_telemetry_frame.
        """
        prefix_hit = PREFIX_SIGNATURES.get(data[:2])
        if prefix_hit is not None:
            return prefix_hit
        code = signature_code(data)
        return code, self.signatures.get(code, "unknown")

    def __len__(self):
        return len(self.signatures)


class SerialEventStore:
    """Append-only local stand-in for the ClickHouse serial_events table."""

    def __init__(self, path: str, registry: SignatureRegistry,
                 recent_capacity: int = 500):
        self.path = path
        self.registry = registry
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._fh = open(path, "a", buffering=1)  # line-buffered appends
        self.total = 0
        self.per_source = Counter()
        self.per_signature = Counter()
        self._recent = deque(maxlen=recent_capacity)

    def append(self, envelope: dict) -> dict:
        """Record one published envelope; returns the stored event row."""
        raw = bytes.fromhex(envelope.get("data_hex", ""))
        code, name = self.registry.classify(raw)
        record = {
            "ts": envelope.get("timestamp", time.time()),
            "source": envelope.get("source"),
            "session_id": envelope.get("session_id"),
            "sequence": envelope.get("sequence"),
            "signature": code,
            "signature_name": name,
            "length_bytes": envelope.get("integrity", {}).get("length_bytes",
                                                              len(raw)),
            "sha256": envelope.get("integrity", {}).get("hash_sha256"),
            "data_hex": envelope.get("data_hex", ""),
        }
        self._fh.write(json.dumps(record) + "\n")
        self.total += 1
        self.per_source[record["source"]] += 1
        self.per_signature[code] += 1
        self._recent.append(record)
        return record

    def recent(self, n: int = 50):
        return list(self._recent)[-n:]

    def stats(self) -> dict:
        return {
            "backend": "local-jsonl (ClickHouse serial_events stand-in)",
            "path": self.path,
            "total_events": self.total,
            "per_source": dict(self.per_source),
            "per_signature": dict(self.per_signature),
            "registry_size": len(self.registry),
            "registry": dict(self.registry.signatures),
        }

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass
