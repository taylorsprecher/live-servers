# Cell Tower LAN — telemetry demo container.
#
# Single container: Node telemetry service (APIs + dashboard + ingest
# queue + SQLite persistence) plus the HTTP telemetry simulator, so the
# live dashboard has data. The UDP simulator is NOT run (see DEPLOY.md).
#
# Base image is node:24-slim on purpose: backend/store.js uses Node's
# built-in node:sqlite, which does not exist in Node 20 and is only
# experimental in Node 22. Node 24 matches the runtime this build was
# developed and verified on (v24.22.1) and keeps the SQLite store
# instead of silently degrading to the JSON-file fallback.
FROM node:24-slim

WORKDIR /app

# BASE_PATH is the mount prefix the gateway forwards intact
# (public URL: http://52.41.84.116:443/celltower/). Override with
# -e BASE_PATH= to serve at root instead.
ENV NODE_ENV=production \
    PORT=4173 \
    BASE_PATH=/celltower \
    DATA_DIR=/data

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY start.sh ./start.sh

RUN mkdir -p /data && chmod +x start.sh

# Readings, alarms, queue jobs (telemetry.db) and per-site keys
# (keys.json) live here. Mount a volume to keep them across container
# replacements; an ephemeral /data is acceptable for the demo.
VOLUME ["/data"]

# Internal HTTP port (the gateway publishes it under /celltower/).
# The server also binds UDP 4174 in-process for the spec's datagram
# ingest — deliberately NOT exposed: see DEPLOY.md.
EXPOSE 4173

CMD ["sh", "./start.sh"]
