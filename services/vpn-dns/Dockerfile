FROM python:3.12-slim
# iptables is the only system need: the supervisor installs and
# (on failure or shutdown) removes the port-53 REDIRECT inside
# the shared vpn network namespace.
RUN apt-get update \
 && apt-get install -y --no-install-recommends iptables \
 && rm -rf /var/lib/apt/lists/*
COPY vpn_dns_filter.py /app/vpn_dns_filter.py
WORKDIR /app
CMD ["python3", "vpn_dns_filter.py", "--serve"]
