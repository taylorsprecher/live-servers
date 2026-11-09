'use strict';
/**
 * CellTowerLANEncrypter — AES-256-GCM telemetry encryption.
 *
 * Implements the scheme specified in Drive doc "Encrypt Cell Tower LAN":
 *   - Algorithm: AES-256-GCM
 *   - Key: 32 bytes (256-bit), hex-encoded (64 chars) in env CELL_LAN_MASTER_KEY
 *   - Nonce/IV: 12-byte unique value per encryption operation
 *   - AAD: fixed context string "cell-tower-lan" bound to every operation
 *   - Wire format: Base64( IV || ciphertext || auth_tag )
 *
 * If CELL_LAN_MASTER_KEY is not set, a clearly-labelled local TEST key is
 * used so the demo round-trip works out of the box. Never use it elsewhere.
 */
const crypto = require('node:crypto');

const ALGORITHM = 'aes-256-gcm';
const AAD = Buffer.from('cell-tower-lan', 'utf8');
const IV_BYTES = 12;
const TAG_BYTES = 16;

// Local test key ONLY (generated for this scaffold; not a real secret).
const TEST_KEY_HEX =
  '6e5bf76dee57ac5670920062a88055f8f7611cc40372c00fd2717b0bf573a299';

function loadKey() {
  const hex = process.env.CELL_LAN_MASTER_KEY || TEST_KEY_HEX;
  if (!/^[0-9a-fA-F]{64}$/.test(hex)) {
    throw new Error(
      'CELL_LAN_MASTER_KEY must be a 64-character hex string (32 bytes). ' +
        'Startup fails fast on an invalid key, per spec section 3.3/4.1.'
    );
  }
  return {
    key: Buffer.from(hex, 'hex'),
    usingTestKey: !process.env.CELL_LAN_MASTER_KEY,
  };
}

function makeEncrypter(key) {
  return {
    /** Encrypt a UTF-8 string; returns Base64( IV || ciphertext || tag ). */
    encrypt(plaintext) {
      const iv = crypto.randomBytes(IV_BYTES);
      const cipher = crypto.createCipheriv(ALGORITHM, key, iv, {
        authTagLength: TAG_BYTES,
      });
      cipher.setAAD(AAD);
      const ciphertext = Buffer.concat([
        cipher.update(plaintext, 'utf8'),
        cipher.final(),
      ]);
      const tag = cipher.getAuthTag();
      return Buffer.concat([iv, ciphertext, tag]).toString('base64');
    },

    /** Decrypt a Base64 wire payload; throws if the auth tag does not verify. */
    decrypt(wireBase64) {
      const packed = Buffer.from(wireBase64, 'base64');
      if (packed.length < IV_BYTES + TAG_BYTES + 1) {
        throw new Error('Encrypted payload too short to be valid.');
      }
      const iv = packed.subarray(0, IV_BYTES);
      const tag = packed.subarray(packed.length - TAG_BYTES);
      const ciphertext = packed.subarray(IV_BYTES, packed.length - TAG_BYTES);
      const decipher = crypto.createDecipheriv(ALGORITHM, key, iv, {
        authTagLength: TAG_BYTES,
      });
      decipher.setAAD(AAD);
      decipher.setAuthTag(tag);
      return Buffer.concat([
        decipher.update(ciphertext),
        decipher.final(), // throws on tamper / wrong key / wrong AAD
      ]).toString('utf8');
    },
  };
}

module.exports = { loadKey, makeEncrypter, AAD, TEST_KEY_HEX };
