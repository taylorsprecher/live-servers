# Xbox Cloud Save Platform — demo container.
#
# Serves the FastAPI app (API + frontend at /ui/) on port 8000.
# Path-prefix hosting: set BASE_PATH=/xbox and have the gateway forward
# requests WITH the prefix; the app mounts itself under it. See
# DEPLOY.md for the full env contract.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    XSAVE_STORAGE_DIR=/data/xsave

WORKDIR /app

# Dependencies first for layer caching.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# App code. app.py serves ../frontend relative to backend/, so the
# sibling layout must be preserved in the image.
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

# Runtime data (save files + per-user indexes) lives outside the code
# tree so it can be a mounted volume.
RUN mkdir -p /data/xsave \
    && useradd --system --uid 10001 xsave \
    && chown -R xsave:xsave /data /app
USER xsave
VOLUME ["/data/xsave"]

WORKDIR /app/backend
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:8000' + os.environ.get('BASE_PATH', '') + '/health', timeout=4)"

# --proxy-headers: trust the gateway's X-Forwarded-* so generated
# pre-signed transfer URLs carry the public scheme/host. No
# --root-path: requests arrive WITH the BASE_PATH prefix and the app
# mounts itself under it.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
