#!/usr/bin/env python3
"""SyncFlow — Backend–Frontend Sync Flow demo (Taylor's spec build).

Implements the "Backend–Frontend Sync Flow" diagram Taylor posted on
2026-11-12 and directed to be built ("Implement the Backend–Frontend
Sync Flow in a project"):

  Frontend Client  ->  API Gateway  ->  Sync & Conflict Handling
       ^                                        |
       |--------- Client Updates ---------------|
                     Event Bus / Event Stream

Python 3 stdlib ONLY (http.server). One demo profile record is stored
with an integer version (starts at 1; the diagram's v12 -> v13 is
illustrative). The API gateway surface is:

  GET  {BASE}/api/profile          -> {"profile": {...}, "version": n}
  POST {BASE}/api/profile/update   -> body {"base_version": n,
       "changes": {field: value, ...}}
       base matches: the field-level delta is computed and stored, the
       version increments, DELTA_CREATED then PROFILE_UPDATED are
       emitted on the event stream, and the call returns
       {"profile", "version", "delta"} with HTTP 200.
       base mismatch: CONFLICT_DETECTED is emitted and the call
       returns HTTP 409 with {"server_profile", "server_version",
       "your_base_version"} so the client can run its Conflict
       Resolver (keep mine = re-submit against the server version;
       use server = discard local edits and refresh).
  GET  {BASE}/api/events           -> {"events": [...]} newest last;
       each event: {"seq", "type", "version", "timestamp", "detail"}.
  GET  {BASE}/api/deltas           -> {"deltas": [...]} stored delta
       history, oldest first.
  POST {BASE}/api/reset            -> demo affordance: restores the
       demo profile at version 1 and clears deltas and events.
  GET  {BASE}/health               -> {"status": "ok", ...}

State persists to a JSON file in SYNCFLOW_DATA_DIR (a Docker volume)
so it survives container restarts. Events are only ever appended when
the corresponding thing actually happens; a rollback is never faked.

Demo data only — the profile below is invented and neutral.
"""
import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_PATH = os.environ.get("BASE_PATH", "/syncflow")
PORT = int(os.environ.get("PORT", "8000"))
DATA_DIR = Path(os.environ.get("SYNCFLOW_DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "state.json"
INDEX_HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")

PROFILE_FIELDS = ("name", "display_name", "bio", "location", "timezone")

DEMO_PROFILE = {
    "name": "Alex Demo",
    "display_name": "alex.demo",
    "bio": "Sample profile for the SyncFlow demo - edit me on either device.",
    "location": "Demo City",
    "timezone": "UTC",
}

_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_state():
    return {
        "profile": dict(DEMO_PROFILE),
        "version": 1,
        "deltas": [],
        "events": [],
        "next_seq": 1,
    }


def _load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if (
            isinstance(state.get("profile"), dict)
            and isinstance(state.get("version"), int)
            and isinstance(state.get("deltas"), list)
            and isinstance(state.get("events"), list)
            and isinstance(state.get("next_seq"), int)
        ):
            return state
    except Exception:
        pass
    return _seed_state()


def _save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _emit(state, type_, version, detail):
    state["events"].append(
        {
            "seq": state["next_seq"],
            "type": type_,
            "version": version,
            "timestamp": _now(),
            "detail": detail,
        }
    )
    state["next_seq"] += 1


_state = _load_state()


class Handler(BaseHTTPRequestHandler):
    server_version = "SyncFlow/1.0"

    def log_message(self, fmt, *args):  # keep container logs quiet
        pass

    def _send(self, code, body, content_type="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    # -- routes ------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in (BASE_PATH, BASE_PATH + "/"):
            page = INDEX_HTML.replace("__BASE_PATH__", BASE_PATH).encode()
            self._send(200, page, "text/html; charset=utf-8")
        elif path == BASE_PATH + "/health":
            self._send(200, {"status": "ok", "service": "syncflow"})
        elif path == BASE_PATH + "/api/profile":
            with _lock:
                body = {"profile": dict(_state["profile"]), "version": _state["version"]}
            self._send(200, body)
        elif path == BASE_PATH + "/api/events":
            with _lock:
                body = {"events": list(_state["events"])}
            self._send(200, body)
        elif path == BASE_PATH + "/api/deltas":
            with _lock:
                body = {"deltas": list(_state["deltas"])}
            self._send(200, body)
        else:
            self._send(404, {"error": "not found"})

    do_HEAD = do_GET

    def do_POST(self):
        global _state
        path = self.path.split("?", 1)[0]
        if path == BASE_PATH + "/api/profile/update":
            self._update_profile()
        elif path == BASE_PATH + "/api/reset":
            with _lock:
                _state = _seed_state()
                _save_state(_state)
                body = {"profile": dict(_state["profile"]), "version": _state["version"]}
            self._send(200, body)
        else:
            self._send(404, {"error": "not found"})

    # -- the sync engine ----------------------------------------------

    def _update_profile(self):
        data = self._read_json()
        if data is None:
            return self._send(400, {"error": "invalid JSON body"})
        base_version = data.get("base_version")
        changes = data.get("changes")
        if not isinstance(base_version, int) or isinstance(base_version, bool):
            return self._send(400, {"error": "base_version must be an integer"})
        if not isinstance(changes, dict) or not changes:
            return self._send(400, {"error": "changes must be a non-empty object"})
        unknown = [f for f in changes if f not in PROFILE_FIELDS]
        if unknown:
            return self._send(400, {"error": "unknown profile fields", "fields": unknown})
        if any(not isinstance(v, str) for v in changes.values()):
            return self._send(400, {"error": "change values must be strings"})

        with _lock:
            current = _state["version"]
            if base_version != current:
                _emit(
                    _state,
                    "CONFLICT_DETECTED",
                    current,
                    {
                        "your_base_version": base_version,
                        "fields_attempted": sorted(changes.keys()),
                    },
                )
                _save_state(_state)
                return self._send(
                    409,
                    {
                        "error": "version_conflict",
                        "server_profile": dict(_state["profile"]),
                        "server_version": current,
                        "your_base_version": base_version,
                    },
                )

            delta = {}
            for field, new_value in changes.items():
                old_value = _state["profile"][field]
                if new_value != old_value:
                    delta[field] = {"from": old_value, "to": new_value}

            if not delta:
                # Nothing actually changed: no version bump, no events -
                # the stream only records things that really happened.
                return self._send(
                    200,
                    {
                        "profile": dict(_state["profile"]),
                        "version": current,
                        "delta": {},
                    },
                )

            for field, change in delta.items():
                _state["profile"][field] = change["to"]
            _state["version"] = current + 1
            _state["deltas"].append(
                {
                    "from_version": current,
                    "to_version": current + 1,
                    "changes": delta,
                    "timestamp": _now(),
                }
            )
            fields = sorted(delta.keys())
            _emit(
                _state,
                "DELTA_CREATED",
                current + 1,
                {"fields": fields, "from_version": current, "to_version": current + 1},
            )
            _emit(_state, "PROFILE_UPDATED", current + 1, {"fields": fields})
            _save_state(_state)
            return self._send(
                200,
                {
                    "profile": dict(_state["profile"]),
                    "version": current + 1,
                    "delta": delta,
                },
            )


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
