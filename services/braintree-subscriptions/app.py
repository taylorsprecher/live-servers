#!/usr/bin/env python3
"""Braintree subscriptions service — FRESH SCRIPT (owner direction,
2026-11-15: "remove Stripe completely", "Fresh script only",
"Install").

Standalone subscription checkout for 911transedu.net on Braintree
(by PayPal), written from scratch. It replaces the short-lived
Stripe subscriptions service outright: that service's files are
deleted from this repo and its container is removed by this
service's deploy. The member site's own code is not touched; the
one shared thing is the members-data volume, so a confirmed
subscription can unlock course access in members.db for the buyer's
member account (matched by email).

Design (Braintree Vault + Recurring Billing):
  * The browser collects card, Apple Pay, Google Pay, or bank (ACH)
    details through Braintree's Drop-in UI and returns a one-time
    payment_method_nonce; the server vaults it via customer.create
    (card verification on) and starts the subscription against the
    vaulted payment_method_token and a Control Panel Plan ID.
    Plans carry the prices in Braintree: CCCE $15 every two years
    per provider; EMSPR protocols $6,000 every year (owner-set
    cadence, 2026-11-15) — the plan IDs arrive by environment.
  * Credit AND debit cards are accepted. (The reference design's
    optional debit-only rejection is deliberately NOT implemented:
    the owner's prices accept credit cards.)
  * ACH micro-deposit flow (two stages, for bank accounts verified
    by routing + account number instead of the Drop-in bank flow):
    stage 1 vaults the account with verification_method
    "micro_transfers" and stores the pending verification
    server-side; stage 2 (1-2 business days later) confirms the two
    deposit amounts and only then creates the subscription.
  * Access is granted ONLY from the webhook:
    subscription_charged_successfully (or went_active) unlocks,
    subscription_charged_unsuccessfully marks past due, and
    subscription_canceled revokes. Nothing unlocks on the
    browser's word alone.

The HTTP layer is Python standard library; the one dependency is
the official Braintree Python SDK (pip, in the Dockerfile), which
owns Braintree's XML API and webhook signature verification.

Credentials come from the environment ONLY (host .env, delivered
by the deploy workflow from GitHub Actions secrets BT_BRAINTREE_*):
BRAINTREE_MERCHANT_ID, BRAINTREE_PUBLIC_KEY, BRAINTREE_PRIVATE_KEY,
BRAINTREE_ENVIRONMENT (sandbox|production), BRAINTREE_PLAN_CCCE,
BRAINTREE_PLAN_EMSPR, and optionally BRAINTREE_GOOGLE_MERCHANT_ID.
With any required value missing the service runs unconfigured:
pages render, checkout refuses politely. No credential value is
ever logged or printed.
"""

import html
import json
import os
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import braintree

BASE = os.environ.get("BASE_PATH", "/subs").rstrip("/")
PORT = int(os.environ.get("PORT", "8000"))
BTSUBS_DB_PATH = os.environ.get("BTSUBS_DB_PATH", "/data/btsubs.db")
MEMBERS_DB_PATH = os.environ.get("MEMBERS_DB_PATH", "/data/members.db")
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://members.911transedu.net").rstrip("/")

BT_ENVIRONMENT = os.environ.get("BRAINTREE_ENVIRONMENT", "sandbox")
BT_MERCHANT_ID = os.environ.get("BRAINTREE_MERCHANT_ID", "")
BT_PUBLIC_KEY = os.environ.get("BRAINTREE_PUBLIC_KEY", "")
BT_PRIVATE_KEY = os.environ.get("BRAINTREE_PRIVATE_KEY", "")
BT_PLAN_CCCE = os.environ.get("BRAINTREE_PLAN_CCCE", "")
BT_PLAN_EMSPR = os.environ.get("BRAINTREE_PLAN_EMSPR", "")
BT_GOOGLE_MERCHANT_ID = os.environ.get("BRAINTREE_GOOGLE_MERCHANT_ID", "")

BUSINESS_NAME = "Taylor's Transgender Patient Awareness, LLC"
DISPLAY_NAME = "911transedu.net"

