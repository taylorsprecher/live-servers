"""HTTP dispatch — the route inventory transcribed verbatim from the
monolith's Handler, plus the one allowed new route:
GET {BASE}/assets/fleet.css (the vendored design system)."""
import json
import os
import secrets
import shutil
import sqlite3
import time
import zipfile
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlparse

from .auth import (
    create_session,
    hash_password,
    member_for_token,
    token_hash,
    verify_password,
)
from .catalog import (
    COLLECTION_BY_KEY,
    COLLECTION_CONTENT_TYPES,
    COLLECTION_EXTS,
    COURSE_BY_KEY,
    COURSE_KEYS,
    COURSES,
    UPLOAD_TARGETS,
)
from .config import (
    BASE,
    CASHAPP_CASHTAG,
    BTC_RATE_SOURCE,
    BUG_GUARD_ALLOW,
    BUG_GUARD_ENABLED,
    BUG_GUARD_REPORT_PATH,
    BUG_GUARD_STATE_PATH,
    CONTENT_DIR,
    MAX_UPLOAD_BYTES,
    MEMBER_UPLOAD_CONTENT_TYPES,
    MEMBER_UPLOAD_EXTS,
    MEMBER_UPLOAD_MAX_BYTES,
    OWNER_EMAIL,
    SESSION_DAYS,
    STREAM_READ_TOKEN,
)
from .bug_guard import BugGuard
from .ems import (
    PROTOCOLS as EMS_PROTOCOLS,
    evaluate as ems_evaluate,
    epcr_acs,
    epcr_hyperkalemia,
)
from .stream_store import (
    MAX_CHUNK_BYTES,
    list_stream_sessions,
    save_stream_chunk,
    stream_chunk_path,
)
from .pages import (
    PAGE,
    buy_page,
    desk_page,
    esc,
    home_page,
    member_upload_page,
    owner_paylinks_page,
    preview_page,
    upload_page,
    apps_page,
    connectivity_page,
    ssk_page,
    root_page,
    streamapp_page,
    cport_page,
    cportlightning_page,
    emsapp_page,
    bluetooth_page,
    openscad_page,
    bodypalace_shell,
    words_page,
    fsh_page,
    stream_page,
    blocks_page,
    blocks_submit_page,
    blocks_admin_page,
)
from .store import (
    _db,
    _lock,
    _set_setting,
    add_block_submission,
    apps_data,
    evaluate_block_submission,
    btc_quote,
    collection_files,
    grant_access,
    has_content_access,
    member_access,
    member_upload_dir,
    member_upload_magic_ok,
    parse_multipart,
    product_dir,
    resolve_content_path,
    sanitize_filename,
    set_block_submission_status,
)


_GUARD = None


