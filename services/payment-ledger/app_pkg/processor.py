"""Processor layer (SANDBOX) — ISO 8583 codec, STAN, simulated
issuer, clearing files, FBO wires. Moved verbatim from the
monolith; see the original section banner in git history: there is
NO connection to any card network, PANs are used transiently and
stored nowhere, and the wire log masks DE 2."""

import json
import time

from .config import MERCHANT_ID
from .ledger import get_intent, move_status
from .store import new_uuid


ISO_FIELDS = {
    2: ("ll", 19), 3: ("n", 6), 4: ("n", 12), 7: ("n", 10),
    11: ("n", 6), 12: ("n", 6), 13: ("n", 4), 14: ("n", 4),
    22: ("n", 3), 25: ("n", 2), 37: ("an", 12), 38: ("an", 6),
    39: ("an", 2), 41: ("an", 8), 42: ("an", 15), 49: ("an", 3),
    55: ("lll", 255), 70: ("n", 3), 90: ("an", 42),
}


def iso_pack(mti, fields):
    """Pack an ISO 8583 message: MTI + bitmap(s) + data elements."""
    nums = sorted(fields)
    has_secondary = any(n > 64 for n in nums)
    primary = bytearray(8)
    secondary = bytearray(8)
    for n in nums:
        target = primary if n <= 64 else secondary
        idx = (n - 1) % 64
        target[idx // 8] |= 1 << (7 - (idx % 8))
    if has_secondary:
        primary[0] |= 0x80
    out = bytearray(mti.encode("ascii"))
    out += primary
    if has_secondary:
        out += secondary
    for n in nums:
        kind, maxlen = ISO_FIELDS[n]
        value = str(fields[n])
        if kind == "n":
            out += value.encode("ascii")
        elif kind == "an":
            out += value.ljust(maxlen)[:maxlen].encode("ascii")
        elif kind == "ll":
            out += f"{len(value):02d}".encode("ascii") + value.encode("ascii")
        elif kind == "lll":
            raw = bytes.fromhex(value)
            out += f"{len(raw):03d}".encode("ascii") + raw
    return bytes(out)


def iso_unpack(data):
    """Unpack an ISO 8583 message into (mti, {de: value})."""
    mti = data[:4].decode("ascii")
    primary = data[4:12]
    has_secondary = bool(primary[0] & 0x80)
    bitmaps = [primary]
    pos = 12
    if has_secondary:
        bitmaps.append(data[12:20])
        pos = 20
    present = []
    for bi, bitmap in enumerate(bitmaps):
        for bit in range(64):
            n = bi * 64 + bit + 1
            if n == 1:
                continue
            if bitmap[bit // 8] & (1 << (7 - (bit % 8))):
                present.append(n)
    fields = {}
    for n in sorted(present):
        kind, maxlen = ISO_FIELDS[n]
        if kind == "n":
            fields[n] = data[pos:pos + maxlen].decode("ascii")
            pos += maxlen
        elif kind == "an":
            fields[n] = data[pos:pos + maxlen].decode("ascii").rstrip()
            pos += maxlen
        elif kind == "ll":
            ln = int(data[pos:pos + 2].decode("ascii"))
            pos += 2
            fields[n] = data[pos:pos + ln].decode("ascii")
            pos += ln
        elif kind == "lll":
            ln = int(data[pos:pos + 3].decode("ascii"))
            pos += 3
            fields[n] = data[pos:pos + ln].hex()
            pos += ln
    return mti, fields


def mask_pan(pan):
    if not pan or len(pan) < 10:
        return "***"
    return pan[:6] + "*" * (len(pan) - 10) + pan[-4:]


def masked_fields(fields):
    out = {str(k): v for k, v in fields.items()}
    if "2" in out:
        out["2"] = mask_pan(out["2"])
    return out


def log_iso(cur, mti, direction, fields, raw, latency_ms=None):
    # The stored copy is re-packed from the MASKED fields: the design
    # requires DE 2 masked in the wire log, and a hex blob of the
    # unmasked message would defeat that. The in-flight `raw` is used
    # only transiently (in a real deployment it would go to the
    # network socket, which does not exist here).
    masked = masked_fields(fields)
    log_raw = iso_pack(mti, {int(k): v for k, v in masked.items()})
    cur.execute(
        "INSERT INTO iso8583_message_log (id, rrn, stan, mti, direction, raw_hex, parsed_de, latency_ms) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (new_uuid(), fields.get(37), fields.get(11), mti, direction,
         log_raw.hex(), json.dumps(masked), latency_ms),
    )


def next_stan(cur, acquirer_bin, network):
    cur.execute(
        "INSERT INTO network_stan_sequences (acquirer_bin, network, current_stan) "
        "VALUES (%s, %s, 1) ON CONFLICT (acquirer_bin, network) DO UPDATE SET "
        "current_stan = CASE WHEN network_stan_sequences.current_stan >= 999999 "
        "THEN 1 ELSE network_stan_sequences.current_stan + 1 END RETURNING current_stan",
        (acquirer_bin, network),
    )
    return f"{cur.fetchone()['current_stan']:06d}"


def make_rrn(stan):
    """12-char RRN: year digit (1) + Julian day (3) + hour (2) +
    STAN (6). Unique because the STAN never repeats within an
    acquirer/network day."""
    import datetime

    now = datetime.datetime.now()
    return f"{str(now.year)[-1]}{now.strftime('%j')}{now.strftime('%H')}{stan}"


SANDBOX_ISSUER = {
    "4111111111111111": ("00", "Sandbox approval (Visa test card)"),
    "4242424242424242": ("00", "Sandbox approval (Visa test card)"),
    "5555555555554444": ("00", "Sandbox approval (Mastercard test card)"),
    "4000000000000002": ("51", "Sandbox decline: insufficient funds"),
    "4000000000000069": ("54", "Sandbox decline: expired card"),
    "4000000000000127": ("59", "Sandbox decline: suspected fraud"),
}


def sandbox_authorize(cur, data):
    """Build MTI 0100, simulate the issuer, log both directions, and
    record the outcome on a ledger payment intent. Returns the
    response summary. Test PANs only; the PAN is never stored."""
    import datetime

    pan = str(data.get("pan") or data.get("card_token") or "")
    amount = int(data["amount"])
    currency = (data.get("currency") or "USD").upper()
    network = (data.get("network") or "VISA").upper()
    acquirer_bin = str(data.get("acquirer_bin") or "412345")
    merchant_id = data.get("merchant_id") or MERCHANT_ID
    now = datetime.datetime.now()

    stan = next_stan(cur, acquirer_bin, network)
    rrn = make_rrn(stan)
    fields = {
        2: pan, 3: "000000", 4: f"{amount:012d}",
        7: now.strftime("%m%d%H%M%S"), 11: stan,
        12: now.strftime("%H%M%S"), 13: now.strftime("%m%d"),
        22: "810" if data.get("entry_mode") == "CARD_PRESENT" else "010",
        25: "00", 37: rrn, 41: "SANDBOX1", 42: "SANDBOXMERCH001",
        49: currency,
    }
    if data.get("expiry"):
        fields[14] = str(data["expiry"])[-2:] + str(data["expiry"])[:2] \
            if len(str(data["expiry"])) == 4 else str(data["expiry"])
    if data.get("emv_data_hex"):
        fields[55] = data["emv_data_hex"]
    raw_out = iso_pack("0100", fields)
    started = time.time()
    log_iso(cur, "0100", "OUT", fields, raw_out)

    code, note = SANDBOX_ISSUER.get(
        pan, ("05", "Sandbox decline: do not honor (unknown test card)")
    )
    latency_ms = int((time.time() - started) * 1000) + 42  # simulated network hop
    resp_fields = dict(fields)
    resp_fields[39] = code
    if code == "00":
        resp_fields[38] = "SBX" + stan[-3:]
    raw_in = iso_pack("0110", resp_fields)
    log_iso(cur, "0110", "IN", resp_fields, raw_in, latency_ms)

    intent_id = new_uuid()
    cur.execute(
        "INSERT INTO payment_intents (id, idempotency_key, merchant_id, amount, currency, status) "
        "VALUES (%s, %s, %s, %s, %s, 'CREATED')",
        (intent_id, f"iso:{rrn}", merchant_id, amount, currency),
    )
    cur.execute(
        "INSERT INTO payment_state_transitions (payment_intent_id, from_status, to_status) "
        "VALUES (%s, NULL, 'CREATED')",
        (intent_id,),
    )
    attempt_id = new_uuid()
    feats = {
        "issuer_country": data.get("issuer_country", "US"),
        "network": network, "funding_type": data.get("funding_type", "CREDIT"),
        "entry_mode": data.get("entry_mode", "CARD_NOT_PRESENT"),
        "pan_first6": pan[:6], "pan_last4": pan[-4:],
        "stan": stan, "acquirer_bin": acquirer_bin,
    }
    cur.execute(
        "INSERT INTO payment_attempts (id, payment_intent_id, attempt_type, psp_name, "
        "psp_transaction_id, status, raw_response_code, features) "
        "VALUES (%s, %s, 'AUTHORIZATION', %s, %s, %s, %s, %s)",
        (attempt_id, intent_id, network.lower() + "_sandbox", rrn,
         "SUCCEEDED" if code == "00" else "DECLINED", code, json.dumps(feats)),
    )
    intent = get_intent(cur, intent_id)
    if code == "00":
        move_status(cur, intent, "AUTHORIZED", attempt_id)
    else:
        move_status(cur, intent, "FAILED", attempt_id)
    return {
        "payment_intent_id": intent_id, "rrn": rrn, "stan": stan,
        "response_code": code, "issuer_note": note,
        "auth_code": resp_fields.get(38), "latency_ms": latency_ms,
        "intent_status": intent["status"], "simulated": True,
    }


def sandbox_reverse(cur, intent_id):
    """MTI 0420 reversal advice for an AUTHORIZED sandbox intent."""
    import datetime

    intent = get_intent(cur, intent_id)
    if intent is None:
        return None
    cur.execute(
        "SELECT * FROM payment_attempts WHERE payment_intent_id = %s "
        "AND attempt_type = 'AUTHORIZATION' ORDER BY created_at LIMIT 1",
        (intent_id,),
    )
    attempt = cur.fetchone()
    feats = attempt["features"] or {}
    if isinstance(feats, str):
        feats = json.loads(feats)
    network = (feats.get("network") or "VISA").upper()
    acquirer_bin = feats.get("acquirer_bin") or "412345"
    stan = next_stan(cur, acquirer_bin, network)
    now = datetime.datetime.now()
    orig = attempt["psp_transaction_id"]
    de90 = (
        "0100" + (feats.get("stan") or "000000") + now.strftime("%m%d%H%M%S")
        + acquirer_bin.ljust(11) + "0" * 11
    )[:42]
    fields = {
        # No DE 2: the PAN is never stored, so a reversal references
        # the original through DE 37 (RRN) + DE 90 (original elements).
        3: "000000", 4: f"{intent['amount']:012d}",
        7: now.strftime("%m%d%H%M%S"), 11: stan,
        12: now.strftime("%H%M%S"), 13: now.strftime("%m%d"),
        25: "00", 37: orig, 41: "SANDBOX1", 42: "SANDBOXMERCH001",
        49: intent["currency"], 90: de90,
    }
    raw = iso_pack("0420", fields)
    log_iso(cur, "0420", "OUT", fields, raw)
    move_status(cur, intent, "VOIDED", attempt["id"])
    return {"payment_intent_id": intent_id, "reversed_rrn": orig,
            "reversal_stan": stan, "intent_status": intent["status"],
            "simulated": True}


def generate_clearing(cur, network):
    """Base II-style fixed-width clearing file from CAPTURED intents
    not yet in any batch. Returns the batch row."""
    network = network.upper()
    cur.execute(
        """
        SELECT pi.*, pa.psp_transaction_id AS rrn, pa.features AS feats
        FROM payment_intents pi
        JOIN payment_attempts pa ON pa.payment_intent_id = pi.id
            AND pa.attempt_type = 'AUTHORIZATION'
            AND pa.features ->> 'stan' IS NOT NULL
        WHERE pi.status = 'CAPTURED'
          AND NOT EXISTS (SELECT 1 FROM clearing_batch_items cbi
                          WHERE cbi.payment_intent_id = pi.id)
        ORDER BY pi.created_at
        """
    )
    rows = [r for r in cur.fetchall()
            if (json.loads(r["feats"]) if isinstance(r["feats"], str)
                else r["feats"] or {}).get("network", network) == network]
    batch_id = new_uuid()
    import datetime

    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M")
    file_name = f"clearing_{network.lower()}_{stamp}.dat"
    lines = [f"HDR{network:<10}{stamp:<14}{'SANDBOX BASE II-STYLE CLEARING':<40}"]
    gross = 0
    for r in rows:
        feats = r["feats"] or {}
        if isinstance(feats, str):
            feats = json.loads(feats)
        masked = (feats.get("pan_first6", "000000") + "******"
                  + feats.get("pan_last4", "0000"))
        lines.append(
            f"TC05{r['rrn'] or '':<12}{masked:<19}"
            f"{r['amount']:012d}{r['currency']:<3}CPSQ"
        )
        gross += r["amount"]
    lines.append(
        f"TRL{len(rows):09d}{gross:015d}{'END OF BATCH':<40}"
    )
    body = "\n".join(lines) + "\n"
    cur.execute(
        "INSERT INTO clearing_batches (id, network, file_name, record_count, gross_total, file_body, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'GENERATED') RETURNING *",
        (batch_id, network, file_name, len(rows), gross, body),
    )
    batch = cur.fetchone()
    for r in rows:
        cur.execute(
            "INSERT INTO clearing_batch_items (batch_id, payment_intent_id) VALUES (%s, %s)",
            (batch_id, r["id"]),
        )
    return batch
