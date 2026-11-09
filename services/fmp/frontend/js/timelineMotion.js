// TimelineMotion — Timeline Motion Engine v1 (FMP-1.0 guide).
//
// Modes: play | pause | scrub | jump. Events are ReplayEvents
// { t, type, payload } fired into the Omni Event Bus strictly in
// chronological order as the playhead passes them.
//
// Scrub semantics: scrubbing re-bases the playhead without replaying every
// historical animation (that would be motion noise); instead the app's
// onScrubApply hook applies the *state* implied by events up to t
// (active zone, decayed severity). Scrubbing backwards also triggers the
// guide's timeline:reverse warp cue.

const TimelineMotion = (() => {
  let mode = 'pause';
  let playhead = 0;
  let events = [];
  let speed = 1.0;
  let firedIndex = 0;      // events[firedIndex] is next to fire
  let rafId = null;
  let lastFrameAt = 0;
  let onScrubApply = null;

  function loadReplay(data) {
    events = [...data].sort((a, b) => a.t - b.t);
    firedIndex = 0;
    playhead = 0;
  }

  function setSpeed(m) { speed = m; UIStateEngine.setSpeed(m); }

  function fireUpTo(t) {
    while (firedIndex < events.length && events[firedIndex].t <= t) {
      OmniBus.dispatchImmediate(events[firedIndex]);
      firedIndex++;
    }
  }

  function loop(now) {
    if (mode !== 'play') return;
    const dt = now - lastFrameAt;
    lastFrameAt = now;
    playhead += dt * speed;
    const duration = UIStateEngine.state.duration;
    if (playhead >= duration) {
      playhead = duration;
      fireUpTo(playhead);
      UIStateEngine.setPlayhead(playhead);
      pause();
      UIStateEngine.addMessage(`Replay complete (${events.length} events).`, 'info');
      return;
    }
    fireUpTo(playhead);
    UIStateEngine.setPlayhead(playhead);
    rafId = requestAnimationFrame(loop);
  }

  function play() {
    if (mode === 'play') return;
    if (playhead >= UIStateEngine.state.duration) { // restart from top
      playhead = 0; firedIndex = 0; Viewport.resetIncidents();
    }
    mode = 'play';
    UIStateEngine.setMode('play');
    MotionEngine.trigger('timeline:play');
    lastFrameAt = performance.now();
    rafId = requestAnimationFrame(loop);
  }

  function pause() {
    mode = 'pause';
    UIStateEngine.setMode('pause');
    if (rafId) cancelAnimationFrame(rafId);
    rafId = null;
  }

  function setPlayhead(t, isJump = false) {
    const wasPlaying = mode === 'play';
    if (wasPlaying) pause();
    const backwards = t < playhead;
    playhead = t;
    mode = isJump ? 'jump' : 'scrub';
    UIStateEngine.setMode(mode);
    UIStateEngine.setPlayhead(playhead);
    MotionEngine.trigger('timeline:scrub');
    if (backwards) MotionEngine.trigger('timeline:reverse');
    // Re-base: events at or before t are considered already applied.
    firedIndex = events.findIndex(e => e.t > t);
    if (firedIndex === -1) firedIndex = events.length;
    Viewport.resetIncidents();
    if (onScrubApply) onScrubApply(events.filter(e => e.t <= t), t);
  }

  return {
    loadReplay, play, pause, setSpeed,
    scrub: t => setPlayhead(t, false),
    jump: t => setPlayhead(t, true),
    get events() { return events; },
    get mode() { return mode; },
    set onScrubApply(fn) { onScrubApply = fn; },
  };
})();
