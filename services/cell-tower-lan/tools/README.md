# Cell Tower LAN — tools

## s3_file_cipher.py

Chunked AES-256-GCM streaming file cipher, built locally on 2026-11-18
from the design in `Cell_Tower_LAN_1zilTB.docx` (filed in
`~/workspace/your_files/`, MD5 `6aac2edc341a81b04c9b1731074516d0`).
Python stays the language for the AWS/S3 work, per Taylor's standing rule.

### What is built and tested locally

- `s3_file_cipher.py` — a standalone CLI (`encrypt` / `decrypt`,
  `-i` input, `-o` output). It does not import, modify, or restart the
  running Node cell-tower-lan service.
  - AES-256-GCM authenticated encryption.
  - PBKDF2-HMAC-SHA256 key derivation, 600,000 iterations, random
    16-byte salt, 256-bit key.
  - 1 MiB chunked streaming (files larger than RAM are fine for local
    paths). Each chunk has its own nonce constructed from an 8-byte
    chunk counter + 1-byte last-chunk flag + a per-file base-nonce
    prefix, and the header + counter + flag are bound as associated
    data, so tampering, reordering, or truncation fails decryption.
  - Local paths and `s3://bucket/key` paths are both accepted for
    input and output (boto3 is imported lazily, only for S3 paths;
    S3 writes are staged to a temp file and uploaded only on success).
  - Passwords come from a getpass prompt, or from an environment
    variable named with `--password-env` (default
    `S3_FILE_CIPHER_PASSWORD`). No password or key value is written to
    any file or log, and passwords are not accepted as CLI arguments.
- Local test evidence (2026-11-18, throwaway files in /tmp only):
  - Plaintext: 2,621,478 bytes (text header + random data, so it spans
    two full 1 MiB chunks plus a partial final chunk).
    SHA-256 `d85d85cc30b7eb933d520b869c03b97ef6140d0c7a24be718574e8a466519f07`
  - Encrypted: 2,621,566 bytes (40-byte header + 16-byte GCM tag per
    chunk = +88 bytes, as designed).
    SHA-256 `31d9babfedae793adddc9de2a99a36170ac4e8b0136461419d5b8ca6188372ec`
  - Decrypted/restored SHA-256:
    `d85d85cc30b7eb933d520b869c03b97ef6140d0c7a24be718574e8a466519f07`
    — identical to the original; `cmp` confirmed byte-for-byte match.
  - Tamper test: flipping one byte at offset 500 of the ciphertext
    made decryption exit 1 with "Decryption failed: incorrect password
    or corrupted/tampered data", and the partial output file was
    removed as designed.
  - Empty-file edge case: encrypt → decrypt round trip matched
    (SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
    for both).
  - Test environment: venv at `~/workspace/.venvs/celltower-cipher`
    (cryptography 50.0.2, boto3 1.44.5, Python 3.12.3). Nothing was
    installed system-wide.

### escapeHtml check

The document describes a stray double-quote bug (`replace(/>"/g, …)`)
in an `escapeHtml` helper in a single-file Cell Tower LAN dashboard.
That helper does not exist in this service: searching
`frontend/index.html`, `frontend/dashboard2.html`, and
`frontend/app.js` (and the whole frontend folder) finds no `escapeHtml`,
no HTML-entity replaces, and no stray-quote pattern. No fix was applied
and none was invented. (The bug was in a separate pasted dashboard
prototype, `Cell_Tower_LAN_Dashboard.html`, already patched on
2026-11-01 when it was filed.)

### What is NOT wired

- No live S3 transfer was performed. The `s3://` code path is written
  but untested against the real bucket
  (`zillion-tb-aws-bucket`, us-west-2, SSE-KMS key alias
  `zillion-tb-key`). Nothing was uploaded anywhere during this install.
- No AWS Lambda auto-encryption function (any of the password,
  Secrets Manager/SSM, or KMS-envelope variants from the document) was
  deployed or created.
- The IAM role `zillion-control-plane-role` was not attached to any
  instance, and no AWS resource was created or changed.
- The cipher is not integrated into the Node backend, the dashboard,
  or any deploy workflow. Nothing was committed to GitHub and nothing
  was deployed; the running fleet service `celltower`
  (127.0.0.1:18107) is untouched.

### Next step that needs Taylor's go-ahead

Approve a first live S3 test against `zillion-tb-aws-bucket` — e.g.
encrypt one throwaway file locally, upload the ciphertext via the
`s3://` path (or verify how the utility should coexist with the
bucket's SSE-KMS default encryption), download, decrypt, and compare
hashes — and decide separately whether any Lambda auto-encryption
variant should ever be deployed. Until then, this stays a local-only
utility.
