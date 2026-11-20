"""HTTP dispatch — the route table transcribed verbatim from the
monolith's Handler.do_GET / Handler.do_POST / Handler.webhook.
The only addition is GET {BASE}/assets/fleet.css (DESIGN.md §5)."""

import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .config import ADMIN_TOKEN, BASE, MERCHANT_ID, WEBHOOK_SECRET
from .ledger import (
    bin_lookup,
    estimate_fees,
    get_intent,
    inbox_insert,
    ingest_settlement_csv,
    record_outcome,
    route_decision,
    run_reconciliation,
)
from .pages import STATUS_PAGE, fleet_css_bytes
from .processor import (
    generate_clearing,
    iso_pack,
    log_iso,
    next_stan,
    sandbox_authorize,
    sandbox_reverse,
)
from .store import _lock, db, new_uuid


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- plumbing ------------------------------------------------------
    def send_json(self, obj, status=200):
        data = json.dumps(obj, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, html, status=200):
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_css(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body_bytes(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def body_json(self):
        raw = self.body_bytes()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def admin_ok(self):
        if not ADMIN_TOKEN:
            return True
        return hmac.compare_digest(
            self.headers.get("X-Admin-Token", ""), ADMIN_TOKEN
        )

    # -- GET -----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == BASE + "/assets/fleet.css":
                return self.send_css(fleet_css_bytes())
            if path in (BASE, BASE + "/"):
                return self.send_html(STATUS_PAGE)
            if path == BASE + "/health":
                conn = db()
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) AS n FROM psps")
                    n = cur.fetchone()["n"]
                conn.commit()
                return self.send_json({
                    "status": "ok", "db": True, "psps_registered": n,
                    "processor_connected": False,
                })
            if path == BASE + "/balances":
                conn = db()
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT la.code, la.account_type, la.currency,
                               COALESCE(SUM(CASE WHEN le.direction = la.normal_balance
                                                 THEN le.amount ELSE -le.amount END), 0) AS balance
                        FROM ledger_accounts la
                        LEFT JOIN ledger_entries le ON le.account_id = la.id
                        GROUP BY la.code, la.account_type, la.currency
                        ORDER BY la.code
                        """
                    )
                    rows = cur.fetchall()
                conn.commit()
                return self.send_json({"accounts": rows})
            if path == BASE + "/exceptions":
                qs = parse_qs(urlparse(self.path).query)
                status = qs.get("status", ["OPEN"])[0]
                conn = db()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM reconciliation_exceptions WHERE status = %s "
                        "ORDER BY created_at DESC LIMIT 200",
                        (status,),
                    )
                    rows = cur.fetchall()
                conn.commit()
                return self.send_json({"exceptions": rows})
            if path == BASE + "/recon/status":
                conn = db()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT recon_status, COUNT(*) AS n FROM external_settlement_lines "
                        "GROUP BY recon_status"
                    )
                    lines = {r["recon_status"]: r["n"] for r in cur.fetchall()}
                    cur.execute(
                        "SELECT status, COUNT(*) AS n FROM inbound_events GROUP BY status"
                    )
                    inbox = {r["status"]: r["n"] for r in cur.fetchall()}
                    cur.execute("SELECT COUNT(*) AS n FROM reconciliation_matches")
                    matches = cur.fetchone()["n"]
                conn.commit()
                return self.send_json({
                    "settlement_lines": lines, "inbox": inbox, "matches": matches,
                })
            if path == BASE + "/processor/messages":
                qs = parse_qs(urlparse(self.path).query)
                conn = db()
                with conn.cursor() as cur:
                    if qs.get("rrn"):
                        cur.execute(
                            "SELECT id, rrn, stan, mti, direction, parsed_de, latency_ms, created_at "
                            "FROM iso8583_message_log WHERE rrn = %s ORDER BY created_at",
                            (qs["rrn"][0],),
                        )
                    else:
                        cur.execute(
                            "SELECT id, rrn, stan, mti, direction, parsed_de, latency_ms, created_at "
                            "FROM iso8583_message_log ORDER BY created_at DESC LIMIT 100"
                        )
                    rows = cur.fetchall()
                conn.commit()
                return self.send_json({"messages": rows})
            if path == BASE + "/processor/clearing":
                qs = parse_qs(urlparse(self.path).query)
                conn = db()
                with conn.cursor() as cur:
                    if qs.get("id"):
                        cur.execute(
                            "SELECT * FROM clearing_batches WHERE id = %s",
                            (qs["id"][0],),
                        )
                        batch = cur.fetchone()
                        conn.commit()
                        if batch is None:
                            return self.send_json({"error": "batch not found"}, 404)
                        return self.send_json({"batch": batch})
                    cur.execute(
                        "SELECT id, network, file_name, record_count, gross_total, status, "
                        "created_at, transmitted_at FROM clearing_batches ORDER BY created_at DESC"
                    )
                    rows = cur.fetchall()
                conn.commit()
                return self.send_json({"batches": rows})
            if path == BASE + "/processor/fbo-wires":
                conn = db()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM fbo_settlement_wires ORDER BY created_at DESC LIMIT 100"
                    )
                    rows = cur.fetchall()
                conn.commit()
                return self.send_json({"wires": rows})
            if path.startswith(BASE + "/intents/"):
                intent_id = path[len(BASE + "/intents/"):]
                conn = db()
                with conn.cursor() as cur:
                    intent = get_intent(cur, intent_id)
                    if intent is None:
                        conn.commit()
                        return self.send_json({"error": "intent not found"}, 404)
                    cur.execute(
                        "SELECT * FROM payment_attempts WHERE payment_intent_id = %s ORDER BY created_at",
                        (intent_id,),
                    )
                    attempts = cur.fetchall()
                    cur.execute(
                        "SELECT * FROM payment_state_transitions WHERE payment_intent_id = %s ORDER BY id",
                        (intent_id,),
                    )
                    transitions = cur.fetchall()
                conn.commit()
                return self.send_json({
                    "intent": intent, "attempts": attempts, "transitions": transitions,
                })
            return self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            try:
                db().rollback()
            except Exception:
                pass
            return self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # -- POST ----------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path.startswith(BASE + "/webhooks/"):
                return self.webhook(path[len(BASE + "/webhooks/"):])
            if not self.admin_ok():
                return self.send_json({"error": "admin token required"}, 401)

            if path == BASE + "/intents":
                data = self.body_json()
                conn = db()
                with _lock, conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM payment_intents WHERE idempotency_key = %s",
                        (data["idempotency_key"],),
                    )
                    existing = cur.fetchone()
                    if existing:
                        conn.commit()
                        return self.send_json({"intent": existing, "deduplicated": True})
                    intent_id = new_uuid()
                    cur.execute(
                        "INSERT INTO payment_intents (id, idempotency_key, merchant_id, amount, currency, status) "
                        "VALUES (%s, %s, %s, %s, %s, 'CREATED') RETURNING *",
                        (intent_id, data["idempotency_key"],
                         data.get("merchant_id") or MERCHANT_ID,
                         int(data["amount"]), (data.get("currency") or "USD").upper()),
                    )
                    intent = cur.fetchone()
                    cur.execute(
                        "INSERT INTO payment_state_transitions (payment_intent_id, from_status, to_status) "
                        "VALUES (%s, NULL, 'CREATED')",
                        (intent_id,),
                    )
                conn.commit()
                return self.send_json({"intent": intent, "deduplicated": False}, 201)

            if path == BASE + "/attempts":
                data = self.body_json()
                conn = db()
                with _lock, conn.cursor() as cur:
                    attempt_id = new_uuid()
                    cur.execute(
                        "INSERT INTO payment_attempts (id, payment_intent_id, attempt_type, psp_name, "
                        "psp_transaction_id, status, features) VALUES (%s, %s, %s, %s, %s, 'PENDING', %s) "
                        "RETURNING *",
                        (attempt_id, data["payment_intent_id"], data["attempt_type"],
                         data["psp_name"], data.get("psp_transaction_id"),
                         json.dumps(data.get("features") or {})),
                    )
                    attempt = cur.fetchone()
                conn.commit()
                return self.send_json({"attempt": attempt}, 201)

            if path == BASE + "/attempts/outcome":
                data = self.body_json()
                conn = db()
                with _lock, conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM payment_attempts WHERE id = %s", (data["attempt_id"],)
                    )
                    attempt = cur.fetchone()
                    if attempt is None:
                        conn.commit()
                        return self.send_json({"error": "attempt not found"}, 404)
                    guidance = record_outcome(
                        cur, attempt, data["outcome"],
                        data.get("raw_response_code"),
                        data.get("card_fingerprint"),
                    )
                conn.commit()
                return self.send_json({"guidance": guidance})

            if path == BASE + "/settlement-files":
                qs = parse_qs(urlparse(self.path).query)
                filename = qs.get("filename", ["settlement.csv"])[0]
                body = self.body_bytes()
                report = ingest_settlement_csv(filename, body)
                return self.send_json(report)

            if path == BASE + "/fees/estimate":
                data = self.body_json()
                conn = db()
                with _lock, conn.cursor() as cur:
                    if data.get("bin"):
                        bin_row = bin_lookup(cur, data["bin"])
                        if bin_row:
                            for k in ("network", "funding_type", "card_product",
                                      "issuer_country", "is_regulated"):
                                data.setdefault(k, bin_row[k])
                    breakdown = estimate_fees(
                        cur, data, persist_attempt_id=data.get("attempt_id")
                    )
                conn.commit()
                return self.send_json({"estimate": breakdown})

            if path == BASE + "/route":
                data = self.body_json()
                conn = db()
                with _lock, conn.cursor() as cur:
                    intent = get_intent(cur, data["payment_intent_id"])
                    if intent is None:
                        conn.commit()
                        return self.send_json({"error": "intent not found"}, 404)
                    plan = route_decision(cur, intent, data)
                conn.commit()
                if plan is None:
                    return self.send_json(
                        {"error": "no eligible PSP (constraints or circuit breakers)"}, 422
                    )
                return self.send_json({"routing": plan})

            if path == BASE + "/recon/run":
                conn = db()
                with _lock, conn.cursor() as cur:
                    report = run_reconciliation(cur)
                conn.commit()
                return self.send_json({"reconciliation": report})

            if path == BASE + "/processor/stan":
                data = self.body_json()
                conn = db()
                with _lock, conn.cursor() as cur:
                    stan = next_stan(
                        cur, str(data.get("acquirer_bin") or "412345"),
                        (data.get("network") or "VISA").upper(),
                    )
                conn.commit()
                return self.send_json({"stan": stan})

            if path == BASE + "/processor/authorize":
                data = self.body_json()
                conn = db()
                with _lock, conn.cursor() as cur:
                    result = sandbox_authorize(cur, data)
                conn.commit()
                return self.send_json({"authorization": result})

            if path == BASE + "/processor/reverse":
                data = self.body_json()
                conn = db()
                with _lock, conn.cursor() as cur:
                    result = sandbox_reverse(cur, data["payment_intent_id"])
                conn.commit()
                if result is None:
                    return self.send_json({"error": "intent not found"}, 404)
                return self.send_json({"reversal": result})

            if path == BASE + "/processor/echo":
                data = self.body_json()
                import datetime

                conn = db()
                with _lock, conn.cursor() as cur:
                    stan = next_stan(
                        cur, str(data.get("acquirer_bin") or "412345"),
                        (data.get("network") or "VISA").upper(),
                    )
                    now = datetime.datetime.now()
                    fields = {7: now.strftime("%m%d%H%M%S"), 11: stan, 70: "301"}
                    raw = iso_pack("0800", fields)
                    log_iso(cur, "0800", "OUT", fields, raw)
                    resp = dict(fields)
                    resp[39] = "00"
                    raw_in = iso_pack("0810", resp)
                    log_iso(cur, "0810", "IN", resp, raw_in, 12)
                conn.commit()
                return self.send_json({
                    "echo": "ok", "stan": stan, "simulated": True,
                    "note": "no real network socket exists; sign-on/echo is simulated",
                })

            if path == BASE + "/processor/clearing/generate":
                data = self.body_json()
                conn = db()
                with _lock, conn.cursor() as cur:
                    batch = generate_clearing(cur, data.get("network") or "VISA")
                    batch.pop("file_body", None)
                conn.commit()
                return self.send_json({"batch": batch}, 201)

            if path == BASE + "/processor/clearing/transmit":
                data = self.body_json()
                conn = db()
                with _lock, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE clearing_batches SET status = 'ACKED', transmitted_at = NOW() "
                        "WHERE id = %s AND status = 'GENERATED' RETURNING id, status",
                        (data["batch_id"],),
                    )
                    row = cur.fetchone()
                conn.commit()
                if row is None:
                    return self.send_json(
                        {"error": "batch not found or not GENERATED"}, 404
                    )
                return self.send_json({
                    "batch": row, "simulated": True,
                    "note": "no network endpoint exists; TRANSMITTED->ACKED simulated in one step",
                })

            if path == BASE + "/processor/fbo-wires":
                data = self.body_json()
                conn = db()
                with _lock, conn.cursor() as cur:
                    # Expected net = gross of ACKED batches for the
                    # network/date minus fee estimates on their items.
                    cur.execute(
                        """
                        SELECT COALESCE(SUM(cb.gross_total), 0) AS gross,
                               COALESCE(SUM(tfe.total_minor), 0) AS fees
                        FROM clearing_batches cb
                        LEFT JOIN clearing_batch_items cbi ON cbi.batch_id = cb.id
                        LEFT JOIN payment_attempts pa
                               ON pa.payment_intent_id = cbi.payment_intent_id
                              AND pa.attempt_type = 'CAPTURE'
                        LEFT JOIN transaction_fee_estimates tfe
                               ON tfe.payment_attempt_id = pa.id
                        WHERE cb.network = %s AND cb.status = 'ACKED'
                          AND cb.created_at::date = %s
                        """,
                        ((data.get("network") or "VISA").upper(),
                         data["settlement_date"]),
                    )
                    agg = cur.fetchone()
                    expected = int(agg["gross"]) - int(agg["fees"])
                    wire_amount = int(data["net_wire_amount"])
                    cur.execute(
                        "INSERT INTO fbo_settlement_wires (id, sponsor_bank_ref, network, "
                        "settlement_date, net_wire_amount, currency, expected_amount, reconciled) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                        (new_uuid(), data["sponsor_bank_ref"],
                         (data.get("network") or "VISA").upper(),
                         data["settlement_date"], wire_amount,
                         (data.get("currency") or "USD").upper(),
                         expected, wire_amount == expected),
                    )
                    wire = cur.fetchone()
                conn.commit()
                return self.send_json({"wire": wire}, 201)

            if path == BASE + "/psps":
                data = self.body_json()
                conn = db()
                with _lock, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO psps (id, name, supported_currencies, supported_networks, "
                        "acquirer_countries, supports_3ds, active) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (name) DO UPDATE SET supported_currencies = EXCLUDED.supported_currencies, "
                        "supported_networks = EXCLUDED.supported_networks, "
                        "acquirer_countries = EXCLUDED.acquirer_countries, "
                        "supports_3ds = EXCLUDED.supports_3ds, active = EXCLUDED.active RETURNING *",
                        (new_uuid(), data["name"], data.get("supported_currencies") or [],
                         data.get("supported_networks") or [],
                         data.get("acquirer_countries") or [],
                         data.get("supports_3ds", True), data.get("active", True)),
                    )
                    psp = cur.fetchone()
                conn.commit()
                return self.send_json({"psp": psp}, 201)

            return self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            try:
                db().rollback()
            except Exception:
                pass
            return self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # -- Webhooks (Stage 1: verify, land raw, acknowledge) --------------
    def webhook(self, source):
        raw = self.body_bytes()
        if WEBHOOK_SECRET:
            sig = self.headers.get("X-Signature-256", "")
            expected = hmac.new(
                WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                return self.send_json({"error": "bad signature"}, 401)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            payload = {"raw": raw.decode("utf-8", "replace")}
        external_id = (
            payload.get("id") or payload.get("event_id")
            or hashlib.sha256(raw).hexdigest()
        )
        conn = db()
        with _lock, conn.cursor() as cur:
            inserted = inbox_insert(cur, source, str(external_id), payload)
        conn.commit()
        return self.send_json({"received": True, "duplicate": not inserted})
