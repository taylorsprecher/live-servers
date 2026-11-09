'use strict';
/**
 * UDP telemetry simulator for the demo site SITE-TX-9042.
 *
 * Same readings as simulator.js, but sent over the spec's second
 * transport: encrypted raw UDP datagrams (the packed wire bytes
 * IV || ciphertext || tag, no Base64, no HTTP envelope) to the backend's
 * UDP ingest port. Fire-and-forget, like a lightweight edge exporter.
 *
 * Usage: node simulator-udp.js [host] [udpPort] [tickMs]
 */
const dgram = require('node:dgram');
const path = require('node:path');
const { loadKey } = require('./crypto');
const { KeyManager } = require('./keys');
const { RADIOS, makeReading } = require('./readings');

const HOST = process.argv[2] || '127.0.0.1';
const UDP_PORT = Number(process.argv[3] || process.env.CELL_LAN_UDP_PORT || 4174);
const TICK_MS = Number(process.argv[4] || 2000);
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, 'data');

const { key: legacyKey } = loadKey();
const keys = new KeyManager(DATA_DIR, [...new Set(RADIOS.map((r) => r.site_id))], legacyKey);
const socket = dgram.createSocket('udp4');

function tick(startMs) {
  const tSec = (Date.now() - startMs) / 1000;
  for (const r of RADIOS) {
    const reading = makeReading(r, tSec);
    const { encrypter } = keys.encrypterFor(r.site_id);
    const wireBase64 = encrypter.encrypt(JSON.stringify(reading));
    const packed = Buffer.from(wireBase64, 'base64'); // raw IV || ct || tag
    socket.send(packed, UDP_PORT, HOST, (err) => {
      if (err) console.error(`udp send ${r.radio_id}: ${err.message}`);
    });
  }
  console.log(`tick t+${tSec.toFixed(0)}s — sent ${RADIOS.length} encrypted UDP datagrams`);
}

console.log(`udp simulator -> udp://${HOST}:${UDP_PORT} (per-site keys from ${path.join(DATA_DIR, 'keys.json')})`);
const start = Date.now();
tick(start);
setInterval(() => tick(start), TICK_MS);
