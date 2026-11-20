"""Ledger core: chart-of-accounts posting, the payment-intent
state machine, idempotent ingestion (inbox + settlement files),
the inbox worker, the IC++ fee engine, routing, attempt-outcome
feedback, and reconciliation.

Function-for-function move from the monolith — SQL, account codes,
amount handling, deterministic idempotency keys, and posting logic
are verbatim, never "improved"."""

import hashlib
import json
import random
import time

from .config import MERCHANT_ID
from .store import _lock, db, new_uuid


EEA = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE",
    "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT",
    "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO",
}

TRANSITIONS = {
    "CREATED": {"AUTHORIZING", "FAILED"},
    "AUTHORIZING": {"AUTHORIZED", "CAPTURED", "FAILED"},
    "AUTHORIZED": {"CAPTURING", "VOIDED", "EXPIRED"},
    "CAPTURING": {"CAPTURED", "FAILED"},
    "CAPTURED": {"SETTLED", "REFUNDED"},
    "SETTLED": {"REFUNDED", "DISPUTED"},
}

STATUS_RANK = {
    "CREATED": 0, "AUTHORIZING": 1, "AUTHORIZED": 2, "CAPTURING": 3,
    "CAPTURED": 4, "SETTLED": 5,
}

HARD_DECLINE_CODES = {"51", "54", "14", "59", "41", "43"}
IRREVERSIBLE_CODES = {"14", "41", "43"}
SOFT_DECLINE_CODES = {"05", "91", "96"}
RETRY_PENALTY_MINOR = 5  # $0.05 expected gateway decline fee
SOFT_RETRY_CAP_24H = 2


def account_id(cur, code):
    cur.execute("SELECT id FROM ledger_accounts WHERE code = %s", (code,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"ledger account not seeded: {code}")
    return row["id"]


def post_journal(cur, idempotency_key, payment_intent_id, description, lines):
    """Insert a balanced journal under its deterministic key.
    Returns the ledger_transaction id, or None when the key already
    exists (the economic event was already posted — exactly-once).
    lines: [(account_code, 'DEBIT'|'CREDIT', amount_minor, currency)]"""
    tx_id = new_uuid()
    cur.execute(
        "INSERT INTO ledger_transactions (id, idempotency_key, payment_intent_id, description) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
        (tx_id, idempotency_key, payment_intent_id, description),
    )
    row = cur.fetchone()
    if row is None:
        return None
    for code, direction, amount, currency in lines:
        if amount <= 0:
            continue
        cur.execute(
            "INSERT INTO ledger_entries (id, ledger_transaction_id, account_id, direction, amount, currency) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (new_uuid(), tx_id, account_id(cur, code), direction, amount, currency),
        )
    return tx_id


def move_status(cur, intent, target, trigger_attempt_id=None):
    """Walk the intent to `target` through legal intermediate states
    (BFS over TRANSITIONS), logging every hop. Returns the path taken
    (possibly empty when already there or unreachable)."""
    current = intent["status"]
    if current == target:
        return []
    # BFS for a legal path.
    from collections import deque

    prev = {current: None}
    queue = deque([current])
    while queue:
        node = queue.popleft()
        if node == target:
            break
        for nxt in TRANSITIONS.get(node, set()):
            if nxt not in prev:
                prev[nxt] = node
                queue.append(nxt)
    if target not in prev:
        return []
    path = []
    node = target
    while node != current:
        path.append(node)
        node = prev[node]
    path.reverse()
    frm = current
    for nxt in path:
        cur.execute(
            "UPDATE payment_intents SET status = %s WHERE id = %s",
            (nxt, intent["id"]),
        )
        cur.execute(
            "INSERT INTO payment_state_transitions (payment_intent_id, from_status, to_status, trigger_attempt_id) "
            "VALUES (%s, %s, %s, %s)",
            (intent["id"], frm, nxt, trigger_attempt_id),
        )
        frm = nxt
    intent["status"] = target
    return path


def get_intent(cur, intent_id):
    cur.execute("SELECT * FROM payment_intents WHERE id = %s", (intent_id,))
    return cur.fetchone()


def inbox_insert(cur, source_system, external_event_id, payload):
    """Idempotent raw landing. Returns True when newly inserted."""
    cur.execute(
        "INSERT INTO inbound_events (id, source_system, external_event_id, payload) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (source_system, external_event_id) "
        "DO NOTHING RETURNING id",
        (new_uuid(), source_system, external_event_id, json.dumps(payload)),
    )
    return cur.fetchone() is not None


def row_fingerprint(acquirer, rrn, record_type, amount, settlement_date):
    raw = f"{acquirer}|{rrn}|{record_type}|{amount}|{settlement_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ingest_settlement_csv(filename, body_bytes, source_system="settlement_csv"):
    """File-level sha256 dedupe, then chunked (500) row landing into
    external_settlement_lines + the inbox. Returns a report dict."""
    import csv
    import io

    file_hash = hashlib.sha256(body_bytes).hexdigest()
    conn = db()
    with _lock:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM settlement_batches WHERE file_sha256 = %s", (file_hash,)
            )
            batch = cur.fetchone()
            if batch and batch["status"] == "COMPLETED":
                conn.commit()
                return {"duplicate_file": True, "batch_id": str(batch["id"]), "rows": 0}
            if batch is None:
                batch_id = new_uuid()
                cur.execute(
                    "INSERT INTO settlement_batches (id, filename, file_sha256, source_system, status) "
                    "VALUES (%s, %s, %s, %s, 'PROCESSING')",
                    (batch_id, filename, file_hash, source_system),
                )
            else:
                batch_id = str(batch["id"])
                cur.execute(
                    "UPDATE settlement_batches SET status = 'PROCESSING' WHERE id = %s",
                    (batch_id,),
                )
        conn.commit()

        text = body_bytes.decode("utf-8-sig", "replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        inserted = duplicates = 0
        for start in range(0, len(rows), 500):
            chunk = rows[start:start + 500]
            with conn.cursor() as cur:
                for r in chunk:
                    psp = (r.get("psp_name") or r.get("acquirer_id") or "").strip()
                    rrn = (r.get("network_rrn") or "").strip() or None
                    psp_tx = (r.get("psp_transaction_id") or "").strip() or None
                    rtype = (r.get("record_type") or "SALE").strip().upper()
                    gross = int(r.get("gross_amount") or 0)
                    fee = int(r.get("fee_amount") or 0)
                    currency = (r.get("currency") or "USD").strip().upper()
                    sdate = (r.get("settlement_date") or "").strip()
                    line_id = new_uuid()
                    cur.execute(
                        "INSERT INTO external_settlement_lines "
                        "(id, batch_id, psp_name, network_rrn, psp_transaction_id, record_type, "
                        " gross_amount, fee_amount, net_amount, currency, settlement_date) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (line_id, batch_id, psp, rrn, psp_tx, rtype,
                         gross, fee, gross - fee, currency, sdate),
                    )
                    fp = row_fingerprint(psp, rrn or "", rtype, gross, sdate)
                    payload = {
                        "kind": "settlement_row",
                        "external_line_id": line_id,
                        "psp_name": psp, "network_rrn": rrn,
                        "psp_transaction_id": psp_tx, "record_type": rtype,
                        "gross_amount": gross, "fee_amount": fee,
                        "currency": currency, "settlement_date": sdate,
                    }
                    if inbox_insert(cur, source_system, fp, payload):
                        inserted += 1
                    else:
                        duplicates += 1
            conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE settlement_batches SET status = 'COMPLETED', row_count = %s, "
                "completed_at = NOW() WHERE id = %s",
                (len(rows), batch_id),
            )
        conn.commit()
    return {
        "duplicate_file": False, "batch_id": batch_id,
        "rows": len(rows), "inbox_new": inserted, "inbox_duplicates": duplicates,
    }


