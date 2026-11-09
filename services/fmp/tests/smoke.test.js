// smoke.test.js — FMP-1.0 engine + backend smoke tests (zero-dependency).
//
// Covers the Increment 1 guarantees (state store, cue sequencing, bus
// ordering, degradation buffering, timeline play-through) plus the
// Increment 2 additions: icmp / incidentEcho normalization (backend and
// client mirror), the second sample session, motion-script editor
// plumbing (natural durations, override replace), and static wiring
// checks for the new cues, session picker, and WebSocket source.
//
// Run: node tests/smoke.test.js   (exit code 0 = all pass)

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
let passed = 0, failed = 0;
function assert(cond, name) {
  if (cond) { passed++; console.log(`  ok - ${name}`); }
  else { failed++; console.log(`  FAIL - ${name}`); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---------------------------------------------------------------- backend
console.log('backend: normalize + s3store');
const { normalizeSession } = require(path.join(ROOT, 'backend', 'normalize'));
const s3 = require(path.join(ROOT, 'backend', 's3store'));
const load = name => JSON.parse(fs.readFileSync(path.join(ROOT, 'backend', 'data', 'forensics-bucket', 'replay', name), 'utf8'));

const s1 = normalizeSession(load('session-2026-10-08.json'));
assert(s1.events.length === 32, 'session 1 still normalizes to 32 events');
assert(new Set(s1.events.map(e => e.type)).size === 8, 'session 1 covers the original 8 OmniEvent types');

const s2 = normalizeSession(load('session-2026-11-02.json'));
assert(s2.events.filter(e => e.type === 'icmp').length === 3, 'session 2: vpcflow/icmp records normalize to icmp (x3)');
assert(s2.events.filter(e => e.type === 'incidentEcho').length === 3, 'session 2: kinesis/echo records normalize to incidentEcho (x3)');
assert(s2.events.every((e, i, a) => i === 0 || a[i - 1].t <= e.t), 'session 2 events are in strict chronological order');
const firstIcmp = s2.events.find(e => e.type === 'icmp');
assert(firstIcmp.payload.probes === 64, 'icmp event payload carries probes');
const firstEcho = s2.events.find(e => e.type === 'incidentEcho');
assert(firstEcho.payload.echoOf === 'cap-203', 'incidentEcho payload carries echoOf');

const tmpKey = 'motion-scripts/.smoke-test.json';
try {
  s3.putObject('forensics-bucket', tmpKey, JSON.stringify({ version: 'smoke', cueDurationsMs: { 'incident:icmp': 900 } }));
  const back = JSON.parse(s3.getObject('forensics-bucket', tmpKey).Body);
  assert(back.cueDurationsMs['incident:icmp'] === 900, 's3store putObject/getObject round-trip (script save path)');
} finally {
  fs.unlinkSync(path.join(ROOT, 'backend', 'data', 'forensics-bucket', tmpKey));
}

// ------------------------------------------------------- frontend engines
console.log('frontend engines (DOM-shimmed vm)');
function makeEl() {
  const classes = new Set();
  const el = {
    children: [], style: {}, dataset: {}, hidden: false, value: '', textContent: '', innerHTML: '',
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c),
      toggle: (c, force) => { if (force === undefined) force = !classes.has(c); force ? classes.add(c) : classes.delete(c); },
      contains: c => classes.has(c),
    },
    setAttribute() {}, getAttribute: () => null,
    appendChild(c) { el.children.push(c); return c; },
    addEventListener() {},
    querySelectorAll: () => [], querySelector: () => makeEl(),
    getBoundingClientRect: () => ({ width: 960, height: 420 }),
    getContext: () => ({ clearRect() {}, beginPath() {}, arc() {}, fill() {} }),
    getTotalLength: () => 100, getPointAtLength: () => ({ x: 0, y: 0 }),
  };
  return el;
}
const elements = {};
let rafCb = null;
const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval, performance,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  requestAnimationFrame: cb => { rafCb = cb; return 1; },
  cancelAnimationFrame: () => { rafCb = null; },
  document: {
    getElementById: id => (elements[id] = elements[id] || makeEl()),
    createElement: () => makeEl(),
    createElementNS: () => makeEl(),
    documentElement: makeEl(), body: makeEl(), activeElement: null,
  },
};
sandbox.window = { matchMedia: () => ({ matches: true }), addEventListener() {} };
vm.createContext(sandbox);
const loadJs = rel => vm.runInContext(fs.readFileSync(path.join(ROOT, 'frontend', 'js', rel), 'utf8'), sandbox, { filename: rel });
loadJs('uiStateEngine.js');
loadJs('motionEngine.js');
loadJs('omniBus.js');
loadJs('timelineMotion.js');

