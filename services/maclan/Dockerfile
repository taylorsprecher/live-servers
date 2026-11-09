# MacLAN — single-container deployment (FastAPI serves API + built UI).
#
# The frontend is PREBUILT outside the image (Node is not installed in
# the image). Before `docker build`, produce frontend/dist with the
# deployment base:
#     cd frontend && VITE_BASE_PATH=/maclan/ npm run build
# See DEPLOY.md.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BASE_PATH=/maclan

WORKDIR /app

# Backend dependencies first (better layer caching).
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

# Backend code and the prebuilt frontend. The layout mirrors the repo so
# the app's default paths resolve unchanged: backend at /app/backend,
# frontend/dist at /app/frontend/dist, SQLite DB at /app/backend/maclan.db.
COPY backend/app /app/backend/app
COPY frontend/dist /app/frontend/dist

WORKDIR /app/backend
EXPOSE 8000

# BASE_PATH=/maclan is baked in above; override with `-e BASE_PATH=` to
# run the same image at a domain root instead.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
