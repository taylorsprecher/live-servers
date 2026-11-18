#!/usr/bin/env bash
# Targeted deploy: Pathways Editor (Flutter Web) only.
#
# The Web bundle is compiled by the "Deploy Flutter app" workflow ON THE
# RUNNER and rsynced to /home/deploy/flutter-bundle/build/web before this
# script runs — the host never runs a Flutter build (RAM: the fleet
# shares ~1.9 GB). This script installs the bundle into the service,
# rebuilds the tiny nginx image, restarts just this service plus the
# gateway, and verifies. It never touches the other services or the
# audited production stack.
set -euo pipefail

cd /home/deploy/live-servers

BUNDLE=/home/deploy/flutter-bundle/build/web
if [ ! -f "$BUNDLE/index.html" ] || [ ! -f "$BUNDLE/main.dart.js" ]; then
  echo "DEPLOY FLUTTER FAILED: bundle missing at $BUNDLE (the workflow builds and ships it first)" >&2
  exit 1
fi

echo "== install the prebuilt Web bundle into the service =="
rm -rf services/flutter-app/build
mkdir -p services/flutter-app/build
cp -r "$BUNDLE" services/flutter-app/build/web
ls services/flutter-app/build/web | head

echo "== build + start flutter only =="
docker compose build flutter
docker compose up -d flutter
docker compose ps flutter

echo "== wait for the app on its loopback port =="
ok=0
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18125/flutter/ || true)
  echo "flutter try $i: $code"
  if [ "$code" = "200" ]; then ok=1; break; fi
  sleep 2
done
if [ "$ok" != "1" ]; then
  echo "DEPLOY FLUTTER FAILED: app did not answer 200 on 127.0.0.1:18125/flutter/" >&2
  docker compose logs --tail 50 flutter || true
  exit 1
fi

echo "== recreate the gateway so it picks up the flutter upstream =="
docker compose up -d --force-recreate gateway
sleep 3

echo "== verify through the gateway =="
code=""
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' https://members.911transedu.net/flutter/ || true)
  echo "gateway flutter try $i: $code"
  if [ "$code" = "200" ]; then break; fi
  sleep 2
done
if [ "$code" != "200" ]; then
  echo "DEPLOY FLUTTER FAILED: gateway /flutter/ returned $code" >&2
  exit 1
fi
curl -s https://members.911transedu.net/flutter/ | grep -q "Pathways Editor" \
  && echo "gateway serves the Pathways Editor page" \
  || { echo "DEPLOY FLUTTER FAILED: gateway page is not the Pathways Editor" >&2; exit 1; }

echo "== regressions =="
for url in \
  https://members.911transedu.net/members/ \
  https://members.911transedu.net/portal/ \
  https://members.911transedu.net/files/ \
  https://members.911transedu.net/ledger/health \
  https://members.911transedu.net/blackberry/health \
  http://127.0.0.1:80/health ; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$url" || true)
  echo "$code $url"
  if [ "$code" != "200" ]; then
    echo "DEPLOY FLUTTER FAILED: regression check failed for $url" >&2
    exit 1
  fi
done

docker compose ps flutter gateway
free -m | head -2
echo "DEPLOY FLUTTER DONE"
