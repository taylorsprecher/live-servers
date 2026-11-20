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
