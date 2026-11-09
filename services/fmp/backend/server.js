// server.js — FMP-1.0 backend (zero-dependency Node http server).
//
// Serves:
//   GET  /api/health                             -> { status: 'ok', ... }
//   GET  /api/buckets                            -> S3 ListBuckets (local stub store)
//   GET  /api/buckets/:bucket/objects?prefix=    -> S3 ListObjects (local stub store)
//   GET  /api/replay?bucket=&key=               -> normalized OmniEvent timeline
//   GET  /api/motion-script?bucket=&key=        -> versioned motion script (cue durations)
//   POST /api/motion-script?bucket=&key=        -> save a new motion script version
//                                                  (Increment 2: script editor backend)
//   GET  /api/events/stream?speed=1&key=        -> SSE live OmniEvent stream (replay loop)
//   WS   /api/events/ws?speed=1&key=            -> WebSocket live OmniEvent feed
//                                                  (Increment 2; minimal RFC 6455,
//                                                  zero-dependency — alongside SSE)
//   GET  /*                                      -> static frontend (../frontend)
//
// BASE_PATH (env, default ""): when the app sits behind a path-prefix
// gateway (e.g. BASE_PATH=/fmp), requests arrive WITH the prefix; it is
// stripped before routing, so the same code serves at the root or under
// any prefix. The frontend derives the same prefix from window.location.
//
// Real AWS S3 is STUBBED: the "bucket" is backend/data/<bucket>/<key> via
// s3store.js, which mirrors the S3 API shape.

const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const s3 = require('./s3store');
const { normalizeSession } = require('./normalize');

const PORT = process.env.FMP_PORT || 8787;
const BASE_PATH = (() => {
  let b = process.env.BASE_PATH || '';
  if (b && !b.startsWith('/')) b = `/${b}`;
  return b.replace(/\/+$/, '');
})();

// Remove the gateway prefix from a request path (no-op at the root).
function stripBase(p) {
  if (!BASE_PATH) return p;
  if (p === BASE_PATH) return '/';
  if (p.startsWith(`${BASE_PATH}/`)) return p.slice(BASE_PATH.length);
  return p;
}
const FRONTEND_DIR = path.join(__dirname, '..', 'frontend');
const DEFAULT_BUCKET = 'forensics-bucket';
const DEFAULT_REPLAY_KEY = 'replay/session-2026-10-08.json';
const DEFAULT_SCRIPT_KEY = 'motion-scripts/fmp-motion-v1.json';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
};

function json(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(body);
}

function loadReplay(bucket, key) {
  const obj = s3.getObject(bucket, key);
  return normalizeSession(JSON.parse(obj.Body));
}

// Shared live-playout engine for the SSE and WebSocket feeds: walks a
// replay session's events in event-time, looping, at the given speed.
// send(evt) returns false to stop the stream. Returns a stop function.
function streamEvents(send, bucket, key, speed) {
  const { events, durationMs } = loadReplay(bucket, key);
  let idx = 0;
  let cursor = 0; // event-time cursor (ms)
  const startedAt = Date.now();
  const timer = setInterval(() => {
    const elapsed = (Date.now() - startedAt) * speed;
    const target = elapsed % (durationMs + 2000);
    if (target < cursor) idx = 0; // looped
    cursor = target;
    while (idx < events.length && events[idx].t <= cursor) {
      if (send(events[idx]) === false) { clearInterval(timer); return; }
      idx++;
    }
  }, 100);
  return () => clearInterval(timer);
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const p = stripBase(url.pathname);

  try {
    if (p === '/api/health') {
      return json(res, 200, { status: 'ok', service: 'fmp-backend', basePath: BASE_PATH || '/', s3: 'stubbed-local', liveFeeds: ['sse', 'websocket'], buckets: s3.listBuckets().map(b => b.Name) });
    }
    if (p === '/api/buckets') {
      return json(res, 200, { Buckets: s3.listBuckets() });
    }
    const m = p.match(/^\/api\/buckets\/([^/]+)\/objects$/);
    if (m) {
      const bucket = decodeURIComponent(m[1]);
      return json(res, 200, { Name: bucket, Contents: s3.listObjects(bucket, url.searchParams.get('prefix') || '') });
    }
    if (p === '/api/replay') {
      const bucket = url.searchParams.get('bucket') || DEFAULT_BUCKET;
      const key = url.searchParams.get('key') || DEFAULT_REPLAY_KEY;
      return json(res, 200, loadReplay(bucket, key));
    }
    if (p === '/api/motion-script' && req.method === 'POST') {
      // Motion-script editor save: persist a new versioned script object.
      // Keys are constrained to motion-scripts/*.json inside the bucket.
      const bucket = url.searchParams.get('bucket') || DEFAULT_BUCKET;
      const key = url.searchParams.get('key') || '';
      if (!key.startsWith('motion-scripts/') || !key.endsWith('.json')) {
        return json(res, 400, { error: 'key must be motion-scripts/<name>.json' });
      }
      let body = '';
      req.on('data', chunk => { body += chunk; if (body.length > 65536) req.destroy(); });
      req.on('end', () => {
        try {
          const script = JSON.parse(body);
          const durations = script && script.cueDurationsMs;
          if (!durations || typeof durations !== 'object'
              || !Object.values(durations).every(v => typeof v === 'number' && v > 0)) {
            return json(res, 400, { error: 'cueDurationsMs must map cue names to positive ms numbers' });
          }
          const saved = { version: String(script.version || 'fmp-motion/edited'), cueDurationsMs: durations };
          const meta = s3.putObject(bucket, key, JSON.stringify(saved, null, 2));
          return json(res, 200, { ok: true, bucket, ...meta, version: saved.version });
        } catch (err) {
          return json(res, 400, { error: String(err && err.message || err) });
        }
      });
      return;
    }
    if (p === '/api/motion-script') {
      const bucket = url.searchParams.get('bucket') || DEFAULT_BUCKET;
      const key = url.searchParams.get('key') || DEFAULT_SCRIPT_KEY;
      const obj = s3.getObject(bucket, key);
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      return res.end(obj.Body);
    }
    if (p === '/api/events/stream') {
      // Live mode: play the selected replay session out as an SSE
      // OmniEvent stream, looping. The frontend's graceful-degradation
      // path is exercised by simply stopping this server.
      const speed = Math.max(0.1, parseFloat(url.searchParams.get('speed') || '1'));
      const key = url.searchParams.get('key') || DEFAULT_REPLAY_KEY;
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      });
      const stop = streamEvents(
        evt => { res.write(`event: omnievent\ndata: ${JSON.stringify(evt)}\n\n`); },
        DEFAULT_BUCKET, key, speed);
      req.on('close', stop);
      return;
    }
    if (p.startsWith('/api/')) return json(res, 404, { error: 'not found', path: p });

    // Static frontend
    const rel = p === '/' ? 'index.html' : p.slice(1);
    const file = path.resolve(FRONTEND_DIR, rel);
    if (!file.startsWith(FRONTEND_DIR) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      return res.end('not found');
    }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
    return res.end(fs.readFileSync(file));
  } catch (err) {
    return json(res, 500, { error: String(err && err.message || err) });
  }
});

