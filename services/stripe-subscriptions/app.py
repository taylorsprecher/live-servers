#!/usr/bin/env python3
"""Stripe subscriptions service — FRESH SCRIPT (owner direction, 2026-11-15).

A standalone subscription checkout for 911transedu.net, written from
scratch at the owner's direction ("Fresh script only"). It replaces
nothing and edits nothing in the member site: it is its own service,
its own database, its own deploy. The member site's layered payment
code is not used by this service and this service is not used by it —
the one shared thing is the members-data volume, so a confirmed
subscription can unlock course access in members.db for the buyer's
member account (matched by email).

Design (Stripe Subscriptions API + Payment Element):
  * POST /subs/create-subscription creates a Stripe Customer and a
    Subscription with payment_behavior="default_incomplete" and
    payment_settings.save_default_payment_method="on_subscription",
    so the first invoice's PaymentIntent (or a SetupIntent) is
    confirmed in the browser with Stripe's Payment Element.
  * Payment methods requested: "card" (credit and debit cards) and
    "us_bank_account" (ACH direct debit, Financial Connections
    verification). Apple Pay / Google Pay ride on "card" only when
    the Stripe account enables them; the owner has directed them OFF
    (2026-11-15), which is a Stripe Dashboard payment-method setting.
  * Access is granted ONLY from the webhook: invoice.paid unlocks,
    invoice.payment_failed marks the subscription past due (Stripe
    retries; access stays until deletion), and
    customer.subscription.deleted cancels and revokes. ACH takes
    1-4 business days to clear, so nothing unlocks on the browser's
    word alone.
  * Prices are inline price_data (no Dashboard Price objects to
    create): CCCE $15 every two years per provider; EMSPR protocols
    $6,000 every year (owner-set cadence, 2026-11-15).

Python standard library ONLY — no pip installs, no Stripe SDK; the
Stripe REST API is called with urllib form encoding, the same way
the rest of this fleet talks to Stripe.

Keys come from the environment ONLY (host .env, delivered by the
deploy workflow from GitHub Actions secrets SUBS_STRIPE_*):
  STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PUBLISHABLE_KEY.
With any key missing the service runs in its unconfigured state:
pages render, checkout refuses politely, the webhook refuses.
No key value is ever logged or printed.
"""

import hashlib
import hmac
import html
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.environ.get("BASE_PATH", "/subs").rstrip("/")
PORT = int(os.environ.get("PORT", "8000"))
SUBS_DB_PATH = os.environ.get("SUBS_DB_PATH", "/data/subs.db")
MEMBERS_DB_PATH = os.environ.get("MEMBERS_DB_PATH", "/data/members.db")
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://members.911transedu.net").rstrip("/")

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_API = "https://api.stripe.com"

# Owner-set cadence (2026-11-15): EMSPR every year; CCCE every two
# years per provider. Amounts in cents.
PRODUCTS = {
    "ccce": {
        "name": "CCCE Course — Transgender Emergency Medicine (OHA CCCE)",
        "short": "CCCE Course",
        "amount_cents": 1500,
        "interval": "year",
        "interval_count": 2,
        "price_line": "$15 USD — every two years, per provider",
    },
    "emspr": {
        "name": "EMSPR Protocols — Transgender Emergency Medicine Protocols",
        "short": "EMSPR Protocols",
        "amount_cents": 600000,
        "interval": "year",
        "interval_count": 1,
        "price_line": "$6,000 USD — every year",
    },
}


def stripe_configured():
    """True only when all three keys are present in the environment."""
    return bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET
                and STRIPE_PUBLISHABLE_KEY)


# ---------------------------------------------------------------------------
# Storage (this service's own database on the shared members-data volume)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_db = None


