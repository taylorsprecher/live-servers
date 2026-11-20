"""Files Library — HTTP dispatch.

Route inventory (DESIGN.md §5, preserved verbatim, plus the one
allowed addition, the vendored design-system stylesheet):

  GET {BASE}/                  listing page (rendered once at startup)
  GET {BASE}/health            entry/downloadable/held counts
  GET {BASE}/dl/<id>           download, streamed from FILESLIB_DATA_DIR
                               in 256 KB chunks (never loaded whole
                               into memory)
  GET {BASE}/assets/fleet.css  vendored Fleet design system (new)
  anything else                404 {"error": "not found"}
"""
import json
import mimetypes
from http.server import BaseHTTPRequestHandler

from .config import BASE_PATH, DATA_DIR
from .pages import FLEET_CSS, PAGE
from .store import DL_ID, FILES, HEALTH


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
        name, size = FILES[dl_id]
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
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == BASE_PATH + "/health":
            body = json.dumps(HEALTH).encode()
            self._send(200, body, "application/json")
        elif path.startswith(BASE_PATH + "/dl/"):
            dl_id = path[len(BASE_PATH) + 4 :].strip("/")
            if DL_ID.match(dl_id) and dl_id in FILES:
                self._download(dl_id)
            else:
                body = json.dumps({"error": "not found"}).encode()
                self._send(404, body, "application/json")
        elif path == BASE_PATH + "/assets/fleet.css":
            self._send(200, FLEET_CSS, "text/css; charset=utf-8")
        else:
            body = json.dumps({"error": "not found"}).encode()
            self._send(404, body, "application/json")

    do_HEAD = do_GET
