"""Data layer — SQLite store, settings, content staging, uploads,
and the websites data feed. Schema statements and queries verbatim."""
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import zipfile

from .catalog import (
    COLLECTION_BY_KEY,
    COURSES,
)
from .config import (
    BTC_RATE_URL,
    CONTENT_DIR,
    DB_PATH,
    OLE_MAGIC,
    UPLOADS_DIR,
    ZIP_MAGICS,
)


_lock = threading.Lock()


_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row


def init_db():
    os.makedirs(CONTENT_DIR, exist_ok=True)
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    with _lock, _db:
        _db.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                pw_salt TEXT NOT NULL,
                pw_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                member_id INTEGER NOT NULL REFERENCES members(id),
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS access (
                member_id INTEGER NOT NULL REFERENCES members(id),
                course_key TEXT NOT NULL,
                PRIMARY KEY (member_id, course_key)
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL,
                course_key TEXT NOT NULL,
                external_id TEXT UNIQUE,
                external_event_id TEXT,
                amount_cents INTEGER,
                currency TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS manual_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL,
                course_key TEXT NOT NULL,
                method TEXT NOT NULL,
                usd_cents INTEGER NOT NULL,
                amount_text TEXT NOT NULL,
                detail TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                confirmed_at INTEGER,
                confirmed_by TEXT
            );
            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_email TEXT NOT NULL,
                course_key TEXT NOT NULL,
                filename TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS member_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL REFERENCES members(id),
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )


def _setting(key):
    """One row of the settings table (owner-saved site configuration)."""
    try:
        with _lock:
            row = _db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else ""
    except Exception:
        return ""


def _set_setting(key, value):
    with _lock, _db:
        if value:
            _db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        else:
            _db.execute("DELETE FROM settings WHERE key = ?", (key,))


def godaddy_link(course_key):
    """The GoDaddy Pay Link the owner saved for a product on the
    owner Pay Links page, or "" — card payments run on these links
    (Taylor, 2026-11-15: "Use GoDaddy Pay Links for cards"), so the
    site needs no processor keys of its own for the card rail."""
    return _setting(f"godaddy_link_{course_key}")


def any_godaddy_link():
    return any(godaddy_link(c["key"]) for c in COURSES)


def member_access(member_id):
    with _lock:
        rows = _db.execute(
            "SELECT course_key FROM access WHERE member_id = ?", (member_id,)
        ).fetchall()
    return {r["course_key"] for r in rows}


def grant_access(member_id, course_key):
    with _lock, _db:
        _db.execute(
            "INSERT OR IGNORE INTO access (member_id, course_key) VALUES (?, ?)",
            (member_id, course_key),
        )


def member_upload_dir(member_id):
    """Per-member inbox directory on the volume (never public)."""
    return os.path.join(UPLOADS_DIR, str(member_id))


def member_uploads(member_id):
    with _lock:
        return _db.execute(
            "SELECT * FROM member_uploads WHERE member_id = ? "
            "ORDER BY created_at DESC",
            (member_id,),
        ).fetchall()


def all_member_uploads():
    """Every member upload, newest first, with the uploader's identity
    (owner view)."""
    with _lock:
        return _db.execute(
            "SELECT u.*, m.name AS member_name, m.email AS member_email "
            "FROM member_uploads u JOIN members m ON m.id = u.member_id "
            "ORDER BY u.created_at DESC"
        ).fetchall()


def member_upload_magic_ok(filename, data):
    """Sniff that the bytes match the document type the name claims."""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        return data.startswith(b"%PDF-")
    if ext == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if ext in (".zip", ".docx", ".xlsx"):
        return any(data.startswith(magic) for magic in ZIP_MAGICS)
    if ext in (".doc", ".xls"):
        return data.startswith(OLE_MAGIC)
    return False


def product_dir(course_key):
    return os.path.join(CONTENT_DIR, course_key)


def staged_files(course_key):
    """Zip files staged for a product: [(filename, size, mtime), ...]."""
    out = []
    d = product_dir(course_key)
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            if os.path.isfile(path) and name.lower().endswith(".zip"):
                out.append((name, os.path.getsize(path), int(os.path.getmtime(path))))
    return out


