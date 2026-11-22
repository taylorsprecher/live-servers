#!/usr/bin/env python3
"""Offline unit checks for the auto-renewal module
(app_pkg/renewal.py). No network, no database, no real credentials:
gateway traffic is a fake transport returning canned Authorize.net
responses, the ledger write path is a fake module capturing the
canonical event, and the database cursor is a fake implementing
exactly the statements renewal.py + authnet's recording seam issue
(including SAVEPOINT snapshots, so the gateway-error rollback is
really exercised).

Covered: the third-switch gates, anniversary math, CIM profile
creation from a transaction, enrollment (consent row, token hashed,
idempotent per buyer+product), buyer/admin cancel, the profile
charge payload, and the scheduler's four outcomes — approved
(advance + receipt), declined (bounded retries, then parked),
gateway error (nothing recorded, retry tomorrow), and the
already-recorded period heal.

Run: python3 test_renewal.py   (exit 1 on any FAIL)
"""

import copy
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from app_pkg import authnet, renewal  # noqa: E402
from app_pkg.authnet import AuthNetConfig, ProcessorDisabled, ProcessorError  # noqa: E402

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
    "AUTHNET_ECHECK_ENABLED": "1",
    "AUTHNET_RENEWAL_ENABLED": "1",
}


def cfg_from(env):
    return AuthNetConfig(environ=env)


def env_without(*drop):
    return {k: v for k, v in DUMMY_ENV.items() if k not in drop}


CFG = cfg_from(DUMMY_ENV)
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)

OK_MESSAGES = {"resultCode": "Ok", "message": [{"code": "I00001", "text": "Successful."}]}
AUTH_BAD = {"messages": {"resultCode": "Error", "message": [
    {"code": "E00007", "text": "User authentication failed due to invalid authentication values."}]}}
PROFILE_OK = {
    "messages": OK_MESSAGES,
    "customerProfileId": "555001",
    "customerPaymentProfileIdList": [{"numericString": "555002"}],
    "customerShippingAddressIdList": [],
    "validationDirectResponseList": [],
}
PROFILE_EMPTY = {"messages": OK_MESSAGES, "customerProfileId": "555001",
                 "customerPaymentProfileIdList": []}
CHARGE_OK = {
    "messages": OK_MESSAGES,
    "transactionResponse": {
        "responseCode": "1", "authCode": "ABC123", "transId": "777000111",
        "messages": [{"code": "1", "description": "This transaction has been approved."}],
    },
}
CHARGE_DECLINE = {
    "messages": OK_MESSAGES,
    "transactionResponse": {
        "responseCode": "2", "transId": "777000222",
        "errors": [{"errorCode": "27", "errorText": "The transaction resulted in an AVS mismatch."}],
    },
}


def transport_returning(resp, calls=None):
    def _t(cfg, payload):
        if calls is not None:
            calls.append(payload)
        return resp
    return _t


def transport_routing(profile_resp, charge_resp, calls=None):
    def _t(cfg, payload):
        if calls is not None:
            calls.append(payload)
        if "createCustomerProfileFromTransactionRequest" in payload:
            return profile_resp
        return charge_resp
    return _t


def raises(exc_type, fn):
    try:
        fn()
        return False
    except exc_type:
        return True


# ------------------------------------------------------------------ gates

check("renewal enabled only with all three switches + credentials",
      CFG.renewal_enabled() is True)
check("renewal disabled without AUTHNET_RENEWAL_ENABLED (reason names it)",
      cfg_from(env_without("AUTHNET_RENEWAL_ENABLED")).renewal_enabled() is False
      and "AUTHNET_RENEWAL_ENABLED" in cfg_from(env_without("AUTHNET_RENEWAL_ENABLED")).renewal_disabled_reason())
check("renewal disabled when eCheck is off, even with the renewal switch on",
      cfg_from(env_without("AUTHNET_ECHECK_ENABLED")).renewal_enabled() is False)
check("renewal disabled when credentials are missing",
      cfg_from(env_without("AUTHNET_TRANSACTION_KEY")).renewal_enabled() is False)
