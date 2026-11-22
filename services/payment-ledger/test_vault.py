#!/usr/bin/env python3
"""Offline unit checks for the SecureStore checkout-receipt vault
(app_pkg/vault.py). No network, no database: each scenario runs with
its own environment and a throwaway XDG_DATA_HOME, and the vault
module's cached store is reset between scenarios.

Covers the fail-closed gates (dark by default, flag without key,
key without flag, invalid key), the enabled round-trip (receipt is
ciphertext on disk, loads back intact, carries no nonce), and the
never-raise guarantee that keeps the vault out of the buyer's path.

Run: python3 test_vault.py   (exit 1 on any FAIL)
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name
          + (f" :: {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


from cryptography.fernet import Fernet  # noqa: E402

from app_pkg import vault  # noqa: E402

RESULT = {
    "product": "ccce",
    "amount_minor": 1500,
    "deduplicated": False,
    "charged": True,
    "approved": True,
    "trans_id": "12345678901",
    "message": "This transaction has been approved.",
    "payment_method": "ECHECK",
    "settlement_state": "PENDING_SETTLEMENT",
}


def scenario(env):
    """Reset the vault module and os.environ to exactly `env`."""
    vault._store = None
    vault._store_tried = False
    for name in ("PAYLEDGER_SECURESTORE_ENABLED",
                 "PAYLEDGER_SECURESTORE_KEY", "XDG_DATA_HOME"):
        os.environ.pop(name, None)
    os.environ.update(env)


def main():
    # 1. Dark by default: no env at all.
    with tempfile.TemporaryDirectory() as tmp:
        scenario({"XDG_DATA_HOME": tmp})
        check("default: vault disabled", vault.vault_enabled() is False)
        check("default: record is a no-op",
              vault.record_receipt(RESULT, "buyer@example.com", "idem-1")
              is False)
        check("default: nothing written",
              os.listdir(tmp) == [])

    # 2. Flag on, no key.
    with tempfile.TemporaryDirectory() as tmp:
        scenario({"XDG_DATA_HOME": tmp,
                  "PAYLEDGER_SECURESTORE_ENABLED": "1"})
        check("flag without key: disabled", vault.vault_enabled() is False)
        check("flag without key: record is a no-op",
              vault.record_receipt(RESULT, "buyer@example.com", "idem-1")
              is False)

    # 3. Key present, flag off.
    with tempfile.TemporaryDirectory() as tmp:
        scenario({"XDG_DATA_HOME": tmp,
                  "PAYLEDGER_SECURESTORE_KEY":
                      Fernet.generate_key().decode()})
        check("key without flag: disabled", vault.vault_enabled() is False)
        check("key without flag: record is a no-op",
              vault.record_receipt(RESULT, "buyer@example.com", "idem-1")
              is False)
        check("key without flag: nothing written", os.listdir(tmp) == [])

    # 4. Invalid key with the flag on: disabled, and no exception.
    with tempfile.TemporaryDirectory() as tmp:
        scenario({"XDG_DATA_HOME": tmp,
                  "PAYLEDGER_SECURESTORE_ENABLED": "1",
                  "PAYLEDGER_SECURESTORE_KEY": "not-a-fernet-key"})
        check("invalid key: disabled", vault.vault_enabled() is False)
        check("invalid key: record is a no-op, no raise",
              vault.record_receipt(RESULT, "buyer@example.com", "idem-1")
              is False)

    # 5. Enabled: round-trip, ciphertext at rest, no nonce anywhere.
    with tempfile.TemporaryDirectory() as tmp:
        scenario({"XDG_DATA_HOME": tmp,
                  "PAYLEDGER_SECURESTORE_ENABLED": "1",
                  "PAYLEDGER_SECURESTORE_KEY":
                      Fernet.generate_key().decode()})
        check("enabled: vault reports enabled",
              vault.vault_enabled() is True)
        check("enabled: record writes a receipt",
              vault.record_receipt(RESULT, "buyer@example.com", "idem-1")
              is True)
        path = os.path.join(
            tmp, "PayLedgerVault", "receipt-12345678901.enc")
        check("enabled: receipt file exists", os.path.isfile(path))
        raw = open(path, "rb").read() if os.path.isfile(path) else b""
        check("enabled: receipt is ciphertext at rest",
              b"buyer@example.com" not in raw
              and b"12345678901" not in raw
              and b"ccce" not in raw)
        back = vault.load_receipt("12345678901")
        check("enabled: receipt loads back intact",
              back.get("product") == "ccce"
              and back.get("amount_minor") == 1500
              and back.get("email") == "buyer@example.com"
              and back.get("trans_id") == "12345678901"
              and back.get("approved") is True
              and back.get("settlement_state") == "PENDING_SETTLEMENT"
              and bool(back.get("recorded_at")))
        check("enabled: receipt carries no nonce or bank data",
              "nonce" not in back and "data_value" not in back
              and "account_number" not in back
              and "routing_number" not in back)

        # 6. A failing store can never raise into the checkout path.
        store = vault._get_store()
        original_save = store.save

        def boom(*args, **kwargs):
            raise RuntimeError("disk on fire")

        store.save = boom
        try:
            outcome = vault.record_receipt(
                RESULT, "buyer@example.com", "idem-2")
        finally:
            store.save = original_save
        check("failing store: record returns False, never raises",
              outcome is False)

    if failures:
        print(f"\n{len(failures)} FAILURES")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