// ---------- WebSocket live feed (Increment 2) ----------
// Minimal RFC 6455 server, zero-dependency: handshake via
// Sec-WebSocket-Accept, unmasked server->client text frames, and just
// enough client-frame parsing to answer pings and honor close frames.
// One-way feed: clients send nothing but control frames.
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

function wsAccept(key) {
  return crypto.createHash('sha1').update(key + WS_GUID).digest('base64');
}

function wsEncodeFrame(str, opcode = 0x1) {
  const payload = Buffer.from(str, 'utf8');
  const len = payload.length;
  let header;
  if (len < 126) {
    header = Buffer.from([0x80 | opcode, len]);
  } else if (len < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x80 | opcode; header[1] = 126; header.writeUInt16BE(len, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x80 | opcode; header[1] = 127; header.writeBigUInt64BE(BigInt(len), 2);
  }
  return Buffer.concat([header, payload]);
}

// Extract { opcode, payload } from the first client frame in buf
// (client frames are masked), or null if incomplete.
function wsParseFrame(buf) {
  if (buf.length < 2) return null;
  const opcode = buf[0] & 0x0f;
  const masked = (buf[1] & 0x80) !== 0;
  let len = buf[1] & 0x7f;
  let off = 2;
  if (len === 126) { if (buf.length < off + 2) return null; len = buf.readUInt16BE(off); off += 2; }
  else if (len === 127) { if (buf.length < off + 8) return null; len = Number(buf.readBigUInt64BE(off)); off += 8; }
  let mask = null;
  if (masked) { if (buf.length < off + 4) return null; mask = buf.slice(off, off + 4); off += 4; }
  if (buf.length < off + len) return null;
  const payload = Buffer.from(buf.slice(off, off + len));
  if (mask) for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i % 4];
  return { opcode, payload, rest: buf.slice(off + len) };
}

server.on('upgrade', (req, socket) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const key = req.headers['sec-websocket-key'];
  if (stripBase(url.pathname) !== '/api/events/ws' || !key) { socket.destroy(); return; }
  socket.write(
    'HTTP/1.1 101 Switching Protocols\r\n'
    + 'Upgrade: websocket\r\n'
    + 'Connection: Upgrade\r\n'
    + `Sec-WebSocket-Accept: ${wsAccept(key)}\r\n\r\n`);

  const speed = Math.max(0.1, parseFloat(url.searchParams.get('speed') || '1'));
  const replayKey = url.searchParams.get('key') || DEFAULT_REPLAY_KEY;
  let stop;
  try {
    stop = streamEvents(evt => {
      if (socket.destroyed || !socket.writable) return false;
      socket.write(wsEncodeFrame(JSON.stringify(evt)));
    }, DEFAULT_BUCKET, replayKey, speed);
  } catch (err) {
    socket.destroy();
    return;
  }

  let buf = Buffer.alloc(0);
  socket.on('data', chunk => {
    buf = Buffer.concat([buf, chunk]);
    let frame;
    while ((frame = wsParseFrame(buf))) {
      buf = frame.rest;
      if (frame.opcode === 0x8) { stop(); socket.end(); return; }       // close
      if (frame.opcode === 0x9) {                                        // ping -> pong
        if (!socket.destroyed) socket.write(wsEncodeFrame(frame.payload.toString('utf8'), 0xA));
      }
    }
  });
  socket.on('close', stop);
  socket.on('error', stop);
});

server.listen(PORT, () => {
  console.log(`FMP backend listening on http://localhost:${PORT}${BASE_PATH}/ (S3 stubbed: ${s3.ROOT})`);
});