def _guard():
    """The one BugGuard for this process (built lazily; its state
    lives on the data volume)."""
    global _GUARD
    if _GUARD is None:
        _GUARD = BugGuard(
            BUG_GUARD_ENABLED, BUG_GUARD_STATE_PATH,
            BUG_GUARD_REPORT_PATH, BUG_GUARD_ALLOW)
    return _GUARD


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- helpers ---------------------------------------------------------
    def _client_ip(self):
        # The gateway overwrites X-Real-IP with the real remote
        # address and the app port is loopback-only, so the header
        # is the client address; the socket peer is the fallback
        # for direct loopback calls (health checks).
        return (self.headers.get("X-Real-IP")
                or self.client_address[0] or "").strip()

    def _bug_blocked(self):
        """True when the bug guard refuses this request. The guard
        fails open: any error inside it allows the request, so the
        real site is never taken down by its own guard."""
        if not BUG_GUARD_ENABLED:
            return False
        try:
            return _guard().check(
                self._client_ip(), urlparse(self.path).path)
        except Exception:
            return False

    def send_html(self, body_html, status=200, extra_headers=None):
        data = body_html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200, extra_headers=None):
        data = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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
        if self._bug_blocked():
            return self.send_html("<h1>403 Forbidden</h1>", status=403)
        parsed = urlparse(self.path)
        path = parsed.path
        if path == BASE:
            return self.redirect(BASE + "/")
        if path == BASE + "/health":
            payload = {"status": "ok",
                       "bug_guard_enabled": bool(BUG_GUARD_ENABLED),
                       "bug_guard_blocked":
                           _guard().blocked_count()
                           if BUG_GUARD_ENABLED else 0}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if path == BASE + "/assets/fleet.css":
            css_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "assets", "fleet.css",
            )
            try:
                with open(css_path, "rb") as fh:
                    data = fh.read()
            except OSError:
                return self.send_html("<h1>Not found</h1>", status=404)
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if path == BASE + "/root":
            return self.send_html(root_page())
        if path == BASE + "/preview":
            return self.send_html(preview_page())
        if path == BASE + "/apps":
            member, _ = self.current_member()
            return self.send_html(apps_page(member))
        if path == BASE + "/apps/data":
            return self.send_json(apps_data())
        if path == BASE + "/connectivity":
            return self.send_html(connectivity_page())
        if path == BASE + "/ssk":
            return self.send_html(ssk_page())
        if path == BASE + "/ssk/project.zip":
            return self.stream_file(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "sskpython-briefcase.zip",
                ),
                "application/zip",
            )
        if path == BASE + "/streamapp":
            return self.send_html(streamapp_page())
        if path == BASE + "/streamapp/streammic-briefcase.zip":
            return self.stream_file(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "streammic-briefcase.zip",
                ),
                "application/zip",
            )
        if path == BASE + "/cport":
            return self.send_html(cport_page())
        if path == BASE + "/cport/project.zip":
            return self.stream_file(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "cport-python.zip",
                ),
                "application/zip",
            )
        if path == BASE + "/cportlightning":
            return self.send_html(cportlightning_page())
        if path == BASE + "/cportlightning/cport-lightening.zip":
            return self.stream_file(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "cport-lightening.zip",
                ),
                "application/zip",
            )
        if path == BASE + "/emsapp":
            return self.send_html(emsapp_page())
        if path == BASE + "/emsapp/api/protocols":
            return self.send_json(EMS_PROTOCOLS)
        if path == BASE + "/bluetooth":
            return self.send_html(bluetooth_page())
        if path == BASE + "/bluetooth/bluetooth-checkin.zip":
            return self.stream_file(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "bluetooth-checkin.zip",
                ),
                "application/zip",
            )
        if path == BASE + "/openscad":
            return self.send_html(openscad_page())
        if path == BASE + "/openscad/openscad-projects.zip":
            return self.stream_file(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "openscad-projects.zip",
                ),
                "application/zip",
            )
        if path == BASE + "/bodypalace" or path == BASE + "/bodypalace/":
            return self.send_html(bodypalace_shell())
        if path.startswith(BASE + "/bodypalace/"):
            # Body Palace Tracker static assets (built Vite output in
            # bodypalace/ beside app.py). Basename only — no path
            # traversal — and only the file kinds the build emits.
            rel = path[len(BASE + "/bodypalace/"):]
            parts = [p for p in rel.split("/") if p and p not in (".", "..")]
            if len(parts) == 1 and parts[0] in ("favicon.svg", "icons.svg"):
                sub, name = "", parts[0]
            elif len(parts) == 2 and parts[0] == "assets":
                sub, name = "assets", parts[1]
            else:
                return self.send_html("<h1>Not found</h1>", status=404)
            ext = os.path.splitext(name)[1].lower()
            ctypes = {
                ".js": "text/javascript",
                ".css": "text/css",
                ".svg": "image/svg+xml",
            }
            if ext not in ctypes:
                return self.send_html("<h1>Not found</h1>", status=404)
            fpath = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "bodypalace", sub, name,
            )
            if not os.path.isfile(fpath):
                return self.send_html("<h1>Not found</h1>", status=404)
            return self.stream_asset(fpath, ctypes[ext])
        if path == BASE + "/words":
            return self.send_html(words_page())
        if path == BASE + "/fsh":
            return self.send_html(fsh_page())
        if path == BASE + "/words/words_generator.py":
            return self.stream_file(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "words_generator.py",
                ),
                "text/x-python; charset=utf-8",
            )
        if path == BASE + "/stream":
            # Open access (2026-11-25, Taylor: "Deploy it live, open
            # access"): the Stream Mic page is public — no sign-in.
            return self.send_html(stream_page())
        if path == BASE + "/stream/sessions":
            # Jasmine's read access (Taylor, 2026-11-25: "Allow all
            # for yourself also" / "Use my microphone"). Gated by the
            # STREAM_READ_TOKEN header; unset token or a wrong one
            # answers 404, exactly as if the route did not exist.
            if not STREAM_READ_TOKEN or (
                self.headers.get("X-Stream-Read") or ""
            ) != STREAM_READ_TOKEN:
                return self.send_html("<h1>Not found</h1>", status=404)
            return self.send_json({"sessions": list_stream_sessions()})
        if path == BASE + "/stream/chunk":
            if not STREAM_READ_TOKEN or (
                self.headers.get("X-Stream-Read") or ""
            ) != STREAM_READ_TOKEN:
                return self.send_html("<h1>Not found</h1>", status=404)
            qs = parse_qs(parsed.query)
            fpath, ext = stream_chunk_path(
                qs.get("session", [""])[0], qs.get("seq", [""])[0]
            )
            if not fpath:
                return self.send_html("<h1>Not found</h1>", status=404)
            return self.stream_file(
                fpath,
                {
                    "m4a": "audio/mp4",
                    "mp4": "audio/mp4",
                    "webm": "audio/webm",
                    "ogg": "audio/ogg",
                    "wav": "audio/wav",
                }.get(ext, "application/octet-stream"),
            )
        if path == BASE + "/stream/stream_receiver.py":
            return self.stream_file(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "stream_receiver.py",
                ),
                "text/x-python; charset=utf-8",
            )
        if path == BASE + "/blocks":
            return self.send_html(blocks_page())
        if path == BASE + "/blocks/submit":
            qs = parse_qs(parsed.query)
            message = qs.get("msg", [""])[0]
            return self.send_html(
                blocks_submit_page(message, ok=bool(message))
            )
        if path == BASE + "/blocks/admin":
            member, _ = self.current_member()
            # Fail closed: the approval queue exists only for the owner.
            if not member or member["email"].lower() != OWNER_EMAIL:
                return self.send_html("<h1>Forbidden</h1>", status=403)
            qs = parse_qs(parsed.query)
            message = qs.get("msg", [""])[0]
            return self.send_html(
                blocks_admin_page(member, message, ok=bool(message))
            )
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
                    "automatically as soon as the payment is "
                    "confirmed — refresh this page in a few seconds."
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
        if path == BASE + "/owner/paylinks":
            member, _ = self.current_member()
            # Fail closed: the Pay Links page exists only for the owner.
            if not member or member["email"].lower() != OWNER_EMAIL:
                return self.send_html("<h1>Forbidden</h1>", status=403)
            qs = parse_qs(parsed.query)
            message = qs.get("msg", [""])[0]
            return self.send_html(
                owner_paylinks_page(member, message, ok=bool(message))
            )
        if path.startswith(BASE + "/content/"):
            rest = path[len(BASE + "/content/"):]
            key, _, sub = rest.partition("/")
            return self.serve_content(key, sub)
        return self.send_html("<h1>Not found</h1>", status=404)

    def stream_asset(self, fpath, content_type):
        """Like stream_file but without the attachment disposition —
        for app assets (JS/CSS/SVG) the browser must load inline."""
        size = os.path.getsize(fpath)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(fpath, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

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
        return self.send_html(PAGE.format(base=BASE, title="Teach-Back Collection", body=body))

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
        if self._bug_blocked():
            return self.send_html("<h1>403 Forbidden</h1>", status=403)
        path = urlparse(self.path).path
        if path == BASE + "/admin/upload":
            return self.admin_upload()
        if path == BASE + "/upload":
            return self.member_upload()
        if path == BASE + "/stream/upload":
            # Stream Mic chunk intake (2026-11-24; open access kept
            # 2026-11-25 at Taylor's direction while the stream code
            # reverted to the original build). Public, raw body (not
            # a form): the phone page POSTs one audio chunk per 10
            # seconds with session/seq/ext in the query.
            qs = parse_qs(urlparse(self.path).query)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_CHUNK_BYTES:
                return self.send_html("bad size", status=400)
            data = self.rfile.read(length)
            ok, msg = save_stream_chunk(
                qs.get("session", [""])[0],
                qs.get("seq", [""])[0],
                qs.get("ext", [""])[0],
                data,
            )
            return self.send_html(
                ("ok " + msg) if ok else ("error " + msg),
                status=200 if ok else 400,
            )
        if path == BASE + "/emsapp/api/evaluate" or path == BASE + "/emsapp/api/epcr":
            # EMS Protocol APP JSON APIs (2026-11-26): the safety-gate
            # evaluator and the ePCR smart-phrase generators. Public,
            # stateless, nothing stored.
            try:
                payload = json.loads(self.raw_body() or b"{}")
            except (ValueError, UnicodeDecodeError):
                return self.send_json({"error": "bad json"}, status=400)
            if path.endswith("/evaluate"):
                return self.send_json(ems_evaluate(payload))
            kind = payload.get("kind")
            fields = payload.get("fields") or {}
            if kind == "acs":
                return self.send_json({"text": epcr_acs(fields)})
            if kind == "hyperkalemia":
                return self.send_json({"text": epcr_hyperkalemia(fields)})
            return self.send_json({"error": "unknown kind"}, status=400)
        form = self.form()
        if path == BASE + "/owner/paylinks":
            member, _ = self.current_member()
            # Fail closed: only the owner can save or clear Pay Links.
            if not member or member["email"].lower() != OWNER_EMAIL:
                return self.send_html("<h1>Forbidden</h1>", status=403)
            links = {}
            for c in COURSES:
                v = form.get(f"godaddy_link_{c['key']}", "").strip()
                if v and not v.startswith("https://"):
                    return self.send_html(owner_paylinks_page(
                        member,
                        "That link doesn't look right — a Pay Link "
                        "starts with https://. Nothing was saved.",
                    ))
                links[c["key"]] = v
            for key, v in links.items():
                _set_setting(f"godaddy_link_{key}", v)
            return self.redirect(
                BASE + "/owner/paylinks?msg=" + quote(
                    "Pay links saved — each buy page now shows the "
                    "card button for the links you set."
                )
            )
        if path == BASE + "/blocks/submit":
            # Public submission (2026-11-22, Taylor: anyone submits,
            # Taylor approves). Lands as 'pending'; never rendered
            # until the owner approves it on /blocks/admin.
            name = form.get("name", "").strip()[:60]
            station = form.get("station", "").strip()[:60]
            block_date = form.get("block_date", "").strip()[:10]
            start_time = form.get("start_time", "").strip()[:5]
            end_time = form.get("end_time", "").strip()[:5]
            pay = form.get("pay", "").strip()[:20]
            notes = form.get("notes", "").strip()[:1000]
            if not (name and station and block_date and pay):
                return self.send_html(blocks_submit_page(
                    "Name, station, block date and pay are required — "
                    "nothing was sent."))
            # Auto-approval (Taylor, 2026-11-22): a submission that
            # meets every posted criterion posts immediately; any
            # other waits in the owner's queue — never auto-rejected.
            fails = evaluate_block_submission(
                name, station, block_date, start_time, end_time,
                pay, notes)
            if not fails:
                add_block_submission(name, station, block_date,
                                     start_time, end_time, pay,
                                     notes, status="approved")
                return self.redirect(
                    BASE + "/blocks/submit?msg=" + quote(
                        "Posted — your block met the criteria and "
                        "is live on the Blocks page now."))
            add_block_submission(name, station, block_date,
                                 start_time, end_time, pay, notes)
            return self.redirect(
                BASE + "/blocks/submit?msg=" + quote(
                    "Sent — your block didn't meet the auto-post "
                    "criteria, so it's in Taylor's approval queue "
                    "for a personal look."))
        if path == BASE + "/blocks/admin":
            member, _ = self.current_member()
            # Fail closed: only the owner can approve or reject.
            if not member or member["email"].lower() != OWNER_EMAIL:
                return self.send_html("<h1>Forbidden</h1>", status=403)
            action = form.get("action", "")
            new_status = {"approve": "approved",
                          "reject": "rejected"}.get(action)
            try:
                sub_id = int(form.get("id", ""))
            except ValueError:
                sub_id = 0
            if new_status and sub_id and set_block_submission_status(
                    sub_id, new_status):
                verdict = ("approved — it is live on the Blocks page"
                           if new_status == "approved" else
                           "rejected — it is not public")
                return self.redirect(
                    BASE + "/blocks/admin?msg=" + quote(
                        f"Submission {verdict}."))
            return self.redirect(
                BASE + "/blocks/admin?msg=" + quote(
                    "That action didn't apply — refresh and try again."))
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
        if path == BASE + "/godaddy/order":
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
                    "VALUES (?, ?, 'godaddy', ?, ?, ?, ?)",
                    (member["id"], course["key"], course["cents"], amount_text,
                     "GoDaddy Pay Link", int(time.time())),
                )
            return self.send_html(
                desk_page(
                    member,
                    f"Card order created: if you have not paid yet, pay "
                    f"{amount_text} through the GoDaddy Pay Link on the "
                    "buy page. Access unlocks when the owner confirms "
                    "your payment.",
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
