"""Auto-renewal for the member-site catalog — stored Authorize.net
Customer Profiles, charged by this platform on schedule.

Why the platform is the scheduler: Authorize.net's own recurring
billing (ARB) cannot express a 24-month interval, and CCCE renews
every two years (Taylor, 2026-11-15: CCCE $15 every two years per
provider; EMSPR $6,000 every year). So a buyer who opts in at
checkout gets a Customer Profile at Authorize.net (created FROM the
completed first transaction — the bank account itself never reaches
this service, at enrollment or ever), and this module charges that
profile when a renewal comes due.

Design rules, mirroring authnet.py's:

1. THIRD SWITCH, FAIL CLOSED. Every operation requires
   AUTHNET_RENEWAL_ENABLED=1 on top of the processor and eCheck
   gates (authnet.AuthNetConfig.renewal_enabled). While disabled,
   enrollment is skipped, the scheduler idles, and /ledger/health
   reports the disabled state. Nothing runs at import time.

2. OPT-IN ONLY, WITH A PAPER TRAIL. Enrollment happens solely when
   the checkout request carries auto_renew=true — the buyer checked
   the auto-renew box, whose text is the recurring WEB debit
   authorization (AUTHORIZATION_VERSION below; the buy page carries
   the same version). The enrollment row stores the consent time
   and the authorization version. Cancellation is always available:
   the buyer with their one-time cancel token (shown once, at
   enrollment; only its SHA-256 hash is stored), or the admin route.

3. RENEWAL CHARGES RECORD LIKE FIRST CHARGES. A due renewal goes
   through the same intent + attempt + canonical "captured" event
   path as a checkout charge (authnet._create_intent_attempt /
   authnet.record_charge), with payment_method=ECHECK /
   settlement_state=PENDING_SETTLEMENT markers and is_recurring in
   the features, under the deterministic idempotency key
   renewal:{profile_id}:{due date} — a crashed or repeated run
   never charges a period twice. Amounts come from the server-side
   catalog (authnet.ECHECK_PRODUCTS), never from stored buyer input.

4. FAILURES ARE BOUNDED AND VISIBLE. A definitive decline records
   the FAILED intent (no journal), bumps the attempt counter, and
   retries in RETRY_DAYS; after MAX_ATTEMPTS the enrollment parks
   at NEEDS_ATTENTION for a human — it never retries forever and
   never charges silently. A gateway ERROR (network, auth) has an
   unknown outcome, so it records nothing in the ledger, does not
   count toward the cap, and retries the next day with the reason
   kept in last_result where an operator can see it.

5. ENROLLMENT NEVER JEOPARDIZES A SALE. The checkout route calls
   enroll() only after the first charge has committed, best-effort:
   any failure is logged and reported in the result, and the buyer's
   completed purchase stands either way.

Python standard library only (Taylor's standing stack rule); the
store/ledger packages are touched lazily so offline tests import
this module without psycopg.
"""

import hashlib
import hmac
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import authnet
from .authnet import (
    DEFAULT_DESCRIPTOR,
    ECHECK_PRODUCTS,
    SETTLEMENT_PENDING,
    ApiError,
    AuthNetConfig,
    ProcessorError,
    explain_auth_failure,
    interpret_charge_response,
    parse_amount_minor,
    post,
    require_renewal_enabled,
)
from .config import MERCHANT_ID

# The recurring WEB debit authorization the buyer agrees to. The
# buy page's auto-renew checkbox carries this version string; bump
# the version whenever the wording changes, so an enrollment row
# always names the exact text its buyer saw.
AUTHORIZATION_VERSION = "recurring-web-v1"

MAX_ATTEMPTS = 3          # definitive declines before NEEDS_ATTENTION
RETRY_DAYS = 3            # delay between decline retries
LOOP_SECONDS = 1800       # scheduler cadence
BATCH_LIMIT = 10          # profiles charged per scheduler pass


def _now():
    return datetime.now(timezone.utc)


def add_years(moment, years):
    """Anniversary arithmetic on a datetime (Feb 29 -> Feb 28)."""
    try:
        return moment.replace(year=moment.year + years)
    except ValueError:
        return moment.replace(year=moment.year + years, day=28)


def _token_hash(token):
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- gateway