# Display + wallet totals. The billing amounts and intervals live in
# the Braintree Control Panel plans (plan IDs by environment).
PRODUCTS = {
    "ccce": {
        "short": "CCCE Course",
        "name": "CCCE Course — Transgender Emergency Medicine (OHA CCCE)",
        "price_line": "$15 USD — every two years, per provider",
        "amount": "15.00",
        "plan_env": "BRAINTREE_PLAN_CCCE",
    },
    "emspr": {
        "short": "EMSPR Protocols",
        "name": "EMSPR Protocols — Transgender Emergency Medicine Protocols",
        "price_line": "$6,000 USD — every year",
        "amount": "6000.00",
        "plan_env": "BRAINTREE_PLAN_EMSPR",
    },
}


def plan_id_for(course_key):
    return {"ccce": BT_PLAN_CCCE, "emspr": BT_PLAN_EMSPR}.get(course_key, "")


def bt_configured():
    return bool(BT_MERCHANT_ID and BT_PUBLIC_KEY and BT_PRIVATE_KEY
                and BT_PLAN_CCCE and BT_PLAN_EMSPR)


def make_gateway():
    environment = (braintree.Environment.Production
                   if BT_ENVIRONMENT == "production"
                   else braintree.Environment.Sandbox)
    return braintree.BraintreeGateway(
        braintree.Configuration(
            environment=environment,
            merchant_id=BT_MERCHANT_ID,
            public_key=BT_PUBLIC_KEY,
            private_key=BT_PRIVATE_KEY,
        ))


_gateway = None


def gateway():
    global _gateway
    if _gateway is None:
        _gateway = make_gateway()
    return _gateway


# ---------------------------------------------------------------------------
# Storage (this service's own database on the shared members-data volume)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_db = None


def init_db():
    global _db
    _db = sqlite3.connect(BTSUBS_DB_PATH, check_same_thread=False)
    _db.row_factory = sqlite3.Row
    with _lock, _db:
        _db.execute(
            "CREATE TABLE IF NOT EXISTS subscriptions ("
            " bt_subscription_id TEXT PRIMARY KEY,"
            " bt_customer_id TEXT,"
            " email TEXT NOT NULL,"
            " course_key TEXT NOT NULL,"
            " status TEXT NOT NULL,"
            " member_id INTEGER,"
            " grant_note TEXT,"
            " created_at INTEGER NOT NULL,"
            " updated_at INTEGER NOT NULL)")
        _db.execute(
            "CREATE TABLE IF NOT EXISTS ach_pending ("
            " email TEXT NOT NULL,"
            " course_key TEXT NOT NULL,"
            " bt_customer_id TEXT NOT NULL,"
            " payment_method_token TEXT NOT NULL,"
            " verification_id TEXT NOT NULL,"
            " status TEXT NOT NULL,"
            " created_at INTEGER NOT NULL,"
            " PRIMARY KEY (email, course_key))")


def _record_subscription(sub_id, customer_id, email, course_key, status):
    now = int(time.time())
    with _lock, _db:
        _db.execute(
            "INSERT INTO subscriptions (bt_subscription_id, bt_customer_id,"
            " email, course_key, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(bt_subscription_id) DO UPDATE SET"
            " status = excluded.status, updated_at = excluded.updated_at",
            (sub_id, customer_id, email, course_key, status, now, now))


def _set_subscription_status(sub_id, status):
    with _lock, _db:
        _db.execute(
            "UPDATE subscriptions SET status = ?, updated_at = ?"
            " WHERE bt_subscription_id = ?",
            (status, int(time.time()), sub_id))


def _get_subscription(sub_id):
    with _lock:
        return _db.execute(
            "SELECT * FROM subscriptions WHERE bt_subscription_id = ?",
            (sub_id,)).fetchone()


def _store_ach_pending(email, course_key, customer_id, token,
                       verification_id):
    with _lock, _db:
        _db.execute(
            "INSERT OR REPLACE INTO ach_pending (email, course_key,"
            " bt_customer_id, payment_method_token, verification_id,"
            " status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (email, course_key, customer_id, token, verification_id,
             int(time.time())))


def _get_ach_pending(email, course_key):
    with _lock:
        return _db.execute(
            "SELECT * FROM ach_pending WHERE email = ? AND course_key = ?",
            (email, course_key)).fetchone()


def _clear_ach_pending(email, course_key):
    with _lock, _db:
        _db.execute(
            "DELETE FROM ach_pending WHERE email = ? AND course_key = ?",
            (email, course_key))


