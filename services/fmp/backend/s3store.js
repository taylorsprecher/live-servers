// s3store.js — S3-shaped storage interface over a local directory.
//
// The on-disk layout mirrors S3 addressing exactly:
//   <root>/<bucket>/<key...>
// so swapping this module for a real @aws-sdk/client-s3 implementation
// later requires no changes to routes or the frontend.
//
// Implemented surface (subset of S3 used by FMP-1.0):
//   listBuckets()                  -> [{ Name, CreationDate }]
//   listObjects(bucket, prefix)    -> [{ Key, Size, LastModified }]
//   getObject(bucket, key)         -> { Body: string, ContentType, LastModified }
//   putObject(bucket, key, body)   -> { Key, Size, LastModified }  (Increment 2)

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, 'data');

function safeJoin(...parts) {
  const p = path.resolve(ROOT, ...parts);
  if (!p.startsWith(ROOT)) throw new Error('invalid key');
  return p;
}

function listBuckets() {
  return fs.readdirSync(ROOT, { withFileTypes: true })
    .filter(d => d.isDirectory())
    .map(d => ({
      Name: d.name,
      CreationDate: fs.statSync(path.join(ROOT, d.name)).birthtime.toISOString(),
    }));
}

function listObjects(bucket, prefix = '') {
  const base = safeJoin(bucket);
  const out = [];
  (function walk(dir, keyPrefix) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const key = keyPrefix ? `${keyPrefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) walk(path.join(dir, entry.name), key);
      else if (key.startsWith(prefix)) {
        const st = fs.statSync(path.join(dir, entry.name));
        out.push({ Key: key, Size: st.size, LastModified: st.mtime.toISOString() });
      }
    }
  })(base, '');
  return out;
}

function getObject(bucket, key) {
  const file = safeJoin(bucket, key);
  const body = fs.readFileSync(file, 'utf8');
  return {
    Body: body,
    ContentType: key.endsWith('.json') ? 'application/json' : 'application/octet-stream',
    LastModified: fs.statSync(file).mtime.toISOString(),
  };
}

function putObject(bucket, key, body) {
  const file = safeJoin(bucket, key);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, body);
  return { Key: key, Size: Buffer.byteLength(body), LastModified: fs.statSync(file).mtime.toISOString() };
}

module.exports = { listBuckets, listObjects, getObject, putObject, ROOT };
