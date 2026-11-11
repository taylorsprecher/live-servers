'use strict';
/**
 * One-off maintenance for the Cell Tower LAN telemetry store:
 * prune completed queue jobs (keeping the newest
 * CELL_LAN_KEEP_COMPLETED_JOBS, default 1000) and VACUUM the database
 * file back down.
 *
 * Why this exists: completed jobs were never pruned, so by
 * Nov 11, 2026 the jobs table held 345k rows / 204 MB and every queue
 * scan read the whole table — half of the wedge that killed the
 * service's event loop. The store now prunes on boot and the queue
 * prunes periodically, so this script is the backstop / deep clean:
 * run it with the service STOPPED (fix-celltower.sh does exactly
 * that) so it gets the database to itself.
 *
 * Usage: DATA_DIR=/data node backend/prune-jobs.js
 */
const fs = require('node:fs');
const path = require('node:path');

const DATA_DIR = process.env.DATA_DIR || '/data';
const KEEP = Number(process.env.CELL_LAN_KEEP_COMPLETED_JOBS || 1000);
const file = path.join(DATA_DIR, 'telemetry.db');

if (!fs.existsSync(file)) {
  console.log(`prune-jobs: no ${file} — nothing to do`);
  process.exit(0);
}

const { DatabaseSync } = require('node:sqlite');
const db = new DatabaseSync(file);
db.exec('PRAGMA busy_timeout = 30000');

const sizeBefore = fs.statSync(file).size;
const counts = {};
for (const r of db.prepare('SELECT status, COUNT(*) AS n FROM jobs GROUP BY status').all()) {
  counts[r.status] = r.n;
}
console.log(`prune-jobs: jobs by status before: ${JSON.stringify(counts)}`);
console.log(`prune-jobs: db size before: ${(sizeBefore / 1048576).toFixed(1)} MB`);

const info = db.prepare(`
  DELETE FROM jobs WHERE status = 'completed' AND id NOT IN
    (SELECT id FROM jobs WHERE status = 'completed'
     ORDER BY created_at DESC LIMIT ?)
`).run(KEEP);
console.log(`prune-jobs: deleted ${Number(info.changes)} completed jobs (kept newest ${KEEP})`);

db.exec('VACUUM');
db.close();

const sizeAfter = fs.statSync(file).size;
console.log(`prune-jobs: db size after: ${(sizeAfter / 1048576).toFixed(1)} MB`);
console.log('prune-jobs: done');
