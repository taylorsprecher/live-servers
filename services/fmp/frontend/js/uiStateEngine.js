// UIStateEngine — global UI state store (FMP-1.0 guide, subsystem #1).
// Booted first: establishes state, DOM hooks, theme persistence, and a
// subscribe/emit channel. State changes flow through here; motion flows
// through MotionEngine. The two never call each other directly — the Omni
// Event Bus and app wiring connect them.

const UIStateEngine = (() => {
  const listeners = new Set();

  const state = {
    theme: 'dark',
    sidebar: 'open',
    mode: 'pause',          // timeline mode: play | pause | scrub | jump
    source: 'replay',       // replay | live
    playhead: 0,            // ms
    duration: 90000,        // ms
    speed: 1.0,
    activeZone: 'RAN',
    severity: 0,            // 0..1, decays in app loop
    feedStatus: 'idle',     // idle | live | degraded
    messages: [],           // event log entries
  };

  function emit() {
    const snapshot = { ...state, messages: [...state.messages] };
    listeners.forEach(cb => cb(snapshot));
  }

  function subscribe(cb) {
    listeners.add(cb);
    cb({ ...state, messages: [...state.messages] });
    return () => listeners.delete(cb);
  }

  function patch(partial) {
    Object.assign(state, partial);
    emit();
  }

  // ---------- Theme (persisted, per guide) ----------
  function setTheme(theme) {
    state.theme = theme;
    document.documentElement.classList.remove('light', 'dark');
    document.documentElement.classList.add(theme);
    try { localStorage.setItem('fmp-theme', theme); } catch (e) { /* private mode */ }
    emit();
  }
  function toggleTheme() { setTheme(state.theme === 'dark' ? 'light' : 'dark'); }
  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem('fmp-theme'); } catch (e) { /* ignore */ }
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    setTheme(saved || (prefersDark ? 'dark' : 'light'));
  }

  // ---------- Sidebar ----------
  function toggleSidebar() {
    state.sidebar = state.sidebar === 'open' ? 'closed' : 'open';
    document.body.classList.toggle('sidebar-closed', state.sidebar === 'closed');
    emit();
  }

  // ---------- Messages / event log ----------
  function addMessage(text, kind = 'info') {
    state.messages.push({ at: Date.now(), text, kind });
    if (state.messages.length > 200) state.messages.splice(0, state.messages.length - 200);
    emit();
  }

  function init() {
    initTheme();
    document.body.classList.toggle('sidebar-closed', state.sidebar === 'closed');
    emit();
  }

  return {
    init, subscribe, patch,
    get state() { return state; },
    setTheme, toggleTheme, toggleSidebar, addMessage,
    setMode: m => patch({ mode: m }),
    setPlayhead: t => patch({ playhead: t }),
    setSpeed: s => patch({ speed: s }),
    setSource: s => patch({ source: s }),
    setActiveZone: z => patch({ activeZone: z }),
    setSeverity: v => patch({ severity: Math.max(0, Math.min(1, v)) }),
    setFeedStatus: f => patch({ feedStatus: f }),
  };
})();
