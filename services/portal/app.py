#!/usr/bin/env python3
"""Network Portal — 911transedu.net live-status dashboard (static).

Python 3 stdlib ONLY (http.server). Serves the portal index page
(index.html: CSS and JS embedded) under the /portal base path, plus one
company route page per company in COMPANIES below (rendered from
company.html by substituting the company name). The index page is the
live-status portal Taylor pasted on 2026-11-12 and directed to be put
live (verbatim source: Section 1 of
~/workspace/user/files/Other_AI_Upgraded_Portal_Configs_Docs_2026-11-12.md).
On load the pages probe the eight live-server subdomains client-side
(fetch, no-cors) and show a green/red reachability dot per card, with a
manual Refresh Status button. The company routes were added 2026-11-12
at Taylor's direction ("Per company routes"); each carries the company
name and public platform links only — no individual names, emails, or
phone numbers. A company page also carries an optional per-company
Teachbacks block (COMPANY_TEACHBACK_SECTIONS below, substituted for
__TEACHBACK_SECTION__ in the template); only Hillsboro has one, for the
Hillsboro Fire Teach-Back on the training site (added 2026-11-12 at
Taylor's direction, completing the "Add ALL Teachback" pass and the
company separation).

Stateless: nothing is stored; the probes run in the visitor's browser.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_PATH = os.environ.get("BASE_PATH", "/portal")
PORT = int(os.environ.get("PORT", "8000"))
INDEX_HTML = (Path(__file__).parent / "index.html").read_bytes()
_COMPANY_TEMPLATE = (Path(__file__).parent / "company.html").read_text(encoding="utf-8")

# slug -> company display name (pre-escaped for HTML substitution).
COMPANIES = {
    "hillsboro": "Hillsboro Fire &amp; Rescue",
    "woodburn": "Woodburn Ambulance",
    "monitor": "Monitor RFPD",
    "oha": "OHA EMS",
}

_TRAINING_SITE = (
    "https://muse.ai/s/911transedu-net-website-xkk6xaxm0ib1ak/"
    "-hQ_ySKdwECtNsqmEyusrdqJDfrhiRX9-5oxqk2dnI4"
)

# slug -> optional Teachbacks block (HTML, no trailing newline; empty for
# companies with no company-specific teachback on record). Substituted
# for __TEACHBACK_SECTION__ in company.html, followed by a newline.
COMPANY_TEACHBACK_SECTIONS = {
    "hillsboro": (
        '    <h2>Teachbacks</h2>\n'
        '    <div class="grid">\n'
        f'      <a class="card" href="{_TRAINING_SITE}#hillsboro-fire-teachback">'
        '<div class="card-top"><span>Hillsboro Fire Teach-Back</span></div>'
        '<span class="tag">This company&rsquo;s own teach-back — the 2019 '
        'Hillsboro Fire training session and delivery record</span></a>\n'
        '    </div>\n'
    ),
}


def _company_page(slug):
    page = _COMPANY_TEMPLATE.replace("__COMPANY_NAME__", COMPANIES[slug])
    section = COMPANY_TEACHBACK_SECTIONS.get(slug, "")
    return page.replace("__TEACHBACK_SECTION__", section).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "Portal/1.0"

    def log_message(self, fmt, *args):  # keep container logs quiet
        pass

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in (BASE_PATH, BASE_PATH + "/"):
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif path.startswith(BASE_PATH + "/") and path[len(BASE_PATH) + 1:].strip("/") in COMPANIES:
            slug = path[len(BASE_PATH) + 1:].strip("/")
            self._send(200, _company_page(slug), "text/html; charset=utf-8")
        elif path == BASE_PATH + "/health":
            body = json.dumps({"status": "ok", "service": "portal"}).encode()
            self._send(200, body, "application/json")
        else:
            body = json.dumps({"error": "not found"}).encode()
            self._send(404, body, "application/json")

    do_HEAD = do_GET


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
