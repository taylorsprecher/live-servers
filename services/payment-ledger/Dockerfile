FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "psycopg[binary]==3.2.3"
COPY app.py schema.sql ./
COPY app_pkg ./app_pkg
COPY assets ./assets
CMD ["python3", "app.py"]
