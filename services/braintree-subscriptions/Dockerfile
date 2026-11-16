# Braintree subscriptions service — FRESH SCRIPT (2026-11-15).
# Python 3 stdlib HTTP layer + the official Braintree SDK (the one
# pip dependency in the fleet; Braintree's API is signed XML, which
# is what the SDK exists for). Own database /data/btsubs.db; shares
# the members-data volume so confirmed subscriptions can unlock
# access in /data/members.db.
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "braintree==4.48.0"
COPY app.py README.md ./

ENV PYTHONUNBUFFERED=1 \
    BASE_PATH=/subs \
    BTSUBS_DB_PATH=/data/btsubs.db \
    MEMBERS_DB_PATH=/data/members.db \
    PUBLIC_BASE_URL=https://members.911transedu.net \
    PORT=8000

EXPOSE 8000
CMD ["python3", "app.py"]
