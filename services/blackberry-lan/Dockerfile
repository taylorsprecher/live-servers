# BlackBerry LAN — Air platform core (AirCore VM + AirBridge bus + AirShield
# policy) from BlackBerry_LAN.pdf. Single stdlib-Python server.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8771 \
    BASE_PATH=/blackberry

WORKDIR /app
COPY backend/ ./backend/
COPY frontend/ ./frontend/

RUN useradd --create-home blackberry && chown -R blackberry:blackberry /app
USER blackberry

EXPOSE 8771
CMD ["python3", "backend/server.py"]
