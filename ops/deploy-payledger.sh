#!/usr/bin/env bash
# Targeted deploy for the payment platform ONLY (services/payment-ledger
# + the paydb PostgreSQL container).
#
# What this installs: a PostgreSQL-backed payment-intent state machine
# and immutable double-entry ledger, an idempotent webhook/settlement
# ingestion inbox with an async worker, a three-way reconciliation
# engine, an Interchange++ fee engine, and a routing decision log —
# reachable at https://members.911transedu.net/ledger/. Installed
# 2026-11-15 at the owner's direction from the five filed design
# documents (~/workspace/user/files/Payment_*_pasted_2026-11-15.txt).
# No payment processor is connected by this deploy.
#
# Secrets: PAYLEDGER_DB_PASSWORD, PAYLEDGER_ADMIN_TOKEN and
# PAYLEDGER_WEBHOOK_SECRET live ONLY in the host .env (gitignored,
# excluded from rsync). When absent, this script generates random
# values and appends them; values are never printed.
#
# Why targeted (same reasoning as ops/deploy-members-apps.sh):
# deploy.sh's RAM gate protects the host during a FULL build. This
# script builds only paydb + payledger and force-recreates the
# gateway so it picks up the /ledger/ location. It does NOT lower or
# edit deploy.sh's gate and does not rebuild any other service.
set -eu
cd /home/deploy/live-servers

echo "=== ENSURE PAYLEDGER SECRETS IN HOST .env (values never printed) ==="
touch .env
for key in PAYLEDGER_DB_PASSWORD PAYLEDGER_ADMIN_TOKEN PAYLEDGER_WEBHOOK_SECRET; do
  if ! grep -q "^${key}=" .env; then
    val="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo "${key}=${val}" >> .env
    echo "generated ${key} (stored in host .env only)"
  else
    echo "${key} already present"
  fi
done

echo "=== MEMORY BEFORE ==="
free -m

echo "=== BUILD + UP paydb + payledger ==="
docker compose up -d --build paydb payledger

wait_code() {
  local url="$1" want="$2" attempts="${3:-100}" code="000" i
  for i in $(seq 1 "$attempts"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")" || code="000"
    [ "$code" = "$want" ] && break
    sleep 5
  done
  echo "$code"
}

echo "=== WAIT FOR payledger health (direct loopback :18123) ==="
code="$(wait_code http://127.0.0.1:18123/ledger/health 200)"
echo "CHECK payledger-health direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== SCHEMA SMOKE (inside paydb) ==="
tables="$(docker exec live-paydb psql -U payledger -d payledger -tAc \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'")"
echo "CHECK paydb tables ${tables} (want >= 20)"
[ "$tables" -ge 20 ]
triggers="$(docker exec live-paydb psql -U payledger -d payledger -tAc \
  "SELECT COUNT(DISTINCT trigger_name) FROM information_schema.triggers WHERE trigger_name IN ('trg_payment_state_machine','trg_verify_balanced_journal','trg_immutable_ledger_entries')")"
echo "CHECK ledger triggers ${triggers} (want 3)"
[ "$triggers" = "3" ]
accounts="$(docker exec live-paydb psql -U payledger -d payledger -tAc \
  "SELECT COUNT(*) FROM ledger_accounts")"
echo "CHECK seeded ledger accounts ${accounts} (want >= 7)"
[ "$accounts" -ge 7 ]

echo "=== GATEWAY FORCE-RECREATE (picks up /ledger/ location) ==="
docker compose up -d --force-recreate gateway

echo "=== WAIT FOR gateway /ledger/health (public HTTPS) ==="
code="$(wait_code https://members.911transedu.net/ledger/health 200)"
echo "CHECK gateway ledger-health ${code} (want 200)"
[ "$code" = "200" ]

echo "=== REGRESSIONS ==="
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/members/health || true)"
echo "CHECK members-health gateway ${code} (want 200 or 301)"
[ "$code" = "200" ] || [ "$code" = "301" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/portal/ || true)"
echo "CHECK portal gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/files/ || true)"
echo "CHECK files gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:80/health || true)"
echo "CHECK audited-app ${code} (want 200)"
[ "$code" = "200" ]

echo "=== MEMORY AFTER ==="
free -m
echo "DEPLOY PAYLEDGER DONE"