def init_db():
    global _db
    _db = sqlite3.connect(SUBS_DB_PATH, check_same_thread=False)
    _db.row_factory = sqlite3.Row
    with _lock, _db:
        _db.execute(
            "CREATE TABLE IF NOT EXISTS customers ("
            " email TEXT PRIMARY KEY,"
            " stripe_customer_id TEXT NOT NULL,"
            " created_at INTEGER NOT NULL)")
        _db.execute(
            "CREATE TABLE IF NOT EXISTS subscriptions ("
            " stripe_subscription_id TEXT PRIMARY KEY,"
            " stripe_customer_id TEXT NOT NULL,"
            " email TEXT NOT NULL,"
            " course_key TEXT NOT NULL,"
            " status TEXT NOT NULL,"
            " member_id INTEGER,"
            " grant_note TEXT,"
            " created_at INTEGER NOT NULL,"
            " updated_at INTEGER NOT NULL)")


def _record_subscription(sub_id, customer_id, email, course_key, status):
    now = int(time.time())
    with _lock, _db:
        _db.execute(
            "INSERT INTO subscriptions (stripe_subscription_id,"
            " stripe_customer_id, email, course_key, status, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(stripe_subscription_id) DO UPDATE SET"
            " status = excluded.status, updated_at = excluded.updated_at",
            (sub_id, customer_id, email, course_key, status, now, now))


def _set_subscription_status(sub_id, status):
    with _lock, _db:
        _db.execute(
            "UPDATE subscriptions SET status = ?, updated_at = ?"
            " WHERE stripe_subscription_id = ?",
            (status, int(time.time()), sub_id))


def _get_subscription(sub_id):
    with _lock:
        return _db.execute(
            "SELECT * FROM subscriptions WHERE stripe_subscription_id = ?",
            (sub_id,)).fetchone()


# ---------------------------------------------------------------------------
# Member access (members.db on the same volume; email is the match key)
# ---------------------------------------------------------------------------

def _members_conn():
    conn = sqlite3.connect(MEMBERS_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def grant_member_access(email, course_key):
    """Unlock course_key for the member account with this email.
    Returns (member_id, note). No account -> (None, note); the
    subscription still stands at Stripe, and the note says why no
    access row exists yet."""
    conn = _members_conn()
    try:
        row = conn.execute(
            "SELECT id FROM members WHERE lower(email) = lower(?)",
            (email,)).fetchone()
        if row is None:
            return None, "no member account for this email yet"
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO access (member_id, course_key)"
                " VALUES (?, ?)", (row["id"], course_key))
        return row["id"], "access granted"
    finally:
        conn.close()


def revoke_member_access(member_id, course_key, keep_subscription_id):
    """Remove the access row unless another live subscription in this
    service still covers the same member + course."""
    with _lock:
        other = _db.execute(
            "SELECT COUNT(*) AS n FROM subscriptions"
            " WHERE member_id = ? AND course_key = ?"
            " AND stripe_subscription_id != ?"
            " AND status IN ('active', 'past_due')",
            (member_id, course_key, keep_subscription_id)).fetchone()
    if other and other["n"]:
        return False
    conn = _members_conn()
    try:
        with conn:
            conn.execute(
                "DELETE FROM access WHERE member_id = ? AND course_key = ?",
                (member_id, course_key))
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stripe REST client (stdlib urllib, form-encoded like the API expects)
# ---------------------------------------------------------------------------

class StripeError(Exception):
    pass


def _stripe_request(method, path, params=None):
    data = None
    if params is not None:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        STRIPE_API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY,
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode())
            msg = detail.get("error", {}).get("message") or str(e)
        except Exception:
            msg = str(e)
        raise StripeError(msg)
    except urllib.error.URLError as e:
        raise StripeError("could not reach Stripe: %s" % e.reason)