def process_event(cur, event):
    """Process one claimed inbox row inside the caller's transaction.
    Canonical payload kinds: authorized, captured, settled, refunded,
    dispute, settlement_row. Returns a short result string."""
    p = event["payload"]
    if isinstance(p, str):
        p = json.loads(p)
    kind = p.get("kind", "")
    currency = (p.get("currency") or "USD").upper()

    if kind == "settlement_row":
        return process_settlement_row(cur, event, p)

    intent_id = p.get("payment_intent_id")
    intent = get_intent(cur, intent_id) if intent_id else None
    if intent is None:
        raise ValueError(f"unknown payment_intent_id: {intent_id}")
    attempt_id = p.get("attempt_id")
    psp = p.get("psp_name") or "unknown"
    rrn = p.get("network_rrn")
    amount = int(p.get("amount") or intent["amount"])
    fee = int(p.get("fee_amount") or 0)

    if attempt_id:
        # The RRN is a lifecycle key: an earlier attempt (e.g. the
        # authorization) may already hold it under the UNIQUE
        # constraint, so only claim it when it is still unclaimed.
        cur.execute(
            "UPDATE payment_attempts SET status = 'SUCCEEDED', "
            "psp_transaction_id = CASE WHEN psp_transaction_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM payment_attempts p2 "
            "WHERE p2.psp_transaction_id = %s) THEN %s "
            "ELSE psp_transaction_id END WHERE id = %s",
            (rrn, rrn, attempt_id),
        )

    if kind == "authorized":
        move_status(cur, intent, "AUTHORIZED", attempt_id)
        return "authorized (state only; no money movement)"

    if kind == "captured":
        tx = post_journal(
            cur, f"capture:{intent_id}", intent_id,
            f"Capture {intent_id} via {psp}",
            [("acquirer_clearing_expected", "DEBIT", amount, currency),
             ("merchant_payable_main", "CREDIT", amount, currency)],
        )
        move_status(cur, intent, "CAPTURED", attempt_id)
        return f"captured; journal {'posted' if tx else 'already present'}"

    if kind == "settled":
        record_type = (p.get("record_type") or "SALE").upper()
        tx = post_journal(
            cur, f"settle:{psp}:{rrn}:{record_type}", intent_id,
            f"Settlement {rrn} via {psp}",
            [("bank_cash", "DEBIT", amount - fee, currency),
             ("psp_processing_fee_expense", "DEBIT", fee, currency),
             ("acquirer_clearing_expected", "CREDIT", amount, currency)],
        )
        move_status(cur, intent, "SETTLED", attempt_id)
        return f"settled; journal {'posted' if tx else 'already present'}"

    if kind == "refunded":
        refund_id = p.get("refund_id") or event["external_event_id"]
        tx = post_journal(
            cur, f"refund:{refund_id}", intent_id,
            f"Refund {refund_id} via {psp}",
            [("merchant_payable_main", "DEBIT", amount, currency),
             ("bank_cash", "CREDIT", amount, currency)],
        )
        move_status(cur, intent, "REFUNDED", attempt_id)
        return f"refunded; journal {'posted' if tx else 'already present'}"

    if kind == "dispute":
        dispute_id = p.get("dispute_id") or event["external_event_id"]
        stage = (p.get("stage") or "CHARGEBACK").upper()
        tx = post_journal(
            cur, f"dispute:{dispute_id}:{stage}", intent_id,
            f"Dispute {dispute_id} stage {stage} via {psp}",
            [("merchant_payable_main", "DEBIT", amount, currency),
             ("bank_cash", "CREDIT", amount, currency)],
        )
        if stage == "CHARGEBACK":
            move_status(cur, intent, "DISPUTED", attempt_id)
        return f"dispute posted; journal {'posted' if tx else 'already present'}"

    raise ValueError(f"unknown event kind: {kind!r}")


