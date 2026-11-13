#!/usr/bin/env python3
"""Files Library — Taylor's file index with downloads (static + streamer).

Python 3 stdlib ONLY (http.server). Serves one listing page under the
/files base path: every entry from library.json (generated from the All
Files Index with Taylor's hold decisions applied), grouped by project
with a client-side search filter, plus one download route per file that
is present on disk.

library.json is SANITIZED at generation time: held entries carry only
file/type/date/group (no description, no size, no download id), so held
content cannot leak through this service. Held files' bytes are never
staged into the data volume, and download ids exist only for non-held
files, so a held file is unreachable by construction.

Downloads are streamed from FILESLIB_DATA_DIR in 256 KB chunks (never
loaded whole into memory — the host has ~1.9 GB RAM). The volume is
populated out-of-band by ops/deploy-files-library.sh from a checksummed
archive; the file bytes are never in git.

Stateless: nothing is stored; the listing is rendered once at startup.
"""
import html
import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_PATH = os.environ.get("BASE_PATH", "/files")
PORT = int(os.environ.get("PORT", "8000"))
DATA_DIR = Path(os.environ.get("FILESLIB_DATA_DIR", "/data"))
_HERE = Path(__file__).parent
_TEMPLATE = (_HERE / "index.html").read_text(encoding="utf-8")
_LIBRARY = json.loads((_HERE / "library.json").read_text(encoding="utf-8"))
_ENTRIES = _LIBRARY["entries"]

_DL_ID = re.compile(r"^f\d{4}$")
# download id -> (original filename, size)
_FILES = {}
for _e in _ENTRIES:
    if _e.get("dl"):
        _FILES[_e["dl"]] = (_e["file"], _e.get("size"))


def _human_size(n):
    if not isinstance(n, (int, float)):
        return "—"
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f}".rstrip("0").rstrip(".") + " KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f}".rstrip("0").rstrip(".") + " MB"
    return f"{n / 1024 ** 3:.2f} GB"


def _row(e, held):
    if held:
        search = html.escape(
            " ".join([e["file"], e.get("type") or "", e.get("date") or ""]).lower(),
            quote=True,
        )
        main = f'<span class="file-name">{html.escape(e["file"])}</span>'
        meta = (
            f'<span>{html.escape(e.get("type") or "—")}</span>'
            f'<span>{html.escape(e.get("date") or "—")}</span>'
        )
    else:
        search = html.escape(
            " ".join(
                [
                    e["file"],
                    e.get("desc") or "",
                    e["group"],
                    e.get("type") or "",
                    e.get("date") or "",
                ]
            ).lower(),
            quote=True,
        )
        main = f'<span class="file-name">{html.escape(e["file"])}</span>'
        if e.get("desc"):
            main += f'<span class="file-desc">{html.escape(e["desc"])}</span>'
        meta = (
            f'<span>{html.escape(e.get("type") or "—")}</span>'
            f'<span>{html.escape(_human_size(e.get("size")) if e.get("dl") else "—")}</span>'
            f'<span>{html.escape(e.get("date") or "—")}</span>'
        )
        if e.get("dl"):
            meta += f'<a class="file-dl" href="{BASE_PATH}/dl/{e["dl"]}" download>Download</a>'
        else:
            meta += '<span class="file-tag">On file — available on request</span>'
    return (
        f'<li class="file-row" data-search="{search}">'
        f'<div class="file-main">{main}</div>'
        f'<div class="file-meta">{meta}</div></li>'
    )


def _render_page():
    groups = {}
    held = []
    for e in _ENTRIES:
        if e.get("held"):
            held.append(e)
        else:
            groups.setdefault(e["group"], []).append(e)
    parts = []
    for name in sorted(groups, key=lambda s: (s.lower(), s)):
        rows = sorted(groups[name], key=lambda e: (e["file"].lower(), e["file"]))
        label = "file" if len(rows) == 1 else "files"
        parts.append(
            '<div class="files-group">'
            f'<h2 class="files-group-title">{html.escape(name)} '
            f'<span class="files-group-count">{len(rows)} {label}</span></h2>'
            '<ul class="files-list">'
            + "".join(_row(e, False) for e in rows)
            + "</ul></div>"
        )
    if held:
        rows = sorted(held, key=lambda e: (e["file"].lower(), e["file"]))
        parts.append(
            '<div class="files-group files-held">'
            '<h2 class="files-group-title">Held — ask Taylor '
            f'<span class="files-group-count">{len(rows)} files</span></h2>'
            '<ul class="files-list">'
            + "".join(_row(e, True) for e in rows)
            + "</ul></div>"
        )
    counts = (
        f'{len(_ENTRIES)} entries · {len(_FILES)} downloadable files · '
        f'{sum(1 for e in _ENTRIES if not e.get("held") and not e.get("dl"))} on file by request · '
        f"{len(held)} held"
    )
    return _TEMPLATE.replace("__GROUPS__", "\n".join(parts)).replace(
        "__COUNTS__", html.escape(counts)
    ).encode()


_PAGE = _render_page()
_HEALTH = {
    "status": "ok",
    "service": "files-library",
    "entries": len(_ENTRIES),
    "downloadable_files": len(_FILES),
    "held": sum(1 for e in _ENTRIES if e.get("held")),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "FilesLibrary/1.0"

    def log_message(self, fmt, *args):  # keep container logs quiet
        pass

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _download(self, dl_id):
        name, size = _FILES[dl_id]
        path = DATA_DIR / dl_id
        if not path.is_file():
            body = json.dumps({"error": "file not staged"}).encode()
            self._send(404, body, "application/json")
            return
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        safe_name = name.replace('"', "").replace("\n", " ").replace("\r", " ")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{safe_name}"'
        )
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in (BASE_PATH, BASE_PATH + "/"):
            self._send(200, _PAGE, "text/html; charset=utf-8")
        elif path == BASE_PATH + "/health":
            body = json.dumps(_HEALTH).encode()
            self._send(200, body, "application/json")
        elif path.startswith(BASE_PATH + "/dl/"):
            dl_id = path[len(BASE_PATH) + 4 :].strip("/")
            if _DL_ID.match(dl_id) and dl_id in _FILES:
                self._download(dl_id)
            else:
                body = json.dumps({"error": "not found"}).encode()
                self._send(404, body, "application/json")
        else:
            body = json.dumps({"error": "not found"}).encode()
            self._send(404, body, "application/json")

    do_HEAD = do_GET


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
