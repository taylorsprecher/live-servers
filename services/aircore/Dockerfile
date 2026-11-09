# AirCore / AirUI demo — single stdlib-Python server (API + static frontend).
FROM python:3.12-slim

# The gateway forwards requests WITH the /aircore prefix, so the app runs
# under BASE_PATH=/aircore by default in the container. Internal port: 8765.
ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8765 \
    BASE_PATH=/aircore

WORKDIR /app
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# The demo developer seed is generated at runtime into backend/keys/
# (see DEPLOY.md); the app user owns /app so that directory stays writable.
RUN useradd --create-home aircore && chown -R aircore:aircore /app
USER aircore

EXPOSE 8765
CMD ["python3", "backend/server.py"]
