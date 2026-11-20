#!/usr/bin/env bash
# Behavioral gate for the payment-ledger redesign deploy (Stage 3).
#
# The build VM had no PostgreSQL, so the ledger's DB-backed boot path
# (schema apply + worker + DB routes) could not be exercised before
# deploy. This script exercises it against the REAL paydb server —
# but only against a scratch database (payledger_behavioral) and a
# scratch instance of the deployed payledger image on :18199.
# Production ledger data is never written by this script; the scratch
# database and container are removed on exit (trap), pass or fail.
#
# Covers: static suite (test_payledger.py on the deployed tree),
# schema smoke on the scratch DB, the API behavioral suite
# (ops/payledger_behavioral.py), and the schema's trigger invariants
# (illegal state transition rejected, unbalanced journal rejected at
# commit, posted ledger entries immutable).
#
# Secrets: the paydb password is read from the host .env into a shell
# variable and used only inside the scratch container's DATABASE_URL;
# scratch admin/webhook tokens are generated per run. No secret value
# is ever printed.
set -u
cd /home/deploy/live-servers

SCRATCH_DB="payledger_behavioral"
SCRATCH_CTR="payledger-behavioral"
FAILURES=0

note_fail() { echo "FAIL $1"; FAILURES=$((FAILURES + 1)); }
note_pass() { echo "PASS $1"; }

TOK="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
SEC="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
PW="$(sed -n 's/^PAYLEDGER_DB_PASSWORD=//p' .env | head -n 1)"
if [ -z "${PW}" ]; then
  echo "FAIL host .env has no PAYLEDGER_DB_PASSWORD (cannot build scratch DATABASE_URL)"
  exit 1
fi

psql_scratch() {
  docker exec -i live-paydb psql -U payledger -d "${SCRATCH_DB}" -v ON_ERROR_STOP=1 "$@"
}
psql_admin() {
  docker exec -i live-paydb psql -U payledger -d payledger -v ON_ERROR_STOP=1 "$@"
}

cleanup() {
  docker rm -f "${SCRATCH_CTR}" >/dev/null 2>&1 || true
  psql_admin -c "DROP DATABASE IF EXISTS ${SCRATCH_DB}" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo "=== MEMORY BEFORE ==="
free -m

echo "=== CREATE SCRATCH DB ON REAL paydb ==="
psql_admin -c "CREATE DATABASE ${SCRATCH_DB}" || { echo "FAIL scratch DB create"; exit 1; }
note_pass "scratch DB created on paydb server"

echo "=== START SCRATCH payledger (deployed image) on 127.0.0.1:18199 ==="
docker compose run -d --name "${SCRATCH_CTR}" --no-deps \
  -p 127.0.0.1:18199:8000 \
  -e BASE_PATH=/ledger \
  -e DATABASE_URL="postgresql://payledger:${PW}@paydb:5432/${SCRATCH_DB}" \
  -e PAYLEDGER_ADMIN_TOKEN="${TOK}" \
  -e PAYLEDGER_WEBHOOK_SECRET="${SEC}" \
  payledger >/dev/null || { echo "FAIL scratch container start"; docker logs "${SCRATCH_CTR}" 2>&1 | tail -20; exit 1; }

code="000"
for i in $(seq 1 40); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18199/ledger/health)" || code="000"
  [ "${code}" = "200" ] && break
  sleep 3
done
if [ "${code}" = "200" ]; then
  note_pass "scratch instance healthy (schema applied to real PostgreSQL)"
else
  note_fail "scratch instance health (got ${code})"
  docker logs "${SCRATCH_CTR}" 2>&1 | tail -30
  echo "BEHAVIORAL GATE: FAIL"
  exit 1
fi

echo "=== STATIC SUITE (deployed tree) ==="
if (cd services/payment-ledger && python3 test_payledger.py > /tmp/bt-static.log 2>&1); then
  note_pass "test_payledger.py static suite ($(grep -c '^PASS' /tmp/bt-static.log) checks)"
else
  note_fail "test_payledger.py static suite"
  tail -20 /tmp/bt-static.log
fi

