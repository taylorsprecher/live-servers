#!/usr/bin/env python3
"""vpn_dns_filter.py — DNS-level bug blocking for Taylor's
WireGuard VPN (Taylor, 2026-11-22: "add blocking at my VPN
server", after "only allow real information to be seen").

How it works: the VPN peers' DNS already terminates inside the
vpn container's network namespace (PEERDNS=auto -> the server
address 10.8.0.1). This sidecar shares that namespace
(network_mode: service:vpn) and REDIRECTs port-53 traffic from
the tunnel interface to this filter on port 5353. A query for a
blocklisted domain (advertising, tracking, telemetry — matched
by suffix, so subdomains count) is answered locally with
0.0.0.0 / :: and written to the report file on the volume (the
automatic report). Every other query is forwarded untouched to
the upstream resolvers and the real answer relayed — real
information is never altered, only refused for listed domains.

Fail-open discipline: a supervisor health-checks the filter
every 10 seconds; after 3 consecutive failures it REMOVES the
redirect, and the phones fall back to the container's own
CoreDNS exactly as before this filter existed. On shutdown the
redirect is removed first. While the arming switch
(VPN_DNS_FILTER_ENABLED=1 in the host .env) is off, the
container idles: no listeners, no rules — a plain deploy
changes nothing.

Known limits, stated plainly: this filters plain DNS (port 53)
inside the tunnel. Apps that carry their own encrypted DNS
(DNS-over-HTTPS) or hard-code encrypted resolvers bypass it —
no server-side filter can see that traffic without breaking
the tunnel's encryption, and this one does not try. The seed
list covers advertising/tracking/telemetry domains; extra
domains can be added one per line in the volume file
dns_blocklist_extra.txt (read at container start).

Python standard library only (Taylor's standing stack rule).
"""

import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

PORT = int(os.environ.get("VPN_DNS_PORT", "5353"))
UPSTREAMS = [s.strip() for s in os.environ.get(
    "VPN_DNS_UPSTREAMS", "1.1.1.1,9.9.9.9").split(",") if s.strip()]
REPORT_PATH = os.environ.get(
    "VPN_DNS_REPORT", "/data/dns_filter_report.log")
EXTRA_PATH = os.environ.get(
    "VPN_DNS_EXTRA_BLOCKLIST", "/data/dns_blocklist_extra.txt")
WG_IFACE = os.environ.get("VPN_DNS_IFACE", "wg0")
SERVER_IP = os.environ.get("VPN_DNS_SERVER_IP", "10.8.0.1")
ENABLED = (os.environ.get("VPN_DNS_FILTER_ENABLED") or "") == "1"

# Advertising / tracking / telemetry domains. Suffix-matched:
# listing a domain blocks it and every subdomain. Chosen to be
# functionally inert — apps do not need any of these to work.
SEED_BLOCKLIST = {
    # advertising
    "doubleclick.net", "googlesyndication.com",
    "googleadservices.com", "amazon-adsystem.com",
    "ads.yahoo.com", "adnxs.com", "criteo.com", "taboola.com",
    "outbrain.com", "smartadserver.com", "pubmatic.com",
    "rubiconproject.com", "openx.net", "casalemedia.com",
    "advertising.com", "adform.net", "bidswitch.net",
    "moatads.com", "adsafeprotected.com", "doubleverify.com",
    "ads-twitter.com", "ads.linkedin.com", "ads.pinterest.com",
    # analytics / telemetry / research trackers
    "google-analytics.com", "app-measurement.com",
    "scorecardresearch.com", "quantserve.com",
    "api.mixpanel.com", "api.segment.io", "api.amplitude.com",
    "hotjar.com", "api.fullstory.com", "mouseflow.com",
    "clarity.ms", "analytics.twitter.com", "ct.pinterest.com",
    "px.ads.linkedin.com", "device-metrics-us.amazon.com",
    "mobileanalytics.us-east-1.amazonaws.com",
}
# Deliberately NOT listed: API domains apps need to function
# (e.g. graph.facebook.com is the Facebook API itself) — real
# information stays reachable; only pure ad/tracker/telemetry
# endpoints are refused.
# The health canary must never be blockable.
NEVER_BLOCK = {"example.com"}


def load_blocklist():
    blocked = set(SEED_BLOCKLIST)
    try:
        with open(EXTRA_PATH) as fh:
            for line in fh:
                d = line.strip().lower().rstrip(".")
                if d and not d.startswith("#"):
                    blocked.add(d)
    except OSError:
        pass
    return blocked - NEVER_BLOCK


