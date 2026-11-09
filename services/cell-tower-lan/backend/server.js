'use strict';
/**
 * Cell Tower LAN — telemetry service (increment 2).
 *
 * Zero-dependency Node server implementing:
 *   - Site registry (sites / sectors / radios) from registry.json
 *   - Encrypted telemetry ingest, two transports sharing one pipeline:
 *       HTTP  POST /lan/telemetry/ingest-async  { encryptedPayload, timestamp }
 *       UDP   datagrams on CELL_LAN_UDP_PORT (raw packed bytes, Base64 text,
 *             or the same JSON envelope as HTTP)
 *     Payloads are AES-256-GCM wire format — see crypto.js / keys.js.
 *   - A durable in-process job queue with BullMQ semantics (queue.js):
 *     ingest returns 202 as soon as the job is persisted; a worker
 *     decrypts/validates/stores asynchronously, retries 3x with
 *     exponential backoff, and dead-letters failures instead of dropping
 *     them silently.
 *   - Persistence (store.js): readings, alarms and jobs in SQLite
 *     (node:sqlite; JSON-file fallback) under DATA_DIR — restarts keep
 *     history and alarm acknowledgements.
 *   - Per-site keys with lifecycle (keys.js): generate / rotate / revoke;
 *     decryption tries the whole key ring (active + retired site keys,
 *     legacy shared key last) so rotation has a grace period.
 *   - Alarm acknowledge workflow: POST /api/alarms/:id/ack.
 *   - Read-back APIs used by the dashboard + static hosting of ../frontend.
 *   - Optional path-prefix hosting: BASE_PATH=/celltower makes every
 *     route above (APIs, the spec ingest route, and the dashboard)
 *     answer under that prefix; unset = serve at root. See DEPLOY.md.
 *
 * All RAN hardware is simulated; the only producers are simulator.js
 * (HTTP) and simulator-udp.js (UDP).
 */
const http = require('node:http');
const dgram = require('node:dgram');
const fs = require('node:fs');
const path = require('node:path');
const { loadKey } = require('./crypto');
const { createStore } = require('./store');
const { KeyManager } = require('./keys');
const { TelemetryQueue } = require('./queue');

const PORT = Number(process.env.PORT || 4173);
const UDP_PORT = Number(process.env.CELL_LAN_UDP_PORT || 4174);
// Optional mount prefix for path-prefix hosting (e.g. BASE_PATH=/celltower
// behind a gateway that forwards the prefix intact). '' = serve at root.
const BASE_PATH = (() => {
  const raw = (process.env.BASE_PATH || '').trim();
  if (!raw || raw === '/') return '';
  return '/' + raw.replace(/^\/+|\/+$/g, '');
})();
const FRONTEND_DIR = path.join(__dirname, '..', 'frontend');
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, 'data');
const MAX_HISTORY_PER_RADIO = 300;

// ---------------------------------------------------------------- registry
const registry = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'registry.json'), 'utf8')
);
const radioIndex = new Map(); // radio_id -> { site, sector, radio }
for (const site of registry.sites) {
  for (const sector of site.sectors) {
    for (const radio of sector.radios) {
      radioIndex.set(radio.radio_id, { site, sector, radio });
    }
  }
}

// ------------------------------------------------------- persistence + keys
fs.mkdirSync(DATA_DIR, { recursive: true });
const store = createStore(DATA_DIR);
const { key: legacyKey, usingTestKey } = loadKey();
const keys = new KeyManager(
  DATA_DIR,
  registry.sites.map((s) => s.site_id),
  legacyKey
);

// ------------------------------------------------------------------ alarms
const recentAlarmKeys = new Map(); // `${radio_id}:${code}` -> last raised ms

function raiseAlarm(reading, severity, code, message, value, threshold) {
  const dedupeKey = `${reading.radio_id}:${code}`;
  const now = Date.now();
  if (now - (recentAlarmKeys.get(dedupeKey) || 0) < 10_000) return;
  recentAlarmKeys.set(dedupeKey, now);
  store.insertAlarm({
    ts: reading.ts,
    site_id: reading.site_id,
    sector_id: reading.sector_id,
    radio_id: reading.radio_id,
    severity,
    code,
    message,
    value,
    threshold,
  });
}

