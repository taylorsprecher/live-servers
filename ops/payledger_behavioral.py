#!/usr/bin/env python3
"""Behavioral suite for the payment-ledger service, run by
ops/test-payledger-behavioral.sh against a SCRATCH database on the
production paydb server and a scratch instance of the deployed image
(never against production ledger data).

Mirrors the behaviors verified interactively before the first deploy
(services/payment-ledger/README.md, "Verified before deploy",
2026-11-15): intent + webhook dedupe, sandbox authorize approve /
decline with masked-PAN message log, reversal, capture -> settle
lifecycle landing bank 9710 / fee 290 / clearing 0 on a $100 capture
with a $2.90 fee, settlement-file dedupe + unknown-RRN quarantine
(DLQ), STAN increments. SQL trigger invariants are checked by the
caller script. Fee estimate and routing are exercised and their
outcomes recorded (their exact values depend on fee-rule / PSP-stats
data, which a fresh database legitimately does not have — production
has none either).

Env: BT_BASE (e.g. http://127.0.0.1:18199/ledger), BT_ADMIN,
BT_WEBHOOK_SECRET. Prints PASS/FAIL lines; exit 1 on any FAIL.
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ["BT_BASE"].rstrip("/")
ADMIN = os.environ["BT_ADMIN"]
SECRET = os.environ["BT_WEBHOOK_SECRET"]

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f" :: {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def call(method, path, body=None, headers=None, raw=None):
    url = BASE + path
    data = raw
    hdrs = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
            return resp.status, text
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def callj(method, path, body=None, admin=True, raw=None, headers=None):
    hdrs = dict(headers or {})
    if admin:
        hdrs["X-Admin-Token"] = ADMIN
    status, text = call(method, path, body=body, headers=hdrs, raw=raw)
    try:
        return status, json.loads(text)
    except ValueError:
        return status, {"_raw": text}


def wait_status(intent_id, want, timeout=60):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _, doc = callj("GET", f"/intents/{intent_id}", admin=False)
        last = (doc.get("intent") or {}).get("status")
        if last == want:
            return True
        time.sleep(2)
    print(f"  (last status for {intent_id}: {last}, wanted {want})")
    return False


def balances():
    _, doc = callj("GET", "/balances", admin=False)
    return {r["code"]: int(r["balance"]) for r in doc.get("accounts", [])}


def webhook(event_id, payload):
    body = dict(payload)
    body["id"] = event_id
    raw = json.dumps(body).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return callj("POST", "/webhooks/behavioral", admin=False, raw=raw,
                 headers={"X-Signature-256": sig})


# 1. Health — schema applied to real PostgreSQL, DB routes live.
st, doc = callj("GET", "/health", admin=False)
check("health ok + db true", st == 200 and doc.get("db") is True, f"{st} {doc}")

# 2. Intent create + idempotent dedupe.
st, doc = callj("POST", "/intents", {"idempotency_key": "bt-1", "amount": 10000})
intent1 = (doc.get("intent") or {}).get("id")
check("intent create 201 CREATED",
      st == 201 and (doc.get("intent") or {}).get("status") == "CREATED",
      f"{st} {doc}")
st, doc2 = callj("POST", "/intents", {"idempotency_key": "bt-1", "amount": 10000})
check("intent dedupe on idempotency_key",
      doc2.get("deduplicated") is True and (doc2.get("intent") or {}).get("id") == intent1,
      f"{st} {doc2}")

# 3. STAN increments.
_, s1 = callj("POST", "/processor/stan", {})
_, s2 = callj("POST", "/processor/stan", {})
try:
    check("STAN increments by 1", int(s2["stan"]) == int(s1["stan"]) + 1,
          f"{s1} -> {s2}")
except Exception:
    check("STAN increments by 1", False, f"{s1} -> {s2}")

# 4. Sandbox authorize: approve 4111..., decline 4000...0002 (code 51).
_, d = callj("POST", "/intents", {"idempotency_key": "bt-2", "amount": 10000})
intent2 = (d.get("intent") or {}).get("id")
st, auth = callj("POST", "/processor/authorize",
                 {"pan": "4111111111111111", "amount": 10000,
                  "payment_intent_id": intent2})
check("sandbox authorize approve (HTTP 200)", st == 200, f"{st} {auth}")
check("authorized intent reaches AUTHORIZED", wait_status(intent2, "AUTHORIZED"),
      f"auth resp: {auth}")

_, d = callj("POST", "/intents", {"idempotency_key": "bt-3", "amount": 5000})
intent3 = (d.get("intent") or {}).get("id")
st, dec = callj("POST", "/processor/authorize",
                {"pan": "4000000000000002", "amount": 5000,
                 "payment_intent_id": intent3})
check("sandbox authorize decline carries code 51",
      st == 200 and "51" in json.dumps(dec), f"{st} {dec}")
check("declined intent reaches FAILED", wait_status(intent3, "FAILED"),
      f"decline resp: {dec}")

# 5. Message log stores masked PAN only.
_, msgs = callj("GET", "/processor/messages", admin=False)
blob = json.dumps(msgs)
check("no full PAN in message log",
      "4111111111111111" not in blob and "4000000000000002" not in blob)

# 6. Reversal (MTI 0420) voids the authorized intent.
st, rev = callj("POST", "/processor/reverse", {"payment_intent_id": intent2})
check("reversal accepted", st == 200 and "reversal" in rev, f"{st} {rev}")
check("reversed intent reaches VOIDED", wait_status(intent2, "VOIDED"))
_, msgs = callj("GET", "/processor/messages", admin=False)
check("MTI 0420 logged", any(m.get("mti") == "0420" for m in msgs.get("messages", [])))

# 7. Capture -> settle lifecycle via signed webhooks + the worker.
base_ev = {"payment_intent_id": intent1, "amount": 10000,
           "psp_name": "BehavioralPSP", "network_rrn": "BT-RRN-1"}
st, r = webhook("bt-evt-auth", {**base_ev, "kind": "authorized"})
check("webhook authorized received", st == 200 and r.get("received") is True
      and r.get("duplicate") is False, f"{st} {r}")
check("worker: intent AUTHORIZED", wait_status(intent1, "AUTHORIZED"))
st, r = webhook("bt-evt-cap", {**base_ev, "kind": "captured"})
check("webhook captured received", st == 200 and r.get("received") is True,
      f"{st} {r}")
check("worker: intent CAPTURED", wait_status(intent1, "CAPTURED"))
st, r = webhook("bt-evt-set", {**base_ev, "kind": "settled",
                                "fee_amount": 290, "record_type": "SALE"})
check("webhook settled received", st == 200 and r.get("received") is True,
      f"{st} {r}")
check("worker: intent SETTLED", wait_status(intent1, "SETTLED"))

bal = balances()
check("balances: bank_cash 9710", bal.get("bank_cash") == 9710, f"{bal}")
check("balances: fee expense 290", bal.get("psp_processing_fee_expense") == 290,
      f"{bal}")
check("balances: clearing back to 0", bal.get("acquirer_clearing_expected") == 0,
      f"{bal}")
check("balances: merchant payable 10000", bal.get("merchant_payable_main") == 10000,
      f"{bal}")

# 8. Webhook redelivery is deduplicated; balances do not move.
st, r = webhook("bt-evt-cap", {**base_ev, "kind": "captured"})
check("webhook redelivery deduplicated", st == 200 and r.get("duplicate") is True,
      f"{st} {r}")
time.sleep(5)
check("balances unchanged after duplicate", balances() == bal)

# 9. Settlement file: unknown RRN quarantined (DLQ); re-upload deduped.
csv_body = (
    "psp_name,network_rrn,psp_transaction_id,record_type,gross_amount,"
    "fee_amount,currency,settlement_date\n"
    "BehavioralPSP,UNKNOWN-RRN-9,,SALE,5000,145,USD,2026-11-20\n"
).encode()
st, rep = callj("POST", "/settlement-files?filename=bt.csv", raw=csv_body)
check("settlement file ingested (1 row)",
      st == 200 and rep.get("rows") == 1 and rep.get("duplicate_file") is False,
      f"{st} {rep}")


def dlq_hit():
    _, rs = callj("GET", "/recon/status", admin=False)
    return (rs.get("settlement_lines") or {}).get("EXCEPTION", 0) >= 1


deadline = time.time() + 60
ok = False
while time.time() < deadline:
    if dlq_hit():
        ok = True
        break
    time.sleep(2)
check("unknown RRN quarantined to DLQ (line EXCEPTION)", ok)
st, rep2 = callj("POST", "/settlement-files?filename=bt.csv", raw=csv_body)
check("settlement file re-upload deduplicated",
      st == 200 and rep2.get("duplicate_file") is True, f"{st} {rep2}")

# 10. Fee estimate + routing: exercised; outcomes recorded (values are
# data-dependent on a fresh DB — see module docstring).
st, est = callj("POST", "/fees/estimate",
                {"amount": 10000, "network": "VISA", "funding_type": "CREDIT",
                 "card_product": "CONSUMER", "issuer_country": "US",
                 "merchant_country": "US"})
check("fee estimate returns a breakdown", st == 200 and "estimate" in est,
      f"{st} {est}")
print(f"  fee estimate on $100 (fresh DB): {json.dumps(est.get('estimate'))[:300]}")

for name in ("BehavioralPSP-A", "BehavioralPSP-B"):
    callj("POST", "/psps",
          {"name": name, "supported_currencies": ["USD"],
           "supported_networks": ["VISA"], "acquirer_countries": ["US"],
           "supports_3ds": True})
_, d = callj("POST", "/intents", {"idempotency_key": "bt-4", "amount": 10000})
intent4 = (d.get("intent") or {}).get("id")
st, route = callj("POST", "/route",
                  {"payment_intent_id": intent4, "network": "VISA",
                   "currency": "USD", "amount": 10000,
                   "issuer_country": "US", "merchant_country": "US"})
check("routing answers coherently (plan or documented 422)",
      (st == 200 and "routing" in route) or st == 422, f"{st} {route}")
print(f"  routing outcome: HTTP {st}: {json.dumps(route)[:300]}")

print()
if failures:
    print(f"BEHAVIORAL API SUITE: {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("BEHAVIORAL API SUITE: ALL PASS")
