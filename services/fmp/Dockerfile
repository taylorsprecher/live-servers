# FMP-1.0 — Forensic Motion Protocol backend + frontend (zero-dependency).
# One Node process serves the static frontend, the /api/* REST endpoints,
# the SSE feed and the WebSocket feed. See DEPLOY.md for the gateway
# (path-prefix) configuration.
FROM node:20-slim

WORKDIR /app
ENV NODE_ENV=production \
    FMP_PORT=8787

# backend/ carries its data/ store with it; frontend/ is served statically.
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# The motion-script editor saves new script versions into the local store
# (backend/data/...). That tree must be writable by the runtime user;
# everything else stays read-only root-owned.
RUN chown -R node:node /app/backend/data

USER node
EXPOSE 8787

# BASE_PATH is set at run time (see DEPLOY.md): -e BASE_PATH=/fmp
CMD ["node", "backend/server.js"]
