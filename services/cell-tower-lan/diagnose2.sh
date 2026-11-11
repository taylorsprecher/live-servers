#!/usr/bin/env bash
# Read-only diagnostic #2 for live-celltower: jobs-table state, server-side
# log lines, and the server process's kernel I/O counters. Changes NOTHING.
set -u

echo "=== JOBS TABLE STATE (read-only open of telemetry.db) ==="
docker exec live-celltower node -e '
const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("/data/telemetry.db", { readOnly: true });
const pc = db.prepare("PRAGMA page_count").get();
const ps = db.prepare("PRAGMA page_size").get();
console.log("db pages:", pc.page_count, "page_size:", ps.page_size,
  "=> approx MB:", (pc.page_count * ps.page_size / 1048576).toFixed(1));
console.log("jobs by status:");
for (const r of db.prepare("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").all())
  console.log("  ", r.status, r.n);
console.log("readings:", db.prepare("SELECT COUNT(*) AS n FROM readings").get().n);
console.log("alarms:", db.prepare("SELECT COUNT(*) AS n FROM alarms").get().n);
console.log("oldest waiting/delayed jobs:");
for (const r of db.prepare("SELECT id, status, attempts_made, max_attempts, last_error, created_at, next_run_at FROM jobs WHERE status IN ('"'"'waiting'"'"','"'"'delayed'"'"','"'"'active'"'"') ORDER BY created_at ASC LIMIT 5").all())
  console.log("  ", JSON.stringify(r));
console.log("top last_error values among dead jobs:");
for (const r of db.prepare("SELECT last_error, COUNT(*) AS n FROM jobs WHERE status='"'"'dead'"'"' GROUP BY last_error ORDER BY n DESC LIMIT 5").all())
  console.log("  ", r.n, "x", r.last_error);
console.log("newest job created_at:", JSON.stringify(db.prepare("SELECT MAX(created_at) AS m FROM jobs").get()));
console.log("oldest job created_at:", JSON.stringify(db.prepare("SELECT MIN(created_at) AS m FROM jobs").get()));
' 2>&1
echo

echo "=== SERVER-SIDE LOG LINES (queue/errors/startup), last 60 ==="
docker logs live-celltower 2>&1 \
  | grep -aE 'queue|error|Error|listening|storage:|keys:|DLQ|pump|telemetry service' \
  | tail -60
echo

echo "=== LOG VOLUME ==="
docker logs live-celltower 2>&1 | wc -l
echo "lines matching 'ingest .* fetch failed':"
docker logs live-celltower 2>&1 | grep -ac 'fetch failed' || true
echo "lines matching 'tick t+':"
docker logs live-celltower 2>&1 | grep -ac 'tick t+' || true
echo

echo "=== FIRST 30 LOG LINES (container start) ==="
docker logs live-celltower 2>&1 | head -30
echo

echo "=== SERVER PROCESS KERNEL STATE (in-container) ==="
docker exec live-celltower sh -c '
for p in /proc/[0-9]*; do
  if grep -aq "backend/server.js" "$p/cmdline" 2>/dev/null; then
    echo "pid ${p#/proc/}:"
    grep -E "^(State|Threads):" "$p/status" 2>/dev/null
    cat "$p/io" 2>/dev/null | grep -E "^(read_bytes|write_bytes|syscr|syscw)"
  fi
done' 2>&1
echo

echo "=== DIAGNOSE2 DONE ==="
