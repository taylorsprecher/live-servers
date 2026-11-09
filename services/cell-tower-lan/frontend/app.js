'use strict';
/* Cell Tower LAN dashboard — polls the telemetry service every 2 s.
 * Increment 2: site topology view, VLAN matrix, alarm acknowledge
 * workflow, and a telemetry-pipeline panel (queue stats + per-site keys).
 */

const state = {
  sites: [],
  selectedSite: null,
  selectedRadio: null,
  ackFormFor: null, // alarm id with the ack form open (pauses alarm re-render)
};

const $ = (id) => document.getElementById(id);

// The dashboard may be hosted under a path prefix (e.g. /celltower/)
// behind a gateway. Derive the mount base from the page URL — the path
// of the directory the page itself was served from — so every API call
// stays prefix-relative and works identically at root or under a prefix.
const BASE = new URL('.', window.location.href).pathname; // always ends in '/'
function api(path) {
  return BASE + String(path).replace(/^\//, '');
}

async function getJson(path) {
  const res = await fetch(api(path));
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

function renderSites() {
  const list = $('site-list');
  list.innerHTML = '';
  for (const site of state.sites) {
    const div = document.createElement('div');
    div.className = 'site-item' + (state.selectedSite === site.site_id ? ' active' : '');
    const radioCount = site.sectors.reduce((n, s) => n + s.radios.length, 0);
    div.innerHTML = `<div class="nm">${site.name}</div>
      <div class="mt">${site.code} · ${site.type} · backhaul: ${site.backhaul} · ${radioCount} radios</div>`;
    div.onclick = () => selectSite(site.site_id);
    list.appendChild(div);
  }
  const site = state.sites.find((s) => s.site_id === state.selectedSite);
  if (site) {
    const vlans = site.vlans.map((v) => `VLAN ${v.id} — ${v.name}`).join('<br>');
    $('site-detail').innerHTML =
      `<b>${site.name} (${site.code})</b><br>Switch: ${site.switch_model}<br>${vlans}`;
  }
}

function siteRadios(siteId) {
  const site = state.sites.find((s) => s.site_id === siteId);
  if (!site) return [];
  const out = [];
  for (const sector of site.sectors) {
    for (const radio of sector.radios) {
      out.push({ ...radio, sector_name: sector.name, sector_id: sector.sector_id });
    }
  }
  return out;
}

function renderRadios() {
  const bar = $('radios-bar');
  bar.innerHTML = '';
  for (const radio of siteRadios(state.selectedSite)) {
    const chip = document.createElement('button');
    chip.className = 'radio-chip' + (state.selectedRadio === radio.radio_id ? ' active' : '');
    chip.textContent = `${radio.radio_id} · ${radio.tech}`;
    chip.onclick = () => { state.selectedRadio = radio.radio_id; renderRadios(); refresh(); };
    bar.appendChild(chip);
  }
}

function selectSite(siteId) {
  state.selectedSite = siteId;
  const radios = siteRadios(siteId);
  state.selectedRadio = radios.length ? radios[0].radio_id : null;
  renderSites();
  renderRadios();
  refresh();
}

function fmt(v, digits = 1) {
  return typeof v === 'number' ? v.toFixed(digits) : '—';
}

function drawChart(canvas, series) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height, PAD = 28;
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = '#1e2b47';
  ctx.fillStyle = '#8ea0bd';
  ctx.font = '10px sans-serif';
  for (let i = 0; i <= 4; i++) {
    const y = PAD + ((H - 2 * PAD) * i) / 4;
    ctx.beginPath(); ctx.moveTo(PAD, y); ctx.lineTo(W - 6, y); ctx.stroke();
  }
  for (const s of series) {
    if (!s.values.length) continue;
    // Each series is scaled to its own min/max (noted in the legend) so
    // dBm and dB can share one chart, as in the prototype's combined view.
    let min = Math.min(...s.values), max = Math.max(...s.values);
    if (max - min < 1) { max += 0.5; min -= 0.5; }
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    s.values.forEach((v, i) => {
      const x = PAD + ((W - PAD - 6) * i) / Math.max(s.values.length - 1, 1);
      const y = PAD + (H - 2 * PAD) * (1 - (v - min) / (max - min));
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.lineWidth = 1;
    ctx.fillStyle = s.color;
    ctx.fillText(`${s.label} ${min.toFixed(0)}..${max.toFixed(0)}`, PAD + 4, 12 + series.indexOf(s) * 11);
    ctx.fillStyle = '#8ea0bd';
  }
}

// ------------------------------------------------------------- topology
function radioStatus(radioId, latest, alarms) {
  const active = alarms.filter((a) => a.radio_id === radioId && a.status === 'ACTIVE');
  if (active.some((a) => a.severity === 'CRITICAL')) return 'crit';
  if (active.length) return 'warn';
  if (latest[radioId]) return 'ok';
  return 'nodata';
}

function renderTopology(latest, alarms) {
  const site = state.sites.find((s) => s.site_id === state.selectedSite);
  const el = $('topology');
  if (!site) { el.innerHTML = ''; return; }
  const sectors = site.sectors.map((sector) => {
    const radios = sector.radios.map((radio) => {
      const st = radioStatus(radio.radio_id, latest, alarms);
      const label = { ok: 'live', warn: 'alarm', crit: 'CRITICAL', nodata: 'no data' }[st];
      return `<div class="tnode radio ${st}">
          <span class="dot"></span><span class="tid">${radio.radio_id}</span>
          <span class="tmeta">${radio.tech} · ${radio.band} · ${label}</span>
        </div>`;
    }).join('');
    return `<div class="tnode sector">
        <span class="tid">Sector ${sector.name}</span>
        <span class="tmeta">azimuth ${sector.azimuth_deg}°</span>
        <div class="tchildren">${radios}</div>
      </div>`;
  }).join('');
  el.innerHTML = `
    <div class="tnode root">
      <span class="tid">${site.name} (${site.code})</span>
      <span class="tmeta">${site.type} site</span>
    </div>
    <div class="tchildren">
      <div class="tnode infra"><span class="tid">Backhaul</span><span class="tmeta">${site.backhaul} · IPsec (design)</span></div>
      <div class="tnode infra"><span class="tid">Site switch</span><span class="tmeta">${site.switch_model} · VLANs 10/20/30 trunk</span></div>
      <div class="tnode infra"><span class="tid">Packet core</span><span class="tmeta">Open5GS-style (design — not built)</span></div>
      ${sectors}
    </div>`;
}

// ---------------------------------------------------------- VLAN matrix
const VLAN_FUNCTIONS = [
  { col: 'Telemetry', vlans: [10] },
  { col: 'Mgmt / O&M', vlans: [10] },
  { col: 'User data (GTP)', vlans: [20] },
  { col: 'Timing (PTP)', vlans: [30] },
];

function renderVlanMatrix() {
  const site = state.sites.find((s) => s.site_id === state.selectedSite);
  const el = $('vlan-matrix');
  if (!site) { el.innerHTML = ''; return; }
  const head = VLAN_FUNCTIONS.map((f) => `<th>${f.col}</th>`).join('');
  const rows = site.vlans.map((v) => {
    const cells = VLAN_FUNCTIONS.map((f) =>
      `<td class="${f.vlans.includes(v.id) ? 'on' : 'off'}">${f.vlans.includes(v.id) ? '●' : '—'}</td>`).join('');
    return `<tr><td class="vid">${v.id}</td><td>${v.name}</td>${cells}<td class="purpose">${v.purpose}</td></tr>`;
  }).join('');
  el.innerHTML = `<table class="matrix">
    <thead><tr><th>VLAN</th><th>Name</th>${head}<th>Purpose</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

// --------------------------------------------------------------- alarms
function renderAlarms(alarms) {
  const activeCount = alarms.filter((a) => a.status === 'ACTIVE').length;
  $('alarm-count').textContent = activeCount;
  $('alarm-count').classList.toggle('zero', activeCount === 0);
  if (state.ackFormFor) return; // don't clobber an open ack form mid-poll
  $('alarm-list').innerHTML = alarms.length
    ? alarms.map((a) => {
        const acked = a.status === 'ACKNOWLEDGED';
        const ackLine = acked
          ? `<br><span class="ackinfo">✓ ACK by ${a.ack.by} · ${new Date(a.ack.at).toLocaleTimeString()}${a.ack.note ? ` · ${a.ack.note}` : ''}</span>`
          : '';
        const action = acked
          ? ''
          : `<div class="ackrow">
               <button class="ack-btn" data-ack="${a.id}">Acknowledge</button>
               <span class="ackform hidden" id="ackform-${a.id}">
                 <input class="ack-by" placeholder="your name" value="operator">
                 <input class="ack-note" placeholder="note (optional)">
                 <button class="ack-confirm" data-confirm="${a.id}">Confirm</button>
                 <button class="ack-cancel" data-cancel="${a.id}">Cancel</button>
               </span>
             </div>`;
        return `<div class="alarm ${a.severity}${acked ? ' acked' : ''}">
             <span class="sev">${a.severity}</span> · ${a.code}${acked ? ' · <span class="ackbadge">ACK</span>' : ''}<br>${a.message}
             <br><span class="ts">${new Date(a.ts).toLocaleTimeString()} · ${a.id}</span>${ackLine}${action}
           </div>`;
      }).join('')
    : '<p class="none">No alarms.</p>';
}

async function postAck(alarmId, formEl) {
  const by = formEl.querySelector('.ack-by').value.trim() || 'operator';
  const note = formEl.querySelector('.ack-note').value.trim() || null;
  const res = await fetch(api(`/api/alarms/${alarmId}/ack`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ by, note }),
  });
  if (!res.ok) throw new Error(`ack -> HTTP ${res.status}`);
  state.ackFormFor = null;
  refresh();
}

document.addEventListener('click', (ev) => {
  const openBtn = ev.target.closest('[data-ack]');
  if (openBtn) {
    state.ackFormFor = openBtn.dataset.ack;
    const form = $(`ackform-${state.ackFormFor}`);
    if (form) form.classList.remove('hidden');
    openBtn.classList.add('hidden');
    return;
  }
  const cancelBtn = ev.target.closest('[data-cancel]');
  if (cancelBtn) {
    state.ackFormFor = null;
    refresh();
    return;
  }
  const confirmBtn = ev.target.closest('[data-confirm]');
  if (confirmBtn) {
    postAck(confirmBtn.dataset.confirm, confirmBtn.closest('.ackform'))
      .catch((err) => console.error(err));
  }
});

// ------------------------------------------------------------- pipeline
function renderPipeline(queueStats, keysList) {
  const el = $('pipeline');
  if (!queueStats) { el.innerHTML = '<p class="none">Pipeline status unavailable.</p>'; return; }
  const chip = (label, n, cls = '') =>
    `<span class="pchip ${cls}">${label} <b>${n}</b></span>`;
  const keyLines = (keysList || [])
    .filter((s) => s.site_id)
    .map((s) => {
      const active = s.keys.find((k) => k.status === 'active');
      return `<div class="pkey">${s.site_id}: ${active
        ? `active key <code>${active.key_id}</code> · fp ${active.fingerprint}`
        : '<span class="warn">no active key</span>'}</div>`;
    }).join('');
  el.innerHTML = `
    <div class="pchips">
      ${chip('completed', queueStats.completed, 'good')}
      ${chip('waiting', queueStats.waiting)}
      ${chip('delayed (retry)', queueStats.delayed)}
      ${chip('active', queueStats.active)}
      ${chip('dead-letter', queueStats.dead, queueStats.dead ? 'bad' : '')}
      ${chip('total', queueStats.total)}
    </div>${keyLines}`;
}

// -------------------------------------------------------------- refresh
async function refresh() {
  if (!state.selectedSite) return;
  try {
    const [latestRes, alarmsRes, queueRes, keysRes] = await Promise.all([
      getJson(`/api/telemetry/latest?site_id=${state.selectedSite}`),
      getJson(`/api/alarms?site_id=${state.selectedSite}`),
      getJson('/api/queue/stats').catch(() => null),
      getJson('/api/keys').catch(() => null),
    ]);
    const latest = latestRes.latest;
    const alarms = alarmsRes.alarms;
    const hasData = Object.keys(latest).length > 0;
    $('empty-state').classList.toggle('hidden', hasData);

    renderAlarms(alarms);
    renderTopology(latest, alarms);
    renderVlanMatrix();
    renderPipeline(queueRes && queueRes.stats, keysRes && keysRes.keys);

    const reading = state.selectedRadio ? latest[state.selectedRadio] : null;
    if (reading) {
      const m = reading.metrics;
      $('m-rsrp').textContent = fmt(m.rsrp_dbm);
      $('m-sinr').textContent = fmt(m.sinr_db);
      $('m-dl').textContent = fmt(m.dl_throughput_mbps);
      $('m-ul').textContent = fmt(m.ul_throughput_mbps);
      $('m-ues').textContent = m.connected_ues;
      $('m-temp').textContent = fmt(m.temp_c);
    } else {
      for (const id of ['m-rsrp', 'm-sinr', 'm-dl', 'm-ul', 'm-ues', 'm-temp']) $(id).textContent = '—';
    }

    if (state.selectedRadio) {
      const hist = await getJson(`/api/telemetry/history?radio_id=${state.selectedRadio}&limit=120`);
      const readings = hist.readings;
      drawChart($('chart-signal'), [
        { label: 'RSRP', color: '#22d3ee', values: readings.map((r) => r.metrics.rsrp_dbm) },
        { label: 'SINR', color: '#fbbf24', values: readings.map((r) => r.metrics.sinr_db) },
      ]);
      drawChart($('chart-tput'), [
        { label: 'DL', color: '#34d399', values: readings.map((r) => r.metrics.dl_throughput_mbps) },
        { label: 'UL', color: '#a78bfa', values: readings.map((r) => r.metrics.ul_throughput_mbps) },
      ]);
    }
  } catch (err) {
    console.error(err);
  }
}

async function init() {
  try {
    const health = await getJson('/api/health');
    const el = $('backend-status');
    el.textContent = `backend: ok · ${health.radios} radios · storage: ${health.storage} · DLQ: ${health.queue.dead}`;
    el.className = 'status ok';
  } catch {
    $('backend-status').textContent = 'backend: unreachable';
  }
  const { sites } = await getJson('/api/sites');
  state.sites = sites;
  if (sites.length) selectSite(sites[0].site_id);
  setInterval(refresh, 2000);
}

init();
