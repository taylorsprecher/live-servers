# Network Portal — Python 3 stdlib ONLY (no pip installs).
# Serves the index page (index.html) plus per-company route pages
# (company.html template) under /portal.
FROM python:3.12-slim

WORKDIR /app
COPY app.py index.html company.html README.md ./

ENV PYTHONUNBUFFERED=1 \
    BASE_PATH=/portal \
    PORT=8000

EXPOSE 8000
CMD ["python3", "app.py"]