# ---------------------------------------------------------------------------
# Member access (members.db on the same volume; email is the match key)
# ---------------------------------------------------------------------------

def _members_conn():
    conn = sqlite3.connect(MEMBERS_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def grant_member_access(email, course_key):
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
    with _lock:
        other = _db.execute(
            "SELECT COUNT(*) AS n FROM subscriptions"
            " WHERE member_id = ? AND course_key = ?"
            " AND bt_subscription_id != ?"
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
# Braintree flows
# ---------------------------------------------------------------------------

class FlowError(Exception):
    pass


def _result_message(result):
    return getattr(result, "message", None) or "Braintree declined the request"


def start_subscription_with_token(customer_id, token, email, course_key):
    """Create the recurring subscription against a vaulted token and
    record it. Returns the Braintree subscription object."""
    plan_id = plan_id_for(course_key)
    if not plan_id:
        raise FlowError("No Braintree plan is configured for this product")
    result = gateway().subscription.create({
        "payment_method_token": token,
        "plan_id": plan_id,
    })
    if not result.is_success:
        raise FlowError(_result_message(result))
    sub = result.subscription
    status = (sub.status or "").lower()
    _record_subscription(sub.id, customer_id, email, course_key,
                         "active" if status == "active" else status)
    return sub


def create_subscription(email, first_name, last_name, nonce, device_data,
                        course_key):
    """Drop-in flow: vault the nonce on a new customer, then subscribe."""
    params = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "payment_method_nonce": nonce,
        "credit_card": {"options": {"verify_card": True}},
    }
    if device_data:
        params["device_data"] = device_data
    result = gateway().customer.create(params)
    if not result.is_success:
        raise FlowError(_result_message(result))
    customer = result.customer
    if not customer.payment_methods:
        raise FlowError("Braintree did not vault a payment method")
    token = customer.payment_methods[0].token
    sub = start_subscription_with_token(customer.id, token, email,
                                        course_key)
    return sub


def initiate_micro_deposits(email, first_name, last_name, nonce,
                            device_data, course_key):
    """ACH stage 1: vault the bank account with micro_transfers
    verification; the subscription starts only after stage 2."""
    params = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "payment_method_nonce": nonce,
        "us_bank_account": {"verification_method": "micro_transfers"},
    }
    if device_data:
        params["device_data"] = device_data
    result = gateway().customer.create(params)
    if not result.is_success:
        raise FlowError(_result_message(result))
    customer = result.customer
    if not customer.us_bank_accounts:
        raise FlowError("Braintree did not vault a bank account")
    account = customer.us_bank_accounts[0]
    if not account.verifications:
        raise FlowError("Braintree returned no verification for the"
                        " bank account")
    verification = account.verifications[0]
    _store_ach_pending(email, course_key, customer.id, account.token,
                       verification.id)
    return verification


def confirm_and_subscribe(email, course_key, amounts_cents):
    """ACH stage 2: confirm the two micro-deposit amounts; when the
    verification reads verified, start the subscription."""
    pending = _get_ach_pending(email, course_key)
    if pending is None:
        raise FlowError("No pending bank verification for this email"
                        " and product — start with stage 1 first")
    amounts = [str(int(a)) for a in amounts_cents]
    result = gateway().us_bank_account_verification \
        .confirm_micro_transfer_amounts(pending["verification_id"], amounts)
    if not result.is_success:
        raise FlowError("Micro-deposit verification failed: %s"
                        % _result_message(result))
    verification = result.us_bank_account_verification
    if verification.status != braintree.UsBankAccountVerification \
            .Status.Verified:
        raise FlowError("Bank account not verified yet (status: %s)"
                        % verification.status)
    sub = start_subscription_with_token(
        pending["bt_customer_id"], pending["payment_method_token"],
        email, course_key)
    _clear_ach_pending(email, course_key)
    return sub


# ---------------------------------------------------------------------------
# Webhook (the only path that grants or revokes access)
# ---------------------------------------------------------------------------

