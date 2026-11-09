# USB-SOVEREIGN — live demo container.
# Standard-library-only Python app: no pip installs needed.
FROM python:3.12-slim

WORKDIR /app
COPY backend ./backend
COPY frontend ./frontend

# Bind all interfaces inside the container; the gateway in front owns the
# public side. BASE_PATH matches the gateway's mount prefix — requests
# arrive WITH the /usb prefix and the server strips it before routing.
# USB_SOV_PORTS=sim keeps the simulated plant (with the cellular failover
# cycle) — no socat/PTY hardware plane inside the container.
ENV USB_SOV_HOST=0.0.0.0 \
    USB_SOV_PORT=8765 \
    BASE_PATH=/usb \
    USB_SOV_PORTS=sim \
    PYTHONUNBUFFERED=1

EXPOSE 8765

CMD ["python3", "backend/main.py"]