def process_settlement_row(cur, event, p):
    """A settlement-file row: match its RRN to a succeeded capture
    attempt, post the settle journal, drive the intent to SETTLED.
    Unknown RRN -> RECONCILIATION_EXCEPTION (the DLQ; the rest of
    the file is never blocked by one bad row)."""
    rrn = p.get("network_rrn")
    psp_tx = p.get("psp_transaction_id")
    attempt = None
    if rrn:
        cur.execute(
            "SELECT * FROM payment_attempts WHERE psp_transaction_id = %s "
            "AND attempt_type = 'CAPTURE' ORDER BY created_at LIMIT 1",
            (rrn,),
        )
        attempt = cur.fetchone()
    if attempt is None and psp_tx:
        cur.execute(
            "SELECT * FROM payment_attempts WHERE psp_transaction_id = %s "
            "ORDER BY created_at LIMIT 1",
            (psp_tx,),
        )
        attempt = cur.fetchone()
    if attempt is None:
        cur.execute(
            "UPDATE inbound_events SET status = 'RECONCILIATION_EXCEPTION', "
            "processed_at = NOW(), last_error = %s WHERE id = %s",
            (f"no internal attempt matches RRN {rrn} / PSP tx {psp_tx}", event["id"]),
        )
        cur.execute(
            "UPDATE external_settlement_lines SET recon_status = 'EXCEPTION' WHERE id = %s",
            (p.get("external_line_id"),),
        )
        return "quarantined: unknown RRN"
    intent = get_intent(cur, attempt["payment_intent_id"])
    gross = int(p["gross_amount"])
    fee = int(p["fee_amount"])
    currency = p["currency"]
    record_type = (p.get("record_type") or "SALE").upper()
    tx = post_journal(
        cur, f"settle:{p['psp_name']}:{rrn}:{record_type}", intent["id"],
        f"Settlement {rrn} via {p['psp_name']} (file row)",
        [("bank_cash", "DEBIT", gross - fee, currency),
         ("psp_processing_fee_expense", "DEBIT", fee, currency),
         ("acquirer_clearing_expected", "CREDIT", gross, currency)],
    )
    move_status(cur, intent, "SETTLED", attempt["id"])
    return f"settled intent {intent['id']}; journal {'posted' if tx else 'already present'}"


def worker_loop():
    while True:
        try:
            conn = db()
            with _lock:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM inbound_events WHERE status = 'PENDING' "
                        "ORDER BY received_at LIMIT 20"
                    )
                    candidates = cur.fetchall()
                for cand in candidates:
                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT * FROM inbound_events WHERE id = %s "
                                "AND status = 'PENDING' FOR UPDATE SKIP LOCKED",
                                (cand["id"],),
                            )
                            event = cur.fetchone()
                            if event is None:
                                conn.commit()
                                continue
                            result = process_event(cur, event)
                            cur.execute(
                                "SELECT status FROM inbound_events WHERE id = %s",
                                (event["id"],),
                            )
                            now_status = cur.fetchone()["status"]
                            if now_status == "PENDING":
                                cur.execute(
                                    "UPDATE inbound_events SET status = 'PROCESSED', "
                                    "processed_at = NOW(), last_error = %s WHERE id = %s",
                                    (result, event["id"]),
                                )
                        conn.commit()
                    except Exception as exc:  # surfaced, never hidden
                        conn.rollback()
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE inbound_events SET status = 'FAILED', "
                                "processed_at = NOW(), last_error = %s WHERE id = %s",
                                (f"{type(exc).__name__}: {exc}", cand["id"]),
                            )
                        conn.commit()
        except Exception:
            try:
                db().rollback()
            except Exception:
                pass
        time.sleep(2)


def region_scope(issuer_country, merchant_country):
    if issuer_country == merchant_country:
        return "DOMESTIC"
    if issuer_country in EEA and merchant_country in EEA:
        return "INTRA_REGIONAL"
    return "INTER_REGIONAL"


def bin_lookup(cur, bin_digits):
    """Longest-prefix (smallest containing range) BIN lookup."""
    if not bin_digits:
        return None
    padded = (str(bin_digits) + "00000000")[:8]
    try:
        value = int(padded)
    except ValueError:
        return None
    cur.execute(
        "SELECT * FROM bin_ranges WHERE bin_range @> %s::bigint "
        "ORDER BY upper(bin_range) - lower(bin_range) ASC LIMIT 1",
        (value,),
    )
    return cur.fetchone()


