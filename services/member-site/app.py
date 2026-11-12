#!/usr/bin/env python3
"""Member-login training site — Taylor's Transgender Patient Awareness, LLC.

Python 3 stdlib ONLY (http.server, sqlite3, hashlib.pbkdf2_hmac, urllib for
the Stripe REST API). Real member accounts persist in SQLite on a volume.

Payments (on-site; no GoDaddy in this app):
  * Stripe Checkout (hosted page at checkout.stripe.com). Keys come from the
    host environment (STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET) — never
    hardcoded, never committed. Checkout uses the account's default payment
    methods (payment_method_types is NOT restricted), so card, Google Pay
    and Apple Pay surface when enabled in the Stripe Dashboard and the
    buyer's device supports them. Apple Card pays as an ordinary card /
    via Apple Pay. Payment unlocks automatically: the signature-verified
    webhook (POST /stripe/webhook, checkout.session.completed) grants the
    product to the paying member.
  * Bitcoin (separate path — Stripe does not process BTC): the app quotes
    BTC from the USD price (CoinGecko rate), creates a PENDING order, and
    instructs the buyer to send that BTC amount to Taylor's cashtag
    (CASHAPP_CASHTAG) INSIDE Cash App, with the buyer's account email in
    the payment note — the same manual pattern as Cash App cash. The
    owner confirms receipt in the roster; confirmation unlocks the
    product. There is NO on-chain deposit address: Taylor confirmed
    2026-11-12 that Cash App shows none ("There isn't one"), so Bitcoin
    is received through Cash App's own Bitcoin send to the cashtag and
    no address is ever displayed.
  * Cash App (same manual pattern as Bitcoin): the app creates a PENDING
    order showing the USD amount and Taylor's cashtag (CASHAPP_CASHTAG,
    default $taylorsprecher2026 — a public receive handle). The owner
    confirms receipt in the roster; confirmation unlocks the product.
    Manual orders (Bitcoin + Cash App) share one log: order, member,
    product, amount, timestamp, confirmation.

SALES DECISION (2026-11-12, Taylor: "Sales on our own site — buyers pay
through members.911transedu.net"): sales run fully through THIS site.
Stripe is NOT connected (no keys on the host), so the operative
purchase path is the manual Cash App flow; the Stripe checkout stays
wired in code but is never presented as available while
STRIPE_SECRET_KEY is unset. Each product has a stable public buy page
at /members/buy/<key> showing the price, the cashtag, the note
guidance (the buyer's account email), and the owner-confirmation
unlock — the public training site's buy buttons point at those pages.

STRIPE GO-LIVE PREP (2026-11-12, Taylor: "Add them via Stripe"):
Google Pay and Apple Pay are added through the Stripe Checkout path
above, alongside Cash App (which remains the no-fee option). The site
side is ready: automatic payment methods, HTTPS return URLs on
members.911transedu.net, wallet-named copy that renders only while
STRIPE_SECRET_KEY is set, and a key-delivery step in the members
deploy workflow that writes the GitHub Actions secrets
STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET into the host .env (never
committed, never logged). Until Taylor connects Stripe and those
secrets exist, behavior is exactly as described above. The full
checklist lives in STRIPE_GO_LIVE.md in this directory.

Content: full course files are served ONLY to signed-in members with
access, from CONTENT_DIR on the volume — never a public URL.

Course-file upload: POST /members/admin/upload (owner session only,
fail-closed 403 for everyone else). Accepts either a browser multipart
form (fields: product, file) or a raw zip body with the product named in
the ?product= query parameter (or X-Product header) and the filename in
X-Filename. Files are zip-only (magic bytes + zipfile check), capped at
MAX_UPLOAD_BYTES, and written to CONTENT_DIR/<product>/ on the volume —
never into the repo tree, never committed to git. Every upload is logged
(owner email, product, filename, bytes, timestamp) in the uploads table.
Re-uploading the same filename replaces it atomically. Downloads are
served from CONTENT_DIR/<product>/ (with a fallback to the legacy
CONTENT_DIR/<bundle file> location) only to signed-in members who hold
access for that product.

Member uploads (department inbox): any signed-in member can upload
documents at GET/POST /members/upload (anonymous users are redirected
to sign-in). Files are validated against a document allowlist
(pdf/doc/docx/xls/xlsx/png/jpg/jpeg/zip, magic-byte checked), capped
at MEMBER_UPLOAD_MAX_BYTES, filename-sanitized, and stored on the
volume under UPLOADS_DIR/<member_id>/ — never in CONTENT_DIR, never in
git, never at a public URL. Each member sees only their own uploads;
downloads (/members/upload/download/<id>) are served only to the
owning member or to the owner. The owner sees every member's uploads,
grouped by member, in the roster area of the member desk. Deleting an
account removes its upload rows and files.

Connection-permissions standard (Taylor, 2026-11-08): account creation
in Taylor's apps asks for Cellular, Wi-Fi (Local Network) and
Bluetooth up front. A web page cannot raise iOS native permission
prompts, so the signup flow and the upload page present this as an
explicit connection step/checklist naming all three, with live
browser-reported status only where the browser genuinely exposes it
(online status, connection type, Bluetooth availability) — never fake
system prompts.

Collections: the upload endpoint also accepts collection targets
(currently "teachback"). Collection files are individual documents
(.zip/.pdf/.md/.txt/.docx) staged under CONTENT_DIR/teachback/ and
served file-by-file at /members/content/teachback[/<filename>].
DESIGN DECISION (2026-11-11): the Teach-Back collection unlocks with
CCCE course access — no separate purchase/access row — so the owner's
roster grant/revoke of CCCE controls it. Teach-backs are never folded
into CCCE_Course_Files.zip; the bundle stays byte-identical.

Owner = member registered with OWNER_EMAIL; the roster (grant/revoke,
BTC confirm, payment log) is fail-closed to everyone else.
Sessions: random token, stored hashed, HttpOnly cookie, 30-day lifetime.
Passwords: salted PBKDF2-HMAC-SHA256, never plaintext.
"""
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.request
import urllib.error
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlencode

BASE = os.environ.get("BASE_PATH", "/members").rstrip("/")
DB_PATH = os.environ.get("MEMBERS_DB_PATH", "/data/members.db")
CONTENT_DIR = os.environ.get("CONTENT_DIR", "/data/content")
PORT = int(os.environ.get("PORT", "8000"))
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://members.911transedu.net"
).rstrip("/")
OWNER_EMAIL = "taylorsprecher@gmail.com"
SESSION_DAYS = 30
PBKDF2_ROUNDS = 200_000

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
# Bitcoin is received INSIDE Cash App at the cashtag below — there is no
# on-chain deposit address (Taylor, 2026-11-12: "There isn't one"), so no
# BTC address env var exists or is ever rendered.
CASHAPP_CASHTAG = os.environ.get("CASHAPP_CASHTAG") or "$taylorsprecher2026"
BTC_RATE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
BTC_RATE_SOURCE = "CoinGecko (api.coingecko.com simple/price, USD)"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB cap on course-file uploads
ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .doc/.xls container

# Member (department) uploads: a per-member inbox on the same volume,
# deliberately OUTSIDE CONTENT_DIR so member files can never be served
# as course content or reached by a public URL.
UPLOADS_DIR = os.environ.get("UPLOADS_DIR", "/data/uploads")
MEMBER_UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # 25 MB cap per member file
MEMBER_UPLOAD_EXTS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".png", ".jpg", ".jpeg", ".zip",
)
MEMBER_UPLOAD_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".zip": "application/zip",
}

COURSES = [
    {
        "key": "ccce",
        "name": "Cultural Competence Continuing Education: Transgender Emergency Medicine",
        "short": "CCCE Course",
        "price": "$15",
        "cents": 1500,
        "file": "CCCE_Course_Files.zip",
    },
    {
        "key": "emspr",
        "name": "EMSPR Protocols",
        "short": "EMSPR",
        "price": "$6,000",
        "cents": 600000,
        "file": "CCCE_Course_Files.zip",
    },
]
COURSE_KEYS = {c["key"] for c in COURSES}
COURSE_BY_KEY = {c["key"]: c for c in COURSES}

