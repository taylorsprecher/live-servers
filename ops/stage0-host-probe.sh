#!/usr/bin/env bash
# Stage 0 host probe — READ-ONLY. Prints the facts needed to plan the
# production-baseline capture and the stateful-data snapshot.
# Changes nothing on the host. Never prints secret values: filenames only.
set -u
echo "== host =="; hostname; date -u +%FT%TZ; whoami
echo "== disk =="; df -h / /home 2>/dev/null | cat
echo "== mem =="; free -m | cat
echo "== docker ps =="; docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | sort
echo "== docker volumes =="; docker volume ls --format '{{.Name}}' | sort
echo "== docker system df (volumes) =="; docker system df -v 2>/dev/null | sed -n '/Local Volumes/,/^$/p' | head -40
echo "== tooling =="; command -v python3 && python3 --version; command -v aws || echo 'no aws cli'; command -v tar; command -v sha256sum; command -v gzip
echo "== host tree =="; cd /home/deploy/live-servers || exit 1; pwd; du -sh . 2>/dev/null; find . -type f | wc -l
echo "== tree: largest top-level dirs =="; du -sh ./*/ 2>/dev/null | sort -rh | head -15
echo "== tree: top-level entries =="; ls -a | sort
echo "== tree: secret-shaped filenames (NAMES ONLY) =="
find . -type f \( -name '.env*' -o -iname '*secret*' -o -iname '*credential*' -o -name '*.key' -o -name '*.pem' \) | sort | head -60
echo "== probe done =="
