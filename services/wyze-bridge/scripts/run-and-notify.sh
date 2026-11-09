#!/bin/sh
# Runs kms-validate.sh and, on failure, posts to the configured
# Slack/Discord webhooks (from Docker Secrets) and to the status
# service's alert webhook. Adapted from "WYZE APP.docx" section 3.
set -u

run_validation() {
  if [ -x /kms-validate.sh ]; then
    /kms-validate.sh
  else
    sh /kms-validate.sh
  fi
}

notify() {
  MSG="$1"
  for f in slack_webhook_url discord_webhook_url; do
    if [ -s "/run/secrets/$f" ]; then
      URL="$(cat /run/secrets/$f)"
      curl -s -X POST -H 'Content-Type: application/json' \
        -d "{\"content\": \"${MSG}\"}" "$URL" >/dev/null 2>&1 || true
    fi
  done
  # Also log to the status service so the dashboard can show it.
  curl -s -X POST -H 'Content-Type: application/json' \
    -d "{\"source\": \"kms-validator\", \"message\": \"${MSG}\"}" \
    "http://status-api:8080/api/webhooks/alerts" >/dev/null 2>&1 || true
}

OUTPUT="$(run_validation 2>&1)"
STATUS=$?
echo "$OUTPUT"
if [ "$STATUS" -ne 0 ]; then
  notify "Wyze S3 pipeline KMS validation FAILED on $(hostname): $(echo "$OUTPUT" | tail -3)"
  exit "$STATUS"
fi
echo "KMS validation passed."
