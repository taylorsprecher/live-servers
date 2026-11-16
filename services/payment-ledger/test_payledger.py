#!/usr/bin/env python3
"""Static regression checks for the payment-ledger service.

The behavioral suite (state machine, balanced journal, dedupe,
fee example, routing, DLQ) runs against a real PostgreSQL before
deploy — see README.md "Verified before deploy". These checks keep
the repo-level wiring honest: schema objects, deterministic key
formats, compose services, and the gateway route."""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

failures = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        failures.append(name)


schema = open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read()
app = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()

for table in (
    "payment_intents", "payment_attempts", "payment_state_transitions",
    "ledger_accounts", "ledger_transactions", "ledger_entries",
    "inbound_events", "settlement_batches", "external_settlement_lines",
    "reconciliation_matches", "reconciliation_exceptions", "bin_ranges",
    "fee_rules", "merchant_contracts", "transaction_fee_estimates",
    "psps", "psp_health", "psp_approval_stats", "routing_rules",
    "routing_decisions", "pan_retry_tracker",
):
    check(f"schema has {table}", f"CREATE TABLE IF NOT EXISTS {table}" in schema)

for trigger in (
    "trg_payment_state_machine", "trg_verify_balanced_journal",
    "trg_immutable_ledger_entries",
):
    check(f"schema has trigger {trigger}", trigger in schema)

check("fee function present", "calculate_line_fee" in schema)
check("inbox unique constraint", "uq_inbound_source_event" in schema)
check("batch sha unique", "file_sha256 CHAR(64) UNIQUE" in schema)

import ast
ast.parse(app)
check("app.py parses", True)
for key_fmt in ("capture:{intent_id}", "settle:{psp}:{rrn}:{record_type}",
                "refund:{refund_id}", "dispute:{dispute_id}:{stage}"):
    check(f"deterministic key {key_fmt}", key_fmt in app)
check("worker uses SKIP LOCKED", "FOR UPDATE SKIP LOCKED" in app)
check("DLQ quarantine status", "RECONCILIATION_EXCEPTION" in app)
check("hard decline set present", "HARD_DECLINE_CODES" in app)

compose = open(os.path.join(REPO, "docker-compose.yml"), encoding="utf-8").read()
check("compose has paydb", re.search(r"^  paydb:", compose, re.M) is not None)
check("compose has payledger", re.search(r"^  payledger:", compose, re.M) is not None)
check("compose payledger port", "127.0.0.1:18123:8000" in compose)
check("compose payledger volume", "payledger-data:" in compose)
check("compose has no btsubs", "btsubs" not in compose)
check("compose has no stripe-subs", "stripe-subscriptions" not in compose)

gateway = open(
    os.path.join(REPO, "gateway", "locations-members-proxy.conf"), encoding="utf-8"
).read()
check("gateway routes /ledger/", "location /ledger/" in gateway and "18123" in gateway)
check("gateway has no /subs/", "/subs/" not in gateway)

if failures:
    print(f"\n{len(failures)} FAILURES")
    sys.exit(1)
print("\nall payledger checks passed")
