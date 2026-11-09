// normalize.js — Omni Event Bus normalization layer (backend mirror).
//
// Per the FMP-1.0 guide, raw telemetry (CloudWatch / Kinesis / VPC Flow /
// Lambda) is mapped into uniform OmniEvent types before it reaches any
// animation handler or state reducer:
//
//   kinesis  packet  -> packet            (packet rematerialization)
//   vpcflow  zone    -> zoneTransition    (zone:deactivate / zone:activate)
//   cloudwatch alarm -> tamper            (severity spike)
//   kinesis  burst   -> storm             (packet storm)
//   vpcflow  breach  -> breach            (zone breach)
//   cloudwatch escalation -> escalation   (forward cascade)
//   omni     collapse -> collapse         (reverse cooling cascade)
//   lambda   dlq     -> dlq               (dead-letter / decrypt failure)
//   vpcflow  icmp    -> icmp              (ICMP sweep / probe incident, Increment 2)
//   kinesis  echo    -> incidentEcho      (packet incident echo, Increment 2)
//
// Every OmniEvent carries t in milliseconds and events are always emitted
// in strict chronological order.

const TYPE_MAP = [
  ['kinesis', 'packet', 'packet'],
  ['vpcflow', 'zone', 'zoneTransition'],
  ['cloudwatch', 'alarm', 'tamper'],
  ['kinesis', 'burst', 'storm'],
  ['vpcflow', 'breach', 'breach'],
  ['cloudwatch', 'escalation', 'escalation'],
  ['omni', 'collapse', 'collapse'],
  ['lambda', 'dlq', 'dlq'],
  ['vpcflow', 'icmp', 'icmp'],
  ['kinesis', 'echo', 'incidentEcho'],
];

function normalizeRecord(rec) {
  const row = TYPE_MAP.find(([s, k]) => s === rec.source && k === rec.kind);
  const type = row ? row[2] : rec.kind;
  return {
    t: rec.t,
    type,
    severity: typeof rec.severity === 'number' ? rec.severity : 0,
    zone: rec.zone || null,
    from: rec.from || null,
    to: rec.to || null,
    payload: {
      source: rec.source,
      kind: rec.kind,
      capsuleId: rec.capsuleId || null,
      bytes: rec.bytes || null,
      alarm: rec.alarm || null,
      queue: rec.queue || null,
      packetsPerSec: rec.packetsPerSec || null,
      probes: rec.probes || null,
      echoOf: rec.echoOf || null,
      detail: rec.detail || null,
    },
  };
}

// Normalize a whole replay object and guarantee chronological order.
function normalizeSession(session) {
  const events = (session.records || []).map(normalizeRecord);
  events.sort((a, b) => a.t - b.t);
  return { sessionId: session.sessionId, durationMs: session.durationMs, events };
}

module.exports = { normalizeRecord, normalizeSession };
