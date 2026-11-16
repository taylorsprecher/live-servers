# Stripe subscriptions service — FRESH SCRIPT (2026-11-15).
# Python 3 stdlib ONLY (no pip installs, no Stripe SDK).
# Own database /data/subs.db; shares the members-data volume so
# confirmed subscriptions can unlock access in /data/members.db.
FROM python:3.12-slim

WORKDIR /app
COPY app.py README.md ./

ENV PYTHONUNBUFFERED=1 \
    BASE_PATH=/subs \
    SUBS_DB_PATH=/data/subs.db \
    MEMBERS_DB_PATH=/data/members.db \
    PUBLIC_BASE_URL=https://members.911transedu.net \
    PORT=8000

EXPOSE 8000
CMD ["python3", "app.py"]
