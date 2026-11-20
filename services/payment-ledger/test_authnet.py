#!/usr/bin/env python3
"""Offline unit checks for the Authorize.net processor module
(app_pkg/authnet.py). No network, no database, no real credentials:
gateway traffic is a fake transport returning canned Authorize.net
responses, the ledger write path is a fake module capturing the
canonical event, and the database cursor is a fake implementing
exactly the statements authnet_charge issues.

Mirrors the standalone pipeline script's self-test
(~/workspace/user/files/authorize_net_pipeline.py): payload shape,
$6,000 cap guard, plain-language error translation — plus the
fleet-specific gates: disabled-by-default, fail-closed behavior,
credential-missing behavior, idempotent charge recording.

Run: python3 test_authnet.py   (exit 1 on any FAIL)
"""

import contextlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from app_pkg import authnet  # noqa: E402
from app_pkg.authnet import (  # noqa: E402
    AuthNetConfig,
    ProcessorDisabled,
    ProcessorError,
)

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f" :: {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


DUMMY_ENV = {
    "AUTHNET_API_LOGIN_ID": "dummy-login-id",
    "AUTHNET_TRANSACTION_KEY": "dummy-transaction-key",
    "AUTHNET_ENV": "production",
    "AUTHNET_ENABLED": "1",
}


def cfg_from(env):
    return AuthNetConfig(environ=env)


def env_without(*drop):
    return {k: v for k, v in DUMMY_ENV.items() if k not in drop}


# Canned gateway responses (Authorize.net JSON shapes).
OK_MESSAGES = {"resultCode": "Ok", "message": [{"code": "I00001", "text": "Successful."}]}
AUTH_OK = {"messages": OK_MESSAGES}
AUTH_BAD = {"messages": {"resultCode": "Error", "message": [
    {"code": "E00007", "text": "User authentication failed due to invalid authentication values."}]}}
CHARGE_OK = {
    "messages": OK_MESSAGES,
    "transactionResponse": {
        "responseCode": "1", "authCode": "ABC123", "transId": "1234567890",
        "messages": [{"code": "1", "description": "This transaction has been approved."}],
    },
}
CHARGE_DECLINE = {
    "messages": OK_MESSAGES,
    "transactionResponse": {
        "responseCode": "2", "transId": "1234567891",
        "errors": [{"errorCode": "27", "errorText": "The transaction resulted in an AVS mismatch."}],
    },
}
STATUS_OK = {
    "messages": OK_MESSAGES,
    "transaction": {
        "transId": "1234567890", "transactionStatus": "capturedPendingSettlement",
        "transactionType": "authCaptureTransaction", "responseCode": 1,
        "authAmount": 15.0, "settleAmount": 15.0,
    },
}


def transport_returning(resp, calls=None):
    def _t(cfg, payload):
        if calls is not None:
            calls.append(payload)
        return resp
    return _t


def transport_forbidden(cfg, payload):
    raise AssertionError("gateway transport must not be called in this state")


# ------------------------------------------------------------------ gates

cfg = cfg_from({})
check("disabled by default: no env at all -> not enabled", not cfg.enabled())
check("disabled by default: reason names AUTHNET_ENABLED",
      "AUTHNET_ENABLED" in (cfg.disabled_reason() or ""))

cfg = cfg_from(env_without("AUTHNET_ENABLED"))
check("credentials present but flag off -> still disabled", not cfg.enabled())

cfg = cfg_from(env_without("AUTHNET_API_LOGIN_ID", "AUTHNET_TRANSACTION_KEY"))
check("flag on but credentials missing -> disabled (fail closed)", not cfg.enabled())
check("missing-credential reason names both variables",
      "AUTHNET_API_LOGIN_ID" in cfg.disabled_reason()
      and "AUTHNET_TRANSACTION_KEY" in cfg.disabled_reason())
check("missing() reports both variables",
      cfg.missing() == ["AUTHNET_API_LOGIN_ID", "AUTHNET_TRANSACTION_KEY"])

cfg = cfg_from(DUMMY_ENV)
check("flag on + credentials present -> enabled", cfg.enabled())
check("status(): enabled/configured true, no credential values anywhere",
      authnet.status(cfg)["enabled"] is True
      and authnet.status(cfg)["configured"] is True
      and "dummy-login-id" not in json.dumps(authnet.status(cfg))
      and "dummy-transaction-key" not in json.dumps(authnet.status(cfg)))

# Operations refuse while disabled, without touching the gateway.
for name, fn in (
    ("authenticate_check", lambda c: authnet.authenticate_check(c, transport=transport_forbidden)),
    ("execute_charge", lambda c: authnet.execute_charge(c, 1500, "nonce-x", transport=transport_forbidden)),
    ("transaction_status", lambda c: authnet.transaction_status(c, "123", transport=transport_forbidden)),
):
    try:
        fn(cfg_from({}))
        check(f"{name} refuses while disabled", False, "no exception raised")
    except ProcessorDisabled as exc:
        check(f"{name} refuses while disabled", "disabled" in str(exc))
    except Exception as exc:  # noqa: BLE001
        check(f"{name} refuses while disabled", False, f"wrong exception: {exc!r}")