check("health status reports the renewal gate",
      authnet.status(CFG)["renewal_enabled"] is True
      and authnet.status(cfg_from(env_without("AUTHNET_RENEWAL_ENABLED")))["renewal_enabled"] is False)

# ------------------------------------------------------------------ anniversary math

check("add_years: CCCE +2y lands on the anniversary",
      renewal.add_years(NOW, 2) == datetime(2028, 11, 21, 12, 0, tzinfo=timezone.utc))
check("add_years: EMSPR +1y",
      renewal.add_years(NOW, 1) == datetime(2027, 11, 21, 12, 0, tzinfo=timezone.utc))
check("add_years: Feb 29 clamps to Feb 28",
      renewal.add_years(datetime(2028, 2, 29, tzinfo=timezone.utc), 1)
      == datetime(2029, 2, 28, tzinfo=timezone.utc))

# ------------------------------------------------------------------ fake DB


class FakeLedger:
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
        row = self.cursor.intent_by_id(intent["id"])
        if row is not None:
            row["status"] = target
        intent["status"] = target
        return [target]


class FakeCursor:
    """Implements the statements renewal.py and authnet's recording
    seam issue. SAVEPOINT snapshots all three stores; ROLLBACK TO
    restores them, so the gateway-error path is honestly tested."""

    def __init__(self):
        self.intents = {}
        self.attempts = {}
        self.profiles = {}   # id -> row
        self._row = None
        self._rows = []
        self.rowcount = 0
        self._snapshot = None

    def intent_by_id(self, intent_id):
        for row in self.intents.values():
            if row["id"] == intent_id:
                return row
        return None

    def _store_snapshot(self):
        return (copy.deepcopy(self.intents), copy.deepcopy(self.attempts),
                copy.deepcopy(self.profiles))

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._row = None
        self._rows = []
        self.rowcount = 0
        if s == "SAVEPOINT renewal_one":
            self._snapshot = self._store_snapshot()
        elif s == "ROLLBACK TO SAVEPOINT renewal_one":
            self.intents, self.attempts, self.profiles = self._snapshot
        elif s == "RELEASE SAVEPOINT renewal_one":
            self._snapshot = None
        elif s.startswith("SELECT * FROM payment_intents WHERE idempotency_key"):
            self._row = self.intents.get(params[0])
        elif s.startswith("INSERT INTO payment_intents"):
            self.intents[params[1]] = {
                "id": params[0], "idempotency_key": params[1],
                "merchant_id": params[2], "amount": params[3],
                "currency": params[4], "status": "CREATED"}
        elif s.startswith("INSERT INTO payment_state_transitions"):
            pass
        elif s.startswith("INSERT INTO payment_attempts"):
            self.attempts[params[0]] = {
                "id": params[0], "payment_intent_id": params[1],
                "psp_name": params[2], "status": "PENDING",
                "features": params[3]}
        elif s.startswith("UPDATE payment_attempts"):
            self.attempts[params[1]]["status"] = "DECLINED"
            self.attempts[params[1]]["raw_response_code"] = params[0]
        elif s.startswith("SELECT * FROM renewal_profiles WHERE email"):
            for row in self.profiles.values():
                if (row["email"] == params[0] and row["product"] == params[1]
                        and row["status"] == "ACTIVE"):
                    self._row = row
                    break
        elif s.startswith("INSERT INTO renewal_profiles"):
            (rid, email, product, cust, pay, consent, version,
             token_hash, nxt) = params
            self.profiles[rid] = {
                "id": rid, "email": email, "product": product,
                "customer_profile_id": cust, "payment_profile_id": pay,
                "status": "ACTIVE", "consent_at": consent,
                "authorization_version": version,
                "cancel_token_hash": token_hash, "next_renewal_at": nxt,
                "attempts": 0, "last_trans_id": None, "last_result": None}
            self.rowcount = 1
        elif s.startswith("SELECT * FROM renewal_profiles WHERE status = 'ACTIVE' AND next_renewal_at"):
            due = [r for r in self.profiles.values()
                   if r["status"] == "ACTIVE" and r["next_renewal_at"] <= params[0]]
            self._rows = sorted(due, key=lambda r: r["next_renewal_at"])[:params[1]]
        elif s.startswith("SELECT email, product, status FROM renewal_profiles") or \
                s.startswith("SELECT email, product, status,"):
            self._rows = list(self.profiles.values())[:params[0]]
        elif s.startswith("UPDATE renewal_profiles SET status = 'CANCELLED'") and "WHERE id" in s:
            row = self.profiles[params[0]]
            row["status"] = "CANCELLED"
            self.rowcount = 1
        elif s.startswith("UPDATE renewal_profiles SET status = 'CANCELLED'"):
            for row in self.profiles.values():
                if (row["email"] == params[0] and row["product"] == params[1]
                        and row["status"] == "ACTIVE"):
                    row["status"] = "CANCELLED"
                    self.rowcount = 1
        elif s.startswith("UPDATE renewal_profiles SET status = 'NEEDS_ATTENTION'"):
            row = self.profiles[params[2]]
            row["status"] = "NEEDS_ATTENTION"
            row["attempts"] = params[0]
            row["last_result"] = params[1]
        elif s.startswith("UPDATE renewal_profiles SET attempts"):
            row = self.profiles[params[3]]
            row["attempts"] = params[0]
            row["next_renewal_at"] = params[1]
            row["last_result"] = params[2]
        elif s.startswith("UPDATE renewal_profiles SET next_renewal_at") and "last_trans_id" in s:
            row = self.profiles[params[3]]
            row["next_renewal_at"] = params[0]
            row["attempts"] = 0
            row["last_trans_id"] = params[1]
            row["last_result"] = params[2]
        elif s.startswith("UPDATE renewal_profiles SET next_renewal_at") and "attempts = 0" in s:
            row = self.profiles[params[2]]
            row["next_renewal_at"] = params[0]
            row["attempts"] = 0
            row["last_result"] = params[1]
        elif s.startswith("UPDATE renewal_profiles SET next_renewal_at"):
            row = self.profiles[params[2]]
            row["next_renewal_at"] = params[0]
            row["last_result"] = params[1]
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