(async () => {
  // UIStateEngine
  vm.runInContext(`UIStateEngine.setSeverity(1.7)`, sandbox);
  assert(vm.runInContext(`UIStateEngine.state.severity`, sandbox) === 1, 'UIStateEngine clamps severity to 1');

  // MotionEngine: definitions, natural durations, editor override plumbing
  vm.runInContext(`MotionEngine.define('test:two-step', [
    { el: () => null, className: 'x', duration: 300, delay: 50 },
    { fx: () => {}, duration: 150 },
  ])`, sandbox);
  assert(vm.runInContext(`MotionEngine.has('test:two-step')`, sandbox) === true, 'MotionEngine.define/has registers a cue');
  assert(vm.runInContext(`MotionEngine.naturalDuration('test:two-step')`, sandbox) === 500, 'naturalDuration sums step durations + delays');
  vm.runInContext(`MotionEngine.replaceDurations({ 'test:two-step': 42, 'incident:icmp': 900 })`, sandbox);
  const ov = vm.runInContext(`MotionEngine.getOverrides()`, sandbox);
  assert(ov['test:two-step'] === 42 && ov['incident:icmp'] === 900, 'replaceDurations/getOverrides round-trip (editor Apply path)');
  vm.runInContext(`MotionEngine.replaceDurations({ 'storm:burst': 1400 })`, sandbox);
  const ov2 = vm.runInContext(`MotionEngine.getOverrides()`, sandbox);
  assert(ov2['test:two-step'] === undefined && ov2['storm:burst'] === 1400, 'replaceDurations fully replaces (Reset path)');

  // MotionEngine: override actually re-times a running cue
  const timing = vm.runInContext(`(() => {
    const el = document.getElementById('timing-el');
    MotionEngine.define('test:timed', [{ el, className: 'on', duration: 5000 }]);
    MotionEngine.replaceDurations({ 'test:timed': 60 });
    MotionEngine.trigger('test:timed');
    return el;
  })()`, sandbox);
  assert(timing.classList.contains('on'), 'trigger applies the cue class immediately');
  await sleep(300);
  assert(!timing.classList.contains('on'), 'override duration (60ms) re-times the cue instead of the natural 5000ms');

  // OmniBus: client-side normalization mirror for the new kinds
  const n1 = vm.runInContext(`OmniBus.normalize({ source: 'vpcflow', kind: 'icmp', t: 10, zone: 'DMZ', severity: 0.5, probes: 8 })`, sandbox);
  assert(n1.type === 'icmp', 'OmniBus.normalize maps vpcflow/icmp -> icmp');
  const n2 = vm.runInContext(`OmniBus.normalize({ source: 'kinesis', kind: 'echo', t: 20, from: 'DATA', to: 'APP' })`, sandbox);
  assert(n2.type === 'incidentEcho', 'OmniBus.normalize maps kinesis/echo -> incidentEcho');

  // OmniBus: chronological flush of out-of-order live events
  const order = [];
  vm.runInContext(`OmniBus.on('storm', e => globalThis.__order.push(e.t))`, sandbox);
  sandbox.__order = order;
  vm.runInContext(`OmniBus.dispatch({ t: 300, type: 'storm', severity: 0.5, zone: 'APP', payload: {} });
                   OmniBus.dispatch({ t: 100, type: 'storm', severity: 0.5, zone: 'APP', payload: {} })`, sandbox);
  await sleep(400);
  assert(order.join(',') === '100,300', 'OmniBus flushes out-of-order live events chronologically');

  // OmniBus: graceful degradation buffers, recovery flushes
  const breachSeen = [];
  sandbox.__breach = breachSeen;
  vm.runInContext(`OmniBus.on('breach', e => globalThis.__breach.push(e.t));
                   OmniBus.setDegraded(true);
                   OmniBus.dispatch({ t: 5000, type: 'breach', severity: 0.9, zone: 'SEC', payload: {} })`, sandbox);
  await sleep(400);
  assert(breachSeen.length === 0, 'degraded bus buffers events instead of dispatching');
  vm.runInContext(`OmniBus.setDegraded(false)`, sandbox);
  await sleep(400);
  assert(breachSeen.join(',') === '5000', 'recovery flushes the buffered event in order');

  // TimelineMotion: sorted load, scrub-apply hook, ordered play-through
  const fired = [];
  sandbox.__fired = fired;
  vm.runInContext(`Viewport = { resetIncidents() {} }; // timeline only needs this stub
                   OmniBus.on('*', e => globalThis.__fired.push(e.t));
                   TimelineMotion.loadReplay([{ t: 9000, type: 'packet' }, { t: 1000, type: 'packet' }, { t: 5000, type: 'packet' }]);
                   TimelineMotion.onScrubApply = applied => { globalThis.__applied = applied.map(e => e.t); };
                   TimelineMotion.jump(5000);`, sandbox);
  assert(JSON.stringify(sandbox.__applied) === '[1000,5000]', 'scrub-apply receives exactly the events at or before t');
  assert(fired.length === 0, 'jump does not fire historical events');
  vm.runInContext(`TimelineMotion.play()`, sandbox);
  // Drive rAF frames manually: +3s of event-time per frame.
  for (let i = 0; i < 6 && rafCb; i++) {
    const cb = rafCb; rafCb = null;
    cb(performance.now() + 3000 * (i + 1) + 50);
    await sleep(5);
  }
  assert(fired.join(',') === '9000', 'play-through after jump fires only the remaining event, in order');

  // ---------------------------------------------------------- static wiring
  console.log('static wiring checks');
  const appSrc = fs.readFileSync(path.join(ROOT, 'frontend', 'js', 'app.js'), 'utf8');
  assert(appSrc.includes(`MotionEngine.define('incident:icmp'`), 'app.js defines the incident:icmp cue');
  assert(appSrc.includes(`MotionEngine.define('packet:incidentEcho'`), 'app.js defines the packet:incidentEcho cue');
  assert(appSrc.includes(`OmniBus.on('icmp'`) && appSrc.includes(`OmniBus.on('incidentEcho'`), 'app.js routes icmp + incidentEcho events to cues');
  assert(appSrc.includes('startLiveWS') && appSrc.includes('/api/events/ws'), 'app.js wires the WebSocket live feed');
  assert(appSrc.includes('listSessions') && appSrc.includes('loadSession'), 'app.js implements the session picker');
  assert(appSrc.includes('cue-apply') && appSrc.includes('cue-save'), 'app.js implements the motion-script editor');
  const html = fs.readFileSync(path.join(ROOT, 'frontend', 'index.html'), 'utf8');
  assert(html.includes('id="session-select"') && html.includes('value="live-ws"') && html.includes('id="cue-editor-rows"'),
    'index.html exposes session picker, WebSocket source, and editor');
  const serverSrc = fs.readFileSync(path.join(ROOT, 'backend', 'server.js'), 'utf8');
  assert(serverSrc.includes("'/api/events/ws'") || serverSrc.includes('"/api/events/ws"') || serverSrc.includes('/api/events/ws'),
    'server.js routes the WebSocket feed');

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(err => { console.error(err); process.exit(1); });
