#!/usr/bin/env python3
"""Stripe go-live tests for the member site (stdlib unittest only).

Run from this directory:  python3 -m unittest test_stripe -v
(or: python3 test_stripe.py)

Covers what the 2026-11-12 Stripe decision depends on:
  * Checkout sessions use automatic payment methods (no
    payment_method_types restriction, so card / Google Pay / Apple
    Pay surface per the Stripe account settings), charge $15.00 /
    $6,000.00, and return to https://members.911transedu.net.
  * With no key the checkout reports "not_configured"; with a fake
    key it fails gracefully (an error string, never an exception).
  * The webhook verifies the Stripe signature, grants the right
    product on checkout.session.completed only when paid, and is
    idempotent on replay.
  * Buyer-facing copy: while no key exists, pages show Cash App as
    the way to buy and card as "not live yet" (no wallet claims);
    while a key exists, buy pages and the desk present paying by
    card, Google Pay or Apple Pay via Stripe Checkout, with Cash App
    still offered.
No real Stripe key is used anywhere in this file.
"""
import hashlib
import hmac
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

_TMP = tempfile.mkdtemp(prefix="members-test-")
os.environ["MEMBERS_DB_PATH"] = os.path.join(_TMP, "members.db")
os.environ["CONTENT_DIR"] = os.path.join(_TMP, "content")
os.environ["UPLOADS_DIR"] = os.path.join(_TMP, "uploads")
os.environ["PUBLIC_BASE_URL"] = "https://members.911transedu.net"
os.environ.pop("STRIPE_SECRET_KEY", None)
os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
os.environ.pop("BTC_RECEIVE_ADDRESS", None)

_spec = importlib.util.spec_from_file_location(
    "member_app", os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)
app.init_db()

WEBHOOK_SECRET = "whsec_test_secret_for_tests"


def make_member(email):
    salt, pw_hash = app.hash_password("password123")
    with app._lock, app._db:
        cur = app._db.execute(
            "INSERT INTO members (name, email, pw_salt, pw_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Test Buyer", email, salt, pw_hash, int(time.time())),
        )
        member_id = cur.lastrowid
    return app.member_for_token(app.create_session(member_id))


def sign(payload_bytes, secret=WEBHOOK_SECRET, ts=None):
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("utf-8") + payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return f"t={ts},v1={mac}"


def completed_event(event_id, session_id, member_id, course_key,
                    payment_status="paid"):
    course = app.COURSE_BY_KEY.get(course_key, {"cents": 0})
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": session_id,
                "payment_status": payment_status,
                "amount_total": course["cents"],
                "currency": "usd",
                "metadata": {
                    "member_id": str(member_id),
                    "course_key": course_key,
                },
            }
        },
    }


