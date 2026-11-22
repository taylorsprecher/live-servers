"""Environment configuration — same names, same defaults as the monolith."""
import os


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

# Bitcoin is received INSIDE Cash App at the cashtag below — there is no
# on-chain deposit address (Taylor, 2026-11-12: "There isn't one"), so no
# BTC address env var exists or is ever rendered.
CASHAPP_CASHTAG = os.environ.get("CASHAPP_CASHTAG") or "$taylorsprecher2026"


# ACH (eCheck) checkout on the buy pages. The section renders only
# when ALL THREE hold — the same "exists only while configured"
# pattern as the GoDaddy Pay Link section:
#   * AUTHNET_ECHECK_ENABLED=1 (the same switch the payment
#     ledger's eCheck gate reads; one host .env entry arms both),
#   * the Authorize.net API Login ID is present,
#   * the Authorize.net PUBLIC Client Key is present.
# The Login ID and Client Key are browser-public identifiers by
# design (Accept.js authData; the Client Key "may safely be stored
# in a website" per Authorize.net) — the Transaction Key is a
# secret, is never configured here, and never appears on a page.
# Bank routing/account numbers are tokenized by Accept.js in the
# buyer's browser and never reach this service.
AUTHNET_ECHECK_ENABLED = (os.environ.get("AUTHNET_ECHECK_ENABLED") or "") == "1"
AUTHNET_API_LOGIN_ID = (os.environ.get("AUTHNET_API_LOGIN_ID") or "").strip()
AUTHNET_CLIENT_KEY = (os.environ.get("AUTHNET_CLIENT_KEY") or "").strip()
AUTHNET_ENV = (os.environ.get("AUTHNET_ENV") or "production").strip().lower()
if AUTHNET_ENV not in ("production", "sandbox"):
    AUTHNET_ENV = "production"
ACH_CHECKOUT_READY = bool(
    AUTHNET_ECHECK_ENABLED and AUTHNET_API_LOGIN_ID and AUTHNET_CLIENT_KEY
)

# Auto-renew offer inside the ACH section: the checkbox renders
# only when the ACH checkout itself renders AND the renewal switch
# is on. The same host .env entry (AUTHNET_RENEWAL_ENABLED) arms
# the payment ledger's renewal gate — the ledger refuses
# enrollment while its own switch is off.
AUTHNET_RENEWAL_ENABLED = (os.environ.get("AUTHNET_RENEWAL_ENABLED") or "") == "1"
RENEWAL_OFFER_READY = bool(ACH_CHECKOUT_READY and AUTHNET_RENEWAL_ENABLED)
ACCEPT_JS_URL = (
    "https://jstest.authorize.net/v1/Accept.js"
    if AUTHNET_ENV == "sandbox"
    else "https://js.authorize.net/v1/Accept.js"
)


BTC_RATE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"


BTC_RATE_SOURCE = "CoinGecko (api.coingecko.com simple/price, USD)"


MAX_UPLOAD_BYTES = 100 * 1024 * 1024


ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Member (department) uploads: a per-member inbox on the same volume,
# deliberately OUTSIDE CONTENT_DIR so member files can never be served
# as course content or reached by a public URL.
UPLOADS_DIR = os.environ.get("UPLOADS_DIR", "/data/uploads")


MEMBER_UPLOAD_MAX_BYTES = 25 * 1024 * 1024


# Bug guard (app_pkg/bug_guard.py; Taylor, 2026-11-22): blocks
# attack-probe traffic inside the app and reports every block to
# a file on the volume. Fail-closed switch, same discipline as
# the Authorize.net gates: MEMBERS_BUG_GUARD_ENABLED=1 in the
# host .env (set via ops/set-bug-guard-enabled.sh through the
# deploy-members workflow's enable_bug_guard input). State and
# report live on the data volume, outside CONTENT_DIR, so they
# can never be served as content.
BUG_GUARD_ENABLED = (os.environ.get("MEMBERS_BUG_GUARD_ENABLED") or "") == "1"
BUG_GUARD_STATE_PATH = os.environ.get(
    "BUG_GUARD_STATE_PATH", "/data/bug_guard_state.json")
BUG_GUARD_REPORT_PATH = os.environ.get(
    "BUG_GUARD_REPORT_PATH", "/data/bug_guard_report.log")
BUG_GUARD_ALLOW = [
    s.strip() for s in
    (os.environ.get("MEMBERS_BUG_GUARD_ALLOW") or "").split(",")
    if s.strip()
]


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
