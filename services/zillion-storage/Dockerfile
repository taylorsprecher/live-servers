# Zillion Storage — the runnable core of Taylor's "Python Cloud —
# 1 Zillion TB" multi-cloud storage design. Single stdlib-Python server.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8772 \
    BASE_PATH=/zillion \
    ZILLION_DATA=/data

WORKDIR /app
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY assets/ ./assets/

RUN useradd --create-home zillion && mkdir -p /data && chown -R zillion:zillion /app /data
USER zillion

EXPOSE 8772
CMD ["python3", "backend/server.py"]
