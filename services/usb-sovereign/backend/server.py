"""Asyncio HTTP + WebSocket server for the USB-SOVEREIGN dashboard.

Standard library only. Serves:

* ``GET /api/status``         — JSON snapshot from UnifiedSerialController
* ``GET /api/events``         — recent controller events as JSON
* ``GET /api/serial-events``  — local serial_events store stats + records
* ``GET /ws``                 — WebSocket push of the status snapshot
                                (RFC 6455, ~2 Hz) — Increment 2 replaces
                                the dashboard's 1 s polling with push;
                                polling remains as a client-side fallback
* ``GET /`` and static files from the frontend/ directory

Base path: when the ``BASE_PATH`` env var is set (e.g. ``/usb``), the app
is mounted under that prefix — a gateway in front forwards requests WITH
the prefix, so the server strips it before routing (page, API and the
``/ws`` WebSocket alike) and answers 404 for anything outside the prefix.
With ``BASE_PATH`` unset the app is served at the root, exactly as before.

Auth hardening for the push channel is noted in the Build Brief as a later
increment; the server binds localhost by default.
"""

import asyncio
import base64
import hashlib
import json
import os
import struct

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_PUSH_INTERVAL_S = 0.5


def _ws_accept(key: str) -> str:
    digest = hashlib.sha1((key + WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def _ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    head = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    return bytes(head) + payload


def normalize_base_path(value: str) -> str:
    """Normalize a mount prefix: '' stays '', '/usb/' -> '/usb'."""
    value = (value or "").strip()
    if not value or value == "/":
        return ""
    if not value.startswith("/"):
        value = "/" + value
    return value.rstrip("/")


class StatusServer:
    def __init__(self, controller, frontend_dir: str,
                 host: str = "127.0.0.1", port: int = 8765,
                 base_path: str = None):
        self.controller = controller
        self.frontend_dir = os.path.abspath(frontend_dir)
        self.host = host
        self.port = port
        if base_path is None:
            base_path = os.environ.get("BASE_PATH", "")
        self.base_path = normalize_base_path(base_path)
        self._server = None
        self._ws_clients = set()
        self._broadcast_task = None

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port)
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        shown = self.base_path or "(root)"
        print(f"[HTTP] Dashboard + API + WS on "
              f"http://{self.host}:{self.port}{self.base_path}/ "
              f"[base path: {shown}]")

    async def close(self):
        if self._broadcast_task is not None:
            self._broadcast_task.cancel()
        for writer in list(self._ws_clients):
            try:
                writer.close()
            except Exception:
                pass
        self._ws_clients.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ---------------- WebSocket push ----------------

    async def _broadcast_loop(self):
        while True:
            await asyncio.sleep(WS_PUSH_INTERVAL_S)
            if not self._ws_clients:
                continue
            try:
                payload = json.dumps(
                    self.controller.status_snapshot()).encode()
            except Exception:
                continue
            frame = _ws_frame(payload)
            dead = []
            for writer in list(self._ws_clients):
                try:
                    writer.write(frame)
                    await writer.drain()
                except Exception:
                    dead.append(writer)
            for writer in dead:
                self._ws_clients.discard(writer)
                try:
                    writer.close()
                except Exception:
                    pass

    async def _ws_session(self, reader, writer, headers):
        key = headers.get("sec-websocket-key", "")
        if not key:
            await self._reply(writer, 400, "text/plain",
                              b"missing Sec-WebSocket-Key")
            return
        accept = _ws_accept(key)
        writer.write((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        self._ws_clients.add(writer)
        try:
            while True:
                opcode, payload = await self._ws_read_frame(reader)
                if opcode == 0x8:          # close
                    try:
                        writer.write(_ws_frame(b"", opcode=0x8))
                        await writer.drain()
                    except Exception:
                        pass
                    break
                if opcode == 0x9:          # ping -> pong
                    writer.write(_ws_frame(payload, opcode=0xA))
                    await writer.drain()
                # text/binary application messages are ignored in this
                # increment (push-only channel).
        except (asyncio.IncompleteReadError, ConnectionError,
                asyncio.TimeoutError):
            pass
        finally:
            self._ws_clients.discard(writer)

    @staticmethod
    async def _ws_read_frame(reader):
        head = await asyncio.wait_for(reader.readexactly(2), 120)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(
                ">H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack(
                ">Q", await reader.readexactly(8))[0]
        mask = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    # ---------------- HTTP ----------------

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), 5)
            if not request_line:
                return
            parts = request_line.decode("latin-1").split()
            path = parts[1] if len(parts) >= 2 else "/"
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), 5)
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            path = path.split("?", 1)[0]
            if self.base_path:
                # Mounted under a prefix: the gateway forwards requests
                # WITH the prefix, so strip it before routing. Anything
                # outside the prefix is not this app.
                if path == self.base_path:
                    path = "/"
                elif path.startswith(self.base_path + "/"):
                    path = path[len(self.base_path):]
                else:
                    await self._reply(writer, 404, "text/plain",
                                      b"not found")
                    return
            if path == "/ws" and \
                    headers.get("upgrade", "").lower() == "websocket":
                await self._ws_session(reader, writer, headers)
                return
            if path == "/api/status":
                body = json.dumps(self.controller.status_snapshot()).encode()
                await self._reply(writer, 200, "application/json", body)
            elif path == "/api/events":
                body = json.dumps(
                    {"events": self.controller.events.recent(100)}).encode()
                await self._reply(writer, 200, "application/json", body)
            elif path == "/api/serial-events":
                store = getattr(self.controller, "store", None)
                if store is None:
                    body = json.dumps({"enabled": False}).encode()
                else:
                    body = json.dumps({
                        "enabled": True,
                        "stats": store.stats(),
                        "recent": store.recent(50),
                    }).encode()
                await self._reply(writer, 200, "application/json", body)
            else:
                await self._serve_static(writer, path)
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _serve_static(self, writer, path: str):
        if path in ("/", ""):
            path = "/index.html"
        # Only plain basenames — no traversal outside frontend_dir.
        name = os.path.basename(path)
        full = os.path.join(self.frontend_dir, name)
        if not os.path.isfile(full):
            await self._reply(writer, 404, "text/plain", b"not found")
            return
        ext = os.path.splitext(name)[1].lower()
        with open(full, "rb") as fh:
            body = fh.read()
        await self._reply(writer, 200, MIME.get(ext, "application/octet-stream"),
                          body)

    @staticmethod
    async def _reply(writer, code: int, content_type: str, body: bytes):
        reason = {200: "OK", 400: "Bad Request",
                  404: "Not Found"}.get(code, "OK")
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(head + body)
        await writer.drain()
