# Member-login training site — Python 3 stdlib ONLY (no pip installs).
# Real member accounts persist in SQLite on the /data volume.
FROM python:3.12-slim

WORKDIR /app
COPY app.py README.md ./

ENV PYTHONUNBUFFERED=1 \
    BASE_PATH=/members \
    MEMBERS_DB_PATH=/data/members.db \
    CONTENT_DIR=/data/content \
    PORT=8000

EXPOSE 8000
CMD ["python3", "app.py"]
