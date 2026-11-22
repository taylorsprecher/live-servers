#!/usr/bin/env python3
"""Tests for app_pkg/bug_guard.py — run: python3 test_bug_guard.py
Plain asserts, stdlib only, temp dirs; no app imports beyond the
guard module itself (config is not consulted: paths and the
enabled flag are constructor arguments)."""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app_pkg.bug_guard import BugGuard  # noqa: E402

NOW = [datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)]


def clock():
    return NOW[0]


def make(enabled=True):
    d = tempfile.mkdtemp()
    return BugGuard(enabled, os.path.join(d, "state.json"),
                    os.path.join(d, "report.log"), clock=clock), d


def test_normal_traffic_never_blocked():
    g, _ = make()
    for path in ["/members/", "/members/buy/ccce",
                 "/members/login", "/members/assets/fleet.css",
                 "/members/health", "/members/buy/emspr?x=1"]:
        assert g.check("198.51.100.23", path) is False, path
    assert g.blocked_count() == 0


def test_scanner_three_strikes():
    g, _ = make()
    ip = "203.0.113.66"
    assert g.check(ip, "/wp-login.php") is False
    assert g.check(ip, "/xmlrpc.php") is False
    assert g.check(ip, "/phpmyadmin") is True  # 3rd strike blocks
    assert g.check(ip, "/members/") is True   # block sticks
    assert g.blocked_count() == 1


def test_credential_hunt_instant_block():
    g, d = make()
    assert g.check("192.0.2.99", "/.env") is True
    report = open(os.path.join(d, "report.log")).read()
    assert "NEW BLOCK 192.0.2.99" in report
    assert "credential hunt: /.env" in report


def test_allowlist_never_blocked():
    g, _ = make()
    for ip in ["127.0.0.1", "100.97.245.119", "100.64.1.1"]:
        assert g.check(ip, "/.env") is False, ip
    assert g.blocked_count() == 0


def test_disabled_guard_is_noop():
    g, _ = make(enabled=False)
    assert g.check("192.0.2.99", "/.env") is False
    assert g.blocked_count() == 0


def test_block_persists_and_expires():
    g, d = make()
    assert g.check("192.0.2.99", "/.env") is True
    g2 = BugGuard(True, os.path.join(d, "state.json"),
                  os.path.join(d, "report.log"), clock=clock)
    assert g2.check("192.0.2.99", "/members/") is True  # reloaded
    NOW[0] = NOW[0] + timedelta(days=8)                # block lapses
    assert g2.check("192.0.2.99", "/members/") is False
    NOW[0] = NOW[0] - timedelta(days=8)


def test_strikes_expire_with_window():
    g, _ = make()
    ip = "203.0.113.66"
    assert g.check(ip, "/wp-login.php") is False
    NOW[0] = NOW[0] + timedelta(hours=25)   # first strike ages out
    assert g.check(ip, "/xmlrpc.php") is False
    assert g.check(ip, "/phpmyadmin") is False  # only 2 in window
    assert g.blocked_count() == 0
    NOW[0] = NOW[0] - timedelta(hours=25)


if __name__ == "__main__":
    test_normal_traffic_never_blocked()
    test_scanner_three_strikes()
    test_credential_hunt_instant_block()
    test_allowlist_never_blocked()
    test_disabled_guard_is_noop()
    test_block_persists_and_expires()
    test_strikes_expire_with_window()
    print("test_bug_guard: 7/7 passed")
