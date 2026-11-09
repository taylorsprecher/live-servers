// build-demo.js — package the FMP-1.0 frontend as one self-contained
// static page: demo/index.html.
//
// Everything is inlined: the stylesheet, all six frontend scripts, both
// sample replay sessions (normalized through backend/normalize.js, the
// same code path the live backend uses), and the v1 motion script. A
// small fetch shim answers the app's API calls from the inlined data, so
// replay, scrubbing, the session picker, visualizations, and the
// motion-script editor (apply-live; saves kept in memory) all work with
// no backend — suitable for embedding as a section in a static website.
// Live transports (SSE/WebSocket) are removed from the demo's source
// picker: they are the only features that genuinely need the server.
//
// Run: node build-demo.js

const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const FE = path.join(ROOT, 'frontend');
const DATA = path.join(ROOT, 'backend', 'data', 'forensics-bucket');
const { normalizeSession } = require(path.join(ROOT, 'backend', 'normalize'));

const read = p => fs.readFileSync(p, 'utf8');
const safeJson = obj => JSON.stringify(obj).replace(/<\//g, '<\\/');

const sessions = {};
for (const name of ['session-2026-10-08.json', 'session-2026-11-02.json']) {
  const raw = JSON.parse(read(path.join(DATA, 'replay', name)));
  sessions[`replay/${name}`] = normalizeSession(raw);
}
const script = JSON.parse(read(path.join(DATA, 'motion-scripts', 'fmp-motion-v1.json')));

const shim = `
// ---- FMP static-demo data shim (inlined sessions + motion script) ----
window.fetch = (() => {
  const REPLAYS = ${safeJson(sessions)};
  let currentScript = ${safeJson(script)};
  const scriptKeys = ['motion-scripts/fmp-motion-v1.json'];
  const savedScripts = {};
  const respond = (obj, status = 200) => Promise.resolve({
    ok: status < 400, status,
    json: () => Promise.resolve(obj),
    text: () => Promise.resolve(JSON.stringify(obj)),
  });
  return (url, opts = {}) => {
    const u = new URL(url, location.href);
    const p = u.pathname;
    // app.js now prefixes API calls with the mount path derived from the
    // page URL, so match on the path suffix, not the whole pathname.
    if (p.endsWith('/api/motion-script') && (opts.method || 'GET') === 'POST') {
      const key = u.searchParams.get('key');
      const body = JSON.parse(opts.body);
      savedScripts[key] = body;
      if (!scriptKeys.includes(key)) scriptKeys.push(key);
      currentScript = body;
      return respond({ ok: true, bucket: 'forensics-bucket', Key: key, version: body.version });
    }
    if (p.endsWith('/api/motion-script')) {
      const key = u.searchParams.get('key');
      return respond(key && savedScripts[key] ? savedScripts[key] : currentScript);
    }
    if (p.endsWith('/api/replay')) {
      const key = u.searchParams.get('key') || 'replay/session-2026-10-08.json';
      return REPLAYS[key] ? respond(REPLAYS[key]) : respond({ error: 'unknown session' }, 404);
    }
    if (/\\/api\\/buckets\\/[^/]+\\/objects$/.test(p)) {
      const prefix = u.searchParams.get('prefix') || '';
      const keys = prefix.startsWith('replay/') ? Object.keys(REPLAYS)
        : prefix.startsWith('motion-scripts/') ? scriptKeys : [];
      return respond({ Name: 'forensics-bucket', Contents: keys.map(k => ({ Key: k, Size: 0, LastModified: new Date().toISOString() })) });
    }
    return respond({ error: 'not available in the static demo' }, 404);
  };
})();
`;

let html = read(path.join(FE, 'index.html'));

const css = read(path.join(FE, 'css', 'styles.css'));
if (css.includes('</style')) throw new Error('CSS contains </style — cannot inline');
html = html.replace('<link rel="stylesheet" href="css/styles.css">', () => `<style>\n${css}\n</style>`);

html = html.replace('<title>FMP-1.0 — Forensic Motion Protocol</title>',
  '<title>FMP-1.0 — Forensic Motion Protocol (Static Demo)</title>');
html = html.replace('Telemetry-driven forensic motion replay',
  'Telemetry-driven forensic motion replay · static demo — no backend required');
html = html.replace('stores a new versioned script object.',
  'stores a new versioned script object (in this demo, kept in memory only).');
// Live transports need the server; the demo is replay-only.
html = html.replace(/\s*<option value="live">.*?<\/option>/, '');
html = html.replace(/\s*<option value="live-ws">.*?<\/option>/, '');

const JS_ORDER = ['uiStateEngine.js', 'motionEngine.js', 'omniBus.js', 'viewport.js', 'timelineMotion.js', 'app.js'];
for (const name of JS_ORDER) {
  const src = read(path.join(FE, 'js', name));
  if (src.includes('</script')) throw new Error(`${name} contains </script — cannot inline`);
  const tag = `<script src="js/${name}"></script>`;
  if (!html.includes(tag)) throw new Error(`script tag not found in index.html: ${tag}`);
  const inline = name === 'app.js' ? `<script>${shim}</script>\n<script>\n${src}\n</script>` : `<script>\n${src}\n</script>`;
  html = html.replace(tag, () => inline);
}

if (/<script src=|<link rel="stylesheet"/.test(html)) throw new Error('external references remain in demo HTML');

const outDir = path.join(ROOT, 'demo');
fs.mkdirSync(outDir, { recursive: true });
const out = path.join(outDir, 'index.html');
fs.writeFileSync(out, html);
console.log(`demo written: ${out} (${(html.length / 1024).toFixed(1)} KiB, sessions: ${Object.keys(sessions).join(', ')})`);