# Boot log line never carries a credential value.
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    authnet.log_state(cfg_from(DUMMY_ENV))
out = buf.getvalue()
check("log_state (enabled) prints one line, no credential values",
      out.count("\n") == 1 and "ENABLED" in out
      and "dummy-login-id" not in out and "dummy-transaction-key" not in out)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    authnet.log_state(cfg_from({}))
check("log_state (disabled) states the refusal plainly",
      "DISABLED" in buf.getvalue() and "fail closed" in buf.getvalue())

# ------------------------------------------------------------------ credential check

res = authnet.authenticate_check(cfg_from(DUMMY_ENV), transport=transport_returning(AUTH_OK))
check("authenticate_check succeeds on Ok response", res["ok"] is True and res["env"] == "production")
try:
    authnet.authenticate_check(cfg_from(DUMMY_ENV), transport=transport_returning(AUTH_BAD))
    check("authenticate_check translates E00007", False, "no exception raised")
except ProcessorError as exc:
    check("authenticate_check translates E00007",
          "regenerated" in str(exc) and "E00007" in str(exc), str(exc))

# ------------------------------------------------------------------ payload shape

calls = []
cfg = cfg_from(DUMMY_ENV)
payload = authnet.build_charge_payload(
    cfg, authnet.parse_amount("15.00"), "COMMON.ACCEPT.INAPP.PAYMENT",
    "nonce-123", "CCCE-TEST", "CCCE course", "buyer@example.com")
req = payload["createTransactionRequest"]
check("charge payload carries merchant auth",
      req["merchantAuthentication"]["name"] == "dummy-login-id")
check("charge payload uses opaqueData, never a card number",
      "opaqueData" in req["transactionRequest"]["payment"]
      and "creditCard" not in json.dumps(payload))
check("CCCE $15.00 formats as 15.00", req["transactionRequest"]["amount"] == "15.00")

res = authnet.execute_charge(cfg, 1500, "nonce-123", invoice="CCCE-TEST",
                             transport=transport_returning(CHARGE_OK, calls))
check("execute_charge approves on responseCode 1",
      res["approved"] is True and res["trans_id"] == "1234567890"
      and res["auth_code"] == "ABC123")
sent = calls[0]["createTransactionRequest"]["transactionRequest"]
check("execute_charge sent the nonce, amount in dollars",
      sent["payment"]["opaqueData"]["dataValue"] == "nonce-123"
      and sent["amount"] == "15.00")

res = authnet.execute_charge(cfg, 1500, "nonce-123",
                             transport=transport_returning(CHARGE_DECLINE))
check("execute_charge decline is not approved, wording flags processing gate",
      res["approved"] is False and "Transaction Processing is likely still off" in res["message"],
      res.get("message", ""))

# ------------------------------------------------------------------ cap guard