class ServerFixture(unittest.TestCase):
    """One local server on an ephemeral port for the whole class."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url(path), timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def post_webhook(self, payload_bytes, sig_header):
        req = urllib.request.Request(
            self.url("/members/stripe/webhook"),
            data=payload_bytes,
            headers={"Stripe-Signature": sig_header},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")


class AmountsTest(unittest.TestCase):
    def test_course_prices(self):
        self.assertEqual(app.COURSE_BY_KEY["ccce"]["cents"], 1500)
        self.assertEqual(app.COURSE_BY_KEY["emspr"]["cents"], 600000)


class CheckoutParamsTest(unittest.TestCase):
    def setUp(self):
        self._key = app.STRIPE_SECRET_KEY
        app.STRIPE_SECRET_KEY = "sk_test_fake_for_params"
        self._urlopen = app.urllib.request.urlopen
        self.captured = {}

        captured = self.captured

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"url": "https://checkout.stripe.com/c/pay/test"}
                ).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data.decode("utf-8")
            captured["auth"] = req.headers.get("Authorization")
            return FakeResp()

        app.urllib.request.urlopen = fake_urlopen

    def tearDown(self):
        app.STRIPE_SECRET_KEY = self._key
        app.urllib.request.urlopen = self._urlopen

    def test_params_for_both_products(self):
        member = {"id": 7, "email": "buyer@example.com"}
        for key, cents in (("ccce", "1500"), ("emspr", "600000")):
            url, err = app.stripe_create_checkout(member, app.COURSE_BY_KEY[key])
            self.assertIsNone(err)
            self.assertEqual(url, "https://checkout.stripe.com/c/pay/test")
            self.assertEqual(self.captured["auth"], "Bearer sk_test_fake_for_params")
            qs = urllib.parse.parse_qs(self.captured["data"])
            # Automatic payment methods: nothing restricts the types,
            # so wallets enabled on the account can surface.
            self.assertFalse(
                any(k.startswith("payment_method_types") for k in qs),
                f"payment_method_types must not be sent: {sorted(qs)}",
            )
            self.assertEqual(qs["line_items[0][price_data][unit_amount]"], [cents])
            self.assertEqual(qs["metadata[member_id]"], ["7"])
            self.assertEqual(qs["metadata[course_key]"], [key])
            self.assertEqual(
                qs["success_url"],
                [f"https://members.911transedu.net/members/desk?paid={key}"],
            )
            self.assertEqual(
                qs["cancel_url"],
                [f"https://members.911transedu.net/members/desk?cancelled={key}"],
            )


class CheckoutConfigTest(unittest.TestCase):
    def test_not_configured_without_key(self):
        old = app.STRIPE_SECRET_KEY
        app.STRIPE_SECRET_KEY = ""
        try:
            url, err = app.stripe_create_checkout(
                {"id": 1, "email": "a@b.c"}, app.COURSE_BY_KEY["ccce"]
            )
        finally:
            app.STRIPE_SECRET_KEY = old
        self.assertIsNone(url)
        self.assertEqual(err, "not_configured")

    def test_fake_key_fails_gracefully(self):
        old = app.STRIPE_SECRET_KEY
        app.STRIPE_SECRET_KEY = "sk_test_0000000000000000000000000000invalid"
        try:
            url, err = app.stripe_create_checkout(
                {"id": 1, "email": "a@b.c"}, app.COURSE_BY_KEY["ccce"]
            )
        finally:
            app.STRIPE_SECRET_KEY = old
        self.assertIsNone(url)
        self.assertTrue(err)
        self.assertNotEqual(err, "not_configured")


class SignatureTest(unittest.TestCase):
    def test_valid_signature(self):
        payload = b'{"id":"evt_1"}'
        self.assertTrue(
            app.stripe_verify_signature(payload, sign(payload), WEBHOOK_SECRET)
        )

    def test_tampered_payload_rejected(self):
        payload = b'{"id":"evt_1"}'
        header = sign(payload)
        self.assertFalse(
            app.stripe_verify_signature(b'{"id":"evt_2"}', header, WEBHOOK_SECRET)
        )

    def test_wrong_secret_rejected(self):
        payload = b'{"id":"evt_1"}'
        self.assertFalse(
            app.stripe_verify_signature(payload, sign(payload), "whsec_other")
        )

    def test_stale_timestamp_rejected(self):
        payload = b'{"id":"evt_1"}'
        header = sign(payload, ts=int(time.time()) - 3600)
        self.assertFalse(
            app.stripe_verify_signature(payload, header, WEBHOOK_SECRET)
        )

    def test_garbage_header_rejected(self):
        self.assertFalse(
            app.stripe_verify_signature(b"{}", "not-a-signature", WEBHOOK_SECRET)
        )
        self.assertFalse(app.stripe_verify_signature(b"{}", "", WEBHOOK_SECRET))
        self.assertFalse(app.stripe_verify_signature(b"{}", sign(b"{}"), ""))


class WebhookHTTPTest(ServerFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._wh = app.STRIPE_WEBHOOK_SECRET
        app.STRIPE_WEBHOOK_SECRET = WEBHOOK_SECRET

    @classmethod
    def tearDownClass(cls):
        app.STRIPE_WEBHOOK_SECRET = cls._wh
        super().tearDownClass()

    def payment_count(self, member_id):
        with app._lock:
            row = app._db.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE member_id = ?",
                (member_id,),
            ).fetchone()
        return row["n"]

    def test_completed_paid_grants_right_product_and_logs(self):
        member = make_member("wh-grant@example.com")
        evt = completed_event("evt_g1", "cs_test_g1", member["id"], "ccce")
        payload = json.dumps(evt).encode("utf-8")
        code, _ = self.post_webhook(payload, sign(payload))
        self.assertEqual(code, 200)
        self.assertEqual(app.member_access(member["id"]), {"ccce"})
        self.assertEqual(self.payment_count(member["id"]), 1)
        with app._lock:
            row = app._db.execute(
                "SELECT * FROM payments WHERE member_id = ?", (member["id"],)
            ).fetchone()
        self.assertEqual(row["amount_cents"], 1500)
        self.assertEqual(row["course_key"], "ccce")

    def test_replay_is_idempotent(self):
        member = make_member("wh-replay@example.com")
        evt = completed_event("evt_r1", "cs_test_r1", member["id"], "emspr")
        payload = json.dumps(evt).encode("utf-8")
        for _ in range(2):
            code, _ = self.post_webhook(payload, sign(payload))
            self.assertEqual(code, 200)
        self.assertEqual(app.member_access(member["id"]), {"emspr"})
        self.assertEqual(self.payment_count(member["id"]), 1)

    def test_bad_signature_no_grant(self):
        member = make_member("wh-bad@example.com")
        evt = completed_event("evt_b1", "cs_test_b1", member["id"], "ccce")
        payload = json.dumps(evt).encode("utf-8")
        code, _ = self.post_webhook(payload, "t=1,v1=deadbeef")
        self.assertEqual(code, 400)
        self.assertEqual(app.member_access(member["id"]), set())
        self.assertEqual(self.payment_count(member["id"]), 0)

    def test_unpaid_no_grant(self):
        member = make_member("wh-unpaid@example.com")
        evt = completed_event(
            "evt_u1", "cs_test_u1", member["id"], "ccce", payment_status="unpaid"
        )
        payload = json.dumps(evt).encode("utf-8")
        code, _ = self.post_webhook(payload, sign(payload))
        self.assertEqual(code, 200)
        self.assertEqual(app.member_access(member["id"]), set())
        self.assertEqual(self.payment_count(member["id"]), 0)

    def test_unknown_course_no_grant(self):
        member = make_member("wh-unknown@example.com")
        evt = completed_event("evt_k1", "cs_test_k1", member["id"], "nope")
        payload = json.dumps(evt).encode("utf-8")
        code, _ = self.post_webhook(payload, sign(payload))
        self.assertEqual(code, 200)
        self.assertEqual(app.member_access(member["id"]), set())
        self.assertEqual(self.payment_count(member["id"]), 0)


class CopyTest(unittest.TestCase):
    def setUp(self):
        self._key = app.STRIPE_SECRET_KEY
        self.member = make_member(f"copy-{time.time_ns()}@example.com")

    def tearDown(self):
        app.STRIPE_SECRET_KEY = self._key

    def test_no_key_copy_unchanged(self):
        app.STRIPE_SECRET_KEY = ""
        buy = app.buy_page(app.COURSE_BY_KEY["ccce"], None)
        self.assertIn("$taylorsprecher2026", buy)
        self.assertNotIn("being switched on", buy)
        self.assertNotIn("not live yet", buy)
        self.assertNotIn("Google Pay", buy)
        self.assertNotIn("/checkout", buy)
        self.assertIn("not live", app.preview_page())
        self.assertIn("not live yet", app.home_page())
        desk = app.desk_page(self.member)
        self.assertNotIn("not live yet", desk)
        self.assertNotIn("card checkout", desk)
        self.assertNotIn("accepted at checkout", desk)

    def test_with_key_wallet_copy(self):
        app.STRIPE_SECRET_KEY = "sk_test_fake_for_copy"
        buy_member = app.buy_page(app.COURSE_BY_KEY["ccce"], self.member)
        self.assertIn("Or pay by card, Google Pay or Apple Pay", buy_member)
        self.assertIn('action="/members/checkout"', buy_member)
        self.assertIn("unlocks automatically", buy_member)
        self.assertIn("$taylorsprecher2026", buy_member)  # Cash App stays
        self.assertNotIn("not live yet", buy_member)
        buy_anon = app.buy_page(app.COURSE_BY_KEY["emspr"], None)
        self.assertIn("pay by card, Google Pay or Apple Pay at Stripe Checkout", buy_anon)
        desk = app.desk_page(self.member)
        self.assertIn(
            "Card, Google Pay and Apple Pay are accepted at checkout", desk
        )
        self.assertIn("Buy by card", desk)
        self.assertIn("Pay with Cash App", desk)  # alternative remains


class PagesSmokeTest(ServerFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._key = app.STRIPE_SECRET_KEY
        app.STRIPE_SECRET_KEY = ""

    @classmethod
    def tearDownClass(cls):
        app.STRIPE_SECRET_KEY = cls._key
        super().tearDownClass()

    def test_pages_200_and_cashapp_operative(self):
        for path in ("/members/", "/members/preview", "/members/buy/ccce",
                     "/members/buy/emspr", "/members/health"):
            code, body = self.get(path)
            self.assertEqual(code, 200, path)
            if "buy" in path:
                self.assertIn("$taylorsprecher2026", body)
                self.assertNotIn("not live yet", body)
                self.assertNotIn("being switched on", body)


if __name__ == "__main__":
    unittest.main()