def create_profile_from_transaction(cfg, trans_id, email, transport=None):
    """CIM: create a Customer Profile from a completed transaction.

    The gateway copies the payment instrument from the transaction
    into the profile — this service never sees it. Returns
    (customer_profile_id, payment_profile_id) as strings."""
    payload = {"createCustomerProfileFromTransactionRequest": {
        "merchantAuthentication": cfg.auth_block(),
        "transId": str(trans_id),
        "customer": {"email": email},
    }}
    try:
        resp = post(cfg, payload, transport=transport)
    except ApiError as err:
        raise ProcessorError(
            "PROFILE CREATION FAILED - " + explain_auth_failure(err))
    customer_profile_id = resp.get("customerProfileId")
    payment_ids = resp.get("customerPaymentProfileIdList") or []
    first = payment_ids[0] if payment_ids else {}
    payment_profile_id = first.get("numericString") if isinstance(first, dict) else first
    if not customer_profile_id or not payment_profile_id:
        raise ProcessorError(
            "PROFILE CREATION FAILED - the gateway created no usable "
            "payment profile from the transaction")
    return str(customer_profile_id), str(payment_profile_id)


def build_profile_charge_payload(cfg, amount, row, invoice, description):
    """createTransactionRequest spending a stored payment profile
    (profilePayment) instead of a nonce — the renewal twin of
    authnet.build_charge_payload."""
    txn = {
        "transactionType": "authCaptureTransaction",
        "amount": f"{amount:.2f}",
        "payment": {"profilePayment": {
            "customerProfileId": str(row["customer_profile_id"]),
            "paymentProfile": {
                "paymentProfileId": str(row["payment_profile_id"])},
        }},
        "transactionSettings": {"setting": [
            {"settingName": "duplicateWindow", "settingValue": "120"}]},
    }
    if invoice:
        txn["order"] = {"invoiceNumber": invoice,
                        "description": description or ""}
    if row.get("email"):
        txn["customer"] = {"email": row["email"]}
    return {"createTransactionRequest": {
        "merchantAuthentication": cfg.auth_block(),
        "transactionRequest": txn,
    }}


def charge_profile(cfg, row, amount_minor, invoice, description,
                   transport=None):
    """Gateway call only — no ledger writes, no gate checks (the
    caller gates). Returns interpret_charge_response's result dict;
    a gateway-level failure raises ProcessorError."""
    _, amount = parse_amount_minor(amount_minor)
    payload = build_profile_charge_payload(cfg, amount, row, invoice,
                                           description)
    try:
        resp = post(cfg, payload, transport=transport)
    except ApiError as err:
        raise ProcessorError(
            "RENEWAL CHARGE FAILED - " + explain_auth_failure(err))
    return interpret_charge_response(resp)


# ---------------------------------------------------------------- enrollment

def enroll(cur, email, product_key, trans_id, cfg=None, transport=None,
           now=None):
    """Enroll one buyer + product after their first charge.

    Runs inside the caller's transaction (the caller commits).
    Idempotent per buyer + product: an existing ACTIVE enrollment is
    reported, never duplicated (the partial unique index backs
    this). Returns a buyer-safe dict — profile ids never leave the
    service; the cancel token appears exactly here, once."""
    cfg = cfg or AuthNetConfig()
    require_renewal_enabled(cfg)
    product = ECHECK_PRODUCTS.get(product_key)
    if product is None:
        raise ValueError(f"unknown product: {product_key!r}")
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required to enroll in auto-renewal")
    cur.execute(
        "SELECT * FROM renewal_profiles WHERE email = %s AND product = %s "
        "AND status = 'ACTIVE'",
        (email, product_key),
    )
    existing = cur.fetchone()
    if existing is not None:
        return {
            "enrolled": True,
            "already_enrolled": True,
            "product": product_key,
            "next_renewal_at": str(existing["next_renewal_at"]),
        }
    customer_profile_id, payment_profile_id = create_profile_from_transaction(
        cfg, trans_id, email, transport=transport)
    moment = now or _now()
    next_renewal = add_years(moment, product["renewal_years"])
    token = secrets.token_urlsafe(32)
    cur.execute(
        "INSERT INTO renewal_profiles (id, email, product, "
        "customer_profile_id, payment_profile_id, status, consent_at, "
        "authorization_version, cancel_token_hash, next_renewal_at) "
        "VALUES (%s, %s, %s, %s, %s, 'ACTIVE', %s, %s, %s, %s)",
        (str(uuid.uuid4()), email, product_key, customer_profile_id,
         payment_profile_id, moment, AUTHORIZATION_VERSION,
         _token_hash(token), next_renewal),
    )
    return {
        "enrolled": True,
        "already_enrolled": False,
        "product": product_key,
        "next_renewal_at": next_renewal.isoformat(),
        "cancel_token": token,
    }