def criteria_match(criteria, feats):
    """JSONB predicate: every present key must match the features.
    Supported keys: funding_type, card_product, entry_mode,
    region_scope, is_regulated, mcc_list, min_amount,
    requires_qualified (downgrade simulation gate)."""
    if feats.get("_downgraded") and criteria.get("requires_qualified"):
        return False
    for key in ("funding_type", "card_product", "entry_mode", "region_scope"):
        if key in criteria and criteria[key] != feats.get(key):
            return False
    if "is_regulated" in criteria and bool(criteria["is_regulated"]) != bool(feats.get("is_regulated")):
        return False
    if "mcc_list" in criteria and feats.get("mcc") not in criteria["mcc_list"]:
        return False
    if "min_amount" in criteria and feats.get("amount", 0) < criteria["min_amount"]:
        return False
    return True


def estimate_fees(cur, feats, persist_attempt_id=None):
    """Three-layer estimate. feats: amount, currency, network,
    funding_type, card_product, issuer_country, merchant_country,
    mcc, entry_mode, is_3ds_authenticated, capture_delay_hours,
    avs_cvv_passed, psp_name, merchant_id, at (epoch or None).
    Returns (and optionally persists) the itemized breakdown."""
    import datetime

    at = feats.get("at")
    if at is None:
        at_dt = datetime.datetime.now(datetime.timezone.utc)
    elif isinstance(at, (int, float)):
        at_dt = datetime.datetime.fromtimestamp(at, datetime.timezone.utc)
    else:
        at_dt = at
    feats = dict(feats)
    feats["region_scope"] = region_scope(
        feats.get("issuer_country", ""), feats.get("merchant_country", "")
    )
    downgraded = (feats.get("capture_delay_hours") or 0) > 24 or feats.get("avs_cvv_passed") is False
    feats["_downgraded"] = downgraded
    amount = int(feats["amount"])

    def line_fee(rule):
        cur.execute(
            "SELECT calculate_line_fee(%s, %s, %s, %s) AS fee",
            (amount, rule["rate_bps"], rule["fixed_fee_minor"], rule["cap_amount_minor"]),
        )
        return cur.fetchone()["fee"]

    def rules_for(categories):
        cur.execute(
            "SELECT * FROM fee_rules WHERE fee_category = ANY(%s) AND network = %s "
            "AND effective_from <= %s AND (effective_to IS NULL OR %s < effective_to) "
            "ORDER BY priority DESC",
            (categories, feats.get("network"), at_dt, at_dt),
        )
        return [r for r in cur.fetchall() if criteria_match(r["match_criteria"] or {}, feats)]

    # Layer 1 — interchange: single best match.
    interchange = rules_for(["INTERCHANGE"])
    ic_rule = interchange[0] if interchange else None
    ic_fee = line_fee(ic_rule) if ic_rule else 0

    # Layer 2 — scheme assessments + cross-border: additive stack.
    scheme_fee = 0
    scheme_parts = []
    for rule in rules_for(["SCHEME_ASSESSMENT", "CROSS_BORDER"]):
        if rule["fee_category"] == "CROSS_BORDER" and feats["region_scope"] == "DOMESTIC":
            continue
        f = line_fee(rule)
        scheme_fee += f
        scheme_parts.append({"program": rule["program_code"], "fee": f})

    # Layer 3 — processor markup from the merchant's contract.
    markup_fee = 0
    blended_total = None
    cur.execute(
        "SELECT * FROM merchant_contracts WHERE merchant_id = %s AND psp_name = %s "
        "AND effective_from <= %s AND (effective_to IS NULL OR %s < effective_to) "
        "ORDER BY effective_from DESC LIMIT 1",
        (feats.get("merchant_id") or MERCHANT_ID, feats.get("psp_name"), at_dt, at_dt),
    )
    contract = cur.fetchone()
    if contract:
        if contract["contract_type"] == "BLENDED":
            cur.execute(
                "SELECT calculate_line_fee(%s, %s, %s, NULL) AS fee",
                (amount, contract["blended_bps"], contract["blended_fixed_minor"]),
            )
            blended_total = cur.fetchone()["fee"]
            markup_fee = blended_total - ic_fee - scheme_fee
        else:
            cur.execute(
                "SELECT calculate_line_fee(%s, %s, %s, NULL) AS fee",
                (amount, contract["markup_bps"], contract["markup_fixed_minor"]),
            )
            markup_fee = cur.fetchone()["fee"]

    total = blended_total if blended_total is not None else ic_fee + scheme_fee + markup_fee
    breakdown = {
        "interchange_program": ic_rule["program_code"] if ic_rule else None,
        "interchange_minor": ic_fee,
        "scheme_parts": scheme_parts,
        "scheme_minor": scheme_fee,
        "markup_minor": markup_fee,
        "total_minor": total,
        "blended_override": blended_total is not None,
        "downgraded": downgraded,
        "region_scope": feats["region_scope"],
    }
    if persist_attempt_id:
        cur.execute(
            "INSERT INTO transaction_fee_estimates "
            "(id, payment_attempt_id, interchange_program, interchange_minor, scheme_minor, "
            " markup_minor, total_minor, blended_override, breakdown) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (new_uuid(), persist_attempt_id, breakdown["interchange_program"],
             ic_fee, scheme_fee, markup_fee, total,
             blended_total is not None, json.dumps(breakdown)),
        )
    return breakdown