BLOCKLIST = load_blocklist()


def is_blocked(qname):
    name = qname.lower().rstrip(".")
    if name in NEVER_BLOCK:
        return False
    labels = name.split(".")
    for i in range(len(labels) - 1):
        if ".".join(labels[i:]) in BLOCKLIST:
            return True
    return name in BLOCKLIST


# -- DNS wire format ------------------------------------------------
def parse_question(packet):
    """(qname, qtype, question_end_offset) or None."""
    if len(packet) < 12:
        return None
    qdcount = struct.unpack(">H", packet[4:6])[0]
    if qdcount != 1:
        return None
    off = 12
    labels = []
    while True:
        if off >= len(packet):
            return None
        ln = packet[off]
        off += 1
        if ln == 0:
            break
        if ln & 0xC0 or off + ln > len(packet):
            return None
        labels.append(packet[off:off + ln].decode("latin-1"))
        off += ln
    if off + 4 > len(packet):
        return None
    qtype = struct.unpack(">H", packet[off:off + 2])[0]
    return ".".join(labels), qtype, off + 4


def sinkhole_response(query, qtype, qend):
    """NOERROR answer pointing at 0.0.0.0 / :: for A/AAAA;
    NOERROR with no answers for other types."""
    qid, qflags = struct.unpack(">HH", query[0:4])
    flags = 0x8000 | (qflags & 0x0100) | 0x0080
    answers = b""
    ancount = 0
    if qtype == 1:      # A
        answers = (b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4)
                   + socket.inet_aton("0.0.0.0"))
        ancount = 1
    elif qtype == 28:   # AAAA
        answers = (b"\xc0\x0c" + struct.pack(">HHIH", 28, 1, 60, 16)
                   + socket.inet_pton(socket.AF_INET6, "::"))
        ancount = 1
    header = struct.pack(">HHHHHH", qid, flags, 1, ancount, 0, 0)
    return header + query[12:qend] + answers


# -- reporting ------------------------------------------------------
_report_lock = threading.Lock()
_report_seen = {}


def report_block(qname, src_ip, qtype):
    now = time.time()
    key = (src_ip, qname)
    if now - _report_seen.get(key, 0) < 60:
        return
    _report_seen[key] = now
    stamp = datetime.now(timezone.utc).isoformat()
    line = f"[{stamp}] BLOCKED {qname} from {src_ip} (type {qtype})"
    try:
        with _report_lock:
            with open(REPORT_PATH, "a") as fh:
                fh.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


