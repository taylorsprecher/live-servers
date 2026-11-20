"""Environment configuration — same names, same defaults, same
semantics as the original monolith (DESIGN.md §2)."""

import os

BASE = os.environ.get("BASE_PATH", "/ledger").rstrip("/")
PORT = int(os.environ.get("PORT", "8000"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_TOKEN = os.environ.get("PAYLEDGER_ADMIN_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("PAYLEDGER_WEBHOOK_SECRET", "")
MERCHANT_ID = os.environ.get(
    "PAYLEDGER_MERCHANT_ID", "91100000-0000-4000-8000-000000000001"
)
SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
