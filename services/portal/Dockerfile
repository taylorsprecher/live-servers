# Network Portal — Python 3 stdlib ONLY (no pip installs).
# Serves one static page (index.html) under /portal.
FROM python:3.12-slim

WORKDIR /app
COPY app.py index.html README.md ./

ENV PYTHONUNBUFFERED=1 \
    BASE_PATH=/portal \
    PORT=8000

EXPOSE 8000
CMD ["python3", "app.py"]
