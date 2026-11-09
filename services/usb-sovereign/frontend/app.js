// USB-SOVEREIGN dashboard — Increment 2 transport: WebSocket push from the
// backend (/ws, ~2 Hz) with the original 1 s HTTP polling as fallback when
// the socket is unavailable.
//
// All URLs are prefix-relative: BASE is the directory this page was served
// from, derived from window.location — "/" at root hosting, "/usb/" when
// the app is mounted under a gateway path prefix — so fetch and WebSocket
// calls stay inside the mount point either way.
const BASE = location.pathname.replace(/[^/]*$/, "");
const $ = (id) => document.getElementById(id);

function fmtBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(2) + " MB";
}
function fmtRate(r) { return r >= 1024 ? (r / 1024).toFixed(1) + " KB/s" : r.toFixed(0) + " B/s"; }
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toTimeString().slice(0, 8);
}

const SOURCE_ORDER = ["CELL", "ZILTB", "LAN", "WIFI", "BT", "LH", "FSH", "COM1", "COM2", "Sport", "Coort"];

function renderPairs(pairs) {
  $("pairs").innerHTML = pairs.map(p => `
    <div class="pair-card">
      <div class="pair-title">${p.a} <span>&harr;</span> ${p.b}</div>
      <div class="flow"><span class="dir">${p.a} &rarr; ${p.b}</span>
        <strong>${fmtBytes(p.a_to_b_bytes)} · ${fmtRate(p.a_bytes_per_s)}</strong></div>
      <div class="flow"><span class="dir">${p.b} &rarr; ${p.a}</span>
        <strong>${fmtBytes(p.b_to_a_bytes)} · ${fmtRate(p.b_bytes_per_s)}</strong></div>
    </div>`).join("");
}

function renderSources(sources, ziltbActive) {
  $("sources").innerHTML = SOURCE_ORDER.map(name => {
    const s = sources[name];
    if (!s) return "";
    const recent = s.last_activity_s_ago !== null && s.last_activity_s_ago < 3;
    const cls = name === "ZILTB" && ziltbActive ? "src-card failover-src"
              : recent ? "src-card active" : "src-card";
    const dot = name === "ZILTB" && ziltbActive ? "dot hot" : recent ? "dot on" : "dot";
    const last = s.last_activity_s_ago === null ? "never" : s.last_activity_s_ago.toFixed(1) + " s ago";
    return `
    <div class="${cls}">
      <div class="name"><span class="${dot}"></span>${name}</div>
      <div class="rate">${fmtRate(s.bytes_per_s)}</div>
      <div class="small">${fmtBytes(s.bytes_in)} in · ${s.frames} frames<br>
      last activity ${last} · ${s.baudrate} baud</div>
    </div>`;
  }).join("");
}

function renderEvents(events) {
  $("events").innerHTML = events.slice(-30).reverse().map(e =>
    `<li><span class="t">${fmtTime(e.ts)}</span><span class="lvl ${e.level}">${e.level}</span><span>${e.message}</span></li>`
  ).join("");
}

function render(s, transport) {
  $("conn").textContent = "live · " + transport;
  $("conn").className = "conn";
  $("bridge-id").textContent = s.bridge_id;
  $("session-id").textContent = s.session_id.slice(0, 13) + "…";
  $("uptime").textContent = Math.floor(s.uptime_s) + " s";

  const up = s.uplink;
  $("uplink-card").className = "uplink card" + (up.ziltb_active ? " failover" : "");
  $("active-path").textContent = up.active_path;
  $("path-note").textContent = up.ziltb_active
    ? "FAILOVER — cellular silent beyond 5.0 s, ZilTB bound"
    : "Primary cellular path";
  const pct = Math.min(100, (up.cell_silence_s / up.failover_threshold_s) * 100);
  const fill = $("silence-fill");
  fill.style.width = pct + "%";
  fill.className = up.cell_silence_s > 2.5 ? "hot" : "";
  fill.id = "silence-fill";
  $("silence-s").textContent = up.cell_silence_s.toFixed(2);
  $("failovers").textContent = up.failover_engagements;
  $("cell-rate").textContent = fmtRate(s.sources.CELL.bytes_per_s);
  $("ziltb-rate").textContent = fmtRate(s.sources.ZILTB.bytes_per_s);

  renderPairs(s.pairs);
  renderSources(s.sources, up.ziltb_active);

  $("sink-topic").textContent = s.sinks.mqtt_topic;
  $("sink-mqtt").textContent = s.sinks.mqtt_published;
  $("sink-ddb").textContent = s.sinks.dynamodb_records;
  $("sink-buf").textContent = s.sinks.s3_buffered_entries;
  $("sink-flush").textContent = s.sinks.s3_flushes;
  $("sink-last").textContent = s.sinks.s3_last_uri || "—";
  renderEvents(s.events);
}

// --- WebSocket push (primary) ---
let wsLive = false;
function connectWs() {
  let ws;
  try {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + BASE + "ws");
  } catch (err) {
    setTimeout(connectWs, 3000);
    return;
  }
  ws.onopen = () => { wsLive = true; };
  ws.onmessage = (ev) => {
    try { render(JSON.parse(ev.data), "ws push"); } catch (err) { /* keep last frame */ }
  };
  ws.onclose = () => { wsLive = false; setTimeout(connectWs, 3000); };
  ws.onerror = () => { try { ws.close(); } catch (err) {} };
}

// --- HTTP polling (fallback while the socket is down) ---
async function tick() {
  if (wsLive) return;
  try {
    const res = await fetch(BASE + "api/status", { cache: "no-store" });
    render(await res.json(), "polling");
  } catch (err) {
    $("conn").textContent = "backend unreachable";
    $("conn").className = "conn err";
  }
}

connectWs();
tick();
setInterval(tick, 1000);