def decayed_posterior(cur, psp_id, feats):
    """Beta posterior with hierarchical bucket fallback and 6-hour
    half-life decay toward the (1,1) prior."""
    buckets = [
        "|".join([feats.get("issuer_country", ""), feats.get("network", ""),
                  feats.get("funding_type", ""), feats.get("entry_mode", "")]),
        "|".join([feats.get("issuer_country", ""), feats.get("network", "")]),
        "ALL",
    ]
    for bucket in buckets:
        cur.execute(
            "SELECT * FROM psp_approval_stats WHERE psp_id = %s AND bucket_key = %s",
            (psp_id, bucket),
        )
        row = cur.fetchone()
        if row and row["sample_count"] >= 5:
            age_hours = max(
                0.0,
                (time.time() - row["updated_at"].timestamp()) / 3600.0,
            )
            decay = 0.5 ** (age_hours / 6.0)
            alpha = 1 + (row["alpha"] - 1) * decay
            beta = 1 + (row["beta"] - 1) * decay
            return alpha, beta, bucket
    return 1.0, 1.0, "PRIOR"


def circuit_open(cur, psp_id):
    cur.execute("SELECT * FROM psp_health WHERE psp_id = %s", (psp_id,))
    h = cur.fetchone()
    if not h:
        return False
    if h["circuit_state"] == "OPEN":
        # Half-open probe after 60 seconds.
        if h["opened_at"] and (time.time() - h["opened_at"].timestamp()) > 60:
            cur.execute(
                "UPDATE psp_health SET circuit_state = 'CLOSED', total_count = 0, "
                "timeout_count = 0, error_count = 0, window_started_at = NOW() WHERE psp_id = %s",
                (psp_id,),
            )
            return False
        return True
    return False


def route_decision(cur, intent, feats):
    started = time.time()
    amount = int(feats.get("amount") or intent["amount"])
    margin = int(feats.get("gross_margin") or amount)

    cur.execute("SELECT * FROM psps WHERE active = TRUE")
    psps = {str(p["id"]): p for p in cur.fetchall()}

    # Routing rules: first matching rule (priority order) restricts the pool.
    pool = None
    cur.execute("SELECT * FROM routing_rules WHERE active = TRUE ORDER BY priority DESC")
    for rule in cur.fetchall():
        pred = rule["predicate"] or {}
        ok = True
        if "currency" in pred and pred["currency"] != feats.get("currency"):
            ok = False
        if "issuer_country" in pred and pred["issuer_country"] != feats.get("issuer_country"):
            ok = False
        if "amount_min" in pred and amount < pred["amount_min"]:
            ok = False
        if "is_recurring" in pred and bool(pred["is_recurring"]) != bool(feats.get("is_recurring")):
            ok = False
        if ok:
            pool = [str(x) for x in rule["allowed_psp_ids"]]
            break
    if pool is None:
        pool = list(psps.keys())

    candidates = []
    for pid in pool:
        psp = psps.get(pid)
        if psp is None:
            continue
        # Stage 1 — hard constraints.
        if feats.get("currency") and psp["supported_currencies"] and feats["currency"] not in psp["supported_currencies"]:
            continue
        if feats.get("network") and psp["supported_networks"] and feats["network"] not in psp["supported_networks"]:
            continue
        if feats.get("is_3ds_authenticated") and not psp["supports_3ds"]:
            continue
        if circuit_open(cur, pid):
            continue
        # Stage 2 — fee estimate + Thompson-sampled approval rate.
        f2 = dict(feats)
        f2["psp_name"] = psp["name"]
        f2["amount"] = amount
        est = estimate_fees(cur, f2)
        alpha, beta, bucket = decayed_posterior(cur, pid, feats)
        sampled = random.betavariate(alpha, beta)
        mean_p = alpha / (alpha + beta)
        fee = est["total_minor"]
        ev = sampled * (margin - fee) - (1 - sampled) * RETRY_PENALTY_MINOR
        candidates.append({
            "psp_id": pid, "psp_name": psp["name"],
            "predicted_auth_rate": round(mean_p, 4),
            "sampled_auth_rate": round(sampled, 4),
            "estimated_fee_minor": fee,
            "net_ev": round(ev, 2), "bucket": bucket,
        })

    if not candidates:
        return None
    greedy = max(candidates, key=lambda c: c["predicted_auth_rate"] * (margin - c["estimated_fee_minor"]))
    candidates.sort(key=lambda c: c["net_ev"], reverse=True)
    chosen = candidates[0]
    mode = "EXPLORE_BANDIT" if chosen["psp_id"] != greedy["psp_id"] else "EXPLOIT"
    fallbacks = [c["psp_id"] for c in candidates[1:]]
    latency_ms = int((time.time() - started) * 1000)
    cur.execute(
        "INSERT INTO routing_decisions (id, payment_intent_id, chosen_psp_id, fallback_psp_ids, "
        "decision_mode, candidate_scores, latency_ms) VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (payment_intent_id) DO NOTHING RETURNING id",
        (new_uuid(), intent["id"], chosen["psp_id"], fallbacks, mode,
         json.dumps(candidates), latency_ms),
    )
    return {
        "chosen_psp": chosen["psp_name"], "chosen_psp_id": chosen["psp_id"],
        "fallbacks": [next(p["name"] for p in psps.values() if str(p["id"]) == f) for f in fallbacks],
        "decision_mode": mode, "candidate_scores": candidates,
        "latency_ms": latency_ms,
        "cascade_policy": {
            "soft_decline_codes": sorted(SOFT_DECLINE_CODES),
            "hard_decline_codes": sorted(HARD_DECLINE_CODES),
            "soft_retry_cap_24h": SOFT_RETRY_CAP_24H,
        },
    }