function evaluateAlarms(reading) {
  const m = reading.metrics;
  if (m.temp_c > 65)
    raiseAlarm(reading, 'CRITICAL', 'RADIO_OVERTEMP',
      `${reading.radio_id} temperature ${m.temp_c.toFixed(1)} C exceeds 65 C`, m.temp_c, 65);
  if (m.rsrp_dbm < -105)
    raiseAlarm(reading, 'MAJOR', 'WEAK_SIGNAL',
      `${reading.radio_id} RSRP ${m.rsrp_dbm.toFixed(1)} dBm below -105 dBm`, m.rsrp_dbm, -105);
  if (m.sinr_db < 3)
    raiseAlarm(reading, 'MAJOR', 'LOW_SINR',
      `${reading.radio_id} SINR ${m.sinr_db.toFixed(1)} dB below 3 dB`, m.sinr_db, 3);
  if (m.vswr > 2.0)
    raiseAlarm(reading, 'MAJOR', 'HIGH_VSWR',
      `${reading.radio_id} VSWR ${m.vswr.toFixed(2)} above 2.0 (antenna/feed fault)`, m.vswr, 2.0);
  else if (m.vswr > 1.8)
    raiseAlarm(reading, 'MINOR', 'VSWR_DEGRADED',
      `${reading.radio_id} VSWR ${m.vswr.toFixed(2)} above 1.8`, m.vswr, 1.8);
  if (m.dl_throughput_mbps < 5 && m.connected_ues > 0)
    raiseAlarm(reading, 'MINOR', 'THROUGHPUT_COLLAPSE',
      `${reading.radio_id} DL throughput ${m.dl_throughput_mbps.toFixed(1)} Mbps with ${m.connected_ues} UEs connected`, m.dl_throughput_mbps, 5);
}

// ------------------------------------------------------- queue worker logic
// In the spec this runs in a BullMQ worker consuming the
// "cell-tower-telemetry" queue (job "process-telemetry"). The processor
// is unchanged in shape: decrypt -> validate -> persist -> derive alarms.
// Any throw (auth failure, unknown radio, malformed reading) sends the
// job through the queue's retry/backoff path and finally to the DLQ.
function processTelemetryJob(jobData) {
  const { plaintext } = keys.decrypt(jobData.encryptedPayload); // throws on tamper
  const reading = JSON.parse(plaintext);
  const known = radioIndex.get(reading.radio_id);
  if (!known || known.site.site_id !== reading.site_id ||
      known.sector.sector_id !== reading.sector_id) {
    throw new Error(`Unknown site/sector/radio: ${reading.site_id}/${reading.sector_id}/${reading.radio_id}`);
  }
  const required = ['rsrp_dbm', 'rsrq_db', 'sinr_db', 'dl_throughput_mbps',
    'ul_throughput_mbps', 'connected_ues', 'tx_power_dbm', 'temp_c', 'vswr'];
  for (const field of required) {
    if (typeof reading.metrics?.[field] !== 'number') {
      throw new Error(`Reading missing numeric metric: ${field}`);
    }
  }
  store.insertReading(reading);
  evaluateAlarms(reading);
  return reading;
}

const queue = new TelemetryQueue(store, processTelemetryJob);
queue.start();

