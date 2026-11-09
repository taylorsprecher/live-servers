// MotionEngine — cue-based choreography (FMP-1.0 guide, subsystem #2).
//
// Named cues map to motion sequences. A step is one of:
//   { el, className, duration, delay }  — CSS-class motion on a DOM/SVG node
//                                          (el may be an element or a () => element resolver)
//   { fx, duration }                    — imperative effect: fx(ctx) draws on the viewport
// trigger(cue, ctx) runs the sequence in order; ctx carries the OmniEvent
// that caused the cue so fx steps can be event-driven.
//
// Cue durations can be overridden by the versioned motion script fetched
// from "S3" (see app.js): setDurations({ 'incident:tamper': 240, ... })
// scales matching steps proportionally.

const MotionEngine = (() => {
  const cues = new Map();
  const overrides = new Map(); // cue -> total duration budget (ms) from motion script

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  function define(cue, sequence) { cues.set(cue, sequence); }
  function setDurations(map) {
    for (const [cue, ms] of Object.entries(map || {})) overrides.set(cue, ms);
  }
  function has(cue) { return cues.has(cue); }

  // --- Editor introspection (Increment 2: motion-script editor) ---
  function cueNames() { return [...cues.keys()]; }
  function naturalDuration(cue) {
    const seq = cues.get(cue);
    return seq ? seq.reduce((sum, s) => sum + (s.duration || 0) + (s.delay || 0), 0) : 0;
  }
  function getOverrides() { return Object.fromEntries(overrides); }
  function replaceDurations(map) {
    overrides.clear();
    setDurations(map);
  }

  async function run(cue, seq, ctx) {
    // Scale step durations so the sequence fits the script's budget.
    let scale = 1;
    if (overrides.has(cue)) {
      const natural = seq.reduce((sum, s) => sum + (s.duration || 0) + (s.delay || 0), 0);
      if (natural > 0) scale = overrides.get(cue) / natural;
    }
    for (const step of seq) {
      if (step.delay) await sleep(step.delay * scale);
      const el = typeof step.el === 'function' ? step.el(ctx) : step.el;
      if (step.fx) {
        const maybePromise = step.fx(ctx);
        if (step.duration) await Promise.race([Promise.resolve(maybePromise), sleep(step.duration * scale)]);
        else await maybePromise;
        continue;
      }
      if (!el) continue;
      if (step.className) el.classList.add(step.className);
      if (step.duration) {
        await sleep(step.duration * scale);
        if (step.className) el.classList.remove(step.className);
      }
    }
  }

  function trigger(cue, ctx = {}) {
    const seq = cues.get(cue);
    if (seq) run(cue, seq, ctx);
  }

  return { define, trigger, setDurations, has, cueNames, naturalDuration, getOverrides, replaceDurations };
})();