def resolve_content_path(course):
    """The file a download for this product serves, or None.

    Order: the canonical bundle name inside CONTENT_DIR/<product>/, then
    the newest zip staged there, then the legacy bundle at
    CONTENT_DIR/<bundle file> (pre-upload-path staging location).
    """
    files = staged_files(course["key"])
    for name, _size, _mtime in files:
        if name == course["file"]:
            return os.path.join(product_dir(course["key"]), name)
    if files:
        newest = max(files, key=lambda f: f[2])
        return os.path.join(product_dir(course["key"]), newest[0])
    legacy = os.path.join(CONTENT_DIR, course["file"])
    if os.path.isfile(legacy):
        return legacy
    return None


def content_available(course):
    return resolve_content_path(course) is not None


def sanitize_filename(name, allowed_exts=(".zip",)):
    """Reduce an uploaded filename to a safe basename with an allowed
    extension, or None."""
    name = os.path.basename((name or "").strip().replace("\\", "/"))
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name or name.startswith("."):
        return None
    if os.path.splitext(name)[1].lower() not in allowed_exts:
        return None
    if len(name) > 120:
        return None
    return name


def collection_files(collection_key):
    """All files staged in a collection: [(filename, size, mtime), ...]."""
    out = []
    d = product_dir(collection_key)
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            if os.path.isfile(path) and not name.startswith("."):
                out.append((name, os.path.getsize(path), int(os.path.getmtime(path))))
    return out


def has_content_access(member_id, key):
    """Access check for products AND collections. Collections unlock via
    their linked course (teachback -> ccce; see COLLECTIONS note above)."""
    granted = member_access(member_id)
    if key in COLLECTION_BY_KEY:
        return COLLECTION_BY_KEY[key]["unlock_course"] in granted
    return key in granted


def parse_multipart(body, content_type):
    """Minimal stdlib multipart/form-data parser.

    Returns (fields, files): fields maps name -> str; files maps
    name -> (filename, bytes). Good enough for one browser form.
    """
    fields, files = {}, {}
    m = re.search(r"boundary=([^;]+)", content_type or "")
    if not m:
        return fields, files
    boundary = m.group(1).strip().strip('"').encode("utf-8")
    delimiter = b"--" + boundary
    for part in body.split(delimiter):
        if part in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if part == b"--":
            break
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = {}
        for line in head.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower().decode("utf-8", "replace")] = (
                    v.strip().decode("utf-8", "replace")
                )
        disp = headers.get("content-disposition", "")
        name_m = re.search(r'name="([^"]*)"', disp)
        if not name_m:
            continue
        name = name_m.group(1)
        file_m = re.search(r'filename="([^"]*)"', disp)
        if file_m is not None:
            files[name] = (file_m.group(1), data)
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