def handle_webhook_notification(notification):
    kind = notification.kind
    sub_obj = getattr(notification, "subscription", None)
    sub_id = getattr(sub_obj, "id", None) if sub_obj is not None else None
    if sub_id is None:
        return
    row = _get_subscription(sub_id)
    if row is None:
        return  # not one of this service's subscriptions
    kinds = braintree.WebhookNotification.Kind
    if kind in (kinds.SubscriptionChargedSuccessfully,
                kinds.SubscriptionWentActive):
        member_id, note = grant_member_access(row["email"],
                                              row["course_key"])
        with _lock, _db:
            _db.execute(
                "UPDATE subscriptions SET status = 'active', member_id = ?,"
                " grant_note = ?, updated_at = ?"
                " WHERE bt_subscription_id = ?",
                (member_id, note, int(time.time()), sub_id))
    elif kind == kinds.SubscriptionChargedUnsuccessfully:
        if row["status"] == "active":
            _set_subscription_status(sub_id, "past_due")
    elif kind == kinds.SubscriptionCanceled:
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
 body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
   margin: 0; background: #f4f6f9; color: #1c2733; }
 header { background: #0d2f52; color: #fff; padding: 18px 20px; }
 header h1 { margin: 0; font-size: 20px; }
 header p { margin: 4px 0 0; opacity: .85; font-size: 14px; }
 main { max-width: 640px; margin: 24px auto; padding: 0 16px 40px; }
 .card { background: #fff; border: 1px solid #d8dee6; border-radius: 10px;
   padding: 18px; margin-bottom: 16px; }
 .price { font-size: 18px; font-weight: 600; }
 button { background: #0d2f52; color: #fff; border: 0; border-radius: 8px;
   padding: 12px 18px; font-size: 16px; cursor: pointer; width: 100%;
   margin-top: 8px; }
 button:disabled { opacity: .55; }
 input, select { width: 100%; box-sizing: border-box; padding: 11px;
   border: 1px solid #b9c4d0; border-radius: 8px; font-size: 16px; }
 label { display: block; font-weight: 600; margin: 10px 0 6px; }
 .note { font-size: 14px; color: #46586a; }
 .err { color: #a12622; font-weight: 600; }
 .ok { color: #14632f; font-weight: 600; }
 a { color: #0d2f52; }
</style></head><body>
<header><h1>Subscribe — 911transedu.net</h1>
<p>Transgender Emergency Medicine training, by subscription.</p></header>
<main>{body}</main></body></html>"""


def _page(title, body):
    return PAGE.replace("{title}", title).replace("{body}", body)


def subscribe_page():
    if not bt_configured():
        body = (
            '<div class="card"><h2>Card and bank subscriptions are not'
            " switched on yet</h2>"
            '<p class="note">This checkout opens as soon as the'
            " Braintree credentials reach the server. Until then, both"
            " products are available today by Cash App on their member"
            " buy pages — access unlocks when the owner confirms your"
            " payment.</p>"
            '<p><a href="/members/apps">Back to the apps hub</a></p>'
            "</div>")
        return _page("Subscribe", body)
    config = {
        "googleMerchantId": BT_GOOGLE_MERCHANT_ID,
        "environment": BT_ENVIRONMENT,
        "products": {k: {"short": p["short"], "amount": p["amount"]}
                     for k, p in PRODUCTS.items()},
    }
    cards = ""
    for key, p in PRODUCTS.items():
        cards += (
            '<div class="card"><h2>{short}</h2>'
            '<p class="price">{price}</p>'
            '<button data-course="{key}">Pay for {short}</button>'
            "</div>").format(short=html.escape(p["short"]),
                              price=html.escape(p["price_line"]),
                              key=key)
    mandate = (
        'By clicking "Verify Bank Account", I authorize '
        + BUSINESS_NAME + " to debit the bank account specified above"
        " for any amount owed for charges arising from my recurring"
        " subscription pursuant to " + BUSINESS_NAME + "'s website"
        " and terms, until this authorization is revoked.")
    body = (
        '<div class="card"><label for="email">Your member account'
        " email</label>"
        '<input id="email" type="email" autocomplete="email"'
        ' placeholder="you@example.com">'
        '<label for="fname">First name</label>'
        '<input id="fname" type="text" autocomplete="given-name">'
        '<label for="lname">Last name</label>'
        '<input id="lname" type="text" autocomplete="family-name">'
        '<p class="note">Use the same email as your member account on'
        " this site — your course access unlocks on that account"
        " automatically when Braintree confirms the first successful"
        " charge. Bank (ACH) charges can take a few business days to"
        " settle.</p>"
        '<p class="err" id="err"></p><p class="ok" id="ok"></p></div>'
        + cards
        + '<div class="card" id="paybox" hidden><h2>Payment</h2>'
          '<div id="dropin-container"></div>'
          '<button id="pay">Subscribe</button>'
          '<p class="err" id="payerr"></p></div>'
        + '<div class="card"><h2>Bank account by routing &amp; account'
          " number</h2>"
          '<p class="note">Prefer not to use the bank login above?'
          " Verify with two small deposits instead: we send two"
          " micro-deposits (under $1.00) to your bank account; come"
          " back in 1–2 business days and enter the two amounts to"
          " start your subscription.</p>"
          '<label for="routing">Routing Number</label>'
          '<input id="routing" inputmode="numeric"'
          ' placeholder="021000021">'
          '<label for="account">Account Number</label>'
          '<input id="account" inputmode="numeric">'
          '<label for="accttype">Account Type</label>'
          '<select id="accttype"><option value="checking">Checking'
          '</option><option value="savings">Savings</option></select>'
          '<label for="holder">Full Name on Account</label>'
          '<input id="holder" type="text">'
          '<label for="achcourse">Product</label>'
          '<select id="achcourse">'
          '<option value="ccce">CCCE Course — $15 every two years'
          '</option><option value="emspr">EMSPR Protocols — $6,000'
          " every year</option></select>"
          '<p class="note" id="mandate-text">' + html.escape(mandate)
          + "</p>"
          '<button id="achstart">Verify Bank Account</button>'
          '<p class="err" id="acherr"></p><p class="ok" id="achok"></p>'
          '<div id="stage2" hidden><label for="amt1">First deposit'
          ' (cents)</label><input id="amt1" type="number"'
          ' placeholder="17"><label for="amt2">Second deposit'
          ' (cents)</label><input id="amt2" type="number"'
          ' placeholder="29">'
          '<button id="achconfirm">Confirm &amp; Start Subscription'
          "</button></div></div>"
          '<p class="note">Already sent micro-deposits? Enter the two'
          ' amounts in the form above and confirm.</p>'
        + '<script src="https://js.braintreegateway.com/web/dropin/'
          '1.43.0/js/dropin.min.js"></script>'
          '<script src="https://js.braintreegateway.com/web/3.106.0/'
          'js/client.min.js"></script>'
          '<script src="https://js.braintreegateway.com/web/3.106.0/'
          'js/us-bank-account.min.js"></script>'
          '<script src="https://js.braintreegateway.com/web/3.106.0/'
          'js/data-collector.min.js"></script>'
          "<script>const CFG = " + json.dumps(config) + ";"
          "let dropinInstance = null, currentCourse = null;"
          "function val(id) { return document.getElementById(id)"
          ".value.trim(); }"
          "function buyer() { return {email: val('email'),"
          " first_name: val('fname'), last_name: val('lname')}; }"
          "async function clientToken() { const r = await fetch("
          "'client-token'); const d = await r.json(); if (!r.ok)"
          " throw new Error(d.error || 'Could not start checkout.');"
          " return d.clientToken; }"
          "async function startDropin(course) {"
          "  const err = document.getElementById('err');"
          "  err.textContent = '';"
          "  const b = buyer();"
          "  if (!b.email || !b.first_name || !b.last_name) {"
          "    err.textContent = 'Enter your email and name first.';"
          "    return; }"
          "  currentCourse = course;"
          "  const p = CFG.products[course];"
          "  const token = await clientToken();"
          "  const opts = {authorization: token, container:"
          "   '#dropin-container', dataCollector: true,"
          "   applePay: {displayName: '911transedu.net',"
          "    paymentRequest: {total: {label: p.short,"
          "    amount: p.amount}}},"
          "   usBankAccount: {displayName: '911transedu.net'}};"
          "  if (CFG.googleMerchantId || CFG.environment ==="
          "   'sandbox') { opts.googlePay = {googlePayVersion: 2,"
          "    transactionInfo: {totalPriceStatus: 'FINAL',"
          "    totalPrice: p.amount, currencyCode: 'USD'}};"
          "    if (CFG.googleMerchantId) opts.googlePay.merchantId"
          "     = CFG.googleMerchantId; }"
          "  if (dropinInstance) { await dropinInstance.teardown(); }"
          "  dropinInstance = await braintree.dropin.create(opts);"
          "  document.getElementById('paybox').hidden = false;"
          "  document.getElementById('paybox').scrollIntoView();"
          "}"
          "document.querySelectorAll('button[data-course]').forEach("
          "x => x.addEventListener('click', () => startDropin("
          "x.dataset.course)));"
          "document.getElementById('pay').addEventListener('click',"
          " async () => {"
          "  const perr = document.getElementById('payerr');"
          "  perr.textContent = '';"
          "  try {"
          "    const payload = await dropinInstance"
          ".requestPaymentMethod();"
          "    const res = await fetch('create-subscription',"
          "     {method: 'POST', headers: {'Content-Type':"
          "     'application/json'}, body: JSON.stringify({"
          "     ...buyer(), payment_method_nonce: payload.nonce,"
          "     device_data: payload.deviceData,"
          "     course_key: currentCourse})});"
          "    const data = await res.json();"
          "    if (!res.ok) throw new Error(data.error || 'Subscription"
          " failed.');"
          "    document.getElementById('ok').textContent ="
          "     'Subscription started (' + data.status + '). Your'"
          "     + ' access unlocks when the first charge is"
          " confirmed.';"
          "    document.getElementById('paybox').hidden = true;"
          "  } catch (e) { perr.textContent = e.message; }"
          "});"
          "document.getElementById('achstart').addEventListener("
          "'click', async () => {"
          "  const aerr = document.getElementById('acherr');"
          "  const aok = document.getElementById('achok');"
          "  aerr.textContent = ''; aok.textContent = '';"
          "  const b = buyer();"
          "  if (!b.email) { aerr.textContent = 'Enter your member"
          " email above first.'; return; }"
          "  try {"
          "    const token = await clientToken();"
          "    const client = await braintree.client.create("
          "     {authorization: token});"
          "    const uba = await braintree.usBankAccount.create("
          "     {client: client});"
          "    const dc = await braintree.dataCollector.create("
          "     {client: client});"
          "    const {nonce} = await uba.tokenize({bankDetails: {"
          "     routingNumber: val('routing'),"
          "     accountNumber: val('account'),"
          "     accountType: val('accttype'),"
          "     ownershipType: 'personal',"
          "     accountHolderName: val('holder')},"
          "     mandateText: document.getElementById('mandate-text')"
          ".innerText.trim()});"
          "    const res = await fetch('ach/initiate-micro-deposits',"
          "     {method: 'POST', headers: {'Content-Type':"
          "     'application/json'}, body: JSON.stringify({"
          "     ...b, payment_method_nonce: nonce,"
          "     device_data: dc.deviceData,"
          "     course_key: val('achcourse')})});"
          "    const data = await res.json();"
          "    if (!res.ok) throw new Error(data.error || 'Could not"
          " start verification.');"
          "    aok.textContent = 'Two micro-deposits are on their"
          " way. Come back in 1–2 business days and enter the two"
          " amounts below.';"
          "    document.getElementById('stage2').hidden = false;"
          "  } catch (e) { aerr.textContent = e.message; }"
          "});"
          "document.getElementById('achconfirm').addEventListener("
          "'click', async () => {"
          "  const aerr = document.getElementById('acherr');"
          "  const aok = document.getElementById('achok');"
          "  aerr.textContent = '';"
          "  try {"
          "    const res = await fetch('ach/confirm-and-subscribe',"
          "     {method: 'POST', headers: {'Content-Type':"
          "     'application/json'}, body: JSON.stringify({"
          "     email: val('email'), course_key: val('achcourse'),"
          "     deposit_amounts_cents: [parseInt(val('amt1'), 10),"
          "     parseInt(val('amt2'), 10)]})});"
          "    const data = await res.json();"
          "    if (!res.ok) throw new Error(data.error || 'Confirmation"
          " failed.');"
          "    aok.textContent = 'Bank verified — subscription"
          " started (' + data.status + ').';"
          "  } catch (e) { aerr.textContent = e.message; }"
          "});"
          "</script>")
    return _page("Subscribe", body)


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

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode() or "{}")
        except ValueError:
            return None

    def _buyer_fields(self, payload):
        email = (payload.get("email") or "").strip().lower()
        first = (payload.get("first_name") or "").strip()
        last = (payload.get("last_name") or "").strip()
        course_key = payload.get("course_key") or ""
        if "@" not in email or "." not in email:
            return None, "Enter a valid email address."
        if course_key not in PRODUCTS:
            return None, "Unknown product."
        if not first or not last:
            return None, "Enter your first and last name."
        return (email, first, last, course_key), None

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == BASE + "/health":
            self._json(200, {"status": "ok",
                             "braintree_configured": bt_configured(),
                             "environment": BT_ENVIRONMENT})
        elif path in (BASE, BASE + "/"):
            self._send(200, subscribe_page())
        elif path == BASE + "/client-token":
            if not bt_configured():
                self._json(503, {"error": "Subscriptions are not"
                                         " switched on yet."})
                return
            try:
                token = gateway().client_token.generate()
            except Exception as e:
                self._json(502, {"error": "Braintree: %s" % e})
                return
            self._json(200, {"clientToken": token})
        else:
            self._send(404, "Not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == BASE + "/webhook":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not bt_configured():
                self._json(503, {"error": "Webhook is not configured."})
                return
            form = urllib.parse.parse_qs(raw.decode())
            signature = (form.get("bt_signature") or [""])[0]
            payload_b64 = (form.get("bt_payload") or [""])[0]
            try:
                notification = gateway().webhook_notification.parse(
                    signature, payload_b64)
            except Exception:
                self._json(400, {"error": "Invalid signature."})
                return
            handle_webhook_notification(notification)
            self._json(200, {"status": "received"})
            return
        payload = self._read_json()
        if payload is None:
            self._json(400, {"error": "Bad request."})
            return
        if not bt_configured():
            self._json(503, {"error": "Subscriptions are not switched"
                                     " on yet."})
            return
        if path == BASE + "/ach/confirm-and-subscribe":
            # Stage 2 needs no name — the pending verification stored
            # server-side at stage 1 carries everything else.
            email = (payload.get("email") or "").strip().lower()
            course_key = payload.get("course_key") or ""
            if "@" not in email or "." not in email:
                self._json(400, {"error": "Enter a valid email address."})
                return
            if course_key not in PRODUCTS:
                self._json(400, {"error": "Unknown product."})
                return
            amounts = payload.get("deposit_amounts_cents") or []
            if len(amounts) != 2:
                self._json(400, {"error": "Enter both deposit amounts."})
                return
            try:
                sub = confirm_and_subscribe(email, course_key, amounts)
            except FlowError as e:
                self._json(400, {"error": str(e)})
                return
            except Exception as e:
                self._json(502, {"error": "Braintree: %s" % e})
                return
            self._json(200, {"status": "subscribed",
                             "subscriptionId": sub.id,
                             "subscriptionStatus": sub.status})
            return
        fields, error = self._buyer_fields(payload)
        if error:
            self._json(400, {"error": error})
            return
        email, first, last, course_key = fields
        try:
            if path == BASE + "/create-subscription":
                nonce = payload.get("payment_method_nonce") or ""
                if not nonce:
                    self._json(400, {"error": "Missing payment nonce."})
                    return
                sub = create_subscription(
                    email, first, last, nonce,
                    payload.get("device_data"), course_key)
                self._json(200, {"subscriptionId": sub.id,
                                 "status": sub.status})
            elif path == BASE + "/ach/initiate-micro-deposits":
                nonce = payload.get("payment_method_nonce") or ""
                if not nonce:
                    self._json(400, {"error": "Missing payment nonce."})
                    return
                initiate_micro_deposits(
                    email, first, last, nonce,
                    payload.get("device_data"), course_key)
                self._json(200, {
                    "status": "pending_verification",
                    "message": "Two micro-deposits have been sent."
                               " Confirm the amounts in 1–2 business"
                               " days."})
            else:
                self._send(404, "Not found")
        except FlowError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            self._json(502, {"error": "Braintree: %s" % e})


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("braintree-subscriptions listening on port %d (base %s,"
          " configured: %s, environment: %s)"
          % (PORT, BASE, bt_configured(), BT_ENVIRONMENT), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