# Content collections beyond the purchasable products.
# DESIGN DECISION (2026-11-11, Taylor: "And similar topic Teach-Back"):
# the Teach-Back collection unlocks with CCCE course access — there is
# no separate purchase and no separate access row for it. The owner's
# existing roster grant/revoke of CCCE access therefore controls the
# collection too. Teach-back files are staged individually under
# CONTENT_DIR/teachback/ and are NEVER folded into CCCE_Course_Files.zip
# (the bundle stays byte-identical to its master).
COLLECTIONS = [
    {
        "key": "teachback",
        "name": "Teach-Back Collection — similar-topic teach-back documents",
        "short": "Teach-Back",
        "unlock_course": "ccce",
    },
]
COLLECTION_BY_KEY = {c["key"]: c for c in COLLECTIONS}
UPLOAD_TARGETS = COURSE_KEYS | set(COLLECTION_BY_KEY)
# Document types accepted into a collection (products stay zip-only).
COLLECTION_EXTS = (".zip", ".pdf", ".md", ".txt", ".docx")
COLLECTION_CONTENT_TYPES = {
    ".zip": "application/zip",
    ".pdf": "application/pdf",
    ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

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
                stripe_session_id TEXT UNIQUE,
                stripe_event_id TEXT,
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
            """
        )


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return salt.hex(), dk.hex()


def verify_password(password, salt_hex, hash_hex):
    _, candidate = hash_password(password, bytes.fromhex(salt_hex))
    return secrets.compare_digest(candidate, hash_hex)


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(member_id):
    token = secrets.token_urlsafe(32)
    with _lock, _db:
        _db.execute(
            "INSERT INTO sessions (token_hash, member_id, expires_at) VALUES (?, ?, ?)",
            (token_hash(token), member_id, int(time.time()) + SESSION_DAYS * 86400),
        )
    return token


def member_for_token(token):
    if not token:
        return None
    with _lock:
        row = _db.execute(
            "SELECT m.* FROM sessions s JOIN members m ON m.id = s.member_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (token_hash(token), int(time.time())),
        ).fetchone()
    return row


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


# ---------------------------------------------------------------- Stripe --
def stripe_create_checkout(member, course):
    """Create a Stripe Checkout Session. Returns (url, None) or (None, err)."""
    if not STRIPE_SECRET_KEY:
        return None, "not_configured"
    params = {
        "mode": "payment",
        "success_url": f"{PUBLIC_BASE_URL}{BASE}/desk?paid={course['key']}",
        "cancel_url": f"{PUBLIC_BASE_URL}{BASE}/desk?cancelled={course['key']}",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(course["cents"]),
        "line_items[0][price_data][product_data][name]": course["name"],
        "metadata[member_id]": str(member["id"]),
        "metadata[course_key]": course["key"],
        "customer_email": member["email"],
        # payment_method_types deliberately omitted: Checkout uses the
        # account's enabled payment methods (card + wallets such as
        # Google Pay / Apple Pay when enabled in the Stripe Dashboard).
    }
    req = urllib.request.Request(
        "https://api.stripe.com/v1/checkout/sessions",
        data=urlencode(params).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {STRIPE_SECRET_KEY}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Idempotency-Key": secrets.token_hex(16),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("url"), None
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))["error"]["message"]
        except Exception:
            err = f"Stripe HTTP {e.code}"
        return None, err
    except Exception as e:  # network etc.
        return None, str(e)


def stripe_verify_signature(payload_bytes, sig_header, secret, tolerance=300):
    """Verify a Stripe webhook signature (stdlib HMAC-SHA256)."""
    if not sig_header or not secret:
        return False
    parts = {}
    for item in sig_header.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            parts.setdefault(k.strip(), []).append(v.strip())
    try:
        timestamp = parts["t"][0]
        signatures = parts["v1"]
    except (KeyError, IndexError):
        return False
    if abs(int(time.time()) - int(timestamp)) > tolerance:
        return False
    signed = f"{timestamp}.".encode("utf-8") + payload_bytes
    expected = hmac.new(
        secret.encode("utf-8"), signed, hashlib.sha256
    ).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)


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


# ----------------------------------------------------------------- pages --
PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 0; background: #101418; color: #e8ecef; }}
 main {{ max-width: 720px; margin: 0 auto; padding: 24px 16px 48px; }}
 h1 {{ font-size: 1.5rem; }} h2 {{ font-size: 1.15rem; margin-top: 28px; }}
 .card {{ background: #1a2129; border: 1px solid #2b3644; border-radius: 10px; padding: 16px; margin: 14px 0; }}
 label {{ display: block; margin: 10px 0 4px; font-size: .9rem; color: #b9c4cf; }}
 input, select {{ width: 100%; box-sizing: border-box; padding: 10px; border-radius: 6px; border: 1px solid #39485a; background: #0d1117; color: #e8ecef; }}
 button, .btn {{ display: inline-block; margin-top: 14px; padding: 10px 18px; border: 0; border-radius: 6px; background: #2f81f7; color: #fff; font-size: 1rem; cursor: pointer; text-decoration: none; }}
 button.secondary, .btn.secondary {{ background: #39485a; }}
 button:disabled {{ background: #39485a; cursor: not-allowed; }}
 .msg {{ background: #3a2b12; border: 1px solid #8a6d1f; color: #ffd97a; padding: 10px 14px; border-radius: 8px; }}
 .ok {{ background: #12331c; border-color: #1f7a3a; color: #8fe3a8; }}
 table {{ width: 100%; border-collapse: collapse; font-size: .92rem; }}
 td, th {{ padding: 8px 6px; border-bottom: 1px solid #2b3644; text-align: left; vertical-align: top; }}
 .small {{ font-size: .85rem; color: #93a3b3; }}
 code {{ word-break: break-all; }}
 form.inline {{ display: inline; }}
 form.inline button {{ margin-top: 0; padding: 5px 10px; font-size: .85rem; }}
</style></head><body><main>
{body}
</main></body></html>"""


def esc(s):
    return html.escape(str(s), quote=True)


def catalog_cards():
    out = ""
    for c in COURSES:
        out += (
            f'<div class="card"><strong>{esc(c["name"])}</strong><br>'
            f'<span class="small">{esc(c["price"])} USD — one-time payment</span><br>'
            f'<a class="btn" href="{BASE}/buy/{c["key"]}">Buy — {esc(c["price"])}</a> '
            f'<a class="btn secondary" href="{BASE}/preview">Preview the course</a></div>'
        )
    return out


def connection_step_html():
    """Taylor's standing app standard (2026-11-08): account creation in
    Taylor's apps asks for Cellular, Wi-Fi (Local Network) and Bluetooth
    up front. This is a web app: it cannot raise iOS native permission
    prompts, so the flow presents the step as an explicit checklist
    naming all three, with live status only where the browser genuinely
    reports it (online state, connection type, Bluetooth availability).
    No fake system dialogs."""
    return """
<div class="card"><h2>Connection step — please allow all three</h2>
<p class="small">When you create an account, this training app asks for
the connections it uses up front. A web page cannot open your phone's
system permission dialogs, so when your phone or browser asks, please
allow all three:</p>
<table>
<tr><td><strong>Cellular</strong></td><td>keeps the site, downloads and
uploads working away from Wi-Fi</td></tr>
<tr><td><strong>Wi-Fi (Local Network)</strong></td><td>reaches the
training pages and files on your local network</td></tr>
<tr><td><strong>Bluetooth</strong></td><td>pairs training devices and
accessories</td></tr>
</table>
<p class="small" id="conn-status">Connection status: checking…</p>
<script>
(function () {
  var parts = [navigator.onLine ? "online" : "offline"];
  if (navigator.connection && navigator.connection.effectiveType) {
    parts.push("connection: " + navigator.connection.effectiveType);
  }
  var el = document.getElementById("conn-status");
  function done(bt) {
    if (bt !== null) { parts.push("Bluetooth: " + bt); }
    el.textContent = "Connection status (as reported by this browser): "
      + parts.join(" · ");
  }
  if (navigator.bluetooth && navigator.bluetooth.getAvailability) {
    navigator.bluetooth.getAvailability().then(function (a) {
      done(a ? "available" : "not available on this device/browser");
    }, function () { done(null); });
  } else { done(null); }
})();
</script></div>
"""


def home_page(message="", ok=False):
    msg = f'<p class="msg{" ok" if ok else ""}">{esc(message)}</p>' if message else ""
    if STRIPE_SECRET_KEY:
        pay_note = (
            "Or pay by card, Google Pay or Apple Pay at checkout — "
            "card access unlocks automatically when payment completes. "
            "You can also pay with Bitcoin inside Cash App — each "
            "course's buy page shows the Bitcoin amount to send."
        )
    else:
        pay_note = (
            "Or pay with Bitcoin inside Cash App — each course's buy "
            "page shows the exact Bitcoin amount to send to the same "
            "cashtag, with the same email note and owner confirmation."
        )
    body = f"""
<h1>Training Site — Member Sign-In</h1>
<p class="small">Taylor's Transgender Patient Awareness, LLC · 911transedu.net</p>
{msg}
<div class="card"><h2>Sign in</h2>
<form method="post" action="{BASE}/signin">
<label>Email</label><input type="email" name="email" required>
<label>Password</label><input type="password" name="password" required>
<button type="submit">Sign in</button></form></div>
<div class="card"><h2>Create an account</h2>
<form method="post" action="{BASE}/signup">
<label>Full name</label><input type="text" name="name" required>
<label>Email</label><input type="email" name="email" required>
<label>Password (8+ characters)</label><input type="password" name="password" minlength="8" required>
<button type="submit">Create account</button></form></div>
{connection_step_html()}
<h2>Courses</h2>
{catalog_cards()}
<p class="small">Sold only on this site. Buy with Cash App: send the
price to {esc(CASHAPP_CASHTAG)} and put your account email in the
payment note — access unlocks once the owner confirms receipt.
{pay_note}</p>
"""
    return PAGE.format(title="Training Site — Members", body=body)


def preview_page():
    if STRIPE_SECRET_KEY:
        card_line = (
            "You can also pay by card, Google Pay or Apple Pay on each "
            "product's buy page — card access unlocks automatically "
            "when payment completes."
        )
    else:
        card_line = (
            "Cash App is the way to buy — follow the four steps above."
        )
    body = f"""
<h1>Course Preview</h1>
<p class="small">Taylor's Transgender Patient Awareness, LLC</p>
<div class="card"><h2>Cultural Competence Continuing Education:
Transgender Emergency Medicine</h2>
<p><strong>Price: $15 USD</strong></p>
<p>OHA Cultural Competence Continuing Education (CCCE) approved course —
"Transgender Emergency Medicine", approved August 5, 2026 through
August 5, 2029.</p>
<p>The course covers all Oregon EMT renewal categories (OAR 333-265-0110
Appendix 1):</p>
<table>
<tr><th>Category</th><th>Hours</th></tr>
<tr><td>Trauma</td><td>3</td></tr>
<tr><td>Medical</td><td>3</td></tr>
<tr><td>Pediatric &amp; Obstetric</td><td>3</td></tr>
<tr><td>Airway / Breathing / Cardiology</td><td>4</td></tr>
<tr><td>Culture / Race / Gender / Ethnicity</td><td>2</td></tr>
<tr><td>Miscellaneous</td><td>9</td></tr>
<tr><td><strong>Total</strong></td><td><strong>24</strong></td></tr>
</table>
<p class="small">This preview shows the outline only. The full course
contents are released to member accounts after payment.</p>
<p><a class="btn" href="{BASE}/buy/ccce">Buy the CCCE Course — $15</a></p></div>
<div class="card"><h2>EMSPR Protocols</h2>
<p><strong>Price: $6,000 USD</strong></p>
<p>The EMSPR protocol set for EMS agencies and training programs.
Full contents are released to member accounts after payment.</p>
<p><a class="btn" href="{BASE}/buy/emspr">Buy EMSPR Protocols — $6,000</a></p></div>
<div class="card"><h2>How to buy</h2>
<p>Both products are sold only on this site, through Cash App:</p>
<ol>
<li>Create your free member account with your email.</li>
<li>In Cash App, send the product price to
<strong><code>{esc(CASHAPP_CASHTAG)}</code></strong>.</li>
<li>Put <strong>your account email</strong> in the Cash App payment
note — that is how your payment is matched to your account.</li>
<li>Access unlocks as soon as the owner confirms receipt — your
download then appears on your Member Desk.</li>
</ol>
<p class="small">You can also pay with <strong>Bitcoin inside Cash
App</strong>: each product's buy page shows the exact Bitcoin amount
to send to the same cashtag, with the same account-email note and
owner confirmation.</p>
<p class="small">{card_line}</p></div>
<p><a class="btn" href="{BASE}/">Sign in / Create account</a></p>
"""
    return PAGE.format(title="Course Preview", body=body)


def buy_page(course, member=None, message="", ok=False):
    """Stable public purchase page for one product: /members/buy/<key>.

    Cash App is the operative way to buy (2026-11-12 sales decision).
    The page always shows the price, the cashtag, the note guidance
    (the buyer's account email), and the owner-confirmation unlock.
    Bitcoin is offered alongside Cash App cash (Taylor, 2026-11-12):
    sent inside Cash App to the same cashtag at the live-quoted BTC
    amount — no on-chain address exists or is shown.
    While STRIPE_SECRET_KEY is unset — the standing state since
    Taylor closed the Stripe go-live on 2026-11-12 ("Let's just keep
    Cash App") and the keys were removed — card checkout is never
    offered as a working button here and Cash App is presented as
    the way to buy. Were a key ever set again, the page would also
    present paying by card, Google Pay or Apple Pay via Stripe
    Checkout, with Cash App remaining the alternative. Signed-in
    members get the order button
    on this page; everyone else gets the sign-in/create-account step
    first (their account email is the identity the payment note must
    carry)."""
    msg = f'<p class="msg{" ok" if ok else ""}">{esc(message)}</p>' if message else ""
    amount = f"${course['cents'] / 100:,.2f}"
    if member is not None and course["key"] in member_access(member["id"]):
        action = (
            "<p><strong>You already have access to this product.</strong></p>"
            f'<p><a class="btn" href="{BASE}/content/{course["key"]}">'
            "Download the full contents</a></p>"
        )
    elif member is not None:
        with _lock:
            pending = _db.execute(
                "SELECT * FROM manual_orders WHERE member_id = ? AND "
                "course_key = ? AND method = 'cashapp' AND status = 'pending' "
                "ORDER BY created_at",
                (member["id"], course["key"]),
            ).fetchall()
        if pending:
            o = pending[-1]
            action = (
                f'<p>Your Cash App order #{o["id"]} is recorded: send '
                f"<strong>{esc(o['amount_text'])}</strong> to "
                f"<code>{esc(CASHAPP_CASHTAG)}</code> in Cash App with your "
                "account email in the payment note. Access unlocks as soon "
                "as the owner confirms receipt.</p>"
            )
        else:
            action = (
                f'<form method="post" action="{BASE}/cashapp/order">'
                f'<input type="hidden" name="course_key" value="{course["key"]}">'
                f'<button type="submit">Record my Cash App order — {amount}</button></form>'
                '<p class="small">Record the order when you send the '
                "payment — it is what the owner confirms against.</p>"
            )
    else:
        action = (
            f'<p><a class="btn" href="{BASE}/">Sign in / Create your free '
            "account</a></p>"
            '<p class="small">Your account comes first — the email on it '
            "is what you put in the Cash App note, and it is the account "
            "that receives the access.</p>"
        )
    per_person = (
        "<p>Course access is <strong>$15 per person</strong> — each "
        "buyer needs their own account and payment.</p>"
        if course["key"] == "ccce"
        else ""
    )
    # Card / wallet option (Taylor, 2026-11-12: "Add them via Stripe").
    # While STRIPE_SECRET_KEY is unset the note stays exactly as before
    # — card is never presented as live and Cash App is the way to buy.
    # When the key exists, the page presents paying by card, Google Pay
    # or Apple Pay via Stripe Checkout, with Cash App remaining the
    # alternative above; signed-in members without access get the
    # checkout button itself.
    if not STRIPE_SECRET_KEY:
        card_note = ""
    elif member is not None and course["key"] in member_access(member["id"]):
        card_note = ""
    elif member is not None:
        card_note = (
            "<h2>Or pay by card, Google Pay or Apple Pay</h2>"
            "<p>Pay through Stripe Checkout — card, Google Pay and "
            "Apple Pay are accepted (Apple Card works as a card and via "
            "Apple Pay). Access unlocks automatically as soon as "
            "payment completes.</p>"
            f'<form method="post" action="{BASE}/checkout">'
            f'<input type="hidden" name="course_key" value="{course["key"]}">'
            f'<button type="submit">Pay by card / Google Pay / Apple Pay '
            f"— {amount}</button></form>"
        )
    else:
        card_note = (
            '<p class="small">You can also pay by card, Google Pay or '
            "Apple Pay at Stripe Checkout once you sign in — card "
            "access unlocks automatically when payment completes.</p>"
        )
    # Bitcoin alongside Cash App cash (Taylor, 2026-11-12: "Offer
    # bitcoin alongside"): paid INSIDE Cash App to the cashtag — Cash
    # App shows Taylor no on-chain deposit address ("There isn't
    # one"), so no address is shown or implied; the buyer sends the
    # quoted BTC amount to the cashtag with their account email in
    # the note and records the order here, exactly like a Cash App
    # cash order. The quote is live from CoinGecko; if the rate is
    # unreachable the option stays named with a check-back note
    # rather than a wrong amount.
    btc_section = ""
    if member is None or course["key"] not in member_access(member["id"]):
        btc_amount, _rate = btc_quote(course["cents"])
        if btc_amount is None:
            btc_section = (
                '<div class="card"><h2>Or pay with Bitcoin — inside '
                "Cash App</h2>"
                '<p class="small">The Bitcoin rate is unavailable right '
                "now — please check back shortly. Cash App cash above "
                "works as usual.</p></div>"
            )
        else:
            if member is not None:
                with _lock:
                    btc_pending = _db.execute(
                        "SELECT * FROM manual_orders WHERE member_id = ? AND "
                        "course_key = ? AND method = 'bitcoin' AND "
                        "status = 'pending' ORDER BY created_at",
                        (member["id"], course["key"]),
                    ).fetchall()
                if btc_pending:
                    o = btc_pending[-1]
                    btc_action = (
                        f'<p>Your Bitcoin order #{o["id"]} is recorded: send '
                        f"<strong>{esc(o['amount_text'])}</strong> to "
                        f"<code>{esc(CASHAPP_CASHTAG)}</code> inside Cash App "
                        "with your account email in the payment note. Access "
                        "unlocks as soon as the owner confirms receipt.</p>"
                    )
                else:
                    btc_action = (
                        f'<form method="post" action="{BASE}/bitcoin/order">'
                        f'<input type="hidden" name="course_key" value="{course["key"]}">'
                        f'<button class="secondary" type="submit">Record my '
                        f"Bitcoin order — {btc_amount} BTC</button></form>"
                        '<p class="small">Record the order when you send the '
                        "payment — it is what the owner confirms against.</p>"
                    )
            else:
                btc_action = (
                    f'<p><a class="btn secondary" href="{BASE}/">Sign in / '
                    "Create your free account</a></p>"
                    '<p class="small">Your account comes first — the email '
                    "on it is what you put in the Cash App note.</p>"
                )
            btc_section = f"""
<div class="card"><h2>Or pay with Bitcoin — inside Cash App</h2>
<ol>
<li>In Cash App, send exactly <strong>{btc_amount} BTC</strong>
(the Bitcoin amount for {amount} at the current rate) to
<strong><code>{esc(CASHAPP_CASHTAG)}</code></strong> using Cash App's
Bitcoin send to the cashtag — there is no separate wallet address.</li>
<li>In the Cash App payment note, put
<strong>your account email</strong> — that is how your payment is
matched to your account.</li>
<li>Access unlocks as soon as the owner confirms receipt. Your
download then appears on your Member Desk.</li>
</ol>
{btc_action}
</div>"""
    body = f"""
<h1>Buy — {esc(course["name"])}</h1>
<p class="small">Taylor's Transgender Patient Awareness, LLC · sold only on this site</p>
{msg}
<div class="card"><h2>Price: {esc(course["price"])} USD</h2>
<p>One-time payment. The files are released to your member account
only — access unlocks here on this site.</p>
{per_person}
<h2>How to buy — Cash App</h2>
<ol>
<li>Create your member account (or sign in) with your email.</li>
<li>In Cash App, send exactly <strong>{amount}</strong> to
<strong><code>{esc(CASHAPP_CASHTAG)}</code></strong>.</li>
<li>In the Cash App payment note, put
<strong>your account email</strong> — that is how your payment is
matched to your account.</li>
<li>Access unlocks as soon as the owner confirms receipt. Your
download then appears on your Member Desk.</li>
</ol>
{action}
</div>
{card_note}
{btc_section}
<p><a class="btn secondary" href="{BASE}/preview">Preview the courses</a>
<a class="btn secondary" href="{BASE}/desk">Member Desk</a></p>
"""
    return PAGE.format(title=f"Buy — {course['short']}", body=body)


def upload_page(member, message="", ok=False):
    msg = f'<p class="msg{" ok" if ok else ""}">{esc(message)}</p>' if message else ""
    options = "".join(
        f'<option value="{c["key"]}">{esc(c["short"])} — {esc(c["name"])}</option>'
        for c in COURSES
    ) + "".join(
        f'<option value="{c["key"]}">{esc(c["short"])} — {esc(c["name"])}</option>'
        for c in COLLECTIONS
    )
    sections = ""
    for c in COURSES:
        files = staged_files(c["key"])
        legacy = os.path.join(CONTENT_DIR, c["file"])
        legacy_note = ""
        if os.path.isfile(legacy):
            legacy_note = (
                f'<tr><td>{esc(c["file"])} (top level)</td>'
                f"<td>{os.path.getsize(legacy):,} bytes</td></tr>"
            )
        if files:
            trs = "".join(
                f"<tr><td>{esc(name)}</td><td>{size:,} bytes</td></tr>"
                for name, size, _mtime in files
            )
        elif not legacy_note:
            trs = '<tr><td colspan="2" class="small">No files staged yet.</td></tr>'
        else:
            trs = ""
        sections += (
            f'<div class="card"><h2>Staged files — {esc(c["short"])}</h2>'
            f"<table><tr><th>File</th><th>Size</th></tr>{trs}{legacy_note}</table></div>"
        )
    for c in COLLECTIONS:
        files = collection_files(c["key"])
        if files:
            trs = "".join(
                f"<tr><td>{esc(name)}</td><td>{size:,} bytes</td></tr>"
                for name, size, _mtime in files
            )
        else:
            trs = '<tr><td colspan="2" class="small">No files staged yet.</td></tr>'
        sections += (
            f'<div class="card"><h2>Staged files — {esc(c["short"])} collection</h2>'
            f"<table><tr><th>File</th><th>Size</th></tr>{trs}</table>"
            '<p class="small">Teach-Back documents unlock with CCCE course '
            "access and are served individually — they are not added to "
            "the course bundle zip.</p></div>"
        )
    with _lock:
        log_rows = _db.execute(
            "SELECT * FROM uploads ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    log = ""
    if log_rows:
        log = (
            '<div class="card"><h2>Recent uploads</h2><table>'
            "<tr><th>When (UTC)</th><th>Product</th><th>File</th><th>Size</th></tr>"
            + "".join(
                f'<tr><td class="small">{esc(time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["created_at"])))}</td>'
                f'<td>{esc(r["course_key"])}</td><td>{esc(r["filename"])}</td>'
                f'<td>{r["bytes"]:,} bytes</td></tr>'
                for r in log_rows
            )
            + "</table></div>"
        )
    body = f"""
<h1>Course File Upload</h1>
<p class="small">Owner only. Files go to the protected content area on the
server volume — they are never public and never part of the site code.</p>
{msg}
<div class="card"><h2>Upload course content</h2>
<form method="post" action="{BASE}/admin/upload" enctype="multipart/form-data">
<label>Target</label>
<select name="product">{options}</select>
<label>File (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)</label>
<input type="file" name="file" required>
<button type="submit">Upload</button></form>
<p class="small">Uploading a file with the same name replaces the staged
copy. Products take a .zip bundle: the bundle for both products is
CCCE_Course_Files.zip — upload it once under CCCE and once under EMSPR.
The Teach-Back collection takes individual documents
(.zip, .pdf, .md, .txt, .docx).</p></div>
{sections}
{log}
<p><a class="btn secondary" href="{BASE}/desk">Back to Member Desk</a></p>
"""
    return PAGE.format(title="Course File Upload", body=body)


def member_upload_page(member, message="", ok=False):
    msg = f'<p class="msg{" ok" if ok else ""}">{esc(message)}</p>' if message else ""
    rows = member_uploads(member["id"])
    if rows:
        trs = "".join(
            f'<tr><td><a href="{BASE}/upload/download/{r["id"]}">{esc(r["filename"])}</a></td>'
            f'<td>{r["bytes"]:,} bytes</td>'
            f'<td class="small">{esc(time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["created_at"])))} UTC</td></tr>'
            for r in rows
        )
        listing = (
            "<table><tr><th>File</th><th>Size</th><th>Uploaded</th></tr>"
            f"{trs}</table>"
        )
    else:
        listing = '<p class="small">You have not uploaded anything yet.</p>'
    body = f"""
<h1>Upload Documents</h1>
<p class="small">Send training-related documents for your department to
Taylor's Transgender Patient Awareness, LLC. Your uploads go to your
private member inbox on the server — only you and the site owner can
see them, and they are never public.</p>
{msg}
{connection_step_html()}
<div class="card"><h2>Upload a document</h2>
<form method="post" action="{BASE}/upload" enctype="multipart/form-data">
<label>File — PDF, Word (.doc/.docx), Excel (.xls/.xlsx), image
(.png/.jpg), or ZIP; max {MEMBER_UPLOAD_MAX_BYTES // (1024 * 1024)} MB</label>
<input type="file" name="file" required>
<button type="submit">Upload</button></form></div>
<div class="card"><h2>Your uploads</h2>
{listing}</div>
<p><a class="btn secondary" href="{BASE}/desk">Back to Member Desk</a></p>
"""
    return PAGE.format(title="Upload Documents", body=body)


def desk_page(member, message="", ok=False):
    is_owner = member["email"].lower() == OWNER_EMAIL
    granted = member_access(member["id"])
    msg = f'<p class="msg{" ok" if ok else ""}">{esc(message)}</p>' if message else ""
    with _lock:
        my_pending = _db.execute(
            "SELECT * FROM manual_orders WHERE member_id = ? AND status = 'pending' "
            "ORDER BY created_at",
            (member["id"],),
        ).fetchall()
    pending_by_course = {}
    for o in my_pending:
        pending_by_course.setdefault(o["course_key"], []).append(o)

    rows = ""
    for c in COURSES:
        if c["key"] in granted:
            if content_available(c):
                status = (
                    f'<strong>Access granted.</strong><br>'
                    f'<a class="btn" href="{BASE}/content/{c["key"]}">'
                    f'Download full course contents</a>'
                )
            else:
                status = (
                    "<strong>Access granted</strong> — your access is saved. "
                    "The course files are being added to the site; they "
                    "will appear here to download once added."
                )
        else:
            parts = []
            for o in pending_by_course.get(c["key"], []):
                if o["method"] == "bitcoin":
                    parts.append(
                        f'<p class="small">Bitcoin order #{o["id"]} pending: send '
                        f'<strong>{esc(o["amount_text"])}</strong> to '
                        f'<code>{esc(CASHAPP_CASHTAG)}</code> inside Cash App — '
                        "put your account email in the payment note so it can "
                        "be matched.<br>Access unlocks when the owner confirms "
                        "receipt.</p>"
                    )
                else:
                    parts.append(
                        f'<p class="small">Cash App order #{o["id"]} pending: send '
                        f'<strong>{esc(o["amount_text"])}</strong> to '
                        f'<code>{esc(CASHAPP_CASHTAG)}</code> in Cash App — put '
                        "your account email in the payment note so it can be "
                        "matched.<br>Access unlocks when the owner confirms "
                        "receipt.</p>"
                    )
            # Cash App is the operative purchase path (2026-11-12 sales
            # decision) and is presented first, as the primary action,
            # with the full instructions inline: exact amount, cashtag,
            # account email in the note, owner-confirmation unlock.
            amount = f"${c['cents'] / 100:,.2f}"
            parts.append(
                '<p><strong>Buy with Cash App:</strong> send '
                f"<strong>{amount}</strong> to "
                f"<code>{esc(CASHAPP_CASHTAG)}</code> in Cash App and put "
                "your account email in the payment note, then record "
                "your order:</p>"
                f'<form class="inline" method="post" action="{BASE}/cashapp/order">'
                f'<input type="hidden" name="course_key" value="{c["key"]}">'
                f'<button type="submit">Pay with Cash App — {amount}</button></form>'
                '<p class="small">Access unlocks when the owner confirms '
                "receipt.</p>"
            )
            # Bitcoin alongside Cash App (Taylor, 2026-11-12): sent
            # inside Cash App to the cashtag; the order records the
            # live-quoted BTC amount and the owner confirms receipt,
            # exactly like a Cash App cash order.
            parts.append(
                '<p><strong>Buy with Bitcoin:</strong> send the Bitcoin '
                "amount quoted on the course buy page to "
                f"<code>{esc(CASHAPP_CASHTAG)}</code> inside Cash App "
                "and put your account email in the payment note, then "
                "record your order:</p>"
                f'<form class="inline" method="post" action="{BASE}/bitcoin/order">'
                f'<input type="hidden" name="course_key" value="{c["key"]}">'
                f'<button class="secondary" type="submit">Pay with Bitcoin</button></form> '
                '<p class="small">Access unlocks when the owner confirms '
                "receipt.</p>"
            )
            manual = "".join(parts)
            if STRIPE_SECRET_KEY:
                status = (
                    f'<form class="inline" method="post" action="{BASE}/checkout">'
                    f'<input type="hidden" name="course_key" value="{c["key"]}">'
                    f'<button type="submit">Buy by card — {esc(c["price"])}</button></form> '
                    + manual
                    + '<p class="small">Card, Google Pay and Apple Pay are '
                    "accepted at checkout (Apple Card works as a card and "
                    "via Apple Pay). Card access unlocks automatically when "
                    "payment completes.</p>"
                )
            else:
                # Cash App only (Taylor, 2026-11-12: "Let's just keep
                # Cash App") — no card wording on the desk.
                status = manual
        rows += (
            f'<div class="card"><strong>{esc(c["name"])}</strong><br>'
            f'<span class="small">{esc(c["price"])} USD</span><br>{status}</div>'
        )

    roster = ""
    if is_owner:
        with _lock:
            all_members = _db.execute(
                "SELECT * FROM members ORDER BY created_at"
            ).fetchall()
            pend = _db.execute(
                "SELECT o.*, m.name, m.email FROM manual_orders o "
                "JOIN members m ON m.id = o.member_id "
                "WHERE o.status = 'pending' ORDER BY o.created_at"
            ).fetchall()
            pays = _db.execute(
                "SELECT p.*, m.email FROM payments p "
                "JOIN members m ON m.id = p.member_id "
                "ORDER BY p.created_at DESC LIMIT 20"
            ).fetchall()
        trs = ""
        for m in all_members:
            m_access = member_access(m["id"])
            cells = ""
            for c in COURSES:
                if c["key"] in m_access:
                    action, label = "revoke", "Revoke"
                else:
                    action, label = "grant", "Grant"
                cells += (
                    f'<td><form class="inline" method="post" action="{BASE}/access">'
                    f'<input type="hidden" name="member_id" value="{m["id"]}">'
                    f'<input type="hidden" name="course_key" value="{c["key"]}">'
                    f'<input type="hidden" name="action" value="{action}">'
                    f'<button type="submit">{label} {esc(c["short"])}</button></form></td>'
                )
            trs += f'<tr><td>{esc(m["name"])}</td><td>{esc(m["email"])}</td>{cells}</tr>'
        btc_rows = ""
        for o in pend:
            btc_rows += (
                f'<tr><td>#{o["id"]}</td><td>{esc(o["name"])} ({esc(o["email"])})</td>'
                f'<td>{esc(o["course_key"])}</td><td>{esc(o["method"])}</td>'
                f'<td>{esc(o["amount_text"])}</td>'
                f'<td><form class="inline" method="post" action="{BASE}/manual/confirm">'
                f'<input type="hidden" name="order_id" value="{o["id"]}">'
                f'<button type="submit">Confirm receipt — unlock</button></form></td></tr>'
            )
        if not btc_rows:
            btc_rows = '<tr><td colspan="6" class="small">No pending manual-payment orders.</td></tr>'
        pay_rows = ""
        for p in pays:
            pay_rows += (
                f'<tr><td>{esc(p["email"])}</td><td>{esc(p["course_key"])}</td>'
                f'<td>${(p["amount_cents"] or 0) / 100:.2f} {esc(p["currency"] or "")}</td>'
                f'<td class="small">{esc(p["stripe_session_id"] or "")}</td></tr>'
            )
        if not pay_rows:
            pay_rows = '<tr><td colspan="4" class="small">No card payments yet.</td></tr>'
        up_rows = all_member_uploads()
        if up_rows:
            grouped = {}
            for r in up_rows:
                grouped.setdefault((r["member_name"], r["member_email"]), []).append(r)
            up_sections = ""
            for (mname, memail), items in grouped.items():
                item_rows = "".join(
                    f'<tr><td class="small">{esc(time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["created_at"])))} UTC</td>'
                    f'<td><a href="{BASE}/upload/download/{r["id"]}">{esc(r["filename"])}</a></td>'
                    f'<td>{r["bytes"]:,} bytes</td></tr>'
                    for r in items
                )
                up_sections += (
                    f'<p><strong>{esc(mname)}</strong> '
                    f'<span class="small">{esc(memail)}</span></p>'
                    "<table><tr><th>When (UTC)</th><th>File</th><th>Size</th></tr>"
                    f"{item_rows}</table>"
                )
        else:
            up_sections = '<p class="small">No member uploads received yet.</p>'
        roster = f"""
<div class="card"><h2>Owner roster — grant / revoke course access</h2>
<table><tr><th>Member</th><th>Email</th><th>CCCE</th><th>EMSPR</th></tr>{trs}</table>
<p class="small">Only the owner account sees this roster.</p></div>
<div class="card"><h2>Pending manual-payment orders (Bitcoin / Cash App)</h2>
<table><tr><th>#</th><th>Member</th><th>Product</th><th>Method</th><th>Amount</th><th></th></tr>{btc_rows}</table></div>
<div class="card"><h2>Course files</h2>
<p class="small">Stage or replace the protected course bundles.</p>
<a class="btn secondary" href="{BASE}/admin/upload">Open the upload page</a></div>
<div class="card"><h2>Member uploads received</h2>
<p class="small">Documents members sent from the upload page, grouped by
member. Only you can see other members' uploads.</p>
{up_sections}</div>
<div class="card"><h2>Recent card payments (Stripe)</h2>
<table><tr><th>Member</th><th>Product</th><th>Amount</th><th>Session</th></tr>{pay_rows}</table></div>"""

    # Teach-Back collection card: unlocks with CCCE access (see the
    # COLLECTIONS design decision near the top of this file).
    if has_content_access(member["id"], "teachback"):
        teachback = (
            '<div class="card"><strong>Teach-Back Collection</strong><br>'
            '<span class="small">Included with your CCCE course access.</span><br>'
            f'<a class="btn" href="{BASE}/content/teachback">'
            "Browse teach-back documents</a></div>"
        )
    else:
        teachback = (
            '<div class="card"><strong>Teach-Back Collection</strong><br>'
            '<span class="small">Unlocks with the CCCE course.</span></div>'
        )

    if STRIPE_SECRET_KEY:
        card_li = (
            "<li>your card checkout completes — access unlocks "
            "automatically when payment completes, or</li>"
        )
    else:
        card_li = ""

    body = f"""
<h1>Member Desk</h1>
{msg}
<div class="card"><strong>{esc(member["name"])}</strong><br>
<span class="small">{esc(member["email"])}</span>
<form class="inline" method="post" action="{BASE}/signout" style="float:right">
<button class="secondary" type="submit">Sign out</button></form></div>
<div class="card"><h2>How access works</h2>
<p>Course access is <strong>$15 per person</strong>.</p>
<p>Your account gets access — it becomes <strong>entitled</strong> — when:</p>
<ul>
<li>your Cash App payment is confirmed by the owner, or</li>
{card_li}
<li>the owner grants your account access from the roster (for example,
teach-back access for departments, or past buyers honored).</li>
</ul>
<p>Until then, you can log in and use the site, but course downloads
stay locked.</p></div>
<h2>Your courses</h2>
{rows}
{teachback}
<div class="card"><strong>Upload documents</strong><br>
<span class="small">Send training-related documents for your
department.</span><br>
<a class="btn" href="{BASE}/upload">Open the upload page</a></div>
{roster}
<div class="card"><h2>Delete account</h2>
<form method="post" action="{BASE}/delete-account">
<label>Password (required to delete your own account)</label>
<input type="password" name="password" required>
<button class="secondary" type="submit">Delete my account</button></form></div>
"""
    return PAGE.format(title="Member Desk", body=body)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- helpers ---------------------------------------------------------
    def send_html(self, body_html, status=200, extra_headers=None):
        data = body_html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, path, extra_headers=None):
        headers = [("Location", path)] + (extra_headers or [])
        self.send_html("", status=303, extra_headers=headers)

    def session_cookie(self, token):
        return (
            "Set-Cookie",
            f"members_session={token}; Path={BASE}/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_DAYS * 86400}",
        )

    def clear_cookie(self):
        return (
            "Set-Cookie",
            f"members_session=; Path={BASE}/; HttpOnly; SameSite=Lax; Max-Age=0",
        )

    def current_member(self):
        cookie = self.headers.get("Cookie", "")
        token = None
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("members_session="):
                token = part.split("=", 1)[1]
        return member_for_token(token), token

    def raw_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def form(self):
        qs = parse_qs(self.raw_body().decode("utf-8"))
        return {k: v[0] for k, v in qs.items()}

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == BASE:
            return self.redirect(BASE + "/")
        if path == BASE + "/health":
            data = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if path == BASE + "/preview":
            return self.send_html(preview_page())
        if path.startswith(BASE + "/buy/"):
            course = COURSE_BY_KEY.get(path[len(BASE + "/buy/"):])
            if course is None:
                return self.send_html("<h1>Not found</h1>", status=404)
            member, _ = self.current_member()
            return self.send_html(buy_page(course, member))
        if path == BASE + "/":
            member, _ = self.current_member()
            if member:
                return self.redirect(BASE + "/desk")
            return self.send_html(home_page())
        if path == BASE + "/desk":
            member, _ = self.current_member()
            if not member:
                return self.redirect(BASE + "/")
            qs = parse_qs(parsed.query)
            message, ok = "", False
            if "paid" in qs:
                message = (
                    "Thank you! If your payment completed, access unlocks "
                    "automatically as soon as Stripe confirms it — refresh "
                    "this page in a few seconds."
                )
                ok = True
            elif "cancelled" in qs:
                message = "Checkout was cancelled — no charge was made."
            return self.send_html(desk_page(member, message, ok))
        if path == BASE + "/upload":
            member, _ = self.current_member()
            if not member:
                return self.redirect(BASE + "/")
            qs = parse_qs(parsed.query)
            message = qs.get("msg", [""])[0]
            return self.send_html(
                member_upload_page(member, message, ok=bool(message))
            )
        if path.startswith(BASE + "/upload/download/"):
            tail = path[len(BASE + "/upload/download/"):]
            try:
                upload_id = int(tail)
            except ValueError:
                return self.send_html("<h1>Not found</h1>", status=404)
            return self.member_upload_download(upload_id)
        if path == BASE + "/admin/upload":
            member, _ = self.current_member()
            # Fail closed: the upload page exists only for the owner.
            if not member or member["email"].lower() != OWNER_EMAIL:
                return self.send_html("<h1>Forbidden</h1>", status=403)
            qs = parse_qs(parsed.query)
            message = qs.get("msg", [""])[0]
            return self.send_html(upload_page(member, message, ok=bool(message)))
        if path.startswith(BASE + "/content/"):
            rest = path[len(BASE + "/content/"):]
            key, _, sub = rest.partition("/")
            return self.serve_content(key, sub)
        return self.send_html("<h1>Not found</h1>", status=404)

    def stream_file(self, fpath, content_type):
        size = os.path.getsize(fpath)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{os.path.basename(fpath)}"',
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(fpath, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def serve_content(self, key, sub=""):
        member, _ = self.current_member()
        if not member:
            return self.redirect(BASE + "/")
        if key in COLLECTION_BY_KEY:
            return self.serve_collection(member, key, sub)
        course = COURSE_BY_KEY.get(key)
        if course is None or sub:
            return self.send_html("<h1>Not found</h1>", status=404)
        if key not in member_access(member["id"]):
            return self.send_html(
                "<h1>Locked</h1><p>This content needs course access.</p>",
                status=403,
            )
        fpath = resolve_content_path(course)
        if fpath is None:
            return self.send_html(
                "<h1>Files being added</h1><p>Your access is saved. The "
                "course files are being added to the site and will "
                "appear here once added.</p>",
                status=404,
            )
        return self.stream_file(fpath, "application/zip")

    def serve_collection(self, member, key, sub=""):
        """Serve a content collection (teachback). Gating decision: the
        collection unlocks with its linked course's access (teachback ->
        CCCE) — see the COLLECTIONS design note. No access -> 403."""
        collection = COLLECTION_BY_KEY[key]
        if not has_content_access(member["id"], key):
            return self.send_html(
                "<h1>Locked</h1><p>This collection unlocks with the "
                f"{esc(COURSE_BY_KEY[collection['unlock_course']]['short'])} "
                "course.</p>",
                status=403,
            )
        files = collection_files(key)
        if sub:
            # Only ever serve a name that is actually staged (no path
            # traversal: the staged list is the allowlist).
            name = sanitize_filename(sub, COLLECTION_EXTS)
            staged = {n for n, _s, _m in files}
            if name is None or name not in staged:
                return self.send_html("<h1>Not found</h1>", status=404)
            ext = os.path.splitext(name)[1].lower()
            return self.stream_file(
                os.path.join(product_dir(key), name),
                COLLECTION_CONTENT_TYPES.get(ext, "application/octet-stream"),
            )
        if not files:
            return self.send_html(
                "<h1>Files being added</h1><p>The teach-back documents "
                "are being added to the site and will appear here once "
                "added.</p>",
                status=404,
            )
        items = "".join(
            f'<div class="card"><strong>{esc(name)}</strong><br>'
            f'<span class="small">{size:,} bytes</span><br>'
            f'<a class="btn" href="{BASE}/content/{key}/{esc(name)}">Download</a></div>'
            for name, size, _mtime in files
        )
        body = f"""
<h1>{esc(collection["short"])} Collection</h1>
<p class="small">Similar-topic teach-back documents from Taylor's
Transgender Patient Awareness, LLC — included with CCCE course access.</p>
{items}
<p><a class="btn secondary" href="{BASE}/desk">Back to Member Desk</a></p>
"""
        return self.send_html(PAGE.format(title="Teach-Back Collection", body=body))

    def admin_upload(self):
        """Owner-only content upload. Fail-closed: 403 for anyone who is
        not the signed-in owner. Accepts a browser multipart form
        (fields product + file) or a raw body with
        ?product=ccce|emspr|teachback (or X-Product header) and
        X-Filename. Products take zip bundles only; the teachback
        collection takes individual documents (COLLECTION_EXTS).
        Writes ONLY to CONTENT_DIR/<target>/ on the volume — never the
        repo tree."""
        member, _ = self.current_member()
        if not member or member["email"].lower() != OWNER_EMAIL:
            return self.send_html("<h1>Forbidden</h1>", status=403)

        def reject(message, status=400):
            return self.send_html(upload_page(member, message), status=status)

        length = int(self.headers.get("Content-Length", 0) or 0)
        # Small headroom over the file cap for multipart framing.
        if length <= 0 or length > MAX_UPLOAD_BYTES + 1024 * 1024:
            self.close_connection = True
            return reject(
                f"Upload missing or larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap.",
                status=413,
            )
        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        product = None
        filename = None
        data = None
        if ctype.startswith("multipart/form-data"):
            fields, files = parse_multipart(body, ctype)
            product = (fields.get("product") or "").strip().lower()
            if "file" in files:
                filename, data = files["file"]
        else:
            qs = parse_qs(urlparse(self.path).query)
            product = (
                qs.get("product", [""])[0]
                or self.headers.get("X-Product", "")
            ).strip().lower()
            filename = self.headers.get("X-Filename", "")
            data = body
        if product not in UPLOAD_TARGETS:
            return reject("Choose a target: ccce, emspr, or teachback.")
        if not data:
            return reject("No file data received.")
        if len(data) > MAX_UPLOAD_BYTES:
            return reject(
                f"That file is over the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap.",
                status=413,
            )
        is_collection = product in COLLECTION_BY_KEY
        if is_collection:
            target_label = COLLECTION_BY_KEY[product]["short"]
            filename = sanitize_filename(filename, COLLECTION_EXTS)
            if filename is None:
                return reject(
                    "Collection files must be one of: "
                    + ", ".join(COLLECTION_EXTS)
                    + "."
                )
            ext = os.path.splitext(filename)[1].lower()
            if ext in (".zip", ".docx") and not any(
                data.startswith(magic) for magic in ZIP_MAGICS
            ):
                return reject("That file is not a valid zip-based document.")
            if ext == ".pdf" and not data.startswith(b"%PDF-"):
                return reject("That file is not a PDF document.")
        else:
            target_label = COURSE_BY_KEY[product]["short"]
            filename = sanitize_filename(filename) or sanitize_filename(
                COURSE_BY_KEY[product]["file"]
            )
            if filename is None:
                return reject("The uploaded file must be a .zip file.")
            if not any(data.startswith(magic) for magic in ZIP_MAGICS):
                return reject("That file is not a zip archive.")
        dest_dir = product_dir(product)
        os.makedirs(dest_dir, exist_ok=True)
        tmp_path = os.path.join(dest_dir, f".upload-{secrets.token_hex(6)}.tmp")
        final_path = os.path.join(dest_dir, filename)
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(data)
            if os.path.splitext(filename)[1].lower() in (".zip", ".docx") and (
                not zipfile.is_zipfile(tmp_path)
            ):
                os.unlink(tmp_path)
                return reject("That file is not a valid zip archive.")
            os.replace(tmp_path, final_path)  # duplicate upload replaces cleanly
        except OSError as e:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return reject(f"Upload could not be saved: {e}", status=500)
        with _lock, _db:
            _db.execute(
                "INSERT INTO uploads (owner_email, course_key, filename, bytes, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (member["email"], product, filename, len(data), int(time.time())),
            )
        return self.send_html(
            upload_page(
                member,
                f"Uploaded {filename} ({len(data):,} bytes) for "
                f"{target_label}. Members with access can download it now.",
                ok=True,
            )
        )

    def member_upload(self):
        """Signed-in member document upload (department inbox). Anyone
        not signed in is sent to sign-in. Files are allowlist + magic
        checked, capped at MEMBER_UPLOAD_MAX_BYTES, sanitized, and
        written ONLY to UPLOADS_DIR/<member_id>/ on the volume — never
        CONTENT_DIR, never the repo tree, never a public URL."""
        member, _ = self.current_member()
        if not member:
            return self.redirect(BASE + "/")

        def reject(message, status=400):
            return self.send_html(member_upload_page(member, message), status=status)

        length = int(self.headers.get("Content-Length", 0) or 0)
        # Small headroom over the file cap for multipart framing.
        cap_mb = MEMBER_UPLOAD_MAX_BYTES // (1024 * 1024)
        if length <= 0 or length > MEMBER_UPLOAD_MAX_BYTES + 1024 * 1024:
            self.close_connection = True
            return reject(
                f"Upload missing or larger than the {cap_mb} MB cap.",
                status=413,
            )
        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return reject("Please use the upload form on this page.")
        _fields, files = parse_multipart(body, ctype)
        if "file" not in files:
            return reject("No file received — choose a file and try again.")
        filename, data = files["file"]
        if not data:
            return reject("That file is empty.")
        if len(data) > MEMBER_UPLOAD_MAX_BYTES:
            return reject(f"That file is over the {cap_mb} MB cap.", status=413)
        filename = sanitize_filename(filename, MEMBER_UPLOAD_EXTS)
        if filename is None:
            return reject(
                "That file type is not accepted. Please send a PDF, Word "
                "(.doc/.docx), Excel (.xls/.xlsx), image (.png/.jpg), or "
                "ZIP file."
            )
        if not member_upload_magic_ok(filename, data):
            return reject(
                "That file does not look like the document type its name "
                "claims."
            )
        dest_dir = member_upload_dir(member["id"])
        os.makedirs(dest_dir, exist_ok=True)
        # Same name again -> keep both (name-2.ext, name-3.ext, ...):
        # a member inbox never silently overwrites an earlier document.
        stored = filename
        stem, ext = os.path.splitext(filename)
        n = 2
        while os.path.exists(os.path.join(dest_dir, stored)):
            stored = f"{stem}-{n}{ext}"
            n += 1
        tmp_path = os.path.join(dest_dir, f".upload-{secrets.token_hex(6)}.tmp")
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(data)
            os.replace(tmp_path, os.path.join(dest_dir, stored))
        except OSError as e:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return reject(f"Upload could not be saved: {e}", status=500)
        with _lock, _db:
            _db.execute(
                "INSERT INTO member_uploads (member_id, filename, "
                "stored_name, bytes, created_at) VALUES (?, ?, ?, ?, ?)",
                (member["id"], filename, stored, len(data), int(time.time())),
            )
        return self.send_html(
            member_upload_page(
                member,
                f"Uploaded {filename} ({len(data):,} bytes). Thank you — "
                "it is in your member inbox.",
                ok=True,
            )
        )

    def member_upload_download(self, upload_id):
        """Download one member upload. Served ONLY to the member who
        uploaded it or to the owner; everyone else is fail-closed 403,
        anonymous users are sent to sign-in. The allowlist is the
        member_uploads table itself — a file that is not recorded
        there is never served."""
        member, _ = self.current_member()
        if not member:
            return self.redirect(BASE + "/")
        with _lock:
            row = _db.execute(
                "SELECT * FROM member_uploads WHERE id = ?", (upload_id,)
            ).fetchone()
        if row is None:
            return self.send_html("<h1>Not found</h1>", status=404)
        is_owner = member["email"].lower() == OWNER_EMAIL
        if not is_owner and row["member_id"] != member["id"]:
            return self.send_html("<h1>Forbidden</h1>", status=403)
        fpath = os.path.join(
            member_upload_dir(row["member_id"]), row["stored_name"]
        )
        if not os.path.isfile(fpath):
            return self.send_html("<h1>Not found</h1>", status=404)
        ext = os.path.splitext(row["filename"])[1].lower()
        return self.stream_file(
            fpath,
            MEMBER_UPLOAD_CONTENT_TYPES.get(ext, "application/octet-stream"),
        )

    def do_POST(self):
        path = urlparse(self.path).path
        if path == BASE + "/stripe/webhook":
            return self.stripe_webhook()
        if path == BASE + "/admin/upload":
            return self.admin_upload()
        if path == BASE + "/upload":
            return self.member_upload()
        form = self.form()
        if path == BASE + "/signup":
            name = form.get("name", "").strip()
            email = form.get("email", "").strip()
            password = form.get("password", "")
            if not name or "@" not in email or len(password) < 8:
                return self.send_html(
                    home_page("Please give a name, a valid email, and a password of 8+ characters.")
                )
            salt_hex, hash_hex = hash_password(password)
            try:
                with _lock, _db:
                    cur = _db.execute(
                        "INSERT INTO members (name, email, pw_salt, pw_hash, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (name, email, salt_hex, hash_hex, int(time.time())),
                    )
                    member_id = cur.lastrowid
            except sqlite3.IntegrityError:
                return self.send_html(
                    home_page("That email is already registered — please sign in instead.")
                )
            token = create_session(member_id)
            return self.redirect(BASE + "/desk", extra_headers=[self.session_cookie(token)])
        if path == BASE + "/signin":
            email = form.get("email", "").strip()
            password = form.get("password", "")
            with _lock:
                row = _db.execute(
                    "SELECT * FROM members WHERE email = ?", (email,)
                ).fetchone()
            if row is None or not verify_password(password, row["pw_salt"], row["pw_hash"]):
                return self.send_html(home_page("Sign-in failed: wrong email or password."))
            token = create_session(row["id"])
            return self.redirect(BASE + "/desk", extra_headers=[self.session_cookie(token)])
        if path == BASE + "/signout":
            _, token = self.current_member()
            if token:
                with _lock, _db:
                    _db.execute(
                        "DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),)
                    )
            return self.redirect(BASE + "/", extra_headers=[self.clear_cookie()])
        if path == BASE + "/checkout":
            member, _ = self.current_member()
            if not member:
                return self.redirect(BASE + "/")
            course = COURSE_BY_KEY.get(form.get("course_key", ""))
            if course is None:
                return self.send_html("<h1>Bad request</h1>", status=400)
            url, err = stripe_create_checkout(member, course)
            if url:
                return self.redirect(url)
            if err == "not_configured":
                return self.send_html(
                    desk_page(member, "Payment is by Cash App — the buy pages show the Cash App instructions for each course.")
                )
            return self.send_html(
                desk_page(member, f"Checkout could not be started: {err}")
            )
        if path == BASE + "/bitcoin/order":
            member, _ = self.current_member()
            if not member:
                return self.redirect(BASE + "/")
            course = COURSE_BY_KEY.get(form.get("course_key", ""))
            if course is None:
                return self.send_html("<h1>Bad request</h1>", status=400)
            if course["key"] in member_access(member["id"]):
                return self.send_html(desk_page(member, "You already have access to this.", ok=True))
            btc_amount, _rate = btc_quote(course["cents"])
            if btc_amount is None:
                return self.send_html(
                    desk_page(member, "The Bitcoin rate is unavailable right now — please try again shortly.")
                )
            with _lock, _db:
                _db.execute(
                    "INSERT INTO manual_orders (member_id, course_key, method, "
                    "usd_cents, amount_text, detail, created_at) "
                    "VALUES (?, ?, 'bitcoin', ?, ?, ?, ?)",
                    (member["id"], course["key"], course["cents"],
                     f"{btc_amount} BTC",
                     f"Cash App {CASHAPP_CASHTAG} · {BTC_RATE_SOURCE}",
                     int(time.time())),
                )
            return self.send_html(
                desk_page(
                    member,
                    f"Bitcoin order created: send {btc_amount} BTC to "
                    f"{CASHAPP_CASHTAG} inside Cash App — put your account "
                    "email in the payment note. Access unlocks when the "
                    "owner confirms receipt.",
                    ok=True,
                )
            )
        if path == BASE + "/cashapp/order":
            member, _ = self.current_member()
            if not member:
                return self.redirect(BASE + "/")
            course = COURSE_BY_KEY.get(form.get("course_key", ""))
            if course is None:
                return self.send_html("<h1>Bad request</h1>", status=400)
            if course["key"] in member_access(member["id"]):
                return self.send_html(desk_page(member, "You already have access to this.", ok=True))
            amount_text = f"${course['cents'] / 100:.2f}"
            with _lock, _db:
                _db.execute(
                    "INSERT INTO manual_orders (member_id, course_key, method, "
                    "usd_cents, amount_text, detail, created_at) "
                    "VALUES (?, ?, 'cashapp', ?, ?, ?, ?)",
                    (member["id"], course["key"], course["cents"], amount_text,
                     CASHAPP_CASHTAG, int(time.time())),
                )
            return self.send_html(
                desk_page(
                    member,
                    f"Cash App order created: send {amount_text} to "
                    f"{CASHAPP_CASHTAG} in Cash App — put your account email "
                    "in the payment note. Access unlocks when the owner "
                    "confirms receipt.",
                    ok=True,
                )
            )
        if path == BASE + "/manual/confirm":
            member, _ = self.current_member()
            if not member or member["email"].lower() != OWNER_EMAIL:
                return self.send_html("<h1>Forbidden</h1>", status=403)
            try:
                order_id = int(form.get("order_id", ""))
            except ValueError:
                return self.send_html("<h1>Bad request</h1>", status=400)
            with _lock, _db:
                row = _db.execute(
                    "SELECT * FROM manual_orders WHERE id = ? AND status = 'pending'",
                    (order_id,),
                ).fetchone()
                if row:
                    _db.execute(
                        "UPDATE manual_orders SET status = 'confirmed', "
                        "confirmed_at = ?, confirmed_by = ? WHERE id = ?",
                        (int(time.time()), member["email"], order_id),
                    )
                    _db.execute(
                        "INSERT OR IGNORE INTO access (member_id, course_key) VALUES (?, ?)",
                        (row["member_id"], row["course_key"]),
                    )
            return self.redirect(BASE + "/desk")
        if path == BASE + "/access":
            member, _ = self.current_member()
            # Fail closed: only the owner can grant/revoke.
            if not member or member["email"].lower() != OWNER_EMAIL:
                return self.send_html("<h1>Forbidden</h1>", status=403)
            try:
                target_id = int(form.get("member_id", ""))
            except ValueError:
                return self.send_html("<h1>Bad request</h1>", status=400)
            course_key = form.get("course_key", "")
            action = form.get("action", "")
            if course_key not in COURSE_KEYS or action not in ("grant", "revoke"):
                return self.send_html("<h1>Bad request</h1>", status=400)
            with _lock, _db:
                if action == "grant":
                    _db.execute(
                        "INSERT OR IGNORE INTO access (member_id, course_key) VALUES (?, ?)",
                        (target_id, course_key),
                    )
                else:
                    _db.execute(
                        "DELETE FROM access WHERE member_id = ? AND course_key = ?",
                        (target_id, course_key),
                    )
            return self.redirect(BASE + "/desk")
        if path == BASE + "/delete-account":
            member, _ = self.current_member()
            if not member:
                return self.redirect(BASE + "/")
            if not verify_password(form.get("password", ""), member["pw_salt"], member["pw_hash"]):
                return self.send_html(desk_page(member, "Delete failed: wrong password."))
            with _lock, _db:
                _db.execute("DELETE FROM access WHERE member_id = ?", (member["id"],))
                _db.execute("DELETE FROM sessions WHERE member_id = ?", (member["id"],))
                _db.execute("DELETE FROM manual_orders WHERE member_id = ?", (member["id"],))
                _db.execute("DELETE FROM payments WHERE member_id = ?", (member["id"],))
                _db.execute("DELETE FROM member_uploads WHERE member_id = ?", (member["id"],))
                _db.execute("DELETE FROM members WHERE id = ?", (member["id"],))
            # The member's inbox files go with the account.
            shutil.rmtree(member_upload_dir(member["id"]), ignore_errors=True)
            return self.redirect(BASE + "/", extra_headers=[self.clear_cookie()])
        return self.send_html("<h1>Not found</h1>", status=404)

    def stripe_webhook(self):
        payload = self.raw_body()
        sig = self.headers.get("Stripe-Signature", "")
        if not STRIPE_WEBHOOK_SECRET or not stripe_verify_signature(
            payload, sig, STRIPE_WEBHOOK_SECRET
        ):
            return self.send_html("<h1>Bad signature</h1>", status=400)
        try:
            event = json.loads(payload.decode("utf-8"))
        except Exception:
            return self.send_html("<h1>Bad payload</h1>", status=400)
        if event.get("type") == "checkout.session.completed":
            obj = event.get("data", {}).get("object", {})
            meta = obj.get("metadata", {}) or {}
            try:
                member_id = int(meta.get("member_id", ""))
            except ValueError:
                member_id = None
            course_key = meta.get("course_key", "")
            if (
                member_id
                and course_key in COURSE_KEYS
                and obj.get("payment_status") == "paid"
            ):
                try:
                    with _lock, _db:
                        _db.execute(
                            "INSERT OR IGNORE INTO payments (member_id, "
                            "course_key, stripe_session_id, stripe_event_id, "
                            "amount_cents, currency, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                member_id,
                                course_key,
                                obj.get("id"),
                                event.get("id"),
                                obj.get("amount_total"),
                                obj.get("currency"),
                                int(time.time()),
                            ),
                        )
                        _db.execute(
                            "INSERT OR IGNORE INTO access (member_id, course_key) "
                            "VALUES (?, ?)",
                            (member_id, course_key),
                        )
                except sqlite3.Error:
                    pass
        data = b'{"received":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"member-site listening on :{PORT} base={BASE} db={DB_PATH}", flush=True)
    server.serve_forever()
