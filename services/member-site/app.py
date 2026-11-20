#!/usr/bin/env python3
"""Member-login training site — Taylor's Transgender Patient Awareness, LLC.

Python 3 stdlib ONLY (http.server, sqlite3, hashlib.pbkdf2_hmac, urllib).
Real member accounts persist in SQLite on a volume.

Payments (on-site sales; SALES DECISION 2026-11-12, Taylor: "Sales
on our own site — buyers pay through members.911transedu.net"):
  * Cash App (the standing manual rail): the app creates a PENDING
    order showing the USD amount and Taylor's cashtag (CASHAPP_CASHTAG,
    default $taylorsprecher2026 — a public receive handle). The buyer
    puts their account email in the payment note; the owner confirms
    receipt in the roster; confirmation unlocks the product.
  * Bitcoin (same manual pattern): the app quotes BTC from the USD
    price (CoinGecko rate), creates a PENDING order, and instructs
    the buyer to send that BTC amount to Taylor's cashtag INSIDE
    Cash App, with the buyer's account email in the payment note.
    There is NO on-chain deposit address: Taylor confirmed
    2026-11-12 that Cash App shows none ("There isn't one"), so
    Bitcoin is received through Cash App's own Bitcoin send to the
    cashtag and no address is ever displayed.
  * GoDaddy Pay Links (the card rail, Taylor 2026-11-15: "Use
    GoDaddy Pay Links for cards"): per-product links saved by the
    owner in the settings table (godaddy_link_<course>) render a
    Pay by card section on each buy page; the buyer records the
    order on the site (method 'godaddy') and the owner's roster
    confirmation unlocks, like Cash App. Pay Links are one-time
    payments — the cadence price is paid per period, no
    auto-renewal.
  * Recurring subscriptions are NOT currently offered: a
    Braintree service briefly stood at /subs/ and was removed the
    same day (2026-11-15) when Taylor ruled PayPal out ("PayPal
    can't be used" — Braintree is PayPal). The cadence prices are
    paid per period through the rails above.

Billing cadence (Taylor, 2026-11-15): EMSPR protocols $6,000 every
year; the CCCE course $15 every two years per provider.

STRIPE: REMOVED COMPLETELY (Taylor, 2026-11-15: "remove Stripe
completely"). An earlier built-in Stripe Checkout integration
(hosted checkout, owner keys page, subscription webhooks) was
deleted from this app wholesale — no Stripe code, keys, settings,
or routes remain. Each product has a stable public buy page at
/members/buy/<key> showing the price, the cashtag, the note
guidance (the buyer's account email), and the owner-confirmation
unlock — the public training site's buy buttons point at those
pages.

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
Passwords: salted PBKDF2-HMAC-SHA256, never plaintext."""
from app_pkg.server import main

if __name__ == "__main__":
    main()