echo "=== SCHEMA SMOKE (scratch DB) ==="
tables="$(psql_scratch -tAc "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'")"
[ "${tables}" -ge 20 ] && note_pass "paydb tables ${tables} (>= 20)" || note_fail "paydb tables ${tables} (want >= 20)"
triggers="$(psql_scratch -tAc "SELECT COUNT(DISTINCT trigger_name) FROM information_schema.triggers WHERE trigger_name IN ('trg_payment_state_machine','trg_verify_balanced_journal','trg_immutable_ledger_entries')")"
[ "${triggers}" = "3" ] && note_pass "ledger triggers present (3)" || note_fail "ledger triggers ${triggers} (want 3)"
accounts="$(psql_scratch -tAc "SELECT COUNT(*) FROM ledger_accounts")"
[ "${accounts}" -ge 7 ] && note_pass "seeded ledger accounts ${accounts} (>= 7)" || note_fail "ledger accounts ${accounts} (want >= 7)"

echo "=== API BEHAVIORAL SUITE (scratch instance) ==="
if BT_BASE="http://127.0.0.1:18199/ledger" BT_ADMIN="${TOK}" BT_WEBHOOK_SECRET="${SEC}" \
     python3 ops/payledger_behavioral.py; then
  note_pass "API behavioral suite"
else
  note_fail "API behavioral suite"
fi

echo "=== SQL TRIGGER INVARIANTS (scratch DB) ==="
# A fresh CREATED intent, inserted directly, for the transition test.
psql_scratch -qAc "INSERT INTO payment_intents (id, idempotency_key, merchant_id, amount, currency, status) VALUES ('00000000-0000-4000-8000-0000000000b1', 'bt-sql-1', '91100000-0000-4000-8000-000000000001', 100, 'USD', 'CREATED')" >/dev/null
if psql_scratch -qAc "UPDATE payment_intents SET status = 'SETTLED' WHERE id = '00000000-0000-4000-8000-0000000000b1'" >/dev/null 2>&1; then
  note_fail "illegal transition CREATED->SETTLED was ACCEPTED"
else
  status_after="$(psql_scratch -tAc "SELECT status FROM payment_intents WHERE id = '00000000-0000-4000-8000-0000000000b1'")"
  [ "${status_after}" = "CREATED" ] \
    && note_pass "illegal transition rejected by trg_payment_state_machine" \
    || note_fail "illegal transition errored but status moved (${status_after})"
fi

# Unbalanced journal: one DEBIT entry only; the deferred balance
# trigger must reject the COMMIT and roll everything back.
if psql_scratch -qA <<'SQL' >/dev/null 2>&1
BEGIN;
INSERT INTO ledger_transactions (id, idempotency_key, payment_intent_id, description)
VALUES ('00000000-0000-4000-8000-0000000000b2', 'bt-unbalanced', '00000000-0000-4000-8000-0000000000b1', 'behavioral invariant probe');
INSERT INTO ledger_entries (id, ledger_transaction_id, account_id, direction, amount, currency)
VALUES ('00000000-0000-4000-8000-0000000000b3', '00000000-0000-4000-8000-0000000000b2',
        (SELECT id FROM ledger_accounts WHERE code = 'bank_cash'), 'DEBIT', 100, 'USD');
COMMIT;
SQL
then
  note_fail "unbalanced journal was ACCEPTED"
else
  left="$(psql_scratch -tAc "SELECT COUNT(*) FROM ledger_transactions WHERE idempotency_key = 'bt-unbalanced'")"
  [ "${left}" = "0" ] \
    && note_pass "unbalanced journal rejected at commit by trg_verify_balanced_journal" \
    || note_fail "unbalanced journal errored but rows remain (${left})"
fi

# Immutability: any UPDATE of a posted ledger entry must be rejected.
entry_id="$(psql_scratch -tAc "SELECT id FROM ledger_entries LIMIT 1")"
if [ -n "${entry_id}" ]; then
  if psql_scratch -qAc "UPDATE ledger_entries SET amount = amount WHERE id = '${entry_id}'" >/dev/null 2>&1; then
    note_fail "posted ledger entry UPDATE was ACCEPTED"
  else
    note_pass "posted ledger entries immutable (trg_immutable_ledger_entries)"
  fi
else
  note_fail "immutability check: no ledger entries exist (API suite must have failed earlier)"
fi

echo "=== MEMORY AFTER ==="
free -m

if [ "${FAILURES}" -gt 0 ]; then
  echo "BEHAVIORAL GATE: FAIL (${FAILURES} failing groups)"
  exit 1
fi
echo "BEHAVIORAL GATE: PASS"