def record_outcome(cur, attempt, outcome, code, card_fingerprint):
    """Update attempt status, PSP health (circuit breaker), approval
    posteriors, and the PAN retry tracker. Returns cascade guidance."""
    cur.execute(
        "UPDATE payment_attempts SET status = %s, raw_response_code = %s WHERE id = %s",
        (outcome, code, attempt["id"]),
    )
    cur.execute("SELECT * FROM psps WHERE name = %s", (attempt["psp_name"],))
    psp = cur.fetchone()
    guidance = {"cascade_allowed": False, "reason": ""}

    if card_fingerprint:
        cur.execute(
            "INSERT INTO pan_retry_tracker (card_fingerprint) VALUES (%s) "
            "ON CONFLICT (card_fingerprint) DO NOTHING",
            (card_fingerprint,),
        )
        cur.execute(
            "SELECT * FROM pan_retry_tracker WHERE card_fingerprint = %s",
            (card_fingerprint,),
        )
        tracker = cur.fetchone()
        if outcome == "DECLINED" and code in IRREVERSIBLE_CODES and tracker["hard_decline_count_30d"] >= 1:
            guidance["reason"] = "PAN blocked: irreversible hard decline already recorded in 30d window"
            return guidance
        if outcome == "DECLINED" and code in SOFT_DECLINE_CODES and tracker["soft_decline_count_24h"] >= SOFT_RETRY_CAP_24H:
            guidance["reason"] = f"PAN retry cap reached ({SOFT_RETRY_CAP_24H} soft declines in 24h)"
            return guidance

    if outcome == "DECLINED" and code in HARD_DECLINE_CODES:
        guidance["reason"] = f"hard decline {code}: never cascade"
    elif outcome in ("TIMEOUT",) or (outcome == "DECLINED" and code in SOFT_DECLINE_CODES):
        guidance = {"cascade_allowed": True, "reason": f"{outcome} {code or ''}: soft failure, cascade permitted".strip()}
    elif outcome == "SUCCEEDED":
        guidance["reason"] = "approved"
    else:
        guidance["reason"] = f"outcome {outcome} recorded"

    if psp:
        pid = psp["id"]
        cur.execute(
            "INSERT INTO psp_health (psp_id) VALUES (%s) ON CONFLICT (psp_id) DO NOTHING",
            (pid,),
        )
        cur.execute(
            "UPDATE psp_health SET total_count = total_count + 1, "
            "timeout_count = timeout_count + %s, error_count = error_count + %s "
            "WHERE psp_id = %s RETURNING *",
            (1 if outcome == "TIMEOUT" else 0,
             1 if outcome in ("TIMEOUT",) or (code in ("91", "96")) else 0, pid),
        )
        h = cur.fetchone()
        if h["total_count"] >= 20:
            bad_rate = (h["timeout_count"] + h["error_count"]) / h["total_count"]
            if bad_rate > 0.02 and h["circuit_state"] == "CLOSED":
                cur.execute(
                    "UPDATE psp_health SET circuit_state = 'OPEN', opened_at = NOW() WHERE psp_id = %s",
                    (pid,),
                )
                guidance["circuit_tripped"] = True
        if attempt["attempt_type"] == "AUTHORIZATION":
            feats = attempt.get("features") or {}
            if isinstance(feats, str):
                feats = json.loads(feats)
            buckets = ["ALL"]
            if feats.get("issuer_country") and feats.get("network"):
                buckets.append("|".join([feats["issuer_country"], feats["network"]]))
                buckets.append("|".join([
                    feats["issuer_country"], feats["network"],
                    feats.get("funding_type", ""), feats.get("entry_mode", ""),
                ]))
            for bucket in buckets:
                cur.execute(
                    "INSERT INTO psp_approval_stats (psp_id, bucket_key, alpha, beta, sample_count) "
                    "VALUES (%s, %s, %s, %s, 1) ON CONFLICT (psp_id, bucket_key) DO UPDATE SET "
                    "alpha = psp_approval_stats.alpha + EXCLUDED.alpha - 1, "
                    "beta = psp_approval_stats.beta + EXCLUDED.beta - 1, "
                    "sample_count = psp_approval_stats.sample_count + 1, updated_at = NOW()",
                    (pid, bucket, 2 if outcome == "SUCCEEDED" else 1,
                     1 if outcome == "SUCCEEDED" else 2),
                )

    if card_fingerprint and outcome == "DECLINED":
        cur.execute(
            "UPDATE pan_retry_tracker SET "
            "hard_decline_count_30d = hard_decline_count_30d + %s, "
            "soft_decline_count_24h = soft_decline_count_24h + %s, "
            "last_decline_code = %s, updated_at = NOW() WHERE card_fingerprint = %s",
            (1 if code in HARD_DECLINE_CODES else 0,
             1 if code in SOFT_DECLINE_CODES else 0, code, card_fingerprint),
        )
    return guidance