def btc_quote(usd_cents):
    """Return (btc_amount_str, rate) quoted from CoinGecko, or (None, None)."""
    try:
        req = urllib.request.Request(
            BTC_RATE_URL, headers={"User-Agent": "member-site/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            rate = float(json.loads(resp.read().decode("utf-8"))["bitcoin"]["usd"])
        btc = (usd_cents / 100.0) / rate
        return f"{btc:.8f}", rate
    except Exception:
        return None, None


APPS_DATA_CACHE = {"at": 0.0, "data": None}


APPS_DATA_LOCK = threading.Lock()


APPS_DATA_TTL = 120


def _live_check(url):
    """One honest reachability check: status, timing, page title."""
    started = time.time()
    out = {
        "checked_url": url,
        "reachable": False,
        "http_status": None,
        "response_ms": None,
        "page_title": None,
        "error": None,
    }
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "911transedu-apps-feed/1.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            out["http_status"] = resp.status
            out["reachable"] = 200 <= resp.status < 400
            body = resp.read(65536)
        match = re.search(rb"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        if match:
            out["page_title"] = (
                match.group(1).decode("utf-8", "replace").strip()[:200]
            )
    except Exception as exc:  # surfaced in the feed, never hidden
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    out["response_ms"] = int((time.time() - started) * 1000)
    return out


def websites_data():
    """All three websites, every field, as one JSON-ready record."""
    main_site = {
        "key": "main-site",
        "name": "911transedu.net — Main Site",
        "url": "https://911transedu.net",
        "domain": "911transedu.net",
        "role": "Main public website for Taylor's Transgender Patient Awareness, LLC",
        "platform": "GoDaddy Website Builder; GoDaddy is also the registrar and DNS host",
        "live_check": _live_check("https://911transedu.net"),
        "on_the_site": [
            "The company's main public website, built with GoDaddy's builder",
            "Kept as-is — owner decision 2026-11-13",
        ],
        "notes": "One of Taylor's three websites (owner record, 2026-11-13).",
    }
    training_site = {
        "key": "training-site",
        "name": "EMS Training Website",
        "url": "https://ems.911transedu.net",
        "public_link": "https://muse.ai/s/911transedu-net-website-xkk6xaxm0ib1ak/-hQ_ySKdwECtNsqmEyusrdqJDfrhiRX9-5oxqk2dnI4",
        "domain": "ems.911transedu.net",
        "role": "EMS training website — teach-backs, the Medical Wing, course information",
        "platform": "Muse web app, published at the public link; built and versioned by Jasmine",
        "version": "v251 (2026-11-15) — Medical Wing expansion",
        "sections_count": 233,
        "live_check": _live_check("https://ems.911transedu.net"),
        "on_the_site": [
            "The Medical Wing — hub gathering 47 medical sections in 5 rooms",
            "All Teachbacks — 20 teach-backs, 16 groups, 42 links",
            "Amazon Hub teach-back (Flex tag)",
            "TTY Dictionary — connection terms & how data flows, per app",
            "OHA CCCE approval letter — Transgender Emergency Medicine, approved through August 5, 2029",
            "2019 Field Reference Guide sections",
            "Buy buttons point at the member site: CCCE course $15, EMSPR protocols $6,000",
        ],
        "notes": "New versions are published automatically (owner rule, 2026-11-08). The ems.911transedu.net address can lag the public link right after an update.",
    }
    member_server = {
        "key": "member-server",
        "name": "Member Server",
        "url": "https://members.911transedu.net",
        "domain": "members.911transedu.net",
        "role": "Member accounts, course sales, company portals, files library, apps hub, and the live-server gateway",
        "platform": "Taylor's own server — AWS EC2 host running the live-servers suite behind an nginx gateway on port 443",
        "live_check": {
            "source": "self — this server is answering the pull",
            "reachable": True,
            "http_status": 200,
        },
        "on_the_site": [
            "Member Desk — accounts, courses, owner roster",
            "Buy pages — CCCE course and EMSPR protocols (Cash App)",
            "Department upload page",
            "Five company portals",
            "Files library",
            "Member & Server Apps hub (this app)",
            "SyncFlow demo",
        ],
        "products": [
            {
                "name": "Transgender Emergency Medicine — CCCE Course",
                "price": "$15",
                "cadence": "every two years, per provider",
                "buy_url": "https://members.911transedu.net/members/buy/ccce",
                "payment": "By credit/debit card through the product's GoDaddy Pay Link when the owner has saved one on the owner Pay Links page (buyer records the order on the site; owner confirms the payment in the roster), or Cash App to $taylorsprecher2026 — buyer puts their account email in the payment note; access unlocks when the owner confirms receipt in the roster.",
                "includes": "CCCE_Course_Files.zip",
            },
            {
                "name": "EMSPR Protocols",
                "price": "$6,000",
                "cadence": "every year",
                "buy_url": "https://members.911transedu.net/members/buy/emspr",
                "payment": "By credit/debit card through the product's GoDaddy Pay Link when the owner has saved one on the owner Pay Links page (buyer records the order on the site; owner confirms the payment in the roster), or Cash App to $taylorsprecher2026 — buyer puts their account email in the payment note; access unlocks when the owner confirms receipt in the roster.",
                "includes": "The full EMS protocol package; CCCE course files included",
            },
        ],
        "portals": [
            {"name": "Hillsboro Fire & Rescue", "url": "https://members.911transedu.net/portal/hillsboro"},
            {"name": "Woodburn Ambulance", "url": "https://members.911transedu.net/portal/woodburn"},
            {"name": "Monitor RFPD", "url": "https://members.911transedu.net/portal/monitor"},
            {"name": "OHA EMS", "url": "https://members.911transedu.net/portal/oha"},
            {"name": "State of Maine", "url": "https://members.911transedu.net/portal/maine"},
        ],
        "files_library": {
            "url": "https://members.911transedu.net/files/",
            "entries_listed": 1088,
            "downloadable_files": 747,
            "downloadable_size": "214.4 MB",
            "on_file_available_on_request": 255,
            "held_name_only_no_download": 66,
            "counts_as_of": "2026-11-13",
            "held_note": "Held entries are paid course material, credential/SENSITIVE items, and other people's private records — listed by name only, never downloadable",
        },
        "apps_hub": {
            "url": "https://members.911transedu.net/members/apps",
            "data_feed": "https://members.911transedu.net/members/apps/data",
        },
        "notes": "Sales run on this server only (owner decision, 2026-11-12). Cards run through GoDaddy Pay Links (owner's choice, 2026-11-15): one link per product saved on the owner Pay Links page, buyers record an order on the site, unlock confirmed in the roster; Pay Links are one-time per period on each product's cadence. Cash App to $taylorsprecher2026 remains the no-fee option.",
    }
    live_servers = [
        {"name": "MacLAN", "url": "https://maclan.911transedu.net", "gateway_path": "/maclan/", "purpose": "Dashboard for the Macs and the home network", "access": "Public page"},
        {"name": "Xbox Cloud Saves", "url": "https://xbox.911transedu.net", "gateway_path": "/xbox/", "purpose": "Back up and restore Xbox save files through the cloud", "access": "Public page"},
        {"name": "Wyze Bridge", "url": "https://wyze.911transedu.net", "gateway_path": "/wyze/", "purpose": "Camera bridge status page — a status/dashboard demo; the real bridge needs a home-LAN host", "access": "Public page"},
        {"name": "USB-SOVEREIGN", "url": "https://usb.911transedu.net", "gateway_path": "/usb/", "purpose": "Serial bridge control for USB devices, Python-powered", "access": "Public page"},
        {"name": "AirCore", "url": "https://aircore.911transedu.net", "gateway_path": "/aircore/", "purpose": "The BlackBerry LAN bridge", "access": "Public page"},
        {"name": "FMP-1.0", "url": "https://fmp.911transedu.net", "gateway_path": "/fmp/", "purpose": "Forensic Motion Protocol — motion capture and review", "access": "Public page"},
        {"name": "Cell Tower LAN", "url": "https://celltower.911transedu.net", "gateway_path": "/celltower/", "purpose": "Cell tower and LAN signal watch; Dashboard 2 at /celltower/dashboard2.html", "access": "Public page"},
        {"name": "Spec Matrix", "url": "https://specmatrix.911transedu.net", "gateway_path": "/specmatrix/", "purpose": "Every live server's spec sheet, side by side", "access": "Public page"},
        {"name": "SyncFlow", "url": "https://members.911transedu.net/syncflow/", "gateway_path": "/syncflow/", "purpose": "SyncFlow demo", "access": "Public page — demo data only"},
        {"name": "Personal proxy", "url": None, "gateway_path": "/proxy (via the specmatrix vhost)", "purpose": "Shadowsocks proxy; clients exit with the server's address", "access": "Private — share link held by the owner only, never published"},
        {"name": "WireGuard VPN", "url": None, "endpoint": "52.41.84.116, UDP 51820 (published directly on the host)", "purpose": "Phone VPN — full tunnel; peers: taylor (owner's phone) and phone2–phone5, reserved for 911app downloaders, one config each", "access": "Config issued per person by the owner; key material is never a field in this feed"},
    ]
    return {
        "feed": "911transedu websites — all fields",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "owner_local_timezone": "America/Los_Angeles",
        "websites": [main_site, training_site, member_server],
        "live_servers": live_servers,
        "pull": {
            "this_feed_json": "https://members.911transedu.net/members/apps/data",
            "apps_hub": "https://members.911transedu.net/members/apps",
        },
    }


def apps_data():
    """The websites feed, cached briefly so pulls stay cheap."""
    now = time.time()
    with APPS_DATA_LOCK:
        cached = APPS_DATA_CACHE["data"]
        if cached is not None and now - APPS_DATA_CACHE["at"] < APPS_DATA_TTL:
            return cached
    data = websites_data()
    with APPS_DATA_LOCK:
        APPS_DATA_CACHE["at"] = time.time()
        APPS_DATA_CACHE["data"] = data
    return data
