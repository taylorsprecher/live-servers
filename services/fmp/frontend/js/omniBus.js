// OmniBus — the Omni Event Bus (FMP-1.0 guide, subsystem #3).
//
// Routes standardized OmniEvent payloads to animation handlers and state
// reducers. Guarantees from the guide:
//   * Normalization — inbound raw records are mapped to uniform OmniEvent
//     types (packet, zoneTransition, tamper, storm, breach, escalation,
//     collapse, dlq). The backend already normalizes; normalize() here is
//     the client-side mirror for any raw source.
//   * Timestamp ordering — events are processed strictly in chronological
//     order (t in ms). Live events arriving out of order are held in a
//     short reorder window and flushed in order.
//   * Graceful degradation — if the upstream feed fails, events buffer
//     locally and dispatch pauses; UI state is never broken.

const OmniBus = (() => {
  const handlers = new Map(); // type -> Set<fn>
  const REORDER_WINDOW_MS = 250;

  let lastDispatchedT = -Infinity;
  let reorderBuffer = [];
  let flushTimer = null;
  let degraded = false;
  let onDegradedChange = null;

  const KIND_MAP = {
    'kinesis:packet': 'packet',
    'vpcflow:zone': 'zoneTransition',
    'cloudwatch:alarm': 'tamper',
    'kinesis:burst': 'storm',
    'vpcflow:breach': 'breach',
    'cloudwatch:escalation': 'escalation',
    'omni:collapse': 'collapse',
    'lambda:dlq': 'dlq',
    'vpcflow:icmp': 'icmp',
    'kinesis:echo': 'incidentEcho',
  };

  function normalize(raw) {
    if (raw && raw.payload && raw.type && typeof raw.t === 'number') return raw; // already an OmniEvent
    const type = KIND_MAP[`${raw.source}:${raw.kind}`] || raw.kind || 'packet';
    return {
      t: raw.t, type,
      severity: raw.severity || 0,
      zone: raw.zone || null, from: raw.from || null, to: raw.to || null,
      payload: { ...raw },
    };
  }

  function on(type, fn) {
    if (!handlers.has(type)) handlers.set(type, new Set());
    handlers.get(type).add(fn);
    return () => handlers.get(type).delete(fn);
  }

  function dispatchNow(evt) {
    lastDispatchedT = Math.max(lastDispatchedT, evt.t);
    const set = handlers.get(evt.type);
    if (set) set.forEach(fn => fn(evt));
    const any = handlers.get('*');
    if (any) any.forEach(fn => fn(evt));
  }

  function flush() {
    flushTimer = null;
    if (degraded) return; // buffered until feed recovers
    reorderBuffer.sort((a, b) => a.t - b.t);
    const ready = reorderBuffer;
    reorderBuffer = [];
    ready.forEach(dispatchNow);
  }

  // Ordered dispatch for live sources.
  function dispatch(evt) {
    const e = normalize(evt);
    if (e.t < lastDispatchedT) {
      // Late arrival inside the forensic window: still route it (replay of
      // history is the product's purpose) but it cannot rewind the clock.
      dispatchNow(e);
      return;
    }
    reorderBuffer.push(e);
    if (!flushTimer) flushTimer = setTimeout(flush, REORDER_WINDOW_MS);
  }

  // Immediate dispatch for replay, where the timeline itself is the clock
  // and events are pre-sorted by TimelineMotion.
  function dispatchImmediate(evt) {
    dispatchNow(normalize(evt));
  }

  function setDegraded(value) {
    degraded = value;
    if (onDegradedChange) onDegradedChange(value);
    if (!value) flush(); // feed recovered: release the buffer in order
  }

  return {
    on, dispatch, dispatchImmediate, normalize, setDegraded,
    get degraded() { return degraded; },
    set onDegradedChange(fn) { onDegradedChange = fn; },
  };
})();