def raises_value_error(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


check("EMSPR $6,000 exactly at cap is allowed (dollars)",
      authnet.parse_amount("6000") == 6000)
check("$6,000.01 is refused by the cap guard (dollars)",
      raises_value_error(lambda: authnet.parse_amount("6000.01")))
check("non-numeric amount is refused",
      raises_value_error(lambda: authnet.parse_amount("abc")))
check("EMSPR 600000 minor exactly at cap is allowed",
      authnet.parse_amount_minor(600000) == (600000, authnet.Decimal("6000.00")))
check("600001 minor is refused by the cap guard",
      raises_value_error(lambda: authnet.parse_amount_minor(600001)))
check("zero / negative minor refused",
      raises_value_error(lambda: authnet.parse_amount_minor(0))
      and raises_value_error(lambda: authnet.parse_amount_minor(-5)))
check("fractional minor units refused",
      raises_value_error(lambda: authnet.parse_amount_minor("1500.5")))
check("cap guard fires before the gateway is touched",
      raises_value_error(lambda: authnet.execute_charge(
          cfg, 600001, "nonce-x", transport=transport_forbidden)))
check("missing nonce refused before the gateway is touched",
      raises_value_error(lambda: authnet.execute_charge(
          cfg, 1500, "", transport=transport_forbidden)))

# ------------------------------------------------------------------ transaction status

res = authnet.transaction_status(cfg, "1234567890", transport=transport_returning(STATUS_OK))
check("transaction_status summarizes the gateway response",
      res["status"] == "capturedPendingSettlement" and res["type"] == "authCaptureTransaction")

# ------------------------------------------------------------------ ledger recording seam


class FakeLedger:
    """Stands in for app_pkg.ledger: captures the canonical event and
    emulates the state walk, so the adapter's use of the existing
    write path is verified without a database."""

    def __init__(self, cursor):
        self.cursor = cursor
        self.events = []

    def process_event(self, cur, event):
        self.events.append(event)
        p = event["payload"]
        row = self.cursor.intent_by_id(p["payment_intent_id"])
        row["status"] = "CAPTURED"
        return "captured; journal posted"

    def get_intent(self, cur, intent_id):
        return self.cursor.intent_by_id(intent_id)

    def move_status(self, cur, intent, target, trigger_attempt_id=None):
        # The real move_status UPDATEs the stored row; emulate that,
        # since record_charge re-reads the intent afterwards.
        row = self.cursor.intent_by_id(intent["id"])
        if row is not None:
            row["status"] = target
        intent["status"] = target
        return [target]


class FakeCursor:
    """Implements exactly the statements authnet_charge issues."""

    def __init__(self):
        self.intents = {}   # idempotency_key -> row
        self.attempts = {}  # attempt id -> row
        self._row = None

    def intent_by_id(self, intent_id):
        for row in self.intents.values():
            if row["id"] == intent_id:
                return row
        return None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._row = None
        if s.startswith("SELECT * FROM payment_intents WHERE idempotency_key"):
            self._row = self.intents.get(params[0])
        elif s.startswith("INSERT INTO payment_intents"):
            self.intents[params[1]] = {
                "id": params[0], "idempotency_key": params[1],
                "merchant_id": params[2], "amount": params[3],
                "currency": params[4], "status": "CREATED",
            }
        elif s.startswith("INSERT INTO payment_state_transitions"):
            pass
        elif s.startswith("INSERT INTO payment_attempts"):
            # Placeholders: id, payment_intent_id, psp_name, features
            # ('AUTHORIZATION', NULL, 'PENDING', NULL are literals).
            self.attempts[params[0]] = {
                "id": params[0], "payment_intent_id": params[1],
                "psp_name": params[2], "status": "PENDING",
            }
        elif s.startswith("UPDATE payment_attempts"):
            self.attempts[params[1]]["status"] = "DECLINED"
            self.attempts[params[1]]["raw_response_code"] = params[0]
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._row


real_ledger = authnet._ledger
cur = FakeCursor()
fake = FakeLedger(cur)
authnet._ledger = lambda: fake  # noqa: E731
try:
    calls = []
    out = authnet.authnet_charge(
        cur, {"amount": 1500, "nonce": "nonce-123", "invoice": "CCCE-TEST",
              "idempotency_key": "test-1"},
        cfg=cfg_from(DUMMY_ENV), transport=transport_returning(CHARGE_OK, calls))
    check("approved charge is recorded and intent lands CAPTURED",
          out["approved"] is True and out["intent"]["status"] == "CAPTURED")
    check("recording used the canonical captured event via process_event",
          len(fake.events) == 1
          and fake.events[0]["payload"]["kind"] == "captured"
          and fake.events[0]["payload"]["psp_name"] == "authorizenet"
          and fake.events[0]["payload"]["network_rrn"] == "1234567890"
          and fake.events[0]["payload"]["amount"] == 1500)
    check("attempt was created for the authorizenet PSP",
          len(cur.attempts) == 1
          and list(cur.attempts.values())[0]["psp_name"] == "authorizenet")

    out2 = authnet.authnet_charge(
        cur, {"amount": 1500, "nonce": "nonce-123", "invoice": "CCCE-TEST",
              "idempotency_key": "test-1"},
        cfg=cfg_from(DUMMY_ENV), transport=transport_returning(CHARGE_OK, calls))
    check("repeat idempotency key deduplicates — gateway not called again",
          out2["deduplicated"] is True and len(calls) == 1 and len(fake.events) == 1)

    out3 = authnet.authnet_charge(
        cur, {"amount": 1500, "nonce": "nonce-123", "idempotency_key": "test-2"},
        cfg=cfg_from(DUMMY_ENV), transport=transport_returning(CHARGE_DECLINE))
    check("declined charge: intent FAILED, attempt DECLINED, no journal event",
          out3["approved"] is False and out3["intent"]["status"] == "FAILED"
          and len(fake.events) == 1
          and [a for a in cur.attempts.values() if a["status"] == "DECLINED"])

    try:
        authnet.authnet_charge(
            cur, {"amount": 1500, "nonce": "nonce-123", "idempotency_key": "test-3"},
            cfg=cfg_from({}), transport=transport_forbidden)
        check("charge refuses while disabled before any DB write", False, "no exception")
    except ProcessorDisabled:
        check("charge refuses while disabled before any DB write",
              "authnet:test-3" not in cur.intents)
finally:
    authnet._ledger = real_ledger

# ------------------------------------------------------------------ summary

if failures:
    print(f"\n{len(failures)} FAILURES")
    sys.exit(1)
print("\nall authnet checks passed")