def find_or_create_customer(email):
    with _lock:
        row = _db.execute(
            "SELECT stripe_customer_id FROM customers WHERE email = ?",
            (email,)).fetchone()
    if row:
        return row["stripe_customer_id"]
    found = _stripe_request(
        "GET", "/v1/customers?" + urllib.parse.urlencode(
            {"email": email, "limit": 1}))
    if found.get("data"):
        customer_id = found["data"][0]["id"]
    else:
        created = _stripe_request("POST", "/v1/customers", {
            "email": email,
            "metadata[source]": "911transedu-subs-service",
        })
        customer_id = created["id"]
    with _lock, _db:
        _db.execute(
            "INSERT OR REPLACE INTO customers (email, stripe_customer_id,"
            " created_at) VALUES (?, ?, ?)",
            (email, customer_id, int(time.time())))
    return customer_id


def create_subscription(email, course_key):
    """Create the default_incomplete subscription and return
    (subscription_id, client_secret, intent_type). Raises StripeError."""
    product = PRODUCTS[course_key]
    customer_id = find_or_create_customer(email)
    params = {
        "customer": customer_id,
        "items[0][price_data][currency]": "usd",
        "items[0][price_data][unit_amount]": str(product["amount_cents"]),
        "items[0][price_data][product_data][name]": product["name"],
        "items[0][price_data][recurring][interval]": product["interval"],
        "items[0][price_data][recurring][interval_count]":
            str(product["interval_count"]),
        "payment_behavior": "default_incomplete",
        "payment_settings[save_default_payment_method]": "on_subscription",
        "payment_settings[payment_method_types][0]": "card",
        "payment_settings[payment_method_types][1]": "us_bank_account",
        "payment_settings[payment_method_options][us_bank_account]"
        "[financial_connections][permissions][0]": "payment_method",
        "payment_settings[payment_method_options][us_bank_account]"
        "[verification_method]": "automatic",
        "payment_settings[payment_method_options][card]"
        "[request_three_d_secure]": "automatic",
        "metadata[email]": email,
        "metadata[course_key]": course_key,
        "expand[0]": "latest_invoice.payment_intent",
        "expand[1]": "pending_setup_intent",
    }
    sub = _stripe_request("POST", "/v1/subscriptions", params)
    _record_subscription(sub["id"], customer_id, email, course_key,
                         sub.get("status") or "incomplete")
    setup_intent = sub.get("pending_setup_intent")
    if isinstance(setup_intent, dict) and setup_intent.get("client_secret"):
        return sub["id"], setup_intent["client_secret"], "setup"
    invoice = sub.get("latest_invoice") or {}
    intent = invoice.get("payment_intent") if isinstance(invoice, dict) \
        else None
    if isinstance(intent, dict) and intent.get("client_secret"):
        return sub["id"], intent["client_secret"], "payment"
    raise StripeError("Stripe returned no client secret for the"
                      " subscription's first invoice")


# ---------------------------------------------------------------------------
# Webhook (the only path that grants or revokes access)
# ---------------------------------------------------------------------------

def verify_signature(payload, sig_header, secret, tolerance=300):
    if not sig_header:
        return False
    timestamp = None
    signatures = []
    for part in sig_header.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if not timestamp or not signatures:
        return False
    try:
        if abs(int(time.time()) - int(timestamp)) > tolerance:
            return False
    except ValueError:
        return False
    signed = timestamp.encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in signatures)


def handle_webhook_event(event):
    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    if etype == "invoice.paid":
        sub_id = obj.get("subscription")
        row = _get_subscription(sub_id) if sub_id else None
        if row is None:
            return  # not one of this service's subscriptions
        member_id, note = grant_member_access(row["email"],
                                              row["course_key"])
        with _lock, _db:
            _db.execute(
                "UPDATE subscriptions SET status = 'active', member_id = ?,"
                " grant_note = ?, updated_at = ?"
                " WHERE stripe_subscription_id = ?",
                (member_id, note, int(time.time()), sub_id))
    elif etype == "invoice.payment_failed":
        sub_id = obj.get("subscription")
        row = _get_subscription(sub_id) if sub_id else None
        if row is not None and row["status"] == "active":
            _set_subscription_status(sub_id, "past_due")
    elif etype == "customer.subscription.deleted":
        sub_id = obj.get("id")
        row = _get_subscription(sub_id) if sub_id else None
        if row is not None:
            _set_subscription_status(sub_id, "canceled")
            if row["member_id"]:
                revoke_member_access(row["member_id"], row["course_key"],
                                     sub_id)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — 911transedu.net</title>