real_ledger = authnet._ledger


def with_fake_ledger(cur):
    fake = FakeLedger(cur)
    authnet._ledger = lambda: fake  # noqa: E731
    return fake


# ------------------------------------------------------------------ CIM profile creation

calls = []
ids = renewal.create_profile_from_transaction(
    CFG, "4242424242", "buyer@example.com",
    transport=transport_returning(PROFILE_OK, calls))
check("profile creation returns the gateway profile ids",
      ids == ("555001", "555002"))
sent = calls[0]["createCustomerProfileFromTransactionRequest"]
check("profile creation is built FROM the completed transaction",
      sent["transId"] == "4242424242"
      and sent["customer"]["email"] == "buyer@example.com"
      and sent["merchantAuthentication"]["name"] == "dummy-login-id")
check("profile creation with no payment profile is a plain failure",
      raises(ProcessorError, lambda: renewal.create_profile_from_transaction(
          CFG, "1", "b@example.com", transport=transport_returning(PROFILE_EMPTY))))
check("gateway auth failure is translated, not leaked raw",
      raises(ProcessorError, lambda: renewal.create_profile_from_transaction(
          CFG, "1", "b@example.com", transport=transport_returning(AUTH_BAD))))

# ------------------------------------------------------------------ enrollment

cur = FakeCursor()
with_fake_ledger(cur)
calls = []
out = renewal.enroll(cur, "Buyer@Example.com", "ccce", "4242424242",
                     cfg=CFG, transport=transport_returning(PROFILE_OK, calls),
                     now=NOW)
row = list(cur.profiles.values())[0]
check("enrollment stores an ACTIVE consent row (+2y for CCCE)",
      out["enrolled"] is True and row["status"] == "ACTIVE"
      and row["next_renewal_at"] == renewal.add_years(NOW, 2)
      and row["authorization_version"] == renewal.AUTHORIZATION_VERSION
      and row["email"] == "buyer@example.com")
