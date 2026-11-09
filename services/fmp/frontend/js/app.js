// app.js — FMP-1.0 boot sequence and wiring.
// Guide order: 1) UIStateEngine, 2) MotionEngine cues, 3) replay timelines
// + versioned motion scripts from S3, 4) bind telemetry (Omni Event Bus).
//
// Increment 2: session picker (any replay/ object through the normalizer),
// the guide's remaining cues (incident:icmp, packet:incidentEcho), a
// WebSocket live feed alongside SSE, and a motion-script editor whose
// duration edits apply live and can be saved as a new script version.

(() => {
  const BUCKET = 'forensics-bucket';
  const DEFAULT_SESSION_KEY = 'replay/session-2026-10-08.json';

  // Base path: derive the mount prefix from the page URL so every API
  // call works at the root (BASE_PATH = "") and behind a path-prefix
  // gateway (e.g. served from /fmp/). The page is always index.html at
  // the mount root, so the prefix is the page's directory.
  const BASE_PATH = (() => {
    let p = window.location.pathname;
    if (p.endsWith('/index.html')) p = p.slice(0, -'/index.html'.length);
    if (!p.endsWith('/')) p = p.slice(0, p.lastIndexOf('/') + 1);
    return p.replace(/\/+$/, '');
  })();

  // ---------- 1. Boot UIStateEngine first ----------
  UIStateEngine.init();
  Viewport.build();

  const $ = id => document.getElementById(id);
  const viewportEl = $('viewport');

  // ---------- 2. Register MotionEngine cues (motion grammar) ----------
  MotionEngine.define('timeline:play', [
    { el: () => $('playhead'), className: 'playhead-glide', duration: 400 },
  ]);
  MotionEngine.define('timeline:scrub', [
    { el: () => $('playhead'), className: 'playhead-snap', duration: 120 },
  ]);
  MotionEngine.define('timeline:reverse', [
    { el: () => viewportEl, className: 'reverse-warp', duration: 260 },
  ]);
  MotionEngine.define('packet:rematerialize', [
    { fx: ctx => Viewport.rematerialize(ctx.event), duration: 1150 },
  ]);
  MotionEngine.define('packet:highlight', [
    { el: () => $('packet-info'), className: 'packet-pop', duration: 180 },
  ]);
  MotionEngine.define('packet:incidentEcho', [
    { fx: ctx => Viewport.incidentEcho(ctx.event), duration: 1250 },
  ]);
  MotionEngine.define('zone:focus', [
    { el: ctx => Viewport.haloEl(ctx.event.to || ctx.event.zone), className: 'zone-pulse', duration: 300 },
  ]);
  MotionEngine.define('incident:tamper', [
    { el: () => viewportEl, className: 'tamper-flash', duration: 120 },
    { fx: ctx => { if (ctx.event.zone) { Viewport.shockwave(ctx.event.zone, 'ring-pulse'); Viewport.sparksAtZone(ctx.event.zone, '#ffb84d'); } }, duration: 240 },
  ]);
  MotionEngine.define('incident:dlq', [
    { el: ctx => Viewport.zoneEl(ctx.event.zone || 'APP'), className: 'dlq-shake', duration: 200 },
  ]);
  MotionEngine.define('incident:icmp', [
    { fx: ctx => Viewport.icmpSweep(ctx.event.zone || 'RAN'), duration: 900 },
  ]);
  MotionEngine.define('storm:burst', [
    { fx: ctx => Viewport.storm(ctx.event.zone || 'APP', ctx.event.severity || 0.8), duration: 1400 },
    { el: () => $('severity-meter'), className: 'severity-spike', duration: 350 },
  ]);
  MotionEngine.define('breach:surge', [
    { fx: ctx => Viewport.breach(ctx.event.zone || 'SEC'), duration: 1200 },
  ]);
  MotionEngine.define('escalation:cascade', [
    { fx: () => Viewport.cascadeForward(), duration: 1900 },
  ]);
  MotionEngine.define('collapse:cooling', [
    { fx: () => Viewport.cascadeCooling(), duration: 2100 },
  ]);

  // ---------- Omni Event Bus handlers: event -> cue + state ----------
  OmniBus.on('packet', evt => {
    MotionEngine.trigger('packet:rematerialize', { event: evt });
    MotionEngine.trigger('packet:highlight', { event: evt });
  });
  OmniBus.on('incidentEcho', evt => {
    MotionEngine.trigger('packet:incidentEcho', { event: evt });
  });
  OmniBus.on('zoneTransition', evt => {
    if (evt.from) Viewport.deactivateZone(evt.from);
    if (evt.to) {
      Viewport.activateZone(evt.to);
      UIStateEngine.setActiveZone(evt.to);
    }
    MotionEngine.trigger('zone:focus', { event: evt });
  });
  OmniBus.on('tamper', evt => {
    UIStateEngine.setSeverity(Math.max(UIStateEngine.state.severity, evt.severity));
    MotionEngine.trigger('incident:tamper', { event: evt });
  });
  OmniBus.on('dlq', evt => MotionEngine.trigger('incident:dlq', { event: evt }));
  OmniBus.on('icmp', evt => {
    UIStateEngine.setSeverity(Math.max(UIStateEngine.state.severity, (evt.severity || 0) * 0.8));
    MotionEngine.trigger('incident:icmp', { event: evt });
  });
  OmniBus.on('storm', evt => {
    UIStateEngine.setSeverity(Math.max(UIStateEngine.state.severity, evt.severity));
    MotionEngine.trigger('storm:burst', { event: evt });
  });
  OmniBus.on('breach', evt => {
    UIStateEngine.setSeverity(Math.max(UIStateEngine.state.severity, evt.severity));
    MotionEngine.trigger('breach:surge', { event: evt });
  });
  OmniBus.on('escalation', evt => {
    UIStateEngine.setSeverity(Math.max(UIStateEngine.state.severity, evt.severity));
    MotionEngine.trigger('escalation:cascade', { event: evt });
  });
  OmniBus.on('collapse', evt => {
    MotionEngine.trigger('collapse:cooling', { event: evt });
    setTimeout(() => UIStateEngine.setSeverity(0.12), 900);
  });
  OmniBus.on('*', evt => {
    const where = (evt.type === 'packet' || evt.type === 'incidentEcho') ? `${evt.from}→${evt.to} ${evt.payload.capsuleId || ''}`
      : evt.type === 'zoneTransition' ? `${evt.from}→${evt.to}`
      : (evt.zone || 'all zones');
    UIStateEngine.addMessage(`t=${(evt.t / 1000).toFixed(1)}s · ${evt.type} · ${where} · sev ${(evt.severity || 0).toFixed(2)}`,
      evt.severity >= 0.75 ? 'warn' : 'info');
    $('packet-info-body').textContent = JSON.stringify(evt, null, 2);
  });

  // Graceful degradation wiring (guide: buffer locally, keep UI state).
  OmniBus.onDegradedChange = degraded => {
    UIStateEngine.setFeedStatus(degraded ? 'degraded' : 'live');
    $('feed-banner').hidden = !degraded;
    if (degraded) UIStateEngine.addMessage('Telemetry feed degraded — buffering events locally.', 'warn');
    else UIStateEngine.addMessage('Telemetry feed recovered — flushing buffered events in order.', 'info');
  };

  // Scrub: apply implied state, don't replay every animation.
  TimelineMotion.onScrubApply = (applied) => {
    const lastZone = [...applied].reverse().find(e => e.type === 'zoneTransition');
    if (lastZone && lastZone.to) {
      Viewport.activateZone(lastZone.to);
      UIStateEngine.setActiveZone(lastZone.to);
    }
    const lastIncident = [...applied].reverse().find(e => ['tamper', 'storm', 'breach', 'escalation'].includes(e.type));
    UIStateEngine.setSeverity(lastIncident ? lastIncident.severity * 0.6 : 0);
  };

  // ---------- Render loop (state -> DOM) ----------
  let lastMessageCount = -1;
  UIStateEngine.subscribe(s => {
    $('clock').textContent = `t = ${(s.playhead / 1000).toFixed(1)}s / ${(s.duration / 1000).toFixed(1)}s`;
    $('playhead').style.left = `${(s.playhead / s.duration) * 100}%`;
    const tl = $('timeline');
    if (document.activeElement !== tl) tl.value = String(Math.round(s.playhead));
    $('play-btn').textContent = s.mode === 'play' ? '⏸ Pause' : '▶ Play';
    $('severity-fill').style.width = `${Math.round(s.severity * 100)}%`;
    $('severity-value').textContent = s.severity.toFixed(2);
    const pill = $('feed-status');
    pill.textContent = `feed: ${s.feedStatus}`;
    pill.className = `pill ${s.feedStatus === 'live' ? 'pill-live' : s.feedStatus === 'degraded' ? 'pill-degraded' : 'pill-idle'}`;
    if (s.messages.length !== lastMessageCount) {
      lastMessageCount = s.messages.length;
      $('event-log').innerHTML = s.messages.slice(-60).reverse()
        .map(m => `<li class="${m.kind}">${m.text}</li>`).join('');
    }
  });

  // Severity decay: incidents spike the meter, calm periods cool it.
  setInterval(() => {
    const s = UIStateEngine.state;
    if (s.severity > 0) UIStateEngine.setSeverity(s.severity * 0.965);
  }, 240);

  // ---------- Transport controls ----------
  $('play-btn').addEventListener('click', () => {
    if (UIStateEngine.state.mode === 'play') TimelineMotion.pause();
    else TimelineMotion.play();
  });
  $('speed-select').addEventListener('change', e => {
    TimelineMotion.setSpeed(parseFloat(e.target.value));
    restartLive(); // restart the active live feed (if any) at the new speed
  });
  $('timeline').addEventListener('input', e => TimelineMotion.scrub(parseFloat(e.target.value)));
  $('theme-toggle').addEventListener('click', () => {
    UIStateEngine.toggleTheme();
    MotionEngine.trigger('timeline:scrub');
  });
  $('sidebar-toggle').addEventListener('click', () => UIStateEngine.toggleSidebar());

  // ---------- Live sources (SSE + WebSocket, Increment 2) ----------
  let currentSessionKey = DEFAULT_SESSION_KEY;
  let liveSource = null; // EventSource (SSE)
  let liveWs = null;     // WebSocket

  function startLiveSSE() {
    stopLiveSSE();
    const speed = UIStateEngine.state.speed;
    const key = encodeURIComponent(currentSessionKey);
    liveSource = new EventSource(`${BASE_PATH}/api/events/stream?speed=${speed}&key=${key}`);
    liveSource.addEventListener('omnievent', msg => OmniBus.dispatch(JSON.parse(msg.data)));
    liveSource.onopen = () => OmniBus.setDegraded(false);
    liveSource.onerror = () => OmniBus.setDegraded(true);
    UIStateEngine.setFeedStatus('live');
    UIStateEngine.addMessage('Live telemetry stream connected (SSE).', 'info');
  }
  function stopLiveSSE() {
    if (liveSource) { liveSource.close(); liveSource = null; }
  }
  function startLiveWS() {
    stopLiveWS();
    const speed = UIStateEngine.state.speed;
    const key = encodeURIComponent(currentSessionKey);
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    liveWs = new WebSocket(`${proto}//${location.host}${BASE_PATH}/api/events/ws?speed=${speed}&key=${key}`);
    liveWs.onmessage = msg => OmniBus.dispatch(JSON.parse(msg.data));
    liveWs.onopen = () => { OmniBus.setDegraded(false); UIStateEngine.setFeedStatus('live'); };
    liveWs.onerror = () => OmniBus.setDegraded(true);
    liveWs.onclose = () => {
      if (UIStateEngine.state.source === 'live-ws' && liveWs) OmniBus.setDegraded(true);
    };
    UIStateEngine.addMessage('Live telemetry stream connected (WebSocket).', 'info');
  }
  function stopLiveWS() {
    if (liveWs) { const ws = liveWs; liveWs = null; try { ws.close(); } catch (e) { /* already closed */ } }
  }
  function stopLive() { stopLiveSSE(); stopLiveWS(); }
  function startLive() {
    const src = UIStateEngine.state.source;
    if (src === 'live') startLiveSSE();
    else if (src === 'live-ws') startLiveWS();
  }
  function restartLive() {
    const src = UIStateEngine.state.source;
    if (src === 'live' || src === 'live-ws') { stopLive(); startLive(); }
  }
  $('source-select').addEventListener('change', e => {
    const src = e.target.value;
    UIStateEngine.setSource(src);
    if (src === 'live' || src === 'live-ws') { TimelineMotion.pause(); stopLive(); startLive(); }
    else { stopLive(); OmniBus.setDegraded(false); UIStateEngine.setFeedStatus('idle'); $('feed-banner').hidden = true; }
  });

  // ---------- Session picker (Increment 2) ----------
  function buildMarkers(replay) {
    const markers = $('timeline-markers');
    markers.innerHTML = '';
    for (const evt of replay.events) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = `marker marker-${evt.type}`;
      b.style.left = `${(evt.t / replay.durationMs) * 100}%`;
      b.title = `${(evt.t / 1000).toFixed(1)}s · ${evt.type}`;
      b.addEventListener('click', () => {
        TimelineMotion.jump(evt.t);
        OmniBus.dispatchImmediate(evt);
      });
      markers.appendChild(b);
    }
  }

  async function loadSession(key) {
    currentSessionKey = key;
    TimelineMotion.pause();
    const replay = await (await fetch(`${BASE_PATH}/api/replay?bucket=${BUCKET}&key=${encodeURIComponent(key)}`)).json();
    UIStateEngine.patch({ duration: replay.durationMs, playhead: 0 });
    $('timeline').max = String(replay.durationMs);
    TimelineMotion.loadReplay(replay.events);
    Viewport.resetIncidents();
    buildMarkers(replay);
    UIStateEngine.addMessage(`Replay loaded from S3 stub: ${replay.sessionId}, ${replay.events.length} events, ${(replay.durationMs / 1000).toFixed(0)}s.`, 'info');
    restartLive(); // if a live feed is up, switch it to the new session
  }

  async function listSessions() {
    const listing = await (await fetch(`${BASE_PATH}/api/buckets/${BUCKET}/objects?prefix=replay/`)).json();
    const keys = (listing.Contents || []).map(o => o.Key).filter(k => k.endsWith('.json')).sort();
    const select = $('session-select');
    select.innerHTML = '';
    for (const key of keys) {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = key.replace('replay/', '').replace('.json', '');
      select.appendChild(opt);
    }
    if (keys.includes(DEFAULT_SESSION_KEY)) select.value = DEFAULT_SESSION_KEY;
    return keys.length ? (keys.includes(DEFAULT_SESSION_KEY) ? DEFAULT_SESSION_KEY : keys[0]) : DEFAULT_SESSION_KEY;
  }
  $('session-select').addEventListener('change', e => {
    loadSession(e.target.value).catch(() => UIStateEngine.addMessage('Session failed to load.', 'warn'));
  });

  // ---------- Motion-script editor (Increment 2) ----------
  let loadedScript = null; // { version, cueDurationsMs } as loaded/saved

  function editorRows() {
    return [...$('cue-editor-rows').querySelectorAll('.cue-row')];
  }
  function collectEditorDurations() {
    const map = {};
    for (const row of editorRows()) {
      const v = parseFloat(row.querySelector('input').value);
      if (Number.isFinite(v) && v > 0) map[row.dataset.cue] = Math.round(v);
    }
    return map;
  }
  function buildEditor() {
    const rows = $('cue-editor-rows');
    rows.innerHTML = '';
    const overrides = MotionEngine.getOverrides();
    for (const cue of MotionEngine.cueNames().sort()) {
      const row = document.createElement('div');
      row.className = 'cue-row';
      row.dataset.cue = cue;
      const label = document.createElement('label');
      label.textContent = cue;
      label.htmlFor = `cue-${cue}`;
      const input = document.createElement('input');
      input.type = 'number';
      input.min = '10';
      input.step = '10';
      input.id = `cue-${cue}`;
      input.value = String(overrides[cue] != null ? overrides[cue] : MotionEngine.naturalDuration(cue));
      row.appendChild(label);
      row.appendChild(input);
      rows.appendChild(row);
    }
  }
  $('cue-apply').addEventListener('click', () => {
    const map = collectEditorDurations();
    MotionEngine.replaceDurations(map);
    $('editor-status').textContent = `Applied live: ${Object.keys(map).length} cue durations re-timed.`;
    UIStateEngine.addMessage('Motion script durations applied live from the editor.', 'info');
  });
  $('cue-reset').addEventListener('click', () => {
    if (loadedScript) MotionEngine.replaceDurations(loadedScript.cueDurationsMs);
    buildEditor();
    $('editor-status').textContent = 'Reset to the loaded script’s durations.';
  });
  $('cue-save').addEventListener('click', async () => {
    try {
      const durations = collectEditorDurations();
      const listing = await (await fetch(`${BASE_PATH}/api/buckets/${BUCKET}/objects?prefix=motion-scripts/`)).json();
      const n = (listing.Contents || []).length + 1;
      const key = `motion-scripts/fmp-motion-v${n}.json`;
      const script = { version: `fmp-motion/1.${n - 1}.0`, cueDurationsMs: durations };
      const res = await fetch(`${BASE_PATH}/api/motion-script?bucket=${BUCKET}&key=${encodeURIComponent(key)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(script),
      });
      const out = await res.json();
      if (!res.ok) throw new Error(out.error || `HTTP ${res.status}`);
      loadedScript = script;
      MotionEngine.replaceDurations(durations);
      $('script-version').textContent = `motion script: ${script.version}`;
      $('editor-status').textContent = `Saved as ${key} (${script.version}) and applied live.`;
      UIStateEngine.addMessage(`Motion script saved as new version: ${key}.`, 'info');
    } catch (err) {
      $('editor-status').textContent = `Save failed: ${err.message}`;
    }
  });

  // ---------- 3. Load motion script + sessions from "S3" (backend stub) ----------
  async function boot() {
    try {
      const script = await (await fetch(`${BASE_PATH}/api/motion-script`)).json();
      loadedScript = script;
      MotionEngine.setDurations(script.cueDurationsMs);
      $('script-version').textContent = `motion script: ${script.version}`;
    } catch (err) {
      UIStateEngine.addMessage('Motion script failed to load — using built-in cue durations.', 'warn');
    }
    buildEditor();
    try {
      const first = await listSessions();
      await loadSession(first);
    } catch (err) {
      UIStateEngine.addMessage('Replay failed to load from backend. Is the server running?', 'warn');
      UIStateEngine.setFeedStatus('degraded');
    }
  }
  boot();
})();
