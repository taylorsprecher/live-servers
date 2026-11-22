"""In-app bug guard for the member site (Taylor, 2026-11-22:
"block incoming bugs on my app, automatically report" — and
"only allow real information to be seen").

Design, in one paragraph: the guard watches every request the
app serves and scores the request PATH against attack-probe
signatures only — credential hunts (/.env, /.git/, /.aws/),
injection and traversal shapes, and scanner paths for software
this site does not run (WordPress, phpMyAdmin). Ordinary
browsing can never match a signature, so real visitors seeing
real information are never impeded: there is deliberately NO
rate or 404-burst rule in this in-app version, because a real
member clicking a dead link must not be blockable. A single
credential hunt blocks immediately; probe signatures block at
3 strikes inside 24 hours; blocks last 7 days. State persists
as JSON on the data volume, and every new block is appended to
a report file on the volume (the automatic report) and counted
on /members/health.

Failure discipline: the guard FAILS OPEN. Any error inside the
guard (state file, report file, anything) allows the request —
the guard's own failure must never take the real site down.
It is armed by a fail-closed switch (MEMBERS_BUG_GUARD_ENABLED=1
in the host .env, read in config.py); while the switch is off,
check() is a no-op.

The client address is the gateway's X-Real-IP (the app port is
loopback-only on the host and the nginx gateway overwrites
X-Real-IP with the real remote address), falling back to the
socket peer. Loopback and the Tailscale tailnet range are
allowlisted and can never be blocked — that is how Taylor and
Jasmine reach the server.
"""

import ipaddress
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

WINDOW = timedelta(hours=24)
BLOCK_FOR = timedelta(days=7)
STRIKES = 3

SEVERE = [
    (r"/\.env", "credential hunt: /.env"),
    (r"/\.git/", "credential hunt: /.git"),
    (r"/\.aws/", "credential hunt: /.aws"),
    (r"config\.php", "probe: config.php"),
    (r"\.(sql|bak|dump)$", "probe: backup/dump file"),
]
PROBES = [
    (r"union\s+select", "SQL injection shape"),
    (r"or\s+1\s*=\s*1", "SQL injection shape"),
    (r"<script", "XSS shape"),
    (r"\.\./", "path traversal"),
    (r"/cgi-bin/", "cgi-bin probe"),
    (r"/wp-(admin|login)", "WordPress scan (site is not WordPress)"),
    (r"xmlrpc\.php", "WordPress scan (xmlrpc)"),
    (r"phpmyadmin", "phpMyAdmin scan"),
]
SEVERE_RE = [(re.compile(p, re.I), why) for p, why in SEVERE]
PROBE_RE = [(re.compile(p, re.I), why) for p, why in PROBES]

ALWAYS_ALLOW = ["127.0.0.1", "::1", "100.64.0.0/10"]


def _classify(path):
    decoded = unquote(path or "")
    for rx, why in SEVERE_RE:
        if rx.search(decoded):
            return ("severe", why)
    for rx, why in PROBE_RE:
        if rx.search(decoded):
            return ("probe", why)
    return None


class BugGuard:
    def __init__(self, enabled, state_path, report_path,
                 allow_extra=(), clock=None):
        self.enabled = bool(enabled)
        self.state_path = state_path
        self.report_path = report_path
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        nets = []
        for item in list(ALWAYS_ALLOW) + list(allow_extra):
            try:
                nets.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                continue
        self._allow = nets
        self._ips = {}
        if self.enabled:
            try:
                with open(state_path) as fh:
                    self._ips = json.load(fh).get("ips", {})
            except Exception:
                self._ips = {}

    # -- internals -------------------------------------------------
    def _allowed(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self._allow)

    def _save(self):
        tmp = f"{self.state_path}.tmp"
        with open(tmp, "w") as fh:
            json.dump({"ips": self._ips}, fh, indent=1)
        os.replace(tmp, self.state_path)

    def _report(self, line):
        with open(self.report_path, "a") as fh:
            fh.write(line + "\n")

    # -- public ----------------------------------------------------
    def check(self, ip, path):
        """True when this request must be refused (403). Records a
        signature offense for the address as a side effect and
        starts a block when the rules say so. Never raises."""
        if not self.enabled or not ip:
            return False
        try:
            with self._lock:
                return self._check_locked(ip, path)
        except Exception:
            return False

    def _check_locked(self, ip, path):
        if self._allowed(ip):
            return False
        now = self.clock()
        rec = self._ips.setdefault(
            ip, {"offenses": [], "blocked_until": None})
        blocked_until = rec.get("blocked_until")
        if blocked_until:
            if datetime.fromisoformat(blocked_until) > now:
                return True
            rec["blocked_until"] = None
        hit = _classify(path)
        if not hit:
            return False
        severity, why = hit
        cutoff = now - WINDOW
        rec["offenses"] = [
            o for o in rec["offenses"]
            if datetime.fromisoformat(o["ts"]) >= cutoff]
        rec["offenses"].append(
            {"ts": now.isoformat(), "rule": why,
             "path": (path or "")[:120]})
        if severity == "severe" or len(rec["offenses"]) >= STRIKES:
            rec["blocked_until"] = (now + BLOCK_FOR).isoformat()
            rules = sorted({o["rule"] for o in rec["offenses"]})
            try:
                self._report(
                    f"[{now.isoformat()}] NEW BLOCK {ip} until "
                    f"{rec['blocked_until']} — {'; '.join(rules)}")
            except Exception:
                pass
            self._save()
            return True
        self._save()
        return False

    def blocked_count(self):
        if not self.enabled:
            return 0
        try:
            now = self.clock()
            with self._lock:
                return sum(
                    1 for rec in self._ips.values()
                    if rec.get("blocked_until")
                    and datetime.fromisoformat(rec["blocked_until"]) > now)
        except Exception:
            return 0
