#!/usr/bin/env bash
# preflight.sh — prepare a home-LAN Docker host for the Wyze Bridge stack.
#
# RUN THIS ON THE DEPLOYMENT HOST (the always-on Mac mini / MacBook Air /
# Pi on the home LAN), from an interactive terminal. It:
#
#   1. Checks Docker, the Compose v2 plugin, free disk, and stack ports.
#   2. Creates .env from .env.example and prompts for the Wyze API ID/key
#      (developer portal values — NOT the account password).
#   3. Prompts for the Wyze account email/password and writes them to
#      secrets/ files with chmod 600. Input is never echoed back, never
#      logged, and existing secret files are never overwritten silently.
#   4. Optionally collects the archive-tier (AWS) secrets the same way.
#   5. Creates the recordings/ and wyze-config/ volumes' host folders.
#   6. Pulls the container images.
#
# It does NOT start the stack (deploy/deploy.sh does) and it never sends
# anything anywhere. Secret values exist only in this terminal session and
# in the chmod-600 files under secrets/ on this host.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

fail() { echo "PREFLIGHT FAIL: $*" >&2; exit 1; }
warn() { echo "warning: $*" >&2; }
info() { echo "==> $*"; }

# Prompts need a real person at a real keyboard — refuse to guess values.
[ -t 0 ] || fail "not an interactive terminal; run this from a terminal on the host so credentials can be prompted for safely."

# --- 1. Host checks -------------------------------------------------------
info "Checking Docker…"
command -v docker >/dev/null 2>&1 || fail "docker is not installed. Install Docker Desktop (Mac) or Docker Engine (Linux), then re-run."
docker info >/dev/null 2>&1 || fail "the Docker daemon is not running (or this user cannot reach it). Start Docker and re-run."
docker compose version >/dev/null 2>&1 || fail "the Docker Compose v2 plugin is missing ('docker compose version' failed)."
info "Docker OK: $(docker --version)"

info "Checking disk space…"
AVAIL_KB="$(df -Pk "$PROJECT_ROOT" | awk 'NR==2 {print $4}')"
AVAIL_GB=$(( AVAIL_KB / 1024 / 1024 ))
echo "    ${AVAIL_GB} GB free on the filesystem holding this project."
[ "$AVAIL_GB" -ge 5 ] || warn "less than 5 GB free — recordings will fill this quickly; consider a bigger disk or the S3 archive tier."

info "Checking stack ports (5000, 8554, 8888, 8080, 8081)…"
for port in 5000 8554 8888 8080 8081; do
  if docker ps --format '{{.Ports}}' | grep -q ":${port}->"; then
    warn "port ${port} is already published by a running container — if it is this stack from an earlier run, that is fine."
  fi
done

