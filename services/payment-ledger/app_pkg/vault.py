"""Encrypted checkout-receipt vault — Taylor's SecureStore engine
(app_pkg/securestore.py, filed 2026-11-10; its appdirs dependency is
vendored alongside it as app_pkg/appdirs.py) applied to the one public
checkout write, POST /ledger/checkout/echeck.

What it does: after a checkout commits in Postgres, a buyer-safe
receipt (product, amount, buyer email, Authorize.net transaction id,
approval and settlement state — never the payment nonce, which is
single-use and is not passed to this module, and never bank numbers,
which never reach this service) is written into a Fernet-encrypted
SecureStore file, giving the owner an encrypted, tamper-evident copy
of each sale independent of the database.

Fail-closed and dark by default, on the AUTHNET pattern: the vault is
active only when PAYLEDGER_SECURESTORE_ENABLED is explicitly set AND
a valid Fernet key is present in PAYLEDGER_SECURESTORE_KEY (host .env
only, never in git). Recording is best-effort by design: any vault
failure is logged to stderr and swallowed, so the vault can never
break, delay, or duplicate a charge.
"""

import os
import sys
from datetime import datetime, timezone

APP_NAME = "PayLedgerVault"

_store = None
_store_tried = False


def _flag_on():
    return os.environ.get(
        "PAYLEDGER_SECURESTORE_ENABLED", "").strip() in (
            "1", "true", "TRUE", "yes")


def vault_enabled():
    """True only with the switch on, a key present, AND the store
    actually constructible with that key — an invalid key reports
    disabled here, not merely at write time. Construction is cached
    (see _get_store), so repeat calls are free."""
    if not _flag_on():
        return False
    if not os.environ.get("PAYLEDGER_SECURESTORE_KEY", "").strip():
        return False
    return _get_store() is not None


def _get_store():
    global _store, _store_tried
    if _store is None and not _store_tried:
        _store_tried = True
        try:
            from .securestore import SecureStore
            key = os.environ.get(
                "PAYLEDGER_SECURESTORE_KEY", "").strip().encode("utf-8")
            if not key:
                raise ValueError("PAYLEDGER_SECURESTORE_KEY is not set")
            _store = SecureStore(key=key, app_name=APP_NAME)
        except Exception as exc:  # invalid key, unwritable dir, ...
            print(f"payledger vault: disabled "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)
            _store = None
    return _store


def record_receipt(result, email, idempotency_key=None):
    """Write one encrypted receipt for a committed checkout.

    `result` is the buyer-safe dict returned by
    authnet.checkout_echeck. Returns True when a receipt was written,
    False when the vault is off or the write failed. Never raises.
    """
    if not vault_enabled():
        return False
    try:
        store = _get_store()
        if store is None:
            return False
        receipt = {
            "product": result.get("product"),
            "amount_minor": result.get("amount_minor"),
            "email": email,
            "trans_id": result.get("trans_id"),
            "approved": result.get("approved"),
            "payment_method": result.get("payment_method"),
            "settlement_state": result.get("settlement_state"),
            "deduplicated": bool(result.get("deduplicated")),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        ident = result.get("trans_id") or idempotency_key or "unknown"
        store.save(f"receipt-{ident}.enc", receipt)
        return True
    except Exception as exc:
        print(f"payledger vault: receipt not recorded "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return False


def load_receipt(ident):
    """Read one receipt back (owner tooling/tests). Returns {} when
    the vault is off or the receipt is absent or unreadable."""
    if not vault_enabled():
        return {}
    store = _get_store()
    if store is None:
        return {}
    try:
        return store.load(f"receipt-{ident}.enc")
    except Exception:
        return {}
