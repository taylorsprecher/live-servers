# C-Port CRC16 pipeline — live demo container.
# Pure-Python serial pipeline running in --simulate mode (no hardware):
# ingests simulated CRC16-framed packets, drops corrupt frames, flushes
# timestamped SAS XPORT batches, and serves Prometheus metrics on
# :8000/metrics. MQTT publishing finds no broker in the container and
# disables itself by design — expected, not an error.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY pipeline.py simulator.py README.md ./

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Gentle demo load: 5 frames/s with 20% corruption so the dropped-frames
# counter visibly moves; batch flush every 60 s into a container-local
# directory (ephemeral, no volume). Metrics stay on the default port 8000;
# the gateway owns the public side.
CMD ["python3", "pipeline.py", "--simulate", "--sim-rate", "5", "--sim-corruption", "0.2", "--flush-interval", "60", "--metrics-port", "8000", "--output-dir", "/app/batches"]