<style>
 body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
   margin: 0; background: #f4f6f9; color: #1c2733; }}
 header {{ background: #0d2f52; color: #fff; padding: 18px 20px; }}
 header h1 {{ margin: 0; font-size: 20px; }}
 header p {{ margin: 4px 0 0; opacity: .85; font-size: 14px; }}
 main {{ max-width: 640px; margin: 24px auto; padding: 0 16px 40px; }}
 .card {{ background: #fff; border: 1px solid #d8dee6; border-radius: 10px;
   padding: 18px; margin-bottom: 16px; }}
 .price {{ font-size: 18px; font-weight: 600; }}
 button {{ background: #0d2f52; color: #fff; border: 0; border-radius: 8px;
   padding: 12px 18px; font-size: 16px; cursor: pointer; width: 100%; }}
 button:disabled {{ opacity: .55; }}
 input {{ width: 100%; box-sizing: border-box; padding: 11px;
   border: 1px solid #b9c4d0; border-radius: 8px; font-size: 16px; }}
 label {{ display: block; font-weight: 600; margin: 10px 0 6px; }}
 .note {{ font-size: 14px; color: #46586a; }}
 .err {{ color: #a12622; font-weight: 600; }}
 a {{ color: #0d2f52; }}
</style></head><body>
<header><h1>Subscribe — 911transedu.net</h1>
<p>Transgender Emergency Medicine training, by subscription.</p></header>
<main>{body}</main></body></html>"""


def subscribe_page():
    if not stripe_configured():
        body = (
            '<div class="card"><h2>Card subscriptions are not switched'
            " on yet</h2>"
            '<p class="note">This checkout opens as soon as the Stripe'
            " keys are delivered to the server. Until then, both"
            " products are available today by Cash App on their member"
            " buy pages — access unlocks when the owner confirms your"
            " payment.</p>"
            '<p><a href="/members/apps">Back to the apps hub</a></p>'
            "</div>")
        return PAGE.format(title="Subscribe", body=body)
    cards = ""
    for key, p in PRODUCTS.items():
        cards += (
            '<div class="card"><h2>{short}</h2>'
            '<p class="price">{price}</p>'
            '<button data-course="{key}">Subscribe — {short}</button>'
            "</div>").format(short=html.escape(p["short"]),
                              price=html.escape(p["price_line"]),
                              key=key)
    body = (
        '<div class="card"><label for="email">Your member account'
        " email</label>"
        '<input id="email" type="email" autocomplete="email"'
        ' placeholder="you@example.com">'
        '<p class="note">Use the same email as your member account on'
        " this site — your course access unlocks on that account"
        " automatically when Stripe confirms the first payment. Bank"
        " (ACH) payments take a few business days to clear; cards are"
        " confirmed right away.</p>"
        '<p class="err" id="err"></p></div>'
        + cards
        + '<div class="card" id="paybox" hidden><h2>Payment</h2>'
          '<div id="payment-element"></div>'
          '<p></p><button id="pay">Confirm subscription</button>'
          '<p class="err" id="payerr"></p></div>'
          '<script src="https://js.stripe.com/v3/"></script>'
          "<script>"
          "const stripe = Stripe(" + json.dumps(STRIPE_PUBLISHABLE_KEY)
          + ");"
          "let elements = null, intentType = null;"
          "async function start(course) {"
          "  const email = document.getElementById('email').value.trim();"
          "  const err = document.getElementById('err'); err.textContent = '';"
          "  if (!email) { err.textContent = 'Enter your member email"
          " first.'; return; }"
          "  const res = await fetch('create-subscription', {method:"
          " 'POST', headers: {'Content-Type': 'application/json'}, body:"
          " JSON.stringify({email: email, course_key: course})});"
          "  const data = await res.json();"
          "  if (!res.ok) { err.textContent = data.error || 'Could not"
          " start the subscription.'; return; }"
          "  intentType = data.intentType;"
          "  elements = stripe.elements({clientSecret:"
          " data.clientSecret});"
          "  elements.create('payment').mount('#payment-element');"
          "  document.getElementById('paybox').hidden = false;"
          "  document.getElementById('paybox').scrollIntoView();"
          "}"
          "document.querySelectorAll('button[data-course]').forEach("
          "b => b.addEventListener('click', () => start(b.dataset.course)));"
          "document.getElementById('pay').addEventListener('click',"
          " async () => {"
          "  const perr = document.getElementById('payerr');"
          "  perr.textContent = '';"
          "  const confirmParams = {return_url: "
          + json.dumps(PUBLIC_BASE_URL + BASE + "/welcome") + "};"
          "  const fn = intentType === 'setup' ? 'confirmSetup' :"
          " 'confirmPayment';"
          "  const {error} = await stripe[fn]({elements, confirmParams});"
          "  if (error) perr.textContent = error.message;"
          "});"
          "</script>")
    return PAGE.format(title="Subscribe", body=body)


def welcome_page():
    body = (
        '<div class="card"><h2>Thank you — subscription received</h2>'
        '<p class="note">Stripe is confirming your payment. Card'
        " payments confirm right away; bank (ACH) payments take a few"
        " business days to clear. Your course access unlocks on your"
        ' member account automatically as soon as the payment is'
        ' confirmed — sign in at <a href="/members/">the member'
        " site</a> to check.</p></div>")
    return PAGE.format(title="Subscribed", body=body)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == BASE + "/health":
            self._json(200, {"status": "ok",
                             "stripe_configured": stripe_configured()})
        elif path in (BASE, BASE + "/"):
            self._send(200, subscribe_page())
        elif path == BASE + "/welcome":
            self._send(200, welcome_page())
        else:
            self._send(404, "Not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path == BASE + "/create-subscription":
            if not stripe_configured():
                self._json(503, {"error": "Card subscriptions are not"
                                         " switched on yet."})
                return
            try:
                payload = json.loads(raw.decode() or "{}")
            except ValueError:
                self._json(400, {"error": "Bad request."})
                return
            email = (payload.get("email") or "").strip().lower()
            course_key = payload.get("course_key") or ""
            if "@" not in email or "." not in email:
                self._json(400, {"error": "Enter a valid email address."})
                return
            if course_key not in PRODUCTS:
                self._json(400, {"error": "Unknown product."})
                return
            try:
                sub_id, client_secret, intent_type = create_subscription(
                    email, course_key)
            except StripeError as e:
                self._json(502, {"error": "Stripe: %s" % e})
                return
            self._json(200, {"subscriptionId": sub_id,
                             "clientSecret": client_secret,
                             "intentType": intent_type})
        elif path == BASE + "/webhook":
            if not STRIPE_WEBHOOK_SECRET:
                self._json(503, {"error": "Webhook is not configured."})
                return
            if not verify_signature(raw, self.headers.get("Stripe-Signature"),
                                    STRIPE_WEBHOOK_SECRET):
                self._json(400, {"error": "Invalid signature."})
                return
            try:
                event = json.loads(raw.decode())
            except ValueError:
                self._json(400, {"error": "Bad payload."})
                return
            handle_webhook_event(event)
            self._json(200, {"status": "received"})
        else:
            self._send(404, "Not found")


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("stripe-subscriptions listening on port %d (base %s,"
          " configured: %s)" % (PORT, BASE, stripe_configured()),
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
