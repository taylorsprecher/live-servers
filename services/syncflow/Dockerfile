# SyncFlow — Python 3 stdlib ONLY (no pip installs).
# Serves the Backend–Frontend Sync Flow demo (single-page client plus
# the sync API) under /syncflow. State persists to a JSON file in the
# /data volume so it survives container restarts.
FROM python:3.12-slim

WORKDIR /app
COPY app.py index.html README.md ./

ENV PYTHONUNBUFFERED=1 \
    BASE_PATH=/syncflow \
    PORT=8000 \
    SYNCFLOW_DATA_DIR=/data

EXPOSE 8000
CMD ["python3", "app.py"]