def run_reconciliation(cur):
    report = {"matched": {}, "exceptions": {}, "payout_rollup": []}

    # Pass 1 — EXACT_RRN (RRN + currency + gross amount, per design).
    cur.execute(
        """
        INSERT INTO reconciliation_matches (id, payment_attempt_id, ledger_transaction_id, external_line_id, match_rule)
        SELECT gen_random_uuid(), pa.id,
               (SELECT lt.id FROM ledger_transactions lt
                 WHERE lt.payment_intent_id = pa.payment_intent_id
                   AND lt.idempotency_key = 'settle:' || esl.psp_name || ':' || esl.network_rrn || ':' || esl.record_type
                 LIMIT 1),
               esl.id, 'EXACT_RRN'
        FROM external_settlement_lines esl
        JOIN payment_attempts pa ON pa.psp_transaction_id = esl.network_rrn
        JOIN payment_intents pi ON pi.id = pa.payment_intent_id
        WHERE esl.recon_status = 'UNMATCHED' AND esl.network_rrn IS NOT NULL
          AND pi.currency = esl.currency AND pi.amount = esl.gross_amount
        ON CONFLICT (external_line_id) DO NOTHING
        """
    )
    report["matched"]["EXACT_RRN"] = cur.rowcount

    # Pass 2 — PSP_TX_ID (secondary gateway reference).
    cur.execute(
        """
        INSERT INTO reconciliation_matches (id, payment_attempt_id, ledger_transaction_id, external_line_id, match_rule)
        SELECT gen_random_uuid(), pa.id, NULL, esl.id, 'PSP_TX_ID'
        FROM external_settlement_lines esl
        JOIN payment_attempts pa ON pa.psp_transaction_id = esl.psp_transaction_id
        WHERE esl.recon_status = 'UNMATCHED' AND esl.psp_transaction_id IS NOT NULL
        ON CONFLICT (external_line_id) DO NOTHING
        """
    )
    report["matched"]["PSP_TX_ID"] = cur.rowcount

    # Pass 3 — HEURISTIC (psp + amount + currency within ±48h of capture).
    cur.execute(
        """
        INSERT INTO reconciliation_matches (id, payment_attempt_id, ledger_transaction_id, external_line_id, match_rule)
        SELECT gen_random_uuid(), pa.id, NULL, esl.id, 'HEURISTIC'
        FROM external_settlement_lines esl
        JOIN payment_attempts pa ON pa.psp_name = esl.psp_name AND pa.attempt_type = 'CAPTURE'
        JOIN payment_intents pi ON pi.id = pa.payment_intent_id
        WHERE esl.recon_status = 'UNMATCHED'
          AND pi.amount = esl.gross_amount AND pi.currency = esl.currency
          AND pa.created_at BETWEEN (esl.settlement_date - INTERVAL '48 hours')
                                AND (esl.settlement_date + INTERVAL '48 hours')
        ON CONFLICT (external_line_id) DO NOTHING
        """
    )
    report["matched"]["HEURISTIC"] = cur.rowcount

    cur.execute(
        "UPDATE external_settlement_lines SET recon_status = 'MATCHED' "
        "WHERE id IN (SELECT external_line_id FROM reconciliation_matches) "
        "AND recon_status = 'UNMATCHED'"
    )

    # Pass 4 — BANK_DEPOSIT_CHECK: matched net sums vs PAYOUT lines.
    cur.execute(
        """
        SELECT esl.psp_name, esl.settlement_date, esl.currency,
               SUM(esl.net_amount) AS matched_net
        FROM external_settlement_lines esl
        WHERE esl.recon_status = 'MATCHED' AND esl.record_type <> 'PAYOUT'
        GROUP BY esl.psp_name, esl.settlement_date, esl.currency
        """
    )
    for group in cur.fetchall():
        cur.execute(
            "SELECT COALESCE(SUM(net_amount), 0) AS payout FROM external_settlement_lines "
            "WHERE record_type = 'PAYOUT' AND psp_name = %s AND settlement_date = %s AND currency = %s",
            (group["psp_name"], group["settlement_date"], group["currency"]),
        )
        payout = cur.fetchone()["payout"]
        entry = {
            "psp_name": group["psp_name"],
            "settlement_date": str(group["settlement_date"]),
            "currency": group["currency"],
            "matched_net": group["matched_net"], "bank_payout": payout,
            "agrees": payout == group["matched_net"],
        }
        report["payout_rollup"].append(entry)
        if payout != 0 and payout != group["matched_net"]:
            cur.execute(
                """
                INSERT INTO reconciliation_exceptions
                    (id, exception_type, severity, discrepancy_amount, currency)
                SELECT gen_random_uuid(), 'AMOUNT_MISMATCH', 'CRITICAL', %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM reconciliation_exceptions
                    WHERE exception_type = 'AMOUNT_MISMATCH' AND status IN ('OPEN', 'INVESTIGATING')
                      AND discrepancy_amount = %s
                )
                """,
                (group["matched_net"] - payout, group["currency"],
                 group["matched_net"] - payout),
            )

    # Detection A — MISSING_SETTLEMENT (design doc 3, verbatim logic).
    cur.execute(
        """
        INSERT INTO reconciliation_exceptions (
            id, exception_type, severity, payment_intent_id, discrepancy_amount, currency
        )
        SELECT gen_random_uuid(), 'MISSING_SETTLEMENT', 'HIGH', pi.id, pi.amount, pi.currency
        FROM payment_intents pi
        JOIN payment_attempts pa
          ON pa.payment_intent_id = pi.id
         AND pa.attempt_type = 'CAPTURE'
         AND pa.status = 'SUCCEEDED'
        LEFT JOIN reconciliation_matches rm ON rm.payment_attempt_id = pa.id
        WHERE pi.status = 'CAPTURED'
          AND pi.updated_at < NOW() - INTERVAL '3 days'
          AND rm.id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM reconciliation_exceptions re
              WHERE re.payment_intent_id = pi.id
                AND re.exception_type = 'MISSING_SETTLEMENT'
                AND re.status IN ('OPEN', 'INVESTIGATING')
          )
        """
    )
    report["exceptions"]["MISSING_SETTLEMENT"] = cur.rowcount

    # Detection B — DUPLICATE_CHARGE (verbatim logic).
    cur.execute(
        """
        INSERT INTO reconciliation_exceptions (
            id, exception_type, severity, payment_intent_id, external_line_id, discrepancy_amount, currency
        )
        SELECT gen_random_uuid(), 'DUPLICATE_CHARGE', 'CRITICAL',
               pa.payment_intent_id, esl.id, esl.gross_amount, esl.currency
        FROM external_settlement_lines esl
        JOIN payment_attempts pa ON pa.psp_transaction_id = esl.network_rrn
        WHERE esl.recon_status = 'UNMATCHED'
          AND EXISTS (
              SELECT 1 FROM reconciliation_matches rm WHERE rm.payment_attempt_id = pa.id
          )
        """
    )
    report["exceptions"]["DUPLICATE_CHARGE"] = cur.rowcount

    # Detection C — FEE_VARIANCE (verbatim logic, tolerance 2 minor).
    cur.execute(
        """
        INSERT INTO reconciliation_exceptions (
            id, exception_type, severity, payment_intent_id, external_line_id, discrepancy_amount, currency
        )
        SELECT gen_random_uuid(), 'FEE_VARIANCE',
               CASE WHEN ABS(esl.fee_amount - esl.expected_fee_amount) > 500 THEN 'HIGH' ELSE 'LOW' END,
               pa.payment_intent_id, esl.id,
               (esl.fee_amount - esl.expected_fee_amount), esl.currency
        FROM external_settlement_lines esl
        JOIN reconciliation_matches rm ON rm.external_line_id = esl.id
        JOIN payment_attempts pa ON pa.id = rm.payment_attempt_id
        WHERE esl.expected_fee_amount IS NOT NULL
          AND ABS(esl.fee_amount - esl.expected_fee_amount) > 2
          AND NOT EXISTS (
              SELECT 1 FROM reconciliation_exceptions re
              WHERE re.external_line_id = esl.id AND re.exception_type = 'FEE_VARIANCE'
          )
        """
    )
    report["exceptions"]["FEE_VARIANCE"] = cur.rowcount

    # Auto-resolution: fee variances under $0.05 post a rounding
    # adjustment and close as AUTO_RESOLVED (design doc 3, section 5).
    cur.execute(
        "SELECT * FROM reconciliation_exceptions WHERE exception_type = 'FEE_VARIANCE' "
        "AND status = 'OPEN' AND ABS(discrepancy_amount) < 5"
    )
    auto = 0
    for exc in cur.fetchall():
        delta = exc["discrepancy_amount"]
        lines = []
        if delta > 0:
            lines = [("fee_rounding_variance", "DEBIT", delta, exc["currency"]),
                     ("psp_processing_fee_expense", "CREDIT", delta, exc["currency"])]
        elif delta < 0:
            lines = [("psp_processing_fee_expense", "DEBIT", -delta, exc["currency"]),
                     ("fee_rounding_variance", "CREDIT", -delta, exc["currency"])]
        if lines:
            post_journal(cur, f"feevar:{exc['external_line_id']}", exc["payment_intent_id"],
                         "Auto-resolved minor fee variance", lines)
        cur.execute(
            "UPDATE reconciliation_exceptions SET status = 'AUTO_RESOLVED' WHERE id = %s",
            (exc["id"],),
        )
        auto += 1
    report["exceptions"]["FEE_VARIANCE_AUTO_RESOLVED"] = auto

    # Clearing aging check: clearing_expected balance vs open captures.
    cur.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN le.direction = 'DEBIT' THEN le.amount ELSE -le.amount END), 0) AS bal
        FROM ledger_entries le JOIN ledger_accounts la ON la.id = le.account_id
        WHERE la.code = 'acquirer_clearing_expected'
        """
    )
    clearing_balance = cur.fetchone()["bal"]
    cur.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM payment_intents WHERE status = 'CAPTURED'"
    )
    open_captures = cur.fetchone()["s"]
    report["clearing_check"] = {
        "clearing_expected_balance": clearing_balance,
        "open_captured_sum": open_captures,
        "difference": clearing_balance - open_captures,
    }
    return report