# -- forwarding -----------------------------------------------------
def forward_udp(query, timeout=3.0):
    for upstream in UPSTREAMS:
        try:
            with socket.socket(socket.AF_INET,
                               socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(query, (upstream, 53))
                data, _ = s.recvfrom(65535)
                return data
        except OSError:
            continue
    return None


def forward_tcp(query, timeout=5.0):
    for upstream in UPSTREAMS:
        try:
            with socket.create_connection((upstream, 53),
                                          timeout=timeout) as s:
                s.sendall(struct.pack(">H", len(query)) + query)
                raw = s.recv(2)
                if len(raw) < 2:
                    continue
                (n,) = struct.unpack(">H", raw)
                body = b""
                while len(body) < n:
                    chunk = s.recv(n - len(body))
                    if not chunk:
                        break
                    body += chunk
                if len(body) == n:
                    return body
        except OSError:
            continue
    return None


def servfail(query):
    qid, qflags = struct.unpack(">HH", query[0:4])
    flags = 0x8000 | (qflags & 0x0100) | 0x0080 | 0x0002
    return struct.pack(">HHHHHH", qid, flags, 0, 0, 0, 0)


def handle_query(query, src_ip):
    parsed = parse_question(query)
    if parsed is None:
        return forward_udp(query)
    qname, qtype, qend = parsed
    if is_blocked(qname):
        report_block(qname, src_ip, qtype)
        return sinkhole_response(query, qtype, qend)
    return None  # caller forwards (protocol-specific)


def udp_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    while True:
        try:
            data, addr = srv.recvfrom(65535)
        except OSError:
            continue
        def work(data=data, addr=addr):
            resp = handle_query(data, addr[0])
            if resp is None:
                resp = forward_udp(data) or servfail(data)
            try:
                srv.sendto(resp, addr)
            except OSError:
                pass
        threading.Thread(target=work, daemon=True).start()


def tcp_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(16)
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            continue
        def work(conn=conn, addr=addr):
            try:
                raw = conn.recv(2)
                if len(raw) < 2:
                    return
                (n,) = struct.unpack(">H", raw)
                query = b""
                while len(query) < n:
                    chunk = conn.recv(n - len(query))
                    if not chunk:
                        return
                    query += chunk
                resp = handle_query(query, addr[0])
                if resp is None:
                    resp = forward_tcp(query) or servfail(query)
                conn.sendall(struct.pack(">H", len(resp)) + resp)
            except OSError:
                pass
            finally:
                conn.close()
        threading.Thread(target=work, daemon=True).start()


# -- supervisor: iptables redirect with fail-open -------------------
def _iptables(*args):
    return subprocess.run(["iptables", "-t", "nat", *args],
                          capture_output=True).returncode


def _rule(proto):
    return ("PREROUTING", "-i", WG_IFACE, "-p", proto,
            "--dport", "53", "-j", "REDIRECT",
            "--to-ports", str(PORT))


def rules_present():
    return all(_iptables("-C", *_rule(p)) == 0
               for p in ("udp", "tcp"))


def install_rules():
    for p in ("udp", "tcp"):
        if _iptables("-C", *_rule(p)) != 0:
            _iptables("-A", *_rule(p))


def remove_rules():
    for p in ("udp", "tcp"):
        while _iptables("-C", *_rule(p)) == 0:
            _iptables("-D", *_rule(p))


def query_a(host, server, port, timeout=2.5):
    """One A query; returns the first A rdata as text, or None."""
    qid = int.from_bytes(os.urandom(2), "big")
    labels = b"".join(bytes([len(x)]) + x.encode()
                      for x in host.split(".")) + b"\x00"
    packet = (struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
              + labels + struct.pack(">HH", 1, 1))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(packet, (server, port))
        data, _ = s.recvfrom(65535)
    if len(data) < 12:
        return None
    ancount = struct.unpack(">H", data[6:8])[0]
    off = 12
    # skip question
    while data[off] != 0:
        off += data[off] + 1
    off += 5
    for _ in range(ancount):
        if data[off] & 0xC0:
            off += 2
        else:
            while data[off] != 0:
                off += data[off] + 1
            off += 1
        rtype, _cls, _ttl, rdlen = struct.unpack(
            ">HHIH", data[off:off + 10])
        off += 10
        if rtype == 1 and rdlen == 4:
            return socket.inet_ntoa(data[off:off + 4])
        off += rdlen
    return None


def health_ok():
    try:
        return query_a("example.com", "127.0.0.1", PORT) is not None
    except OSError:
        return False


def serve():
    if not ENABLED:
        print("vpn-dns filter DARK (VPN_DNS_FILTER_ENABLED is not "
              "1): no listeners, no redirect. Idling.", flush=True)
        while True:
            time.sleep(3600)

    def _shutdown(signum, frame):
        remove_rules()
        print("redirect removed; exiting", flush=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    threading.Thread(target=udp_server, daemon=True).start()
    threading.Thread(target=tcp_server, daemon=True).start()
    install_rules()
    print(f"vpn-dns filter LIVE on :{PORT}, redirect from "
          f"{WG_IFACE} port 53, {len(BLOCKLIST)} domains blocked, "
          f"upstreams {UPSTREAMS}", flush=True)
    failures = 0
    while True:
        time.sleep(10)
        if health_ok():
            failures = 0
            if not rules_present():
                install_rules()
                print("redirect reinstalled (filter healthy)",
                      flush=True)
        else:
            failures += 1
            if failures >= 3 and rules_present():
                remove_rules()
                print("FAIL-OPEN: filter unhealthy, redirect "
                      "removed — peers fall back to the "
                      "container's own DNS", flush=True)


def selftest():
    if not ENABLED:
        print("SELFTEST dark: filter disabled, no redirect "
              "installed — nothing to test")
        return 0
    ok = True
    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"SELFTEST {label}: got {got}, want {want} — "
              f"{'PASS' if good else 'FAIL'}")
    try:
        check("filter sinkholes a tracker domain (direct)",
              query_a("doubleclick.net", "127.0.0.1", PORT),
              "0.0.0.0")
        check("redirect sinkholes it too (via server :53)",
              query_a("doubleclick.net", SERVER_IP, 53), "0.0.0.0")
        real = query_a("example.com", SERVER_IP, 53)
        print(f"SELFTEST real domain resolves (via server :53): "
              f"got {real} — "
              f"{'PASS' if real and real != '0.0.0.0' else 'FAIL'}")
        ok = ok and bool(real) and real != "0.0.0.0"
    except OSError as exc:
        print(f"SELFTEST error: {exc} — FAIL")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    serve()
