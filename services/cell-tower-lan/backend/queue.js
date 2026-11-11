'use strict';
/**
 * Telemetry job queue (increment 2) — BullMQ semantics without Redis.
 *
 * Stands at exactly the boundary the spec ("Encrypt Cell Tower LAN",
 * section 3.4) assigns to the BullMQ queue `cell-tower-telemetry`
 * (job `process-telemetry`): producers enqueue { encryptedPayload,
 * timestamp } and return immediately; a worker consumes asynchronously.
 *
 * BullMQ behaviors reproduced here:
 *   - attempts: a job is executed up to `maxAttempts` times (default 3)
 *   - exponential backoff between attempts: base * 2^(n-1) — with the
 *     spec's 2 s base that is 2 s, 4 s (8 s would precede a 4th attempt)
 *   - dead-letter: after the final attempt the job is marked 'dead',
 *     persisted, and listed via the DLQ API instead of vanishing
 *   - durability: job records live in the store (SQLite), so waiting,
 *     delayed, and dead jobs survive a server restart; jobs caught
 *     mid-flight ('active') are recovered to 'waiting' on boot
 *
 * What is NOT reproduced: multi-process workers, Redis-backed locking,
 * priority/fairness ordering across producers. One in-process worker,
 * FIFO by next-due time. Set CELL_LAN_QUEUE_BASE_MS to shrink the
 * backoff base for tests/demos (default 2000, per spec).
 */
const crypto = require('node:crypto');

class TelemetryQueue {
  /**
   * @param {object} store      persistence layer (store.js)
   * @param {function} processor  async/sync fn(jobData) -> result; throws on failure
   * @param {object} [opts]     { baseDelayMs, maxAttempts, onDead }
   */
  constructor(store, processor, opts = {}) {
    this.store = store;
    this.processor = processor;
    this.baseDelayMs = opts.baseDelayMs ?? Number(process.env.CELL_LAN_QUEUE_BASE_MS || 2000);
    this.maxAttempts = opts.maxAttempts ?? 3;
    this.onDead = opts.onDead || null;
    this.name = 'process-telemetry';
    this.queueName = 'cell-tower-telemetry';
    this._timer = null;
    this._running = false;
    this._completedSincePrune = 0;
  }

  enqueue(data) {
    const now = Date.now();
    const job = {
      id: `job-${now.toString(36)}-${crypto.randomBytes(3).toString('hex')}`,
      name: this.name,
      data,
      status: 'waiting',
      attemptsMade: 0,
      maxAttempts: this.maxAttempts,
      lastError: null,
      createdAt: now,
      nextRunAt: now,
      processedAt: null,
    };
    this.store.saveJob(job);
    this._schedule(0);
    return job;
  }

  start() {
    this.store.recoverJobs(); // 'active' at crash time -> 'waiting'
    this._schedule(0);
  }

  stop() {
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
  }

  _schedule(ms) {
    if (this._timer) return;
    this._timer = setTimeout(() => {
      this._timer = null;
      this._pump().catch((err) => console.error('queue pump error:', err));
    }, ms);
    if (this._timer.unref) this._timer.unref();
  }

  async _pump() {
    if (this._running) return;
    this._running = true;
    let pumpError = null;
    try {
      for (;;) {
        const due = this.store.dueJobs(Date.now());
        if (!due.length) break;
        for (const job of due) {
          await this._execute(job);
        }
      }
    } catch (err) {
      // A store-level failure (e.g. SQLITE_BUSY) aborts the pump. It is
      // logged here and — critically — the finally below reschedules
      // with a floor delay instead of 0 ms, so a persistent store error
      // can never turn the pump into a hot spin that starves the event
      // loop (the Nov 11, 2026 wedge: 'database is locked' thrown before
      // _execute's try block, retried at 0 ms forever over a 345k-row
      // jobs table, health endpoint dead while the process lived).
      pumpError = err;
      console.error('queue pump error:', err);
    } finally {
      this._running = false;
      // Wake again when the nearest delayed job comes due.
      const pending = [
        ...this.store.listJobs('waiting', 1),
        ...this.store.listJobs('delayed', 1),
      ];
      if (pending.length) {
        const nextAt = Math.min(...pending.map((j) => j.nextRunAt));
        let delay = Math.max(0, nextAt - Date.now());
        if (pumpError) delay = Math.max(delay, 5000);
        this._schedule(delay);
      }
    }
  }

  async _execute(job) {
    job.status = 'active';
    job.attemptsMade += 1;
    try {
      // This save used to sit BEFORE the try: if it threw (e.g.
      // 'database is locked'), the job stayed 'waiting' with its
      // attempt unrecorded, the exception aborted the pump, and the
      // job was retried at 0 ms forever. Inside the try, a save
      // failure is just a failed attempt — backoff, then DLQ.
      this.store.saveJob(job);
      await this.processor(job.data);
      job.status = 'completed';
      job.processedAt = Date.now();
      job.lastError = null;
      this.store.saveJob(job);
      // Housekeeping: bound the jobs table so queue scans stay small
      // (see store.pruneCompletedJobs). Never fail a job over it.
      if (this.store.pruneCompletedJobs &&
          ++this._completedSincePrune >= 200) {
        this._completedSincePrune = 0;
        try { this.store.pruneCompletedJobs(1000); } catch { /* ignore */ }
      }
    } catch (err) {
      job.lastError = err.message;
      job.processedAt = Date.now();
      if (job.attemptsMade >= job.maxAttempts) {
        job.status = 'dead';
        this.store.saveJob(job);
        console.error(`queue: job ${job.id} exhausted ${job.maxAttempts} attempts -> DLQ: ${err.message}`);
        if (this.onDead) this.onDead(job, err);
      } else {
        const delay = this.baseDelayMs * 2 ** (job.attemptsMade - 1);
        job.status = 'delayed';
        job.nextRunAt = Date.now() + delay;
        this.store.saveJob(job);
        console.warn(`queue: job ${job.id} attempt ${job.attemptsMade} failed (${err.message}); retry in ${delay} ms`);
      }
    }
  }

  stats() { return this.store.jobStats(); }
  dlq(limit = 50) { return this.store.listJobs('dead', limit); }
}

module.exports = { TelemetryQueue };