// -------------------------------------------------------------------- http
function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' };

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  let p = url.pathname;
  // Path-prefix hosting: when BASE_PATH is set, requests arrive WITH the
  // prefix (the gateway forwards it untouched) — strip it before routing
  // so every route below stays prefix-agnostic. Unprefixed paths 404.
  if (BASE_PATH) {
    if (p === BASE_PATH) {
      // Canonicalize to the trailing-slash URL so the dashboard's
      // relative asset paths (style.css, app.js) resolve under the prefix.
      res.writeHead(301, { Location: `${BASE_PATH}/` });
      return res.end();
    }
    if (p.startsWith(BASE_PATH + '/')) {
      p = p.slice(BASE_PATH.length);
    } else {
      return sendJson(res, 404, {
        error: `not found — this service is mounted at ${BASE_PATH}/`,
      });
    }
  }
  try {
    if (req.method === 'GET' && p === '/api/health') {
      return sendJson(res, 200, {
        status: 'ok',
        service: 'cell-tower-lan-telemetry',
        storage: store.backend,
        data_dir: DATA_DIR,
        key_mode: usingTestKey
          ? 'per-site keys (keys.json) + local-test-key fallback'
          : 'per-site keys (keys.json) + CELL_LAN_MASTER_KEY fallback',
        sites: registry.sites.length,
        radios: radioIndex.size,
        readings_stored: store.countReadings(),
        queue: queue.stats(),
        udp_port: UDP_PORT,
      });
    }

    if (req.method === 'GET' && p === '/api/sites') {
      return sendJson(res, 200, { sites: registry.sites });
    }

    const siteMatch = p.match(/^\/api\/sites\/([^/]+)$/);
    if (req.method === 'GET' && siteMatch) {
      const site = registry.sites.find((s) => s.site_id === siteMatch[1]);
      if (!site) return sendJson(res, 404, { error: 'site not found' });
      return sendJson(res, 200, site);
    }

    // Spec route (Encrypt Cell Tower LAN, section 3.4):
    //   POST /lan/telemetry/ingest-async  { encryptedPayload, timestamp }
    // The job is persisted and acknowledged with 202 immediately; the
    // worker decrypts/validates/stores asynchronously (queue.js).
    if (req.method === 'POST' && p === '/lan/telemetry/ingest-async') {
      const body = JSON.parse(await readBody(req));
      if (typeof body.encryptedPayload !== 'string') {
        return sendJson(res, 400, { error: 'encryptedPayload (string) is required' });
      }
      const job = queue.enqueue({
        encryptedPayload: body.encryptedPayload,
        timestamp: body.timestamp || new Date().toISOString(),
        transport: 'http',
      });
      return sendJson(res, 202, {
        accepted: true,
        job: 'process-telemetry',
        queue: 'cell-tower-telemetry',
        job_id: job.id,
      });
    }

    if (req.method === 'GET' && p === '/api/telemetry/latest') {
      const siteId = url.searchParams.get('site_id');
      return sendJson(res, 200, { latest: store.latestByRadio(siteId || undefined) });
    }

    if (req.method === 'GET' && p === '/api/telemetry/history') {
      const radioId = url.searchParams.get('radio_id');
      const limit = Math.min(Number(url.searchParams.get('limit') || 120), MAX_HISTORY_PER_RADIO);
      if (!radioId) return sendJson(res, 400, { error: 'radio_id is required' });
      return sendJson(res, 200, {
        radio_id: radioId,
        readings: store.history(radioId, limit),
      });
    }

    if (req.method === 'GET' && p === '/api/alarms') {
      const siteId = url.searchParams.get('site_id') || undefined;
      const status = url.searchParams.get('status') || undefined;
      return sendJson(res, 200, { alarms: store.listAlarms({ siteId, status, limit: 50 }) });
    }

    // Alarm acknowledge workflow: POST /api/alarms/:id/ack { by, note }
    const ackMatch = p.match(/^\/api\/alarms\/([^/]+)\/ack$/);
    if (req.method === 'POST' && ackMatch) {
      const body = JSON.parse((await readBody(req)) || '{}');
      const alarm = store.ackAlarm(ackMatch[1], {
        by: body.by || 'operator',
        note: body.note || null,
      });
      if (!alarm) return sendJson(res, 404, { error: 'alarm not found' });
      return sendJson(res, 200, { alarm });
    }

    // ------------------------------------------------------------ key admin
    if (req.method === 'GET' && p === '/api/keys') {
      return sendJson(res, 200, { keys: keys.list() });
    }

    const keyAction = p.match(/^\/api\/keys\/([^/]+)\/(generate|rotate|revoke)$/);
    if (req.method === 'POST' && keyAction) {
      const siteId = keyAction[1];
      const action = keyAction[2];
      try {
        let result;
        if (action === 'generate') result = keys.generate(siteId);
        else if (action === 'rotate') result = keys.rotate(siteId);
        else {
          const body = JSON.parse((await readBody(req)) || '{}');
          const keyId = body.keyId || body.key_id;
          if (!keyId) return sendJson(res, 400, { error: 'keyId is required for revoke' });
          result = keys.revoke(siteId, keyId);
        }
        return sendJson(res, 200, { key: result });
      } catch (err) {
        return sendJson(res, 409, { error: err.message });
      }
    }

    // ---------------------------------------------------------- queue admin
    if (req.method === 'GET' && p === '/api/queue/stats') {
      return sendJson(res, 200, { queue: 'cell-tower-telemetry', stats: queue.stats() });
    }

    if (req.method === 'GET' && p === '/api/queue/dlq') {
      const dead = queue.dlq(50).map((j) => ({
        id: j.id,
        name: j.name,
        status: j.status,
        attempts_made: j.attemptsMade,
        max_attempts: j.maxAttempts,
        last_error: j.lastError,
        transport: j.data && j.data.transport,
        created_at: new Date(j.createdAt).toISOString(),
        processed_at: j.processedAt ? new Date(j.processedAt).toISOString() : null,
      }));
      return sendJson(res, 200, { dlq: dead });
    }

    // Static frontend
    if (req.method === 'GET' && (p === '/' || p.startsWith('/app') || p.endsWith('.css'))) {
      const file = p === '/' ? 'index.html' : p.slice(1);
      const full = path.join(FRONTEND_DIR, file);
      if (!full.startsWith(FRONTEND_DIR) || !fs.existsSync(full)) {
        return sendJson(res, 404, { error: 'not found' });
      }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(full)] || 'text/plain' });
      return res.end(fs.readFileSync(full));
    }

    return sendJson(res, 404, { error: 'not found' });
  } catch (err) {
    return sendJson(res, 500, { error: err.message });
  }
});