# --- 2. .env --------------------------------------------------------------
set_env_value() {
  # set_env_value KEY VALUE — insert or replace KEY=VALUE in .env
  local key="$1" value="$2"
  if grep -q "^${key}=" .env 2>/dev/null; then
    # Use a temp file instead of sed -i (BSD/GNU portability).
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k{$0=k"="v} {print}' .env > .env.tmp
    mv .env.tmp .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

env_value_is_placeholder() {
  # true when .env is missing the key or still holds the example value
  local key="$1" placeholder="$2" current=""
  current="$(grep "^${key}=" .env 2>/dev/null | head -n 1 | cut -d= -f2- || true)"
  [ -z "$current" ] || [ "$current" = "$placeholder" ]
}

if [ ! -f .env ]; then
  info "Creating .env from .env.example…"
  cp .env.example .env
  chmod 600 .env
fi

# ARCHIVE flags default off until the tier is really set up.
grep -q '^ARCHIVE_ENABLED=' .env || printf 'ARCHIVE_ENABLED=false\n' >> .env
grep -q '^ARCHIVE_CONFIGURED=' .env || printf 'ARCHIVE_CONFIGURED=false\n' >> .env

if env_value_is_placeholder "API_ID" "your-wyze-api-id"; then
  echo "Wyze API credentials (from the Wyze developer portal — these are"
  echo "NOT your account password):"
  read -r -p "  API_ID: " api_id
  [ -n "$api_id" ] || fail "API_ID was empty."
  set_env_value "API_ID" "$api_id"
fi
if env_value_is_placeholder "API_KEY" "your-wyze-api-key"; then
  read -r -s -p "  API_KEY (hidden): " api_key; echo
  [ -n "$api_key" ] || fail "API_KEY was empty."
  set_env_value "API_KEY" "$api_key"
  unset api_key
fi

# --- 3. Docker Secrets files ----------------------------------------------
write_secret() {
  # write_secret NAME PROMPT HIDDEN(0|1) — prompt (unless the file already
  # holds a non-placeholder value) and store with chmod 600, no newline.
  local name="$1" prompt="$2" hidden="$3"
  local file="secrets/${name}"
  if [ -s "$file" ] && ! grep -q 'your-\|example\|placeholder' "$file"; then
    echo "    secrets/${name}: already present, leaving as-is."
    return 0
  fi
  local value=""
  if [ "$hidden" = "1" ]; then
    read -r -s -p "  ${prompt}: " value; echo
  else
    read -r -p "  ${prompt}: " value
  fi
  [ -n "$value" ] || fail "secret '${name}' was empty."
  printf '%s' "$value" > "$file"
  chmod 600 "$file"
  unset value
  echo "    secrets/${name}: written (chmod 600)."
}

info "Wyze account secrets (stored only in secrets/ on this host)…"
write_secret "wyze_email" "Wyze account email" 0
write_secret "wyze_password" "Wyze account password (hidden)" 1
echo "    Note: if the account has 2FA, the bridge's first sign-in may"
echo "    require a one-time code — watch 'docker compose logs wyze-bridge'."

# --- 4. Optional archive-tier secrets --------------------------------------
ARCHIVE_SECRETS_READY=0
echo
read -r -p "Configure the optional S3 archive tier now? [y/N] " want_archive
case "${want_archive:-N}" in
  y|Y)
    info "Archive-tier secrets (least-privilege IAM user, private bucket)…"
    write_secret "aws_access_key_id" "AWS access key ID (sync user)" 0
    write_secret "aws_secret_access_key" "AWS secret access key (hidden)" 1
    write_secret "aws_region" "AWS region (e.g. us-west-2)" 0
    write_secret "s3_bucket" "Private S3 bucket name" 0
    read -r -p "  Use a KMS key for aws:kms encryption? [y/N] " want_kms
    case "${want_kms:-N}" in
      y|Y) write_secret "kms_key_arn" "KMS key ARN" 0 ;;
      *)   echo "    Skipping KMS — sync will use SSE AES256." ;;
    esac
    read -r -p "  Alert webhook for the daily KMS validator — Slack, Discord, both, or none? [none] " hook
    case "$hook" in
      slack|Slack)     write_secret "slack_webhook_url" "Slack webhook URL (hidden)" 1 ;;
      discord|Discord) write_secret "discord_webhook_url" "Discord webhook URL (hidden)" 1 ;;
      both|Both)
        write_secret "slack_webhook_url" "Slack webhook URL (hidden)" 1
        write_secret "discord_webhook_url" "Discord webhook URL (hidden)" 1
        ;;
      *) echo "    No webhook — validator results stay in container logs." ;;
    esac
    ARCHIVE_SECRETS_READY=1
    set_env_value "ARCHIVE_CONFIGURED" "true"
    echo "    ARCHIVE_CONFIGURED=true recorded in .env (start the tier with:"
    echo "    deploy/deploy.sh --archive)"
    ;;
  *)
    echo "    Skipping the archive tier — it stays 'not configured' and off."
    ;;
esac

# --- 5. Host folders -------------------------------------------------------
info "Creating host folders…"
mkdir -p recordings wyze-config

# --- 6. Pull images ---------------------------------------------------------
info "Pulling images for the default stack…"
docker compose pull
if [ "$ARCHIVE_SECRETS_READY" = "1" ]; then
  info "Pulling images for the archive profile…"
  docker compose --profile archive pull
fi

cat <<'EOF'

PREFLIGHT OK.
Next step:  deploy/deploy.sh            (bridge + status + dashboard)
            deploy/deploy.sh --archive  (also start s3-sync + kms-validator)
Then check: deploy/health-check.sh
EOF
