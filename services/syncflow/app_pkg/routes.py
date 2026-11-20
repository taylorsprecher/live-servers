"""SyncFlow — route dispatch and the sync engine. Every route and
JSON shape of the original app.py, verbatim, plus the one allowed
addition: GET {BASE}/assets/fleet.css."""
import json
from http.server import BaseHTTPRequestHandler

from . import store
from .config import BASE_PATH
from .pages import FLEET_CSS, INDEX_HTML


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
        elif path == BASE_PATH + "/assets/fleet.css":
            self._send(200, FLEET_CSS, "text/css; charset=utf-8")
        elif path == BASE_PATH + "/api/profile":
            with store.lock:
                state = store.current()
                body = {"profile": dict(state["profile"]), "version": state["version"]}
            self._send(200, body)
        elif path == BASE_PATH + "/api/events":
            with store.lock:
                body = {"events": list(store.current()["events"])}
            self._send(200, body)
        elif path == BASE_PATH + "/api/deltas":
            with store.lock:
                body = {"deltas": list(store.current()["deltas"])}
            self._send(200, body)
        else:
            self._send(404, {"error": "not found"})

    do_HEAD = do_GET

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == BASE_PATH + "/api/profile/update":
            self._update_profile()
        elif path == BASE_PATH + "/api/reset":
            with store.lock:
                state = store.reset()
                body = {"profile": dict(state["profile"]), "version": state["version"]}
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
        unknown = [f for f in changes if f not in store.PROFILE_FIELDS]
        if unknown:
            return self._send(400, {"error": "unknown profile fields", "fields": unknown})
        if any(not isinstance(v, str) for v in changes.values()):
            return self._send(400, {"error": "change values must be strings"})

        with store.lock:
            state = store.current()
            current = state["version"]
            if base_version != current:
                store.emit(
                    state,
                    "CONFLICT_DETECTED",
                    current,
                    {
                        "your_base_version": base_version,
                        "fields_attempted": sorted(changes.keys()),
                    },
                )
                store.save()
                return self._send(
                    409,
                    {
                        "error": "version_conflict",
                        "server_profile": dict(state["profile"]),
                        "server_version": current,
                        "your_base_version": base_version,
                    },
                )

            delta = {}
            for field, new_value in changes.items():
                old_value = state["profile"][field]
                if new_value != old_value:
                    delta[field] = {"from": old_value, "to": new_value}

            if not delta:
                # Nothing actually changed: no version bump, no events -
                # the stream only records things that really happened.
                return self._send(
                    200,
                    {
                        "profile": dict(state["profile"]),
                        "version": current,
                        "delta": {},
                    },
                )

            for field, change in delta.items():
                state["profile"][field] = change["to"]
            state["version"] = current + 1
            state["deltas"].append(
                {
                    "from_version": current,
                    "to_version": current + 1,
                    "changes": delta,
                    "timestamp": store.now(),
                }
            )
            fields = sorted(delta.keys())
            store.emit(
                state,
                "DELTA_CREATED",
                current + 1,
                {"fields": fields, "from_version": current, "to_version": current + 1},
            )
            store.emit(state, "PROFILE_UPDATED", current + 1, {"fields": fields})
            store.save()
            return self._send(
                200,
                {
                    "profile": dict(state["profile"]),
                    "version": current + 1,
                    "delta": delta,
                },
            )
