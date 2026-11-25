#!/usr/bin/env python3
"""
Standalone dependency-free Stream Mic receiver & open-access server.
Run: python3 stream_receiver.py --host 0.0.0.0 --port 8080
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STORAGE_DIR = Path("stream_sessions")
FRONTEND_FILE = Path("index.html")
SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def ensure_storage():
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def list_sessions():
    ensure_storage()
    sessions = []
    for entry in sorted(STORAGE_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not entry.is_dir():
            continue
        chunks = sorted([f.name for f in entry.iterdir() if f.is_file() and not f.name.endswith(".json")])
        meta_file = entry / "meta.json"
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        sessions.append({
            "session_id": entry.name,
            "mode": meta.get("mode", "unknown"),
            "created_at": meta.get("created_at"),
            "chunk_count": len(chunks),
            "chunks": chunks,
        })
    return sessions


class OpenStreamHandler(BaseHTTPRequestHandler):
    def _set_cors_headers(self):
        # Open access to all origins, methods, and headers
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Session-Id, X-Stream-Mode, X-Chunk-Index")

    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self._set_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Serve frontend UI
        if path in ("/", "/index.html"):
            if not FRONTEND_FILE.exists():
                self._send_json(404, {"error": "index.html not found next to stream_receiver.py"})
                return
            content = FRONTEND_FILE.read_bytes()
            self.send_response(200)
            self._set_cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        # List all sessions (public access)
        if path == "/api/sessions":
            self._send_json(200, {"access": "public", "sessions": list_sessions()})
            return

        # Download/stream a specific chunk: /api/sessions/<session_id>/<filename>
        if path.startswith("/api/sessions/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4:
                _, _, session_id, filename = parts
                if not SAFE_ID_RE.match(session_id) or "/" in filename or ".." in filename:
                    self._send_json(400, {"error": "Invalid session or filename"})
                    return
                file_path = STORAGE_DIR / session_id / filename
                if not file_path.is_file():
                    self._send_json(404, {"error": "Chunk not found"})
                    return
                data = file_path.read_bytes()
                self.send_response(200)
                self._set_cors_headers()
                self.send_header("Content-Type", "video/webm" if filename.endswith(".webm") else "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/chunk":
            self._send_json(404, {"error": "Not found"})
            return

        qs = parse_qs(parsed.query)
        session_id = self.headers.get("X-Session-Id") or (qs.get("session_id", [None])[0])
        mode = self.headers.get("X-Stream-Mode") or (qs.get("mode", ["screen_mic"])[0])
        chunk_index = self.headers.get("X-Chunk-Index") or (qs.get("chunk", ["0"])[0])

        if not session_id or not SAFE_ID_RE.match(session_id):
            self._send_json(400, {"error": "Valid session_id required"})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            self._send_json(400, {"error": "Empty chunk payload"})
            return

        chunk_bytes = self.rfile.read(content_length)
        ensure_storage()
        session_dir = STORAGE_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        now_iso = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        meta_path = session_dir / "meta.json"
        if not meta_path.exists():
            meta_path.write_text(
                json.dumps({"session_id": session_id, "mode": mode, "created_at": now_iso, "access": "all"}, indent=2),
                encoding="utf-8",
            )

        idx_padded = str(chunk_index).zfill(5)
        filename = f"chunk_{idx_padded}_{now_iso}.webm"
        (session_dir / filename).write_bytes(chunk_bytes)

        saved_count = len([f for f in session_dir.iterdir() if f.is_file() and not f.name.endswith(".json")])
        self._send_json(200, {
            "ok": True,
            "session_id": session_id,
            "saved_file": filename,
            "chunk_count": saved_count,
            "timestamp": now_iso,
        })


def main():
    parser = argparse.ArgumentParser(description="Open-Access Stream Mic Receiver")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (0.0.0.0 grants LAN/public network access)")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    args = parser.parse_args()

    ensure_storage()
    server = ThreadingHTTPServer((args.host, args.port), OpenStreamHandler)
    print(f"Stream receiver running with OPEN ACCESS at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down receiver.")
        server.server_close()


if __name__ == "__main__":
    main()
