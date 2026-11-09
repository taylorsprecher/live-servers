// Wyze Bridge dashboard — talks to the status service.
// API calls are PREFIX-RELATIVE: the base is derived from this page's own
// URL, so the same files work at the site root (compose/nginx or the
// status service itself) and under a hosting prefix such as /wyze (the
// public demo hosting, where the status service also serves this page).
// Set window.WYZE_API_BASE before this script to override explicitly.
const API_BASE = window.WYZE_API_BASE !== undefined
  ? window.WYZE_API_BASE
  : window.location.pathname.replace(/\/?(?:index\.html)?$/, "");

async function getJSON(path) {
  const res = await fetch(API_BASE + path);
  if (!res.ok) throw new Error(path + " -> " + res.status);
  return res.json();
}

function badge(text, kind) {
  return `<span class="badge ${kind}">${text}</span>`;
}

async function renderBridge() {
  const detail = document.getElementById("bridge-detail");
  const list = document.getElementById("bridge-endpoints");
  const badgeEl = document.getElementById("bridge-badge");
  try {
    const s = await getJSON("/api/bridge/status");
    detail.textContent = s.detail;
    badgeEl.innerHTML = s.deployed
      ? badge("Bridge deployed", "ok")
      : badge("Bridge not deployed yet — mocked state", "warn");
    list.innerHTML = Object.entries(s.endpoints)
      .map(([k, v]) => `<li><strong>${k}:</strong> <code>${v}</code></li>`)
      .join("");
  } catch (e) {
    detail.textContent = "Status service unreachable: " + e.message;
    badgeEl.innerHTML = badge("Status service down", "bad");
  }
}

async function renderArchive() {
  const badgeEl = document.getElementById("archive-badge");
  const detail = document.getElementById("archive-detail");
  const facts = document.getElementById("archive-facts");
  try {
    const a = await getJSON("/api/archive/status");
    if (a.state === "enabled") {
      badgeEl.outerHTML = badge("Archive tier enabled", "ok");
    } else if (a.state === "configured_not_enabled") {
      badgeEl.outerHTML = badge("Configured — not currently running", "warn");
    } else {
      badgeEl.outerHTML = badge("Not configured", "warn");
    }
    detail.textContent = a.detail;
    facts.innerHTML = [
      ["Services (compose profile '" + a.profile + "')", a.services.join(", ")],
      ["Sync interval", a.sync_interval_seconds + "s"],
      ["Encryption", a.encryption],
      ["Lifecycle", a.lifecycle],
    ].map(([k, v]) => `<li><strong>${k}:</strong> ${v}</li>`).join("");
  } catch (e) {
    badgeEl.outerHTML = badge("Archive status unknown", "bad");
    detail.textContent = "Status service unreachable: " + e.message;
    facts.innerHTML = "";
  }
}

function cameraCard(cam) {
  const live = cam.live_view === "ok"
    ? badge("Live view OK", "ok")
    : badge("Live view failing", "bad");
  const online = cam.online ? badge("Online", "ok") : badge("Offline", "bad");
  const battery = cam.battery_percent == null
    ? "Battery: not reported"
    : `Battery: ${cam.battery_percent}%`;
  const watch = cam.live_view === "ok"
    ? `<a href="${cam.streams.webrtc_hls}" target="_blank" rel="noopener">Open bridge stream</a>`
    : `<a href="${cam.streams.webrtc_hls}" target="_blank" rel="noopener">Try bridge stream (fallback path)</a>`;
  return `
  <article class="card">
    <h3>${cam.name}</h3>
    <p>${online} ${live}</p>
    <p class="detail">${cam.live_view_detail}</p>
    <p class="meta">${cam.model} · ${battery} ·
       Motion uploads: ${cam.motion_uploads ? "working" : "not working"}</p>
    ${cam.last_event ? `<p class="meta">${cam.last_event}</p>` : ""}
    <p>${watch}</p>
    <p class="meta">RTSP: <code>${cam.streams.rtsp}</code></p>
  </article>`;
}

async function renderCameras() {
  const grid = document.getElementById("cameras");
  try {
    const data = await getJSON("/api/cameras");
    grid.innerHTML = data.cameras.map(cameraCard).join("");
    document.getElementById("data-note").textContent =
      data.source === "mock"
        ? "Camera state shown is mocked demo data (seeded with known facts) until the bridge is deployed on the home LAN."
        : "Camera state is live from the bridge.";
  } catch (e) {
    grid.innerHTML = `<p class="card">Could not load cameras: ${e.message}</p>`;
  }
}

renderBridge();
renderArchive();
renderCameras();
setInterval(() => { renderBridge(); renderArchive(); renderCameras(); }, 30000);
