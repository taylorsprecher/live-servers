"""HTML rendering on the Fleet design system.

The landing page carries the same text as the monolith's
STATUS_PAGE, re-skinned onto the shared header/footer shell
(design-system/README.md). fleet_css_bytes() serves the vendored
byte-identical copy of the canonical design-system stylesheet.
"""

import os

from .config import BASE

_ASSETS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets"
)
_css_cache = None


def fleet_css_bytes():
    global _css_cache
    if _css_cache is None:
        with open(os.path.join(_ASSETS, "fleet.css"), "rb") as fh:
            _css_cache = fh.read()
    return _css_cache


def status_page():
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Payment Ledger — 911transedu.net</title>
<link rel="stylesheet" href="{BASE}/assets/fleet.css">
</head><body>
<header class="fl-header"><div class="fl-header-inner">
  <a class="fl-wordmark" href="/members/">911TransEdu</a>
  <span class="fl-service">Payment Ledger</span>
  <nav class="fl-nav">
    <a href="{BASE}/health">Health</a>
    <a href="{BASE}/balances">Balances</a>
    <a href="{BASE}/exceptions">Exceptions</a>
    <a href="{BASE}/recon/status">Recon status</a>
  </nav>
</div></header>
<main class="fl-main">
<div class="fl-card">
<h1>Payment Ledger</h1>
<p>Payment platform foundation for Taylor's Transgender Patient
Awareness, LLC: a PostgreSQL payment-intent state machine, an
immutable double-entry ledger, idempotent webhook/file ingestion,
three-way reconciliation, an Interchange++ fee engine, and a
routing decision log.</p>
<p><strong>No payment processor is connected.</strong> Events are
recorded through the API and file-ingestion endpoints; balances and
exceptions are readable at <code>GET /ledger/balances</code> and
<code>GET /ledger/exceptions</code>.</p>
<p>Health: <code>GET /ledger/health</code></p>
</div>
</main>
<footer class="fl-footer"><div class="fl-footer-inner">
  <span>Taylor's Transgender Patient Awareness, LLC</span>
  <a href="https://911transedu.net">911transedu.net</a>
</div></footer>
</body></html>"""


STATUS_PAGE = status_page()
