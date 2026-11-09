"""
MacLAN backend — local development build.

This is the Phase 1 / early Phase 4 ("1ZilTBCloud") skeleton from the
MacLAN1ZilTB roadmap, running locally:

- FastAPI app with devices, network status/config, telemetry endpoints.
- SQLite storage (spec's PostgreSQL schema arrives with the real
  1ZilTBCloud phase; the tables here mirror that schema's shape).
- Device state is *simulated* locally, seeded from the spec's prototype
  device list. No real Bluetooth/LAN hardware is touched.

Increment 2 (Phase 1 completion):
- TCP/IP configuration state: DHCP vs manual addressing, persisted.
- Ethernet cable plug/unplug simulation driving interface + summary state.
- Bluetooth radio settings: power, discoverability, local name; turning
  the radio off drops connected Bluetooth devices and blocks pairing.
- Bandwidth telemetry: a Server-Sent Events stream that generates,
  persists, and pushes one throughput sample per second.
"""

import asyncio
import ipaddress
import json
import os
import random
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Public path prefix the app is mounted under (e.g. "/maclan" when a
# gateway forwards http://host/maclan/* to this service, keeping the
# prefix in the request path). Empty for root hosting. FastAPI's
# root_path makes routing ignore the prefix and URL generation include
# it. See DEPLOY.md.
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")

DB_PATH = Path(
    os.environ.get(
        "MACLAN_DB_PATH",
        Path(__file__).resolve().parent.parent / "maclan.db",
    )
)
VERSION = "0.2.0"

app = FastAPI(
    title="MacLAN / 1ZilTBCloud (local dev)", version=VERSION, root_path=BASE_PATH
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Seed data — device inventory from the MacLAN spec's Phase 1 prototype,
# extended with LAN devices for the device table.
# ---------------------------------------------------------------------------
SEED_DEVICES = [
    # name, kind, role, mac, ip, state, battery
    ("1ZilTB Phone Connection", "bluetooth", "PAN Host", "A4:83:E7:1Z:1B:01", None, "paired", None),
    ("AirPods Max", "bluetooth", "Audio", "AC:BC:32:9F:44:10", None, "connected", 88),
    ("Keychron K2 Keyboard", "bluetooth", "HID Keyboard", "DC:2C:26:0B:77:5A", None, "paired", 64),
    ("MX Master 3S", "bluetooth", "HID Mouse", "C9:11:8E:3D:02:9C", None, "unpaired", None),
    ("CenturyLink Router", "lan", "Gateway", "00:11:22:33:44:01", "192.168.0.1", "connected", None),
    ("Apple TV — T Room", "lan", "Media / Subnet Router", "00:11:22:33:44:10", "192.168.0.10", "connected", None),
    ("This Mac (MacLAN host)", "lan", "Host", "00:11:22:33:44:20", "192.168.0.42", "connected", None),
]

SEED_INTERFACES = [
    # name, kind, status, detail
    ("USB Ethernet", "ethernet", "connected", "192.168.0.42 · 1 Gbps"),
    ("Bluetooth PAN", "bluetooth", "connected", "1ZilTB Phone Connection · Network Available"),
    ("Wi-Fi", "wifi", "connected", "CenturyLink6497 · WPA3"),
    ("Thunderbolt Bridge", "thunderbolt", "inactive", "No cable detected"),
]

DHCP_DEFAULTS = {
    "subnet_mask": "255.255.255.0",
    "router": "192.168.0.1",
    "dns": ["192.168.0.1"],
}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                role TEXT NOT NULL,
                mac TEXT,
                ip TEXT,
                state TEXT NOT NULL DEFAULT 'unpaired',
                battery INTEGER,
                last_seen REAL
            );
            CREATE TABLE IF NOT EXISTS interfaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                source TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL,
                payload TEXT
            );
            CREATE TABLE IF NOT EXISTS network_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                tcpip_mode TEXT NOT NULL DEFAULT 'dhcp',
                manual_ip TEXT,
                manual_mask TEXT,
                manual_router TEXT,
                manual_dns TEXT,
                cable_connected INTEGER NOT NULL DEFAULT 1,
                dhcp_ip TEXT NOT NULL DEFAULT '192.168.0.42',
                dhcp_lease_ts REAL,
                bt_power INTEGER NOT NULL DEFAULT 1,
                bt_discoverable INTEGER NOT NULL DEFAULT 1,
                bt_name TEXT NOT NULL DEFAULT 'MacLAN Host'
            );
            """
        )
        if conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO devices (name, kind, role, mac, ip, state, battery, last_seen)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [(*d, time.time()) for d in SEED_DEVICES],
            )
        if conn.execute("SELECT COUNT(*) FROM interfaces").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO interfaces (name, kind, status, detail) VALUES (?,?,?,?)",
                SEED_INTERFACES,
            )
        if conn.execute("SELECT COUNT(*) FROM network_config").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO network_config (id, dhcp_lease_ts) VALUES (1, ?)",
                (time.time(),),
            )
        conn.commit()


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def get_config(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM network_config WHERE id=1").fetchone()
    cfg = dict(row)
    cfg["cable_connected"] = bool(cfg["cable_connected"])
    cfg["bt_power"] = bool(cfg["bt_power"])
    cfg["bt_discoverable"] = bool(cfg["bt_discoverable"])
    cfg["manual_dns"] = json.loads(cfg["manual_dns"]) if cfg["manual_dns"] else []
    return cfg


def effective_network(cfg: dict) -> dict:
    """The addressing the primary (Ethernet) interface effectively has."""
    if not cfg["cable_connected"]:
        return {
            "ip_address": None,
            "subnet_mask": None,
            "router": None,
            "dns": [],
            "link_speed_mbps": 0,
            "state": "cable_unplugged",
        }
    if cfg["tcpip_mode"] == "manual":
        return {
            "ip_address": cfg["manual_ip"],
            "subnet_mask": cfg["manual_mask"],
            "router": cfg["manual_router"],
            "dns": cfg["manual_dns"],
            "link_speed_mbps": 1000,
            "state": "connected",
        }
    return {
        "ip_address": cfg["dhcp_ip"],
        "subnet_mask": DHCP_DEFAULTS["subnet_mask"],
        "router": DHCP_DEFAULTS["router"],
        "dns": DHCP_DEFAULTS["dns"],
        "link_speed_mbps": 1000,
        "state": "connected",
    }


def config_view(cfg: dict) -> dict:
    return {
        "tcpip": {
            "mode": cfg["tcpip_mode"],
            "manual": {
                "ip_address": cfg["manual_ip"],
                "subnet_mask": cfg["manual_mask"],
                "router": cfg["manual_router"],
                "dns": cfg["manual_dns"],
            },
            "dhcp_lease": {
                "ip_address": cfg["dhcp_ip"],
                "leased_at": cfg["dhcp_lease_ts"],
            },
        },
        "cable_connected": cfg["cable_connected"],
        "bluetooth": {
            "power": cfg["bt_power"],
            "discoverable": cfg["bt_discoverable"],
            "name": cfg["bt_name"],
        },
        "effective": effective_network(cfg),
    }


def log_event(conn: sqlite3.Connection, source: str, metric: str,
              value: float | None = None, payload: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO telemetry (ts, source, metric, value, payload) VALUES (?,?,?,?,?)",
        (time.time(), source, metric, value,
         json.dumps(payload) if payload else None),
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class TelemetryIn(BaseModel):
    source: str
    metric: str
    value: float | None = None
    payload: dict | None = None


class PingIn(BaseModel):
    target: str = "192.168.0.1"
    count: int = 4


class TcpIpIn(BaseModel):
    mode: str
    ip_address: str | None = None
    subnet_mask: str | None = None
    router: str | None = None
    dns: list[str] | None = None


class CableIn(BaseModel):
    connected: bool


class BluetoothIn(BaseModel):
    power: bool | None = None
    discoverable: bool | None = None
    name: str | None = None


def _check_ip(value: str | None, field: str) -> None:
    if value is None:
        return
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        raise HTTPException(422, f"'{field}' is not a valid IPv4 address: {value}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "maclan-backend",
        "version": VERSION,
        "base_path": BASE_PATH,
    }


@app.get("/api/network/status")
def network_status():
    with closing(db()) as conn:
        cfg = get_config(conn)
        ifaces = [dict(r) for r in conn.execute("SELECT * FROM interfaces ORDER BY id")]
        dev_count = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        connected = conn.execute(
            "SELECT COUNT(*) FROM devices WHERE state='connected'"
        ).fetchone()[0]
    eff = effective_network(cfg)
    # Interface rows for Ethernet / Bluetooth PAN are derived live from the
    # simulated hardware state (cable, radio) rather than the seed detail.
    for iface in ifaces:
        if iface["kind"] == "ethernet":
            if cfg["cable_connected"]:
                iface["status"] = "connected"
                iface["detail"] = f"{eff['ip_address']} · 1 Gbps · {cfg['tcpip_mode'].upper()}"
            else:
                iface["status"] = "inactive"
                iface["detail"] = "Cable unplugged"
        elif iface["kind"] == "bluetooth":
            if not cfg["bt_power"]:
                iface["status"] = "inactive"
                iface["detail"] = "Bluetooth is off"
            elif not cfg["cable_connected"]:
                iface["status"] = "connected"
                iface["detail"] = "1ZilTB Phone Connection · Network Available"
    return {
        "interfaces": ifaces,
        "summary": {
            "primary_interface": "USB Ethernet",
            "ip_address": eff["ip_address"],
            "subnet_mask": eff["subnet_mask"],
            "router": eff["router"],
            "dns": eff["dns"],
            "tcpip_mode": cfg["tcpip_mode"],
            "cable_connected": cfg["cable_connected"],
            "bluetooth_power": cfg["bt_power"],
            "devices_total": dev_count,
            "devices_connected": connected,
            "link_speed_mbps": eff["link_speed_mbps"],
            "note": "Simulated local state — no live hardware polling yet.",
        },
    }


@app.get("/api/network/config")
def read_config():
    with closing(db()) as conn:
        return config_view(get_config(conn))


@app.put("/api/network/config")
def update_tcpip(body: TcpIpIn):
    """Set TCP/IP mode (dhcp/manual) and the manual addressing fields."""
    if body.mode not in ("dhcp", "manual"):
        raise HTTPException(422, "mode must be 'dhcp' or 'manual'")
    if body.mode == "manual":
        if not body.ip_address:
            raise HTTPException(422, "Manual mode requires an IP address")
        _check_ip(body.ip_address, "ip_address")
        _check_ip(body.subnet_mask, "subnet_mask")
        _check_ip(body.router, "router")
        for server in body.dns or []:
            _check_ip(server, "dns")
    with closing(db()) as conn:
        conn.execute(
            "UPDATE network_config SET tcpip_mode=?, manual_ip=?, manual_mask=?,"
            " manual_router=?, manual_dns=? WHERE id=1",
            (body.mode, body.ip_address, body.subnet_mask, body.router,
             json.dumps(body.dns or [])),
        )
        log_event(conn, "maclan-backend", "tcpip_config",
                  payload={"mode": body.mode, "ip_address": body.ip_address})
        conn.commit()
        return config_view(get_config(conn))


@app.post("/api/network/cable")
def set_cable(body: CableIn):
    """Plug / unplug the simulated Ethernet cable."""
    with closing(db()) as conn:
        conn.execute(
            "UPDATE network_config SET cable_connected=? WHERE id=1",
            (1 if body.connected else 0,),
        )
        log_event(conn, "maclan-backend",
                  "cable_connected" if body.connected else "cable_unplugged",
                  value=1 if body.connected else 0)
        conn.commit()
        return config_view(get_config(conn))


@app.post("/api/network/dhcp/renew")
def renew_dhcp_lease():
    """Simulated DHCP lease renewal — the server hands out a fresh address."""
    with closing(db()) as conn:
        cfg = get_config(conn)
        if cfg["tcpip_mode"] != "dhcp":
            raise HTTPException(409, "Not in DHCP mode")
        if not cfg["cable_connected"]:
            raise HTTPException(409, "Cable is unplugged — no DHCP server reachable")
        new_ip = f"192.168.0.{random.randint(20, 200)}"
        conn.execute(
            "UPDATE network_config SET dhcp_ip=?, dhcp_lease_ts=? WHERE id=1",
            (new_ip, time.time()),
        )
        log_event(conn, "maclan-backend", "dhcp_lease_renewed",
                  payload={"ip_address": new_ip})
        conn.commit()
        return config_view(get_config(conn))


@app.put("/api/bluetooth")
def update_bluetooth(body: BluetoothIn):
    """Bluetooth radio settings: power / discoverability / local name.

    Turning the radio off drops every connected Bluetooth device back to
    'paired', mirroring real macOS behaviour.
    """
    with closing(db()) as conn:
        cfg = get_config(conn)
        power = cfg["bt_power"] if body.power is None else body.power
        discoverable = cfg["bt_discoverable"] if body.discoverable is None else body.discoverable
        name = cfg["bt_name"] if body.name is None else body.name.strip()
        if not name:
            raise HTTPException(422, "Bluetooth name cannot be empty")
        conn.execute(
            "UPDATE network_config SET bt_power=?, bt_discoverable=?, bt_name=? WHERE id=1",
            (1 if power else 0, 1 if discoverable else 0, name),
        )
        if cfg["bt_power"] and not power:
            conn.execute(
                "UPDATE devices SET state='paired' WHERE kind='bluetooth' AND state='connected'"
            )
        log_event(conn, "maclan-backend", "bluetooth_settings",
                  payload={"power": power, "discoverable": discoverable, "name": name})
        conn.commit()
        return config_view(get_config(conn))


@app.get("/api/devices")
def list_devices():
    with closing(db()) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM devices ORDER BY id")]


@app.post("/api/devices/{device_id}/{action}")
def device_action(device_id: int, action: str):
    """Simulated state transitions: pair / connect / disconnect / unpair."""
    transitions = {
        "pair": ("unpaired", "paired"),
        "connect": ("paired", "connected"),
        "disconnect": ("connected", "paired"),
        "unpair": ("paired", "unpaired"),
    }
    if action not in transitions:
        raise HTTPException(404, f"Unknown action '{action}'")
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Device not found")
        if row["kind"] == "bluetooth" and action in ("pair", "connect"):
            if not get_config(conn)["bt_power"]:
                raise HTTPException(409, "Bluetooth is off — turn it on to pair or connect")
        expected, target = transitions[action]
        if row["state"] == target:
            return dict(row)
        if row["state"] != expected and not (action == "connect" and row["state"] == "connected"):
            raise HTTPException(
                409, f"Cannot {action} a device in state '{row['state']}'"
            )
        conn.execute(
            "UPDATE devices SET state=?, last_seen=? WHERE id=?",
            (target, time.time(), device_id),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone())


@app.post("/api/telemetry")
def ingest_telemetry(event: TelemetryIn):
    with closing(db()) as conn:
        cur = conn.execute(
            "INSERT INTO telemetry (ts, source, metric, value, payload) VALUES (?,?,?,?,?)",
            (time.time(), event.source, event.metric, event.value,
             json.dumps(event.payload) if event.payload else None),
        )
        conn.commit()
        return {"id": cur.lastrowid, "accepted": True}


@app.get("/api/telemetry")
def list_telemetry(limit: int = 50, metric: str | None = None):
    with closing(db()) as conn:
        if metric:
            rows = conn.execute(
                "SELECT * FROM telemetry WHERE metric=? ORDER BY id DESC LIMIT ?",
                (metric, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM telemetry ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Bandwidth simulation — a random-walk throughput model. One sample per
# second while a client is attached to the SSE stream; samples are also
# persisted as telemetry (metric='bandwidth') so the graph has history.
# ---------------------------------------------------------------------------
_bw_state = {"down": 120.0, "up": 18.0}


def _next_bandwidth_sample() -> dict:
    down = _bw_state["down"] + random.uniform(-45, 45)
    up = _bw_state["up"] + random.uniform(-6, 6)
    # Gentle pull back toward baseline; occasional burst.
    down += (140 - down) * 0.05
    up += (20 - up) * 0.05
    if random.random() < 0.06:
        down += random.uniform(150, 420)
    _bw_state["down"] = max(0.0, min(940.0, down))
    _bw_state["up"] = max(0.0, min(120.0, up))
    return {
        "ts": time.time(),
        "down_mbps": round(_bw_state["down"], 1),
        "up_mbps": round(_bw_state["up"], 1),
    }


@app.get("/api/telemetry/stream")
async def telemetry_stream(request: Request):
    """SSE stream of live bandwidth samples (1/sec), persisted to telemetry."""

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                sample = _next_bandwidth_sample()
                with closing(db()) as conn:
                    log_event(conn, "bandwidth-sim", "bandwidth",
                              value=sample["down_mbps"], payload=sample)
                    conn.commit()
                yield f"data: {json.dumps(sample)}\n\n"
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/diagnostics/ping")
def ping(req: PingIn):
    """Simulated ICMP ping (spec Phase 1 diagnostics)."""
    times = [round(random.uniform(0.4, 3.5), 2) for _ in range(req.count)]
    return {
        "target": req.target,
        "sent": req.count,
        "received": req.count,
        "loss_pct": 0.0,
        "times_ms": times,
        "avg_ms": round(sum(times) / len(times), 2),
        "simulated": True,
    }


# ---------------------------------------------------------------------------
# Production serving — the built React frontend is served by this app so a
# single container carries UI + API under one prefix: with BASE_PATH set,
# the gateway path /<prefix>/... reaches the static mount at "/" and the
# API routes alike. Mounted last so API routes keep priority; skipped
# when no build exists (Vite dev-server workflow).
# ---------------------------------------------------------------------------
FRONTEND_DIST = Path(
    os.environ.get(
        "FRONTEND_DIST",
        Path(__file__).resolve().parent.parent.parent / "frontend" / "dist",
    )
)

if FRONTEND_DIST.is_dir():
    app.mount(
        "/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend"
    )
