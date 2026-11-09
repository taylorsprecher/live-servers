'use strict';
/**
 * Simulated telemetry readings for the demo site SITE-TX-9042.
 * Shared by simulator.js (HTTP ingest) and simulator-udp.js (UDP ingest).
 *
 * Fault injection so the dashboard's alarm panel is demonstrable:
 *   - Radio RRU-TX9042-B-NR degrades for ~20 s every ~50 s (RSRP plunge,
 *     SINR drop, throughput collapse) -> WEAK_SIGNAL / LOW_SINR alarms.
 *   - Radio RRU-TX9042-C-LTE runs hot, spiking past 65 C periodically
 *     -> RADIO_OVERTEMP alarms.
 */

// Demo site radios, from registry.json
const RADIOS = [
  { site_id: 'SITE-TX-9042', sector_id: 'SEC-A', radio_id: 'RRU-TX9042-A-NR', baseRsrp: -82, maxDl: 320 },
  { site_id: 'SITE-TX-9042', sector_id: 'SEC-A', radio_id: 'RRU-TX9042-A-LTE', baseRsrp: -88, maxDl: 140 },
  { site_id: 'SITE-TX-9042', sector_id: 'SEC-B', radio_id: 'RRU-TX9042-B-NR', baseRsrp: -85, maxDl: 320, fault: 'degrade' },
  { site_id: 'SITE-TX-9042', sector_id: 'SEC-B', radio_id: 'RRU-TX9042-B-LTE', baseRsrp: -90, maxDl: 140 },
  { site_id: 'SITE-TX-9042', sector_id: 'SEC-C', radio_id: 'RRU-TX9042-C-NR', baseRsrp: -80, maxDl: 320 },
  { site_id: 'SITE-TX-9042', sector_id: 'SEC-C', radio_id: 'RRU-TX9042-C-LTE', baseRsrp: -87, maxDl: 140, fault: 'hot' },
];

const noise = (n) => (Math.random() - 0.5) * 2 * n;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function makeReading(r, tSec) {
  const diurnal = Math.sin(tSec / 45) * 0.5 + 0.5; // slow load wave 0..1
  let rsrp = r.baseRsrp + noise(3);
  let sinr = 15 + noise(3) - diurnal * 3;
  let ues = Math.round(clamp(28 * diurnal + noise(6), 1, 60));
  let temp = 42 + diurnal * 8 + noise(2);
  const vswr = 1.25 + Math.abs(noise(0.15));

  if (r.fault === 'degrade') {
    const cycle = tSec % 50;
    if (cycle > 30) { // degraded window ~20 s of every 50 s
      rsrp = -112 + noise(3);
      sinr = 1.5 + noise(1);
      ues = Math.round(clamp(ues * 0.4, 1, 60));
    }
  }
  if (r.fault === 'hot') {
    const cycle = tSec % 70;
    if (cycle > 45) temp = 68 + noise(3); // overtemp window
  }

  const quality = clamp((sinr + 5) / 25, 0.05, 1);
  const dl = r.maxDl * quality * (0.35 + diurnal * 0.6) + noise(8);
  return {
    site_id: r.site_id,
    sector_id: r.sector_id,
    radio_id: r.radio_id,
    ts: new Date().toISOString(),
    metrics: {
      rsrp_dbm: Math.round(rsrp * 10) / 10,
      rsrq_db: Math.round((-11 + noise(1.5)) * 10) / 10,
      sinr_db: Math.round(sinr * 10) / 10,
      dl_throughput_mbps: Math.round(Math.max(dl, 0.5) * 10) / 10,
      ul_throughput_mbps: Math.round(Math.max(dl * 0.22 + noise(3), 0.2) * 10) / 10,
      connected_ues: ues,
      tx_power_dbm: Math.round((43 + noise(0.5)) * 10) / 10,
      temp_c: Math.round(temp * 10) / 10,
      vswr: Math.round(vswr * 100) / 100,
    },
  };
}

module.exports = { RADIOS, makeReading };
