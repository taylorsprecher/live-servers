#!/bin/sh
# KMS/S3 validation — condensed from "WYZE APP.docx" section 3.
# Proves: (1) a KMS-encrypted upload succeeds and is stored encrypted,
# (2) an unencrypted upload is REJECTED by bucket policy,
# (3) the encrypted object can be downloaded back.
# Exits non-zero on any failure; run-and-notify.sh turns that into an alert.
set -u

export AWS_ACCESS_KEY_ID="$(cat /run/secrets/aws_access_key_id)"
export AWS_SECRET_ACCESS_KEY="$(cat /run/secrets/aws_secret_access_key)"
export AWS_DEFAULT_REGION="$(cat /run/secrets/aws_region)"
BUCKET="$(cat /run/secrets/s3_bucket)"
KMS_KEY_ARN="$(cat /run/secrets/kms_key_arn)"

TEST_FILE="$(mktemp)"
echo "kms-validation $(date -u +%FT%TZ)" > "$TEST_FILE"
KMS_OBJECT="health-check/kms-test.txt"
PLAIN_OBJECT="health-check/plain-test.txt"
FAIL=0

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1" >&2; FAIL=1; }

aws s3 rm "s3://${BUCKET}/${KMS_OBJECT}" >/dev/null 2>&1 || true
aws s3 rm "s3://${BUCKET}/${PLAIN_OBJECT}" >/dev/null 2>&1 || true

if aws s3 cp "$TEST_FILE" "s3://${BUCKET}/${KMS_OBJECT}" \
     --sse aws:kms --sse-kms-key-id "$KMS_KEY_ARN" >/dev/null; then
  pass "KMS upload succeeded"
else
  fail "KMS upload failed"
fi

ENCRYPTION="$(aws s3api head-object --bucket "$BUCKET" --key "$KMS_OBJECT" \
  --query 'ServerSideEncryption' --output text 2>/dev/null || echo none)"
[ "$ENCRYPTION" = "aws:kms" ] \
  && pass "Stored object encrypted with aws:kms" \
  || fail "Stored object encryption is '${ENCRYPTION}', expected aws:kms"

if aws s3 cp "$TEST_FILE" "s3://${BUCKET}/${PLAIN_OBJECT}" >/dev/null 2>&1; then
  fail "Unencrypted upload was ALLOWED (bucket policy not enforcing SSE)"
  aws s3 rm "s3://${BUCKET}/${PLAIN_OBJECT}" >/dev/null 2>&1 || true
else
  pass "Unencrypted upload rejected"
fi

DOWNLOAD_FILE="$(mktemp)"
if aws s3 cp "s3://${BUCKET}/${KMS_OBJECT}" "$DOWNLOAD_FILE" >/dev/null 2>&1 \
   && cmp -s "$TEST_FILE" "$DOWNLOAD_FILE"; then
  pass "KMS download round-trip OK"
else
  fail "KMS download failed or content mismatch"
fi

rm -f "$TEST_FILE" "$DOWNLOAD_FILE"
exit "$FAIL"
