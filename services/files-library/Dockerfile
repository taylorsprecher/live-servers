# Files Library — Python 3 stdlib ONLY (no pip installs).
# Serves the listing page (index.html template + library.json) under
# /files and streams staged file bytes from the /data volume.
FROM python:3.12-slim

WORKDIR /app
COPY app.py index.html library.json README.md ./

ENV PYTHONUNBUFFERED=1 \
    BASE_PATH=/files \
    PORT=8000 \
    FILESLIB_DATA_DIR=/data

EXPOSE 8000
CMD ["python3", "app.py"]