check("the cancel token is stored only as its SHA-256 hash",
      row["cancel_token_hash"] == hashlib.sha256(out["cancel_token"].encode()).hexdigest()
      and row["cancel_token_hash"] != out["cancel_token"])
check("the buyer-safe result carries no gateway profile ids",
      "555001" not in str(out) and "555002" not in str(out))

out2 = renewal.enroll(cur, "buyer@example.com", "ccce", "999",
                      cfg=CFG, transport=transport_returning(PROFILE_OK, calls),
                      now=NOW)
check("a second enrollment for the same buyer+product deduplicates",
      out2["enrolled"] is True and out2["already_enrolled"] is True
      and len(cur.profiles) == 1 and len(calls) == 1)
check("enrollment refuses while the renewal switch is off",
      raises(ProcessorDisabled, lambda: renewal.enroll(
          FakeCursor(), "b@example.com", "emspr", "1",
          cfg=cfg_from(env_without("AUTHNET_RENEWAL_ENABLED")),
          transport=transport_returning(PROFILE_OK), now=NOW)))

# ------------------------------------------------------------------ cancel

check("buyer cancel with the right token works",
      renewal.cancel_by_token(cur, "buyer@example.com", "ccce", out["cancel_token"]) is True
      and row["status"] == "CANCELLED")
check("buyer cancel with a wrong token does nothing",
      renewal.cancel_by_token(cur, "buyer@example.com", "ccce", "wrong-token") is False)

cur2 = FakeCursor()
with_fake_ledger(cur2)
renewal.enroll(cur2, "emspr@example.com", "emspr", "3131313131",
               cfg=CFG, transport=transport_returning(PROFILE_OK), now=NOW)
erow = list(cur2.profiles.values())[0]
check("EMSPR enrolls at +1y",
      erow["next_renewal_at"] == renewal.add_years(NOW, 1))
check("admin cancel works once, then reports no change",
      renewal.cancel_admin(cur2, "emspr@example.com", "emspr") is True
      and renewal.cancel_admin(cur2, "emspr@example.com", "emspr") is False)

# ------------------------------------------------------------------ profile charge payload

payload = renewal.build_profile_charge_payload(
    CFG, authnet.Decimal("15.00"), erow, "CCCE-R-20281121", "desc")
txn = payload["createTransactionRequest"]["transactionRequest"]
check("profile charge spends profilePayment, never a nonce",
      txn["payment"]["profilePayment"]["customerProfileId"] == "555001"
      and txn["payment"]["profilePayment"]["paymentProfile"]["paymentProfileId"] == "555002"
      and "opaqueData" not in str(txn["payment"]))

# ------------------------------------------------------------------ scheduler outcomes


def due_row(cur, rid="prof-1", product="ccce", attempts=0, due=None):
    row = {
        "id": rid, "email": "buyer@example.com", "product": product,
        "customer_profile_id": "555001", "payment_profile_id": "555002",
        "status": "ACTIVE", "consent_at": NOW,
        "authorization_version": renewal.AUTHORIZATION_VERSION,
        "cancel_token_hash": "x", "next_renewal_at": due or NOW,
        "attempts": attempts, "last_trans_id": None, "last_result": None}
    cur.profiles[rid] = row
    return row


# Approved: canonical recording + schedule advance + vault receipt.
cur3 = FakeCursor()
fake3 = with_fake_ledger(cur3)
row3 = due_row(cur3)
outcome, receipt = renewal.process_profile(
    cur3, row3, CFG, transport=transport_returning(CHARGE_OK), now=NOW)
check("approved renewal records the canonical captured event",
      outcome["approved"] is True and len(fake3.events) == 1
      and fake3.events[0]["payload"]["kind"] == "captured"
      and fake3.events[0]["payload"]["is_recurring"] is True
      and fake3.events[0]["payload"]["payment_method"] == "ECHECK"
      and fake3.events[0]["payload"]["amount"] == 1500)
check("approved renewal advances the anniversary and resets attempts",
      row3["next_renewal_at"] == renewal.add_years(NOW, 2)
      and row3["attempts"] == 0 and row3["last_trans_id"] == "777000111")
