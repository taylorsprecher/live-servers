#!/usr/bin/env python3
"""Unit tests for vpn_dns_filter: packet parsing, suffix
blocking, sinkhole responses, and the forwarding path against
a fake upstream. The iptables supervisor is verified live by
--selftest in the deploy (it needs the real namespace).
Run: python3 -m unittest test_vpn_dns_filter -v
"""
import socket
import struct
import threading
import unittest

import vpn_dns_filter as f


def make_query(name, qtype=1, qid=0x1234):
    labels = b"".join(bytes([len(x)]) + x.encode()
                      for x in name.split(".")) + b"\x00"
    return (struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
            + labels + struct.pack(">HH", qtype, 1))


class FakeUDPSocket:
    """Stands in for socket.socket in forward_udp: records what
    would be sent upstream and returns one canned A record.
    (This sandbox forbids real UDP even on loopback.)"""

    instances = []

    def __init__(self, *a, **kw):
        self.sent = []
        FakeUDPSocket.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, _t):
        pass

    def close(self):
        pass

    def sendto(self, data, addr):
        self.sent.append((data, addr))
        _name, _t, qend = f.parse_question(data)
        self.reply = (data[0:2]
                      + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0)
                      + data[12:qend]
                      + b"\xc0\x0c"
                      + struct.pack(">HHIH", 1, 1, 60, 4)
                      + socket.inet_aton("203.0.113.7"))

    def recvfrom(self, _n):
        return self.reply, ("127.0.0.1", 53)


class ParseTests(unittest.TestCase):
    def test_parse_question(self):
        q = make_query("sub.example.com", 28)
        name, qtype, qend = f.parse_question(q)
        self.assertEqual(name, "sub.example.com")
        self.assertEqual(qtype, 28)
        self.assertEqual(qend, len(q))

    def test_parse_garbage(self):
        self.assertIsNone(f.parse_question(b"\x00\x01"))
        self.assertIsNone(f.parse_question(b""))


class BlocklistTests(unittest.TestCase):
    def test_exact_and_subdomain(self):
        self.assertTrue(f.is_blocked("doubleclick.net"))
        self.assertTrue(f.is_blocked("ads.doubleclick.net"))
        self.assertTrue(f.is_blocked("WWW.Google-Analytics.com"))

    def test_real_domains_pass(self):
        for d in ("example.com", "apple.com", "oregon.gov",
                  "graph.facebook.com", "mail.google.com",
                  "notdoubleclick.net", "doubleclick.net.evil.io"):
            self.assertFalse(f.is_blocked(d), d)


class SinkholeTests(unittest.TestCase):
    def test_a_record_zero(self):
        q = make_query("doubleclick.net", 1)
        name, qtype, qend = f.parse_question(q)
        resp = f.sinkhole_response(q, qtype, qend)
        qid, flags, qd, an = struct.unpack(">HHHH", resp[0:8])
        self.assertEqual((qid, qd, an), (0x1234, 1, 1))
        self.assertTrue(flags & 0x8000)
        self.assertEqual(resp[-4:], socket.inet_aton("0.0.0.0"))

    def test_aaaa_zero_and_other_types_empty(self):
        q = make_query("doubleclick.net", 28)
        _n, _t, qend = f.parse_question(q)
        resp = f.sinkhole_response(q, 28, qend)
        self.assertEqual(resp[-16:],
                         socket.inet_pton(socket.AF_INET6, "::"))
        q2 = make_query("doubleclick.net", 15)
        _n, _t, qend2 = f.parse_question(q2)
        resp2 = f.sinkhole_response(q2, 15, qend2)
        self.assertEqual(struct.unpack(">H", resp2[6:8])[0], 0)


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.saved_upstreams = f.UPSTREAMS
        f.UPSTREAMS = ["127.0.0.1"]
        self.real_socket = socket.socket
        socket.socket = FakeUDPSocket
        FakeUDPSocket.instances = []

    def tearDown(self):
        f.UPSTREAMS = self.saved_upstreams
        socket.socket = self.real_socket

    def test_blocked_never_reaches_upstream(self):
        q = make_query("ads.doubleclick.net", 1)
        resp = f.handle_query(q, "10.8.0.2")
        self.assertIsNotNone(resp)
        self.assertEqual(resp[-4:], socket.inet_aton("0.0.0.0"))
        self.assertEqual(FakeUDPSocket.instances, [])

    def test_normal_query_forwarded_by_caller(self):
        q = make_query("example.com", 1)
        # handle_query returns None for non-blocked: the UDP
        # caller then forwards — exercise that exact path.
        self.assertIsNone(f.handle_query(q, "10.8.0.2"))
        resp = f.forward_udp(q)
        self.assertEqual(resp[-4:],
                         socket.inet_aton("203.0.113.7"))
        sent = FakeUDPSocket.instances[0].sent
        self.assertEqual(sent[0][1], ("127.0.0.1", 53))


if __name__ == "__main__":
    unittest.main()
