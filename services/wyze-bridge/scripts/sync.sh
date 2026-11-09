#!/bin/sh
# Continuous S3 sync — adapted from "WYZE APP.docx" section 3.
# Reads credentials from Docker Secrets, syncs local recordings to the
# private bucket every 5 minutes with server-side encryption (AES256).
# For aws:kms, set SSE_FLAG="--sse aws:kms" in the container environment.
set -eu

export AWS_ACCESS_KEY_ID="$(cat /run/secrets/aws_access_key_id)"
export AWS_SECRET_ACCESS_KEY="$(cat /run/secrets/aws_secret_access_key)"
export AWS_DEFAULT_REGION="$(cat /run/secrets/aws_region)"
BUCKET="$(cat /run/secrets/s3_bucket)"
SSE_FLAG="${SSE_FLAG:---sse AES256}"

while true; do
  aws s3 sync /recordings "s3://${BUCKET}" ${SSE_FLAG} || \
    echo "[sync] $(date -u +%FT%TZ) sync failed, retrying next cycle" >&2
  sleep 300
done
