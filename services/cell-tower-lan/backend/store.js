'use strict';
/**
 * Persistence layer for the Cell Tower LAN telemetry service (increment 2).
 *
 * Readings, alarms, and queue jobs survive restarts. The primary backend
 * is SQLite via Node's built-in `node:sqlite` (zero dependencies); if that
 * module is unavailable the store falls back to a write-through JSON file
 * with the exact same API:
 *
 *   insertReading(reading)            -> reading (with numeric .id assigned)
 *   latestByRadio(siteId?)            -> { radio_id: reading }
 *   history(radioId, limit)           -> [reading] oldest -> newest
 *   countReadings()                   -> total stored readings
 *   insertAlarm(fields)               -> alarm (with .id / .seq / status)
 *   listAlarms({ siteId, status, limit }) -> [alarm] newest first
 *   getAlarm(id)                      -> alarm | null
 *   ackAlarm(id, { by, note })        -> alarm | null (idempotent)
 *   saveJob(job) / dueJobs(now) / recoverJobs() / jobStats() / listJobs(status, limit)
 *   .backend                          -> 'sqlite' | 'json-file'
 */
const fs = require('node:fs');
const path = require('node:path');

const MAX_HISTORY_PER_RADIO = 300;
const MAX_ALARMS = 200;

// ------------------------------------------------------------ SQLite store
class SqliteStore {
  constructor(dataDir) {
    const { DatabaseSync } = require('node:sqlite');
    this.backend = 'sqlite';
    this.db = new DatabaseSync(path.join(dataDir, 'telemetry.db'));
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        radio_id TEXT NOT NULL,
        site_id TEXT NOT NULL,
        sector_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        payload TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_readings_radio ON readings(radio_id, id);
      CREATE TABLE IF NOT EXISTS alarms (
        id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        ts TEXT NOT NULL,
        site_id TEXT, sector_id TEXT, radio_id TEXT,
        severity TEXT, code TEXT, message TEXT,
        value REAL, threshold REAL,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        ack_by TEXT, ack_at TEXT, ack_note TEXT
      );
      CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        data TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts_made INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        last_error TEXT,
        created_at INTEGER NOT NULL,
        next_run_at INTEGER NOT NULL,
        processed_at INTEGER
      );
      CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    `);
  }

  _nextSeq(key) {
    const row = this.db.prepare('SELECT v FROM meta WHERE k = ?').get(key);
    const next = (row ? Number(row.v) : 0) + 1;
    this.db.prepare(
      'INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v'
    ).run(key, String(next));
    return next;
  }

  insertReading(reading) {
    const info = this.db.prepare(
      'INSERT INTO readings (radio_id, site_id, sector_id, ts, payload) VALUES (?, ?, ?, ?, ?)'
    ).run(reading.radio_id, reading.site_id, reading.sector_id, reading.ts, JSON.stringify(reading));
    reading.id = Number(info.lastInsertRowid);
    // Keep the same bounded history as increment 1 (newest 300 per radio).
    this.db.prepare(`
      DELETE FROM readings WHERE radio_id = ? AND id NOT IN
        (SELECT id FROM readings WHERE radio_id = ? ORDER BY id DESC LIMIT ?)
    `).run(reading.radio_id, reading.radio_id, MAX_HISTORY_PER_RADIO);
    return reading;
  }

  latestByRadio(siteId) {
    const rows = siteId
      ? this.db.prepare(`SELECT payload FROM readings WHERE site_id = ? AND id IN
           (SELECT MAX(id) FROM readings GROUP BY radio_id)`).all(siteId)
      : this.db.prepare(`SELECT payload FROM readings WHERE id IN
           (SELECT MAX(id) FROM readings GROUP BY radio_id)`).all();
    const out = {};
    for (const r of rows) {
      const reading = JSON.parse(r.payload);
      out[reading.radio_id] = reading;
    }
    return out;
  }

  history(radioId, limit) {
    const rows = this.db.prepare(
      'SELECT payload FROM readings WHERE radio_id = ? ORDER BY id DESC LIMIT ?'
    ).all(radioId, limit);
    return rows.map((r) => JSON.parse(r.payload)).reverse();
  }

  countReadings() {
    return this.db.prepare('SELECT COUNT(*) AS n FROM readings').get().n;
  }

  _alarmFromRow(r) {
    if (!r) return null;
    return {
      id: r.id, seq: r.seq, ts: r.ts, site_id: r.site_id, sector_id: r.sector_id,
      radio_id: r.radio_id, severity: r.severity, code: r.code, message: r.message,
      value: r.value, threshold: r.threshold, status: r.status,
      ack: r.ack_by ? { by: r.ack_by, at: r.ack_at, note: r.ack_note } : null,
    };
  }

  insertAlarm(f) {
    const seq = this._nextSeq('alarm_seq');
    const alarm = { ...f, id: `ALM-${seq}`, seq, status: 'ACTIVE', ack: null };
    this.db.prepare(`
      INSERT INTO alarms (id, seq, ts, site_id, sector_id, radio_id, severity,
        code, message, value, threshold, status)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
    `).run(alarm.id, seq, f.ts, f.site_id, f.sector_id, f.radio_id, f.severity,
      f.code, f.message, f.value, f.threshold);
    this.db.prepare(`
      DELETE FROM alarms WHERE seq NOT IN
        (SELECT seq FROM alarms ORDER BY seq DESC LIMIT ?)
    `).run(MAX_ALARMS);
    return alarm;
  }

  listAlarms({ siteId, status, limit = 50 } = {}) {
    let sql = 'SELECT * FROM alarms';
    const where = [], args = [];
    if (siteId) { where.push('site_id = ?'); args.push(siteId); }
    if (status) { where.push('status = ?'); args.push(status); }
    if (where.length) sql += ' WHERE ' + where.join(' AND ');
    sql += ' ORDER BY seq DESC LIMIT ?';
    args.push(limit);
    return this.db.prepare(sql).all(...args).map((r) => this._alarmFromRow(r));
  }

  getAlarm(id) {
    return this._alarmFromRow(this.db.prepare('SELECT * FROM alarms WHERE id = ?').get(id));
  }

  ackAlarm(id, { by, note }) {
    const existing = this.getAlarm(id);
    if (!existing) return null;
    if (existing.status === 'ACKNOWLEDGED') return existing; // idempotent
    const at = new Date().toISOString();
    this.db.prepare(
      "UPDATE alarms SET status = 'ACKNOWLEDGED', ack_by = ?, ack_at = ?, ack_note = ? WHERE id = ?"
    ).run(by || 'operator', at, note || null, id);
    return this.getAlarm(id);
  }

  saveJob(j) {
    this.db.prepare(`
      INSERT INTO jobs (id, name, data, status, attempts_made, max_attempts,
        last_error, created_at, next_run_at, processed_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(id) DO UPDATE SET status = excluded.status,
        attempts_made = excluded.attempts_made, last_error = excluded.last_error,
        next_run_at = excluded.next_run_at, processed_at = excluded.processed_at
    `).run(j.id, j.name, JSON.stringify(j.data), j.status, j.attemptsMade,
      j.maxAttempts, j.lastError, j.createdAt, j.nextRunAt, j.processedAt);
    return j;
  }

  _jobFromRow(r) {
    return {
      id: r.id, name: r.name, data: JSON.parse(r.data), status: r.status,
      attemptsMade: r.attempts_made, maxAttempts: r.max_attempts,
      lastError: r.last_error, createdAt: r.created_at,
      nextRunAt: r.next_run_at, processedAt: r.processed_at,
    };
  }

  dueJobs(now) {
    return this.db.prepare(`
      SELECT * FROM jobs WHERE status IN ('waiting', 'delayed') AND next_run_at <= ?
      ORDER BY next_run_at ASC LIMIT 100
    `).all(now).map((r) => this._jobFromRow(r));
  }

  recoverJobs() {
    this.db.prepare(
      "UPDATE jobs SET status = 'waiting', next_run_at = ? WHERE status = 'active'"
    ).run(Date.now());
  }

  jobStats() {
    const rows = this.db.prepare(
      'SELECT status, COUNT(*) AS n FROM jobs GROUP BY status'
    ).all();
    const stats = { waiting: 0, delayed: 0, active: 0, completed: 0, dead: 0, total: 0 };
    for (const r of rows) { stats[r.status] = r.n; stats.total += r.n; }
    return stats;
  }

  listJobs(status, limit = 50) {
    return this.db.prepare(
      'SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?'
    ).all(status, limit).map((r) => this._jobFromRow(r));
  }
}

// -------------------------------------------------------- JSON file store
// Fallback with the identical API, used only if node:sqlite is missing.
class JsonStore {
  constructor(dataDir) {
    this.backend = 'json-file';
    this.file = path.join(dataDir, 'store.json');
    this.state = { readings: [], alarms: [], jobs: [], alarmSeq: 0, readingSeq: 0 };
    if (fs.existsSync(this.file)) {
      this.state = JSON.parse(fs.readFileSync(this.file, 'utf8'));
    }
  }

  _save() {
    const tmp = this.file + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(this.state));
    fs.renameSync(tmp, this.file);
  }

  insertReading(reading) {
    reading.id = ++this.state.readingSeq;
    this.state.readings.push(reading);
    const mine = this.state.readings.filter((r) => r.radio_id === reading.radio_id);
    if (mine.length > MAX_HISTORY_PER_RADIO) {
      const keep = new Set(mine.slice(-MAX_HISTORY_PER_RADIO).map((r) => r.id));
      this.state.readings = this.state.readings.filter(
        (r) => r.radio_id !== reading.radio_id || keep.has(r.id));
    }
    this._save();
    return reading;
  }

  latestByRadio(siteId) {
    const out = {};
    for (const r of this.state.readings) {
      if (siteId && r.site_id !== siteId) continue;
      if (!out[r.radio_id] || out[r.radio_id].id < r.id) out[r.radio_id] = r;
    }
    return out;
  }

  history(radioId, limit) {
    return this.state.readings.filter((r) => r.radio_id === radioId).slice(-limit);
  }

  countReadings() { return this.state.readings.length; }

  insertAlarm(f) {
    const seq = ++this.state.alarmSeq;
    const alarm = { ...f, id: `ALM-${seq}`, seq, status: 'ACTIVE', ack: null };
    this.state.alarms.unshift(alarm);
    if (this.state.alarms.length > MAX_ALARMS) this.state.alarms.length = MAX_ALARMS;
    this._save();
    return alarm;
  }

  listAlarms({ siteId, status, limit = 50 } = {}) {
    return this.state.alarms
      .filter((a) => (!siteId || a.site_id === siteId) && (!status || a.status === status))
      .slice(0, limit);
  }

  getAlarm(id) { return this.state.alarms.find((a) => a.id === id) || null; }

  ackAlarm(id, { by, note }) {
    const alarm = this.getAlarm(id);
    if (!alarm) return null;
    if (alarm.status !== 'ACKNOWLEDGED') {
      alarm.status = 'ACKNOWLEDGED';
      alarm.ack = { by: by || 'operator', at: new Date().toISOString(), note: note || null };
      this._save();
    }
    return alarm;
  }

  saveJob(j) {
    const i = this.state.jobs.findIndex((x) => x.id === j.id);
    if (i >= 0) this.state.jobs[i] = j; else this.state.jobs.push(j);
    this._save();
    return j;
  }

  dueJobs(now) {
    return this.state.jobs
      .filter((j) => (j.status === 'waiting' || j.status === 'delayed') && j.nextRunAt <= now)
      .sort((a, b) => a.nextRunAt - b.nextRunAt)
      .slice(0, 100);
  }

  recoverJobs() {
    for (const j of this.state.jobs) {
      if (j.status === 'active') { j.status = 'waiting'; j.nextRunAt = Date.now(); }
    }
    this._save();
  }

  jobStats() {
    const stats = { waiting: 0, delayed: 0, active: 0, completed: 0, dead: 0, total: 0 };
    for (const j of this.state.jobs) { stats[j.status]++; stats.total++; }
    return stats;
  }

  listJobs(status, limit = 50) {
    return this.state.jobs
      .filter((j) => j.status === status)
      .sort((a, b) => b.createdAt - a.createdAt)
      .slice(0, limit);
  }
}

function createStore(dataDir) {
  fs.mkdirSync(dataDir, { recursive: true });
  try {
    return new SqliteStore(dataDir);
  } catch (err) {
    console.warn(`store: node:sqlite unavailable (${err.message}); using JSON file store`);
    return new JsonStore(dataDir);
  }
}

module.exports = { createStore, MAX_HISTORY_PER_RADIO, MAX_ALARMS };
