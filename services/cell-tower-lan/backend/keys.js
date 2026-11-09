'use strict';
/**
 * Per-site key management with a local lifecycle (increment 2).
 *
 * Each registered site gets its own AES-256 key, persisted in
 * <dataDir>/keys.json (atomic tmp+rename writes). Lifecycle:
 *
 *   generate  create a site's first active key (provisioning)
 *   rotate    demote the active key to 'retired' and mint a new active key
 *   revoke    mark a key 'revoked' — it leaves the decrypt ring at once
 *
 * Decryption uses a key RING: all 'active' and 'retired' site keys, then
 * the legacy shared key (env CELL_LAN_MASTER_KEY or the local test key)
 * as a final fallback. That is what makes rotation safe: in-flight
 * payloads encrypted under the just-retired key still decrypt during the
 * grace period, until an operator revokes it.
 *
 * In a real deployment keys.json is replaced by an enrollment/KMS flow
 * and private key material never sits next to the service; here the file
 * is the local stand-in, shared by the server and the simulators on the
 * same host (the simulators read it to encrypt with a site's active key,
 * exactly as a provisioned edge exporter would use its enrolled key).
 *
 * Key material is never exposed through list(): only key ids, statuses,
 * timestamps, and a short SHA-256 fingerprint for identification.
 */
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { makeEncrypter } = require('./crypto');

const fingerprint = (keyBuf) =>
  crypto.createHash('sha256').update(keyBuf).digest('hex').slice(0, 16);

class KeyManager {
  /**
   * @param {string} dataDir  directory holding keys.json
   * @param {string[]} siteIds  registered site ids to provision
   * @param {Buffer} legacyKey  shared fallback key (env or local test key)
   */
  constructor(dataDir, siteIds, legacyKey) {
    this.file = path.join(dataDir, 'keys.json');
    this.legacy = {
      keyId: 'legacy-master',
      siteId: null,
      key: legacyKey,
      encrypter: makeEncrypter(legacyKey),
    };
    this.data = { version: 1, sites: {} };
    if (fs.existsSync(this.file)) {
      this.data = JSON.parse(fs.readFileSync(this.file, 'utf8'));
    }
    this._encrypters = new Map(); // keyId -> encrypter
    // Provisioning: a site with no keys at all gets its first key.
    for (const siteId of siteIds) {
      if (!this.data.sites[siteId]) this.data.sites[siteId] = { keys: [] };
      if (this.data.sites[siteId].keys.length === 0) this._mint(siteId);
    }
    this._save();
  }

  _save() {
    const tmp = this.file + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(this.data, null, 2));
    fs.renameSync(tmp, this.file);
  }

  _encrypterFor(keyId, keyBuf) {
    if (!this._encrypters.has(keyId)) {
      this._encrypters.set(keyId, makeEncrypter(keyBuf));
    }
    return this._encrypters.get(keyId);
  }

  _mint(siteId) {
    const site = this.data.sites[siteId];
    const n = site.keys.length + 1;
    const keyBuf = crypto.randomBytes(32);
    const record = {
      keyId: `key-${siteId.replace(/^SITE-/, '').toLowerCase()}-${n}-${crypto.randomBytes(2).toString('hex')}`,
      hex: keyBuf.toString('hex'),
      status: 'active',
      createdAt: new Date().toISOString(),
      rotatedAt: null,
      revokedAt: null,
    };
    site.keys.push(record);
    return record;
  }

  _record(siteId, keyId) {
    const site = this.data.sites[siteId];
    if (!site) return null;
    return site.keys.find((k) => k.keyId === keyId) || null;
  }

  /** The site's current active key record, or null if none is active. */
  current(siteId) {
    const site = this.data.sites[siteId];
    if (!site) return null;
    return site.keys.find((k) => k.status === 'active') || null;
  }

  /** Encrypter for producers: the site's active key, else legacy fallback. */
  encrypterFor(siteId) {
    const cur = this.current(siteId);
    if (!cur) return { encrypter: this.legacy.encrypter, keyId: this.legacy.keyId, fallback: true };
    return {
      encrypter: this._encrypterFor(cur.keyId, Buffer.from(cur.hex, 'hex')),
      keyId: cur.keyId,
      fallback: false,
    };
  }

  /**
   * Decrypt ring: active + retired site keys (newest first), legacy last.
   * Returns { plaintext, keyId, siteId } for the key that authenticated,
   * or throws the last auth error if no key in the ring verifies.
   */
  decrypt(wireBase64) {
    let lastErr = null;
    for (const siteId of Object.keys(this.data.sites)) {
      const keys = [...this.data.sites[siteId].keys].reverse();
      for (const rec of keys) {
        if (rec.status === 'revoked') continue;
        try {
          const plaintext = this._encrypterFor(rec.keyId, Buffer.from(rec.hex, 'hex'))
            .decrypt(wireBase64);
          return { plaintext, keyId: rec.keyId, siteId };
        } catch (err) { lastErr = err; }
      }
    }
    try {
      const plaintext = this.legacy.encrypter.decrypt(wireBase64);
      return { plaintext, keyId: this.legacy.keyId, siteId: null };
    } catch (err) { lastErr = err; }
    throw new Error(
      `decryption failed: no key in ring authenticated the payload (${lastErr ? lastErr.message : 'unknown error'})`
    );
  }

  /** Lifecycle: create a first active key for a site that has none active. */
  generate(siteId) {
    if (!this.data.sites[siteId]) this.data.sites[siteId] = { keys: [] };
    if (this.current(siteId)) {
      throw new Error(`site ${siteId} already has an active key; use rotate`);
    }
    const rec = this._mint(siteId);
    this._save();
    return this._public(siteId, rec);
  }

  /** Lifecycle: retire the active key and mint its successor. */
  rotate(siteId) {
    if (!this.data.sites[siteId]) this.data.sites[siteId] = { keys: [] };
    const cur = this.current(siteId);
    if (cur) {
      cur.status = 'retired';
      cur.rotatedAt = new Date().toISOString();
    }
    const rec = this._mint(siteId);
    this._save();
    return this._public(siteId, rec);
  }

  /** Lifecycle: revoke a key — it leaves the decrypt ring immediately. */
  revoke(siteId, keyId) {
    const rec = this._record(siteId, keyId);
    if (!rec) throw new Error(`key ${keyId} not found for site ${siteId}`);
    rec.status = 'revoked';
    rec.revokedAt = new Date().toISOString();
    this._save();
    return this._public(siteId, rec);
  }

  _public(siteId, rec) {
    return {
      site_id: siteId,
      key_id: rec.keyId,
      status: rec.status,
      fingerprint: fingerprint(Buffer.from(rec.hex, 'hex')),
      created_at: rec.createdAt,
      rotated_at: rec.rotatedAt,
      revoked_at: rec.revokedAt,
    };
  }

  /** Metadata for every key (never the key material itself). */
  list() {
    const out = [];
    for (const siteId of Object.keys(this.data.sites)) {
      out.push({
        site_id: siteId,
        keys: this.data.sites[siteId].keys.map((rec) => this._public(siteId, rec)),
      });
    }
    out.push({
      site_id: null,
      note: 'shared fallback key (env CELL_LAN_MASTER_KEY or local test key)',
      keys: [{
        site_id: null,
        key_id: this.legacy.keyId,
        status: 'active',
        fingerprint: fingerprint(this.legacy.key),
        created_at: null, rotated_at: null, revoked_at: null,
      }],
    });
    return out;
  }
}

module.exports = { KeyManager, fingerprint };
