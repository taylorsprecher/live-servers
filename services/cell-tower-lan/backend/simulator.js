'use strict';
/**
 * Telemetry simulator for the demo site SITE-TX-9042 (Pine Ridge Macro).
 *
 * Stands in for the site's radios/BBU telemetry exporter: every tick it
 * generates one reading per radio (readings.js), encrypts it with the
 * spec's AES-256-GCM scheme using the SITE's own active key (keys.js —
 * provisioned by the server into <dataDir>/keys.json, the local stand-in
 * for an enrollment flow), and POSTs it to the backend's
 * /lan/telemetry/ingest-async endpoint as { encryptedPayload, timestamp }.
 *
 * Usage: node simulator.js [baseUrl] [tickMs]
 *
 * baseUrl may carry the server's mount prefix (e.g.
 * http://127.0.0.1:4173/celltower when BASE_PATH=/celltower); when it is
 * omitted, the BASE_PATH env var is appended to the default root URL.
 * The container entrypoint (start.sh) passes the prefixed loopback URL
 * explicitly so ingest always lands on the prefixed spec route.
 */
const path = require('node:path');
const { loadKey } = require('./crypto');
const { KeyManager } = require('./keys');
const { RADIOS, makeReading } = require('./readings');

const BASE = (process.argv[2] ||
  `http://localhost:4173${(process.env.BASE_PATH || '').replace(/\/$/, '')}`).replace(/\/$/, '');
const TICK_MS = Number(process.argv[3] || 2000);
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, 'data');

const { key: legacyKey } = loadKey();
const keys = new KeyManager(DATA_DIR, [...new Set(RADIOS.map((r) => r.site_id))], legacyKey);

async function tick(startMs) {
  const tSec = (Date.now() - startMs) / 1000;
  for (const r of RADIOS) {
    const reading = makeReading(r, tSec);
    const { encrypter, keyId } = keys.encrypterFor(r.site_id);
    const encryptedPayload = encrypter.encrypt(JSON.stringify(reading));
    try {
      const res = await fetch(`${BASE}/lan/telemetry/ingest-async`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ encryptedPayload, timestamp: reading.ts }),
      });
      if (!res.ok) console.error(`ingest ${r.radio_id}: HTTP ${res.status} ${await res.text()}`);
      else if (tSec < 1) console.log(`ingest ${r.radio_id}: encrypted with site key ${keyId}`);
    } catch (err) {
      console.error(`ingest ${r.radio_id}: ${err.message} (is the backend running at ${BASE}?)`);
    }
  }
  console.log(`tick t+${tSec.toFixed(0)}s — sent ${RADIOS.length} encrypted readings`);
}

console.log(`simulator -> ${BASE} (per-site keys from ${path.join(DATA_DIR, 'keys.json')})`);
const start = Date.now();
tick(start);
setInterval(() => tick(start), TICK_MS);