// --------------------------------------------------------- UDP ingest (spec
// transport: encrypted raw datagrams on a dedicated LAN port). Accepts the
// packed wire bytes (IV || ciphertext || tag), their Base64 text form, or
// the HTTP JSON envelope; all three feed the same queue/worker path.
// Fire-and-forget: no response datagram is sent.
function ingestDatagram(msg, rinfo) {
  let encryptedPayload = null;
  const text = msg.toString('utf8').trim();
  if (text.startsWith('{')) {
    const parsed = JSON.parse(text);
    if (typeof parsed.encryptedPayload === 'string') encryptedPayload = parsed.encryptedPayload;
  } else if (/^[A-Za-z0-9+/=\s]+$/.test(text) && text.length >= 40) {
    encryptedPayload = text.replace(/\s+/g, '');
  }
  if (!encryptedPayload) encryptedPayload = msg.toString('base64'); // raw packed bytes
  return queue.enqueue({
    encryptedPayload,
    timestamp: new Date().toISOString(),
    transport: 'udp',
    remote: `${rinfo.address}:${rinfo.port}`,
  });
}

const udp = dgram.createSocket('udp4');
udp.on('message', (msg, rinfo) => {
  try {
    ingestDatagram(msg, rinfo);
  } catch (err) {
    console.error('udp ingest error:', err.message);
  }
});
udp.on('error', (err) => console.error('udp socket error:', err.message));

if (require.main === module) {
  server.listen(PORT, () => {
    console.log(`cell-tower-lan telemetry service on http://localhost:${PORT}${BASE_PATH}/` +
      (BASE_PATH ? ` (mounted under BASE_PATH=${BASE_PATH})` : ''));
    console.log(`storage: ${store.backend} in ${DATA_DIR}`);
    console.log(`keys: per-site keys in ${path.join(DATA_DIR, 'keys.json')} (legacy fallback: ${usingTestKey ? 'LOCAL TEST KEY' : 'CELL_LAN_MASTER_KEY'})`);
    udp.bind(UDP_PORT, () => {
      console.log(`udp ingest listening on udp://localhost:${UDP_PORT}`);
    });
  });
}

// Exported for tests/diagnostics; importing does not bind any port.
module.exports = { server, udp, ingestDatagram, queue, store, keys, registry };
