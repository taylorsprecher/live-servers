// Viewport — the forensic scene: five network tiers (RAN → DMZ → APP →
// DATA → SEC) rendered in SVG, plus a canvas particle layer for storm
// density fields and rematerialization sparks.
//
// Implements the guide's motion grammar:
//   * Packet rematerialization — capsule materializes (ring + sparks),
//     route re-illuminates, camera micro-sweeps, capsule travels the route.
//   * Zone transitions — zone:deactivate outgoing, zone:activate incoming.
//   * Storm & breach — volumetric density field, shockwave rings,
//     severity-modulated camera turbulence.
//   * Escalation — forward cascade across tiers.
//   * Collapse — CollapseEngine reverse-order cooling cascade + arc
//     dissolution (routes fade) to restore stability.

const Viewport = (() => {
  const ZONES = ['RAN', 'DMZ', 'APP', 'DATA', 'SEC'];
  const X = { RAN: 110, DMZ: 300, APP: 490, DATA: 675, SEC: 845 };
  const Y = 210;
  const SVGNS = 'http://www.w3.org/2000/svg';

  let svg, camera, routesG, ringsG, zonesG, capsulesG, canvas, ctx;
  let particles = [];
  let particleLoopRunning = false;

  function el(name, attrs = {}) {
    const node = document.createElementNS(SVGNS, name);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  }

  function routeId(a, b) {
    return ZONES.indexOf(a) < ZONES.indexOf(b) ? `route-${a}-${b}` : `route-${b}-${a}`;
  }

  function build() {
    svg = document.getElementById('scene');
    camera = document.getElementById('camera');
    routesG = document.getElementById('routes');
    ringsG = document.getElementById('fx-rings');
    zonesG = document.getElementById('zones');
    capsulesG = document.getElementById('capsules');
    canvas = document.getElementById('particles');
    ctx = canvas.getContext('2d');

    for (let i = 0; i < ZONES.length - 1; i++) {
      const a = ZONES[i], b = ZONES[i + 1];
      const path = el('path', {
        id: routeId(a, b), class: 'route',
        d: `M ${X[a]} ${Y} L ${X[b]} ${Y}`,
      });
      routesG.appendChild(path);
    }

    for (const z of ZONES) {
      const g = el('g', { id: `zone-${z}`, class: 'zone' });
      g.appendChild(el('circle', { id: `halo-${z}`, class: 'zone-halo', cx: X[z], cy: Y, r: 52 }));
      g.appendChild(el('circle', { class: 'zone-body', cx: X[z], cy: Y, r: 34 }));
      const label = el('text', { class: 'zone-label', x: X[z], y: Y + 5, 'text-anchor': 'middle' });
      label.textContent = z;
      g.appendChild(label);
      const sub = el('text', { class: 'zone-sub', x: X[z], y: Y + 78, 'text-anchor': 'middle' });
      sub.textContent = { RAN: 'radio access', DMZ: 'perimeter', APP: 'application', DATA: 'data tier', SEC: 'security' }[z];
      g.appendChild(sub);
      zonesG.appendChild(g);
    }
    resizeCanvas();
    window.addEventListener('resize', resizeCanvas);
  }

  // ---------- coordinate mapping (viewBox -> canvas px) ----------
  function resizeCanvas() {
    const r = svg.getBoundingClientRect();
    canvas.width = Math.max(1, Math.round(r.width));
    canvas.height = Math.max(1, Math.round(r.height));
  }
  function toPx(x, y) {
    const r = svg.getBoundingClientRect();
    return { x: (x / 960) * r.width, y: (y / 420) * r.height };
  }

  // ---------- particles (storm density field + sparks) ----------
  function spawnParticle(p) { particles.push(p); runParticleLoop(); }
  function runParticleLoop() {
    if (particleLoopRunning) return;
    particleLoopRunning = true;
    const step = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const now = performance.now();
      particles = particles.filter(p => now - p.born < p.life);
      for (const p of particles) {
        const age = (now - p.born) / p.life;
        p.x += p.vx; p.y += p.vy;
        p.vx += p.ax || 0; p.vy += p.ay || 0;
        ctx.globalAlpha = Math.max(0, 1 - age) * (p.alpha || 0.9);
        ctx.fillStyle = p.color;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r * (p.grow ? 1 + age * 2 : 1), 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
      if (particles.length) requestAnimationFrame(step);
      else { particleLoopRunning = false; ctx.clearRect(0, 0, canvas.width, canvas.height); }
    };
    requestAnimationFrame(step);
  }

  function sparks(x, y, n, color) {
    const c = toPx(x, y);
    for (let i = 0; i < n; i++) {
      const a = Math.random() * Math.PI * 2;
      const sp = 0.4 + Math.random() * 1.6;
      spawnParticle({
        x: c.x, y: c.y, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp,
        r: 1 + Math.random() * 2, color, born: performance.now(),
        life: 380 + Math.random() * 320, alpha: 0.95,
      });
    }
  }

  function sparksAtZone(zone, color) { sparks(X[zone], Y, 14, color); }

  function densityField(zone, severity, durationMs = 1400) {
    const c = toPx(X[zone], Y);
    const count = Math.round(50 + severity * 160);
    const started = performance.now();
    const emitter = setInterval(() => {
      if (performance.now() - started > durationMs) { clearInterval(emitter); return; }
      for (let i = 0; i < 6; i++) {
        const a = Math.random() * Math.PI * 2;
        const rad = 12 + Math.random() * (30 + severity * 46);
        spawnParticle({
          x: c.x + Math.cos(a) * rad, y: c.y + Math.sin(a) * rad * 0.72,
          vx: (Math.random() - 0.5) * 0.9, vy: -0.3 - Math.random() * 0.9,
          r: 1.4 + Math.random() * 3.2, color: severity > 0.8 ? '#ff5d5d' : '#ffb84d',
          born: performance.now(), life: 700 + Math.random() * 800, alpha: 0.55, grow: true,
        });
      }
    }, 50);
    return count;
  }

  // ---------- rings / shockwaves ----------
  function shockwave(zone, cls = 'ring-pulse') {
    const ring = el('circle', { class: `ring ${cls}`, cx: X[zone], cy: Y, r: 36 });
    ringsG.appendChild(ring);
    setTimeout(() => ring.remove(), 1250);
  }

  // ---------- zones ----------
  function zoneEl(z) { return document.getElementById(`zone-${z}`); }
  function haloEl(z) { return document.getElementById(`halo-${z}`); }
  function activateZone(z) {
    ZONES.forEach(other => zoneEl(other).classList.toggle('dim', other !== z));
    zoneEl(z).classList.add('active');
    zoneEl(z).classList.remove('dim');
  }
  function deactivateZone(z) { zoneEl(z).classList.remove('active'); }

  // ---------- camera ----------
  function microSweep(targetZone) {
    const dx = (X[targetZone] - 480) * 0.03;
    camera.style.transition = 'transform 0.45s ease';
    camera.style.transform = `translate(${dx}px, 0px)`;
    setTimeout(() => { camera.style.transform = 'translate(0px, 0px)'; }, 480);
  }
  function turbulence(intensity) {
    const wrap = document.getElementById('viewport');
    wrap.style.setProperty('--shake', `${(intensity * 7).toFixed(1)}px`);
    wrap.classList.add('turbulent');
    return new Promise(r => setTimeout(() => { wrap.classList.remove('turbulent'); r(); }, 620));
  }

  // ---------- packets ----------
  // A capsule that travels the route between two zones. Shared by packet
  // rematerialization and the incident-echo ghosts (Increment 2).
  function travelCapsule(from, to, label, cls, opacity, done) {
    const path = document.getElementById(routeId(from, to));
    const g = el('g', { class: cls });
    if (opacity != null) g.setAttribute('opacity', opacity);
    const rect = el('rect', { class: 'capsule-body', x: -21, y: -11, width: 42, height: 22, rx: 11 });
    const text = el('text', { class: 'capsule-label', y: 4, 'text-anchor': 'middle' });
    text.textContent = label;
    g.appendChild(rect); g.appendChild(text);
    capsulesG.appendChild(g);

    const len = path.getTotalLength();
    const forward = ZONES.indexOf(from) < ZONES.indexOf(to);
    const travelMs = 880;
    const start = performance.now();
    const ease = t => t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
    (function frame(now) {
      const raw = Math.min(1, (now - start) / travelMs);
      const p = ease(raw);
      // First 18% of the run is the materialization scale-in at the source.
      const scale = raw < 0.18 ? raw / 0.18 : 1;
      const dist = forward ? p * len : (1 - p) * len;
      const pt = path.getPointAtLength(dist);
      g.setAttribute('transform', `translate(${pt.x}, ${pt.y}) scale(${scale.toFixed(3)})`);
      if (raw < 1) requestAnimationFrame(frame);
      else {
        setTimeout(() => g.remove(), 260);
        done();
      }
    })(performance.now());
  }

  function capsuleLabel(evt, fallback) {
    const id = evt.payload && evt.payload.capsuleId;
    return id ? id.replace('cap-', '#') : fallback;
  }

  function rematerialize(evt) {
    return new Promise(resolve => {
      const from = evt.from, to = evt.to;
      if (!from || !to || !X[from] || !X[to]) { resolve(); return; }
      const path = document.getElementById(routeId(from, to));
      path.classList.add('illuminated');
      setTimeout(() => path.classList.remove('illuminated'), 1100);

      // Materialization: expanding ring + spark burst at the source zone.
      shockwave(from, 'ring-materialize');
      sparks(X[from], Y, 16, '#7dd3fc');
      microSweep(to);

      travelCapsule(from, to, capsuleLabel(evt, 'pkt'), 'capsule', null, () => {
        sparks(X[to], Y, 10, '#a5f3a1');
        resolve();
      });
    });
  }

  // Packet incident echo (Increment 2): the packet's signature bounces
  // back along its route as fading ghost capsules — the forensic "echo"
  // of an incident packet returning to its origin zone.
  function incidentEcho(evt) {
    return new Promise(resolve => {
      const from = evt.to, to = evt.from; // echo travels back to origin
      if (!from || !to || !X[from] || !X[to]) { resolve(); return; }
      shockwave(from, 'ring-materialize');
      sparks(X[from], Y, 10, '#7dd3fc');
      let pending = 3;
      for (let i = 0; i < 3; i++) {
        setTimeout(() => {
          travelCapsule(from, to, 'echo', 'capsule echo', String(0.85 - i * 0.27), () => {
            if (--pending === 0) {
              sparks(X[to], Y, 8, '#7dd3fc');
              resolve();
            }
          });
        }, i * 170);
      }
    });
  }

  // ---------- incident grammar ----------
  // ICMP incident (Increment 2): probe rings emanate from the swept zone
  // in quick succession — the visual signature of a ping sweep.
  function icmpSweep(zone) {
    shockwave(zone, 'ring-icmp');
    setTimeout(() => shockwave(zone, 'ring-icmp'), 190);
    setTimeout(() => shockwave(zone, 'ring-icmp'), 380);
    sparks(X[zone], Y, 12, '#5eead4');
    return new Promise(r => setTimeout(r, 900));
  }
  function storm(zone, severity) {
    densityField(zone, severity, 1400);
    shockwave(zone, 'ring-storm');
    setTimeout(() => shockwave(zone, 'ring-storm'), 260);
    setTimeout(() => shockwave(zone, 'ring-storm'), 520);
    return turbulence(severity);
  }
  function breach(zone) {
    shockwave(zone, 'ring-breach');
    zoneEl(zone).classList.add('surging');
    setTimeout(() => zoneEl(zone).classList.remove('surging'), 1200);
  }
  function cascadeForward() {
    ZONES.forEach((z, i) => setTimeout(() => {
      zoneEl(z).classList.add('surging');
      shockwave(z, 'ring-pulse');
      setTimeout(() => zoneEl(z).classList.remove('surging'), 700);
    }, i * 300));
    return new Promise(r => setTimeout(r, ZONES.length * 300 + 400));
  }
  function cascadeCooling() {
    // CollapseEngine: reverse-order cooling cascade + arc dissolution.
    const routes = routesG.querySelectorAll('.route');
    routes.forEach(r => r.classList.add('dissolving'));
    [...ZONES].reverse().forEach((z, i) => setTimeout(() => {
      zoneEl(z).classList.add('cooling');
      zoneEl(z).classList.remove('active', 'surging');
      setTimeout(() => zoneEl(z).classList.remove('cooling'), 900);
    }, i * 280));
    return new Promise(r => setTimeout(() => {
      routes.forEach(rt => rt.classList.remove('dissolving'));
      ZONES.forEach(z => zoneEl(z).classList.remove('dim'));
      r();
    }, ZONES.length * 280 + 700));
  }
  function resetIncidents() {
    capsulesG.innerHTML = '';
    ringsG.innerHTML = '';
    ZONES.forEach(z => zoneEl(z).classList.remove('active', 'dim', 'surging', 'cooling'));
  }

  return {
    build, ZONES, zoneEl, haloEl, activateZone, deactivateZone,
    rematerialize, incidentEcho, icmpSweep, storm, breach, cascadeForward, cascadeCooling,
    turbulence, shockwave, sparks, sparksAtZone, resetIncidents, routeId,
  };
})();