def cancel_by_token(cur, email, product_key, token):
    """Buyer self-cancel: the enrollment's one-time token, checked
    against its stored hash. Returns True when an ACTIVE enrollment
    was cancelled."""
    cur.execute(
        "SELECT * FROM renewal_profiles WHERE email = %s AND product = %s "
        "AND status = 'ACTIVE'",
        ((email or "").strip().lower(), product_key),
    )
    row = cur.fetchone()
    if row is None:
        return False
    if not hmac.compare_digest(_token_hash(token),
                               row["cancel_token_hash"] or ""):
        return False
    cur.execute(
        "UPDATE renewal_profiles SET status = 'CANCELLED', "
        "updated_at = NOW() WHERE id = %s",
        (row["id"],),
    )
    return True


def cancel_admin(cur, email, product_key):
    """Admin cancel (no token). Returns True when a row changed."""
    cur.execute(
        "UPDATE renewal_profiles SET status = 'CANCELLED', "
        "updated_at = NOW() WHERE email = %s AND product = %s "
        "AND status = 'ACTIVE'",
        ((email or "").strip().lower(), product_key),
    )
    return bool(getattr(cur, "rowcount", 0))


def list_profiles(cur, limit=100):
    """Admin view: enrollment state without gateway profile ids."""
    cur.execute(
        "SELECT email, product, status, next_renewal_at, attempts, "
        "last_trans_id, last_result, consent_at, created_at "
        "FROM renewal_profiles ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )
    return cur.fetchall()


# ---------------------------------------------------------------- scheduler

def process_profile(cur, row, cfg, transport=None, now=None):
    """Charge one due enrollment, inside the caller's transaction.

    The per-profile work sits behind a SAVEPOINT: a gateway error
    (unknown outcome) rolls back just this profile's intent rows —
    rule 4's "record nothing" — while the batch around it survives.
    Returns (outcome, receipt_or_None); the receipt is a vault-shaped
    dict the caller files AFTER committing."""
    moment = now or _now()
    product = ECHECK_PRODUCTS[row["product"]]
    amount_minor = product["amount_minor"]
    years = product["renewal_years"]
    due = row["next_renewal_at"]
    period_key = f"renewal:{row['id']}:{due.date().isoformat()}"
    invoice = f"{product['invoice_prefix']}-R-{due.strftime('%Y%m%d')}"
    feats = {
        "entry_mode": "ECHECK",
        "payment_method": "ECHECK",
        "echeck_type": authnet.ECHECK_TYPE,
        "settlement_state": SETTLEMENT_PENDING,
        "descriptor": DEFAULT_DESCRIPTOR,
        "invoice": invoice,
        "is_recurring": True,
        "renewal_profile_id": str(row["id"]),
    }
    cur.execute("SAVEPOINT renewal_one")
    try:
        existing, intent_id, attempt_id = authnet._create_intent_attempt(
            cur, {"idempotency_key": period_key}, amount_minor, "USD",
            MERCHANT_ID, feats)
        if existing is not None:
            # This period is already recorded (an earlier run charged
            # it and committed the ledger side): heal the schedule
            # without charging again.
            cur.execute(
                "UPDATE renewal_profiles SET next_renewal_at = %s, "
                "attempts = 0, last_result = %s, updated_at = NOW() "
                "WHERE id = %s",
                (add_years(due, years),
                 "already recorded for this period; schedule advanced",
                 row["id"]),
            )
            cur.execute("RELEASE SAVEPOINT renewal_one")
            return ({"profile_id": str(row["id"]), "deduplicated": True,
                     "charged": False}, None)
        result = charge_profile(cfg, row, amount_minor, invoice,
                                product["description"],
                                transport=transport)
    except ProcessorError as exc:
        cur.execute("ROLLBACK TO SAVEPOINT renewal_one")
        cur.execute(
            "UPDATE renewal_profiles SET next_renewal_at = %s, "
            "last_result = %s, updated_at = NOW() WHERE id = %s",
            (moment + timedelta(days=1),
             f"gateway error, will retry: {exc}", row["id"]),
        )
        cur.execute("RELEASE SAVEPOINT renewal_one")
        return ({"profile_id": str(row["id"]), "charged": False,
                 "gateway_error": str(exc)}, None)
    intent = {"id": intent_id, "status": "CREATED",
              "amount": amount_minor, "currency": "USD"}
    authnet.record_charge(
        cur, intent, attempt_id, result, amount_minor, "USD",
        event_extra={
            "payment_method": "ECHECK",
            "settlement_state": SETTLEMENT_PENDING,
            "is_recurring": True,
        },
    )
    cur.execute("RELEASE SAVEPOINT renewal_one")
    if result["approved"]:
        cur.execute(
            "UPDATE renewal_profiles SET next_renewal_at = %s, "
            "attempts = 0, last_trans_id = %s, last_result = %s, "
            "updated_at = NOW() WHERE id = %s",
            (add_years(due, years), result["trans_id"], "approved",
             row["id"]),
        )
        receipt = {
            "product": row["product"],
            "amount_minor": amount_minor,
            "email": row["email"],
            "trans_id": result["trans_id"],
            "approved": True,
            "payment_method": "ECHECK",
            "settlement_state": SETTLEMENT_PENDING,
            "deduplicated": False,
            "idempotency_key": period_key,
        }
        return ({"profile_id": str(row["id"]), "charged": True,
                 "approved": True, "trans_id": result["trans_id"]},
                receipt)
    attempts = int(row["attempts"] or 0) + 1
    if attempts >= MAX_ATTEMPTS:
        cur.execute(
            "UPDATE renewal_profiles SET status = 'NEEDS_ATTENTION', "
            "attempts = %s, last_result = %s, updated_at = NOW() "
            "WHERE id = %s",
            (attempts,
             f"declined {attempts} times, parked for review: "
             f"{result['message']}", row["id"]),
        )
    else:
        cur.execute(
            "UPDATE renewal_profiles SET attempts = %s, "
            "next_renewal_at = %s, last_result = %s, "
            "updated_at = NOW() WHERE id = %s",
            (attempts, moment + timedelta(days=RETRY_DAYS),
             f"declined (attempt {attempts}): {result['message']}",
             row["id"]),
        )
    return ({"profile_id": str(row["id"]), "charged": True,
             "approved": False, "attempts": attempts,
             "message": result["message"]}, None)


def run_due(cur, cfg=None, transport=None, now=None, limit=BATCH_LIMIT):
    """Charge every ACTIVE enrollment whose renewal is due, in the
    caller's transaction. Returns {"outcomes": [...],
    "receipts": [...]}. (The live loop below commits per profile
    instead, so one profile's failure can never roll back another
    profile's completed charge.)"""
    cfg = cfg or AuthNetConfig()
    require_renewal_enabled(cfg)
    moment = now or _now()
    cur.execute(
        "SELECT * FROM renewal_profiles WHERE status = 'ACTIVE' "
        "AND next_renewal_at <= %s ORDER BY next_renewal_at LIMIT %s",
        (moment, limit),
    )
    outcomes, receipts = [], []
    for row in cur.fetchall():
        outcome, receipt = process_profile(cur, row, cfg,
                                           transport=transport,
                                           now=moment)
        outcomes.append(outcome)
        if receipt is not None:
            receipts.append(receipt)
    return {"outcomes": outcomes, "receipts": receipts}


def renewal_loop():
    """Daemon scheduler — server.py starts it beside the ingestion
    worker. Idles while renewal is disabled. Each pass takes the
    shared lock and connection exactly like worker_loop, then
    processes due enrollments ONE COMMIT EACH (a gateway charge that
    succeeded must never lose its ledger record to a later profile's
    failure), and files each vault receipt after its commit."""
    from . import vault
    from .store import _lock, db

    while True:
        try:
            cfg = AuthNetConfig()
            if cfg.renewal_enabled():
                conn = db()
                with _lock:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT id FROM renewal_profiles WHERE "
                            "status = 'ACTIVE' AND next_renewal_at <= %s "
                            "ORDER BY next_renewal_at LIMIT %s",
                            (_now(), BATCH_LIMIT),
                        )
                        due_ids = [r["id"] for r in cur.fetchall()]
                    conn.commit()
                    for profile_id in due_ids:
                        receipt = None
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT * FROM renewal_profiles WHERE "
                                "id = %s AND status = 'ACTIVE' AND "
                                "next_renewal_at <= %s",
                                (profile_id, _now()),
                            )
                            row = cur.fetchone()
                            if row is not None:
                                outcome, receipt = process_profile(
                                    cur, row, cfg)
                                if outcome.get("charged"):
                                    print("payment-ledger renewal: "
                                          f"profile {profile_id} "
                                          f"approved={outcome.get('approved')}",
                                          flush=True)
                        conn.commit()
                        if receipt is not None:
                            vault.record_receipt(
                                receipt, receipt["email"],
                                receipt["idempotency_key"])
        except Exception as exc:  # the loop must survive any one pass
            print(f"payment-ledger renewal loop: {exc}", flush=True)
            try:
                from .store import db as _db
                _db().rollback()
            except Exception:
                pass
        time.sleep(LOOP_SECONDS)
