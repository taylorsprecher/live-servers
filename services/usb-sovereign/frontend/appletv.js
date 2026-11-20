// Apple TV front end — live view of the USB-SOVEREIGN HDMI bridge pair
// (COM_HDMI1 <-> COM_HDMI2, the Apple TV link). Same transport as the
// main dashboard: WebSocket push with polling fallback, all URLs
// prefix-relative (BASE is this page's directory: "/" at root hosting,
// "/usb/" behind the gateway).
const BASE = location.pathname.replace(/[^/]*$/, "");
const $ = id => document.getElementById(id);

function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(2) + " MB";
}
function fmtRate(r) { return (r == null ? "—" : fmtBytes(Math.round(r)) + "/s"); }
function fmtUptime(s) {
  if (s == null) return "—";
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return (h ? h + "h " : "") + m + "m " + ss + "s";
}
function fmtAgo(s) {
  if (s == null) return "—";
  if (s < 1.5) return "just now";
  if (s < 90) return Math.round(s) + " s ago";
  return Math.round(s / 60) + " min ago";
}

const MAXPTS = 60;
const seriesA = [], seriesB = [];

function drawChart() {
  const cv = $("chart");
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = 120;
  if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const max = Math.max(10, ...seriesA, ...seriesB) * 1.15;
  function line(series, color) {
    if (series.length < 2) return;
    ctx.beginPath();
    series.forEach((v, i) => {
      const x = (i / (MAXPTS - 1)) * w;
      const y = h - 6 - (v / max) * (h - 16);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
  }
  ctx.strokeStyle = "rgba(255,255,255,0.10)"; ctx.lineWidth = 1;
  [0.25, 0.5, 0.75].forEach(f => {
    ctx.beginPath(); ctx.moveTo(0, h * f); ctx.lineTo(w, h * f); ctx.stroke();
  });
  line(seriesA, "#4DA3FF");
  line(seriesB, "#3fce7c");
}

function render(d) {
  const pair = (d.pairs || []).find(p => p.a === "COM_HDMI1");
  const h1 = (d.sources || {}).COM_HDMI1 || {};
  const h2 = (d.sources || {}).COM_HDMI2 || {};
  $("session-id").textContent = (d.session_id || "—").slice(0, 8);
  $("uptime").textContent = fmtUptime(d.uptime_s);
  if (pair) {
    const active = (pair.a_bytes_per_s || 0) + (pair.b_bytes_per_s || 0) > 0;
    const badge = $("link-state");
    badge.textContent = active ? "Link active" : "Link idle";
    badge.classList.toggle("idle", !active);
    $("a-to-b").textContent = fmtBytes(pair.a_to_b_bytes);
    $("b-to-a").textContent = fmtBytes(pair.b_to_a_bytes);
    $("a-rate").textContent = fmtRate(pair.a_bytes_per_s);
    $("b-rate").textContent = fmtRate(pair.b_bytes_per_s);
    seriesA.push(pair.a_bytes_per_s || 0);
    seriesB.push(pair.b_bytes_per_s || 0);
    if (seriesA.length > MAXPTS) seriesA.shift();
    if (seriesB.length > MAXPTS) seriesB.shift();
    drawChart();
  }
  $("h1-frames").textContent = h1.frames ?? "—";
  $("h1-bytes").textContent = fmtBytes(h1.bytes_in);
  $("h1-rate").textContent = fmtRate(h1.bytes_per_s);
  $("h1-last").textContent = fmtAgo(h1.last_activity_s_ago);
  $("h2-frames").textContent = h2.frames ?? "—";
  $("h2-bytes").textContent = fmtBytes(h2.bytes_in);
  $("h2-rate").textContent = fmtRate(h2.bytes_per_s);
  $("h2-last").textContent = fmtAgo(h2.last_activity_s_ago);
  const up = d.uplink || {};
  $("ctx-uplink").textContent = up.active_path || "—";
  $("ctx-failovers").textContent = up.failover_engagements ?? "—";
  $("ctx-published").textContent = (d.sinks || {}).mqtt_published ?? "—";
  $("ctx-mqtt").textContent = (d.sinks || {}).mqtt_connected ? "connected" : "stub / offline";
}

function setConn(ok, label) {
  const el = $("conn");
  el.textContent = label;
  el.className = "conn " + (ok ? "ok" : "bad");
}

let ws = null;
function connectWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(proto + "//" + location.host + BASE + "ws");
  ws.onmessage = ev => { setConn(true, "live (WebSocket)"); render(JSON.parse(ev.data)); };
  ws.onclose = () => { setConn(false, "polling (WebSocket closed)"); setTimeout(connectWs, 5000); poll(); };
  ws.onerror = () => { try { ws.close(); } catch (e) { /* already closed */ } };
}
async function poll() {
  try {
    const res = await fetch(BASE + "api/status", { cache: "no-store" });
    if (res.ok) { setConn(true, "live (polling)"); render(await res.json()); }
  } catch (e) { setConn(false, "disconnected"); }
}
connectWs();
setInterval(() => { if (!ws || ws.readyState !== 1) poll(); }, 2000);
