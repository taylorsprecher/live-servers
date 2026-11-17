#!/usr/bin/env python3
"""BlackBerry LAN — the Air platform core from BlackBerry_LAN.pdf, as a service.

Implements the runnable core of the design document:
  - AirCore: a stack-based VM executing the document's opcode map
    (0x01 PUSH, 0x02 POP, 0x03 DUP, 0x04 SWAP, 0x10 ADD, 0x11 SUB, 0x12 MUL,
     0x13 DIV, 0x20 LOAD, 0x21 STORE, 0x30 JMP, 0x31 JZ, 0x40 CALL, 0x41 RET,
     0xFF HALT), step-limited, no network opcodes.
  - AirBridge: a local message bus with the document's route table
    {"diagnostics": "core.diagnostics"} plus an "echo" channel.
  - AirShield: the document's zero-trust policy
    {"aircore_exec": true, "network": false} — enforced: the VM rejects
    unknown opcodes and the bus serves only its routed channels.
  - Diagnostics in the document's shape (cpu / memory / sandbox). The QNX
    sandbox path is the design target; the live container path is reported
    alongside it.

The BlackBerry 10 / QNX handset layers in the document (Cascades/QML UI,
C++ bridge, signed .bar packaging) are design material for a device build;
they are not runnable on a Linux server and are not claimed here.

Python stdlib only. Runs under BASE_PATH (default /blackberry).
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.environ.get("BASE_PATH", "/blackberry").rstrip("/")
PORT = int(os.environ.get("PORT", "8771"))
STARTED = time.time()

QNX_SANDBOX = "/accounts/1000/appdata/app/native/aircore"
POLICY = {"aircore_exec": True, "network": False}
ROUTES = {"diagnostics": "core.diagnostics", "echo": "core.echo"}

OPCODES = {
    "PUSH": 0x01, "POP": 0x02, "DUP": 0x03, "SWAP": 0x04,
    "ADD": 0x10, "SUB": 0x11, "MUL": 0x12, "DIV": 0x13,
    "LOAD": 0x20, "STORE": 0x21, "JMP": 0x30, "JZ": 0x31,
    "CALL": 0x40, "RET": 0x41, "HALT": 0xFF,
}
STEP_LIMIT = 10000


class VMError(Exception):
    pass


def run_program(program):
    code = []
    for raw in program:
        parts = str(raw).strip().split()
        if not parts:
            continue
        op = parts[0].upper()
        if op not in OPCODES:
            raise VMError(f"AirShield: unknown opcode rejected: {op}")
        arg = None
        if len(parts) > 1:
            try:
                arg = int(parts[1])
            except ValueError:
                raise VMError(f"bad operand in instruction: {raw}")
        if op in ("PUSH", "LOAD", "STORE", "JMP", "JZ", "CALL") and arg is None:
            raise VMError(f"{op} requires an operand")
        code.append((op, arg))
    stack, memory, calls = [], {}, []
    pc, steps = 0, 0
    while 0 <= pc < len(code):
        steps += 1
        if steps > STEP_LIMIT:
            raise VMError(f"step limit {STEP_LIMIT} exceeded")
        op, arg = code[pc]
        pc += 1
        if op == "HALT":
            break
        elif op == "PUSH":
            stack.append(arg)
        elif op == "POP":
            if not stack:
                raise VMError("stack underflow on POP")
            stack.pop()
        elif op == "DUP":
            if not stack:
                raise VMError("stack underflow on DUP")
            stack.append(stack[-1])
        elif op == "SWAP":
            if len(stack) < 2:
                raise VMError("stack underflow on SWAP")
            stack[-1], stack[-2] = stack[-2], stack[-1]
        elif op in ("ADD", "SUB", "MUL", "DIV"):
            if len(stack) < 2:
                raise VMError(f"stack underflow on {op}")
            b, a = stack.pop(), stack.pop()
            if op == "ADD":
                stack.append(a + b)
            elif op == "SUB":
                stack.append(a - b)
            elif op == "MUL":
                stack.append(a * b)
            else:
                if b == 0:
                    raise VMError("division by zero")
                stack.append(int(a / b))
        elif op == "LOAD":
            stack.append(memory.get(arg, 0))
        elif op == "STORE":
            if not stack:
                raise VMError("stack underflow on STORE")
            memory[arg] = stack.pop()
        elif op == "JMP":
            pc = arg
        elif op == "JZ":
            if not stack:
                raise VMError("stack underflow on JZ")
            if stack.pop() == 0:
                pc = arg
        elif op == "CALL":
            calls.append(pc)
            pc = arg
        elif op == "RET":
            if not calls:
                raise VMError("RET with empty call stack")
            pc = calls.pop()
    return {"stack": stack, "memory": {str(k): v for k, v in memory.items()}, "steps": steps}


def diagnostics():
    mem = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith(("MemTotal:", "MemAvailable:")):
                    key, val = line.split(":", 1)
                    mem[key.strip()] = val.strip()
    except OSError:
        pass
    return {
        "cpu": os.cpu_count(),
        "memory": mem,
        "sandbox": QNX_SANDBOX,
        "sandbox_live": os.getcwd(),
        "uptime_seconds": round(time.time() - STARTED, 1),
    }


BUS = {channel: [] for channel in ROUTES}
MSG_ID = [0]


def bridge_send(channel, payload):
    if channel not in ROUTES:
        return None, f"AirShield: unrouted channel rejected: {channel}"
    if channel == "diagnostics":
        return {"route": ROUTES[channel], "result": diagnostics()}, None
    MSG_ID[0] += 1
    msg = {"id": MSG_ID[0], "channel": channel, "payload": payload,
           "route": ROUTES[channel], "ts": time.time()}
    BUS[channel].append(msg)
    return msg, None


FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (
            json.dumps(body).encode() if not isinstance(body, str) else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in (BASE, BASE + "/"):
            try:
                with open(os.path.join(FRONTEND, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(200, {"service": "blackberry-lan", "status": "ok"})
        if path == BASE + "/health":
            return self._send(200, {"status": "ok", "service": "blackberry-lan",
                                    "aircore": "ready", "airbridge": "ready",
                                    "airshield": POLICY})
        if path == BASE + "/diagnostics":
            return self._send(200, diagnostics())
        if path == BASE + "/shield":
            return self._send(200, {"policy": POLICY, "routes": ROUTES})
        if path == BASE + "/bridge/messages":
            from urllib.parse import parse_qs, urlparse
            channel = parse_qs(urlparse(self.path).query).get("channel", ["echo"])[0]
            if channel not in ROUTES:
                return self._send(403, {"error": f"AirShield: unrouted channel: {channel}"})
            return self._send(200, {"channel": channel, "messages": BUS[channel]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._body()
        if path == BASE + "/aircore/run":
            if not POLICY["aircore_exec"]:
                return self._send(403, {"error": "AirShield: aircore_exec disabled"})
            program = body.get("program")
            if not isinstance(program, list) or not program:
                return self._send(400, {"error": "program must be a non-empty list of instructions"})
            try:
                return self._send(200, run_program(program))
            except VMError as exc:
                return self._send(400, {"error": str(exc)})
        if path == BASE + "/bridge/send":
            msg, err = bridge_send(str(body.get("channel", "")), body.get("payload"))
            if err:
                return self._send(403, {"error": err})
            return self._send(200, msg)
        return self._send(404, {"error": "not found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