check("approved renewal yields a vault-shaped receipt",
      receipt is not None and receipt["trans_id"] == "777000111"
      and receipt["product"] == "ccce"
      and receipt["idempotency_key"] == f"renewal:prof-1:{NOW.date().isoformat()}")

# Declined: FAILED recording, bounded retry, then parked.
cur4 = FakeCursor()
fake4 = with_fake_ledger(cur4)
row4 = due_row(cur4)
outcome, receipt = renewal.process_profile(
    cur4, row4, CFG, transport=transport_returning(CHARGE_DECLINE), now=NOW)
check("declined renewal records FAILED, no journal, retry in 3 days",
      outcome["approved"] is False and receipt is None
      and len(fake4.events) == 0
      and cur4.intent_by_id(list(cur4.intents.values())[0]["id"])["status"] == "FAILED"
      and row4["attempts"] == 1
      and row4["next_renewal_at"] == NOW + timedelta(days=renewal.RETRY_DAYS))
row4["attempts"] = renewal.MAX_ATTEMPTS - 1
outcome, _ = renewal.process_profile(
    cur4, row4, CFG, transport=transport_returning(CHARGE_DECLINE), now=NOW)
check("the third decline parks the enrollment at NEEDS_ATTENTION",
      row4["status"] == "NEEDS_ATTENTION" and row4["attempts"] == renewal.MAX_ATTEMPTS)

# Gateway error: nothing recorded (savepoint rollback), retry tomorrow.
cur5 = FakeCursor()
fake5 = with_fake_ledger(cur5)
row5 = due_row(cur5)
outcome, receipt = renewal.process_profile(
    cur5, row5, CFG, transport=transport_returning(AUTH_BAD), now=NOW)
# The savepoint rollback replaced the stored row with its snapshot
# (in Postgres this is just the same row) — re-read before asserting.
row5 = cur5.profiles["prof-1"]
check("gateway error records nothing and does not count an attempt",
      outcome["charged"] is False and receipt is None
      and len(cur5.intents) == 0 and len(fake5.events) == 0
      and row5["attempts"] == 0 and row5["status"] == "ACTIVE"
      and row5["next_renewal_at"] == NOW + timedelta(days=1)
      and "gateway error" in (row5["last_result"] or ""))

# Already-recorded period: heal without charging.
cur6 = FakeCursor()
with_fake_ledger(cur6)
row6 = due_row(cur6)
period_key = f"renewal:prof-1:{NOW.date().isoformat()}"
authnet._create_intent_attempt(cur6, {"idempotency_key": period_key},
                               1500, "USD", "m", {})
calls6 = []
outcome, receipt = renewal.process_profile(
    cur6, row6, CFG, transport=transport_returning(CHARGE_OK, calls6), now=NOW)
check("an already-recorded period heals the schedule, gateway untouched",
      outcome["deduplicated"] is True and calls6 == []
      and row6["next_renewal_at"] == renewal.add_years(NOW, 2))

# run_due picks only due + ACTIVE rows.
cur7 = FakeCursor()
with_fake_ledger(cur7)
due_row(cur7, rid="due-1")
future = due_row(cur7, rid="future-1")
future["next_renewal_at"] = NOW + timedelta(days=30)
gone = due_row(cur7, rid="gone-1")
gone["status"] = "CANCELLED"
summary = renewal.run_due(cur7, CFG, transport=transport_returning(CHARGE_OK), now=NOW)
check("run_due charges only due ACTIVE enrollments",
      len(summary["outcomes"]) == 1
      and summary["outcomes"][0]["profile_id"] == "due-1"
      and len(summary["receipts"]) == 1)
check("run_due refuses while the renewal switch is off",
      raises(ProcessorDisabled, lambda: renewal.run_due(
          FakeCursor(), cfg_from(env_without("AUTHNET_RENEWAL_ENABLED")),
          transport=transport_returning(CHARGE_OK), now=NOW)))

authnet._ledger = real_ledger

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL RENEWAL CHECKS PASSED")
