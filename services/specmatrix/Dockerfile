# Spec Matrix — Python 3 stdlib ONLY (no pip installs).
# Serves one self-contained static page (index.html) under /specmatrix.
FROM python:3.12-slim

WORKDIR /app
COPY app.py index.html README.md ./

ENV PYTHONUNBUFFERED=1 \
    BASE_PATH=/specmatrix \
    PORT=8000

EXPOSE 8000
CMD ["python3", "app.py"]
