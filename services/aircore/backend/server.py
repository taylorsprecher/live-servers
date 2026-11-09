#!/usr/bin/env python3
"""
Demo server for the AirUI frontend.

    python3 server.py [port]        (default 8765)

Serves the frontend (../frontend/) and a small JSON API backed by a real
AirBridge + AirShield + AirCore VM instance:

    GET  /api/state            routes, policy summary, demo program source
    POST /api/send             {"action": "...", "source": optional asm}
                               -> AirBridge envelope (policy verdict + result)
    GET  /api/log              AirShield decision log + AirBridge message log

Environment:
    BASE_PATH   URL prefix the app is served under (default ""). Behind the
                hosting gateway the app lives at /aircore and requests arrive
                WITH that prefix, so routes only match under it.
    HOST        bind address (default 127.0.0.1; the container sets 0.0.0.0)
    PORT        listen port (default: argv[1], else 8765)

Standard library only. Demo harness — not the spec's transport.
"""

import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from airbridge import build_bridge  # noqa: E402

FRONTEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
BRIDGE = build_bridge()

# Serving prefix, e.g. "/aircore" (no trailing slash). Empty = served at root.
BASE_PATH = os.environ.get("BASE_PATH", "").strip().rstrip("/")
if BASE_PATH and not BASE_PATH.startswith("/"):
    BASE_PATH = "/" + BASE_PATH


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FRONTEND, **kwargs)

    def log_message(self, *args):  # keep the console quiet
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        """self.path with BASE_PATH stripped, or None if outside the prefix.

        Requests behind the gateway arrive WITH the prefix ("/aircore/..."),
        while routing and the static file handler work on root-relative
        paths ("/..."). Query strings are preserved.
        """
        path, sep, query = self.path.partition("?")
        if BASE_PATH:
            if path == BASE_PATH:
                path = "/"
            elif path.startswith(BASE_PATH + "/"):
                path = path[len(BASE_PATH):]
            else:
                return None
        return path + (sep + query if sep else "")

    def do_GET(self):
        routed = self._route()
        if routed is None:
            self._json({"error": "not found"}, status=404)
            return
        path = routed.partition("?")[0]
        if path == "/api/state":
            self._json({
                "routes": BRIDGE.routes,
                "permissions": BRIDGE.shield.policy.get("permissions", {}),
                "demo_source": BRIDGE.demo_source,
            })
        elif path == "/api/log":
            self._json({
                "shield_decisions": BRIDGE.shield.decision_log,
                "bridge_messages": BRIDGE.message_log,
            })
        else:
            self.path = routed  # static handler sees the root-relative path
            super().do_GET()

    def do_POST(self):
        routed = self._route()
        if routed is None or routed.partition("?")[0] != "/api/send":
            self._json({"error": "unknown endpoint"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            action = str(req.get("action", "execute"))
            payload = {}
            if req.get("source"):
                payload["source"] = str(req["source"])
            envelope = BRIDGE.send(BRIDGE.make_message(action, payload))
            self._json(envelope)
        except Exception as exc:  # surface VM/assembly errors to the UI
            self._json({"error": f"{type(exc).__name__}: {exc}"}, status=400)


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8765))
    where = f"http://{host}:{port}{BASE_PATH}/" if BASE_PATH else f"http://{host}:{port}"
    print(f"AirUI demo server on {where}  (Ctrl+C to stop)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
