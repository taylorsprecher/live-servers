"""Authorize.net processor — the ledger's first real PSP adapter.

Adapted from the standalone pipeline script
(~/workspace/user/files/authorize_net_pipeline.py, 2026-11-20): same
endpoints, same payload shapes, same plain-language error wording,
same $6,000 single-transaction cap (Global Payments, Gateway
2886423: largest single transaction $6,000, monthly sales $6,000 —
an EMSPR sale at $6,000 exactly hits both caps).

Design rules, in order:

1. DISABLED BY DEFAULT, FAIL CLOSED. The processor is enabled only
   when AUTHNET_ENABLED=1 AND both credentials are present in the
   environment. In every other state the operations raise
   ProcessorDisabled with a plain reason, the service boots and
   serves normally, and /ledger/health reports the disabled state.
   Nothing in this module runs at import time beyond definitions.

2. CREDENTIALS FROM THE ENVIRONMENT ONLY — AUTHNET_API_LOGIN_ID,
   AUTHNET_TRANSACTION_KEY, AUTHNET_ENV ("production" default, or
   "sandbox"). Values are never logged, never placed in ledger rows,
   and never returned by any function in this module.

3. CARD NUMBERS NEVER TOUCH THIS SERVICE — and neither do bank
   routing/account numbers. Charges submit an Accept.js payment
   nonce (opaqueData) minted in the buyer's browser; this module
   only forwards the nonce. Accept.js tokenizes bank accounts the
   same way (secureData.bankData -> opaqueData, per the Accept.js
   documentation), and a bank nonce spent in
   createTransactionRequest is an eCheck.Net debit (SEC code WEB).

3b. eCHECK HAS ITS OWN SECOND SWITCH. Card charges need
   AUTHNET_ENABLED=1; eCheck charges additionally need
   AUTHNET_ECHECK_ENABLED=1 (the eCheck.Net service was activated
   on the account 2026-11-21). Either switch off, or credentials
   missing, and eCheck operations refuse exactly like card ones.
   An eCheck charge also requires the buyer's explicit debit
   authorization (the WEB authorization checkbox on the buy page)
   to be asserted in the request — without it the charge is
   refused before anything is created or sent. An approved
   eCheck response means the gateway ACCEPTED the debit: the
   canonical captured event records it (the clearing expectation
   is real), and the attempt is marked payment_method=ECHECK /
   settlement_state=PENDING_SETTLEMENT, because eCheck funds
   hold ~5 business days and a debit can still return; settlement
   then arrives through the same canonical surfaces as cards.

4. LEDGER WRITES GO THROUGH THE EXISTING WRITE PATH. A successful
   charge is recorded exactly the way the inbox worker records a
   processor event: an intent + attempt are created (the
   sandbox_authorize pattern), then the canonical "captured" event
   is handed to ledger.process_event, which posts the balanced
   journal under the deterministic key capture:{intent_id} (DEBIT
   acquirer_clearing_expected / CREDIT merchant_payable_main) and
   walks the intent to CAPTURED. Settlement arrives later through
   the canonical surfaces (Authorize.net webhook / settlement file)
   and posts settle:authorizenet:{transId}:SALE the same way every
   other PSP settles. Declines record the attempt as DECLINED and
   walk the intent to FAILED; no journal is posted.

Python standard library only (Taylor's standing stack rule); the
ledger package is imported lazily inside the recording functions so
this module — and its offline tests — never require psycopg.
"""

import json
import os
import urllib.error
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation

from .config import MERCHANT_ID

ENDPOINTS = {
    "production": "https://api.authorize.net/xml/v1/request.api",
    "sandbox": "https://apitest.authorize.net/xml/v1/request.api",
}

SINGLE_TXN_CAP = Decimal("6000.00")
SINGLE_TXN_CAP_MINOR = 600000
PSP_NAME = "authorizenet"
DEFAULT_DESCRIPTOR = "COMMON.ACCEPT.INAPP.PAYMENT"


class ApiError(Exception):
    """The gateway answered with resultCode != Ok (or never answered)."""

    def __init__(self, code, text):
        super().__init__(f"{code}: {text}")
        self.code = code
        self.text = text


class ProcessorDisabled(Exception):
    """Raised by every operation while the processor is not enabled.
    The message is the plain-language reason, safe to show an admin."""


class ProcessorError(Exception):
    """The gateway was reached but the operation failed; the message
    is the plain-language translation, safe to show an admin."""


# ---------------------------------------------------------------- config

class AuthNetConfig:
    """Environment snapshot, taken at construction (never at import).

    strip(): pasted secrets often carry stray whitespace/newlines;
    trimming them here fixes the most common "invalid credentials"
    failure without ever printing the values."""

    def __init__(self, environ=None):
        env = os.environ if environ is None else environ
        self.login_id = (env.get("AUTHNET_API_LOGIN_ID") or "").strip()
        self.txn_key = (env.get("AUTHNET_TRANSACTION_KEY") or "").strip()
        name = (env.get("AUTHNET_ENV") or "production").strip().lower()
        self.env = name if name in ENDPOINTS else "production"
        self.endpoint = ENDPOINTS[self.env]
        self.enabled_flag = (env.get("AUTHNET_ENABLED") or "").strip() == "1"
        self.echeck_flag = (env.get("AUTHNET_ECHECK_ENABLED") or "").strip() == "1"

    def missing(self):
        out = []
        if not self.login_id:
            out.append("AUTHNET_API_LOGIN_ID")
        if not self.txn_key:
            out.append("AUTHNET_TRANSACTION_KEY")
        return out

    def configured(self):
        return not self.missing()

    def disabled_reason(self):
        """None when enabled; otherwise why not, in plain language."""
        if not self.enabled_flag:
            return "AUTHNET_ENABLED is not set to 1"
        missing = self.missing()
        if missing:
            return "missing credentials in the environment: " + ", ".join(missing)
        return None

    def enabled(self):
        return self.disabled_reason() is None

    def echeck_disabled_reason(self):
        """None when eCheck charges may run; otherwise why not.
        The base processor gates apply first (processor switch +
        credentials), then the eCheck switch itself."""
        reason = self.disabled_reason()
        if reason is not None:
            return reason
        if not self.echeck_flag:
            return "AUTHNET_ECHECK_ENABLED is not set to 1"
        return None

    def echeck_enabled(self):
        return self.echeck_disabled_reason() is None

    def auth_block(self):
        return {"name": self.login_id, "transactionKey": self.txn_key}


def require_enabled(cfg):
    reason = cfg.disabled_reason()
    if reason is not None:
        raise ProcessorDisabled(
            f"Authorize.net processor is disabled ({reason}); "
            "charges are refused, nothing was sent to the gateway"
        )


def require_echeck_enabled(cfg):
    reason = cfg.echeck_disabled_reason()
    if reason is not None:
        raise ProcessorDisabled(
            f"Authorize.net eCheck is disabled ({reason}); "
            "eCheck charges are refused, nothing was sent to the gateway"
        )


def status(cfg=None):
    """Health-report block. Contains no credential material — only
    whether each gate is satisfied."""
    cfg = cfg or AuthNetConfig()
    return {
        "processor": PSP_NAME,
        "enabled": cfg.enabled(),
        "configured": cfg.configured(),
        "env": cfg.env,
        "disabled_reason": cfg.disabled_reason(),
        "echeck_enabled": cfg.echeck_enabled(),
        "echeck_disabled_reason": cfg.echeck_disabled_reason(),
    }


def log_state(cfg=None):
    """One boot log line. Never includes a credential value."""
    cfg = cfg or AuthNetConfig()
    st = status(cfg)
    if st["enabled"]:
        echeck = "ENABLED" if st["echeck_enabled"] else (
            f"DISABLED ({st['echeck_disabled_reason']})")
        print(
            f"payment-ledger authnet: ENABLED (env={st['env']}); "
            "credentials present (values never logged); "
            f"eCheck {echeck}",
            flush=True,
        )
    else:
        print(
            f"payment-ledger authnet: DISABLED ({st['disabled_reason']}); "
            "Authorize.net charges will be refused (fail closed)",
            flush=True,
        )
    return st


# ---------------------------------------------------------------- api

def _post_json(cfg, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        cfg.endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8-sig")
    except urllib.error.URLError as exc:
        raise ApiError("NETWORK", f"could not reach {cfg.endpoint}: {exc.reason}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError("BAD_RESPONSE", "gateway returned a non-JSON response")


def post(cfg, payload, transport=None):
    """POST one request; return the decoded response dict.

    Authorize.net answers JSON with a UTF-8 BOM (the default
    transport decodes utf-8-sig). Raises ApiError with the gateway's
    own code/text on an Error result. `transport` is injectable for
    offline tests: a callable (cfg, payload) -> response dict, held
    to the same resultCode validation as the real transport."""
    data = transport(cfg, payload) if transport is not None else _post_json(cfg, payload)
    messages = data.get("messages") or {}
    if messages.get("resultCode") != "Ok":
        msgs = messages.get("message") or [{}]
        first = msgs[0]
        raise ApiError(first.get("code", "UNKNOWN"), first.get("text", ""))
    return data


def explain_auth_failure(err):
    """Plain-language read of the common credential failures.
    (Wording carried over from the standalone pipeline script.)"""
    if err.code == "E00007":
        return ("the gateway rejected the credentials (E00007). Fix: confirm the "
                "API Login ID matches the portal exactly and that the Transaction "
                "Key is the one generated 2026-11-20; if the key was regenerated "
                "since, the old one is dead - update the secret.")
    if err.code == "E00003":
        return ("the request itself was malformed (E00003) - that is a pipeline "
                "bug, not an account problem.")
    if err.code == "NETWORK":
        return f"network problem reaching Authorize.net: {err.text}"
    return f"gateway said {err.code}: {err.text}"


def explain_charge_failure(resp):
    """Translate a declined/errored transaction response into plain
    words. (Wording carried over from the standalone script.)"""
    tr = resp.get("transactionResponse") or {}
    errors = tr.get("errors") or []
    if errors:
        e = errors[0]
        code, text = e.get("errorCode", ""), e.get("errorText", "")
        if code in ("11", "27", "8"):  # processing / AVS-class gateway refusals
            return (f"gateway refused the charge ({code}: {text}). If no card "
                    "problem explains it, Transaction Processing is likely still "
                    "off at the acquirer - check the portal banner.")
        return f"gateway refused the charge ({code}: {text})"
    msgs = (resp.get("messages") or {}).get("message") or []
    if msgs:
        return f"gateway: {msgs[0].get('code', '')} {msgs[0].get('text', '')}"
    return "gateway returned no transaction response"


# ---------------------------------------------------------------- amounts

def parse_amount(raw):
    """Decimal dollars, guarded by the account's $6,000 cap.
    (From the standalone script, unchanged.)"""
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"amount {raw!r} is not a number")
    if amount <= 0:
        raise ValueError("amount must be more than $0")
    if amount > SINGLE_TXN_CAP:
        raise ValueError(
            f"amount ${amount} exceeds the $6,000 single-transaction cap on "
            "this merchant account - split it or get the cap raised first")
    return amount


def parse_amount_minor(raw):
    """Ledger convention is minor units (integer cents). Returns
    (amount_minor, amount_dollars) with the same cap enforced."""
    try:
        minor = int(Decimal(str(raw)))
    except (InvalidOperation, ValueError):
        raise ValueError(f"amount {raw!r} is not a number of minor units")
    if Decimal(str(raw)) != minor:
        raise ValueError(f"amount {raw!r} is not a whole number of minor units")
    if minor <= 0:
        raise ValueError("amount must be more than $0")
    if minor > SINGLE_TXN_CAP_MINOR:
        raise ValueError(
            f"amount {minor} minor exceeds the $6,000 single-transaction cap "
            "on this merchant account - split it or get the cap raised first")
    return minor, Decimal(minor) / Decimal(100)


# ---------------------------------------------------------------- operations

def authenticate(cfg, transport=None):
    """authenticateTestRequest - proves the credentials, moves no money."""
    return post(cfg, {"authenticateTestRequest": {"merchantAuthentication": cfg.auth_block()}},
                transport=transport)


def authenticate_check(cfg=None, transport=None):
    """Credential check for admins. Raises ProcessorDisabled while
    disabled and ProcessorError (plain language) on gateway refusal."""
    cfg = cfg or AuthNetConfig()
    require_enabled(cfg)
    try:
        authenticate(cfg, transport=transport)
    except ApiError as err:
        raise ProcessorError("CREDENTIALS: FAILED - " + explain_auth_failure(err))
    return {
        "ok": True,
        "env": cfg.env,
        "message": f"CREDENTIALS: OK - API Login ID + Transaction Key accepted ({cfg.env}).",
    }


def build_charge_payload(cfg, amount, descriptor, nonce, invoice, description, email):
    """(From the standalone script, unchanged.)"""
    txn = {
        "transactionType": "authCaptureTransaction",
        "amount": f"{amount:.2f}",
        "payment": {"opaqueData": {"dataDescriptor": descriptor, "dataValue": nonce}},
        "transactionSettings": {"setting": [
            {"settingName": "duplicateWindow", "settingValue": "120"}]},
    }
    if invoice:
        txn["order"] = {"invoiceNumber": invoice, "description": description or ""}
    if email:
        txn["customer"] = {"email": email}
    return {"createTransactionRequest": {
        "merchantAuthentication": cfg.auth_block(),
        "transactionRequest": txn,
    }}


def interpret_charge_response(resp):
    """One gateway response -> a plain result dict. Approved only on
    transactionResponse.responseCode == "1"."""
    tr = resp.get("transactionResponse") or {}
    if tr.get("responseCode") == "1":
        msgs = tr.get("messages") or [{}]
        return {
            "approved": True,
            "trans_id": tr.get("transId"),
            "auth_code": tr.get("authCode"),
            "response_code": "1",
            "error_code": None,
            "message": msgs[0].get("description") or "approved",
        }
    errors = tr.get("errors") or [{}]
    return {
        "approved": False,
        "trans_id": tr.get("transId"),
        "auth_code": None,
        "response_code": tr.get("responseCode"),
        "error_code": errors[0].get("errorCode"),
        "message": explain_charge_failure(resp),
    }


def _execute_nonce_charge(cfg, amount_minor, nonce, descriptor,
                          invoice, description, email, transport):
    """Gateway call only — no ledger writes, no gate checks (the
    callers gate). A nonce is a nonce: card and bank (eCheck)
    Accept.js nonces spend identically, as an opaqueData
    authCaptureTransaction."""
    _, amount = parse_amount_minor(amount_minor)
    nonce = (nonce or "").strip()
    if not nonce:
        raise ValueError("nonce (Accept.js dataValue) is required")
    payload = build_charge_payload(cfg, amount, descriptor or DEFAULT_DESCRIPTOR,
                                   nonce, invoice, description, email)
    try:
        resp = post(cfg, payload, transport=transport)
    except ApiError as err:
        raise ProcessorError("CHARGE FAILED - " + explain_auth_failure(err))
    return interpret_charge_response(resp)


def execute_charge(cfg, amount_minor, nonce, descriptor=DEFAULT_DESCRIPTOR,
                   invoice="", description="", email="", transport=None):
    """Gateway call only — no ledger writes. Raises ProcessorDisabled,
    ValueError (bad amount / missing nonce), or ProcessorError."""
    require_enabled(cfg)
    return _execute_nonce_charge(cfg, amount_minor, nonce, descriptor,
                                 invoice, description, email, transport)


def execute_echeck_charge(cfg, amount_minor, nonce, descriptor=DEFAULT_DESCRIPTOR,
                          invoice="", description="", email="", transport=None):
    """The eCheck twin of execute_charge: same nonce spend, gated on
    the eCheck switch as well as the processor switch. The nonce
    must have been minted from bank data (Accept.js bankData); the
    gateway then processes the debit as eCheck (WEB)."""
    require_echeck_enabled(cfg)
    return _execute_nonce_charge(cfg, amount_minor, nonce, descriptor,
                                 invoice, description, email, transport)


def transaction_status(cfg=None, trans_id=None, transport=None):
    """getTransactionDetailsRequest summary for one transaction."""
    cfg = cfg or AuthNetConfig()
    require_enabled(cfg)
    if not trans_id:
        raise ValueError("trans_id is required")
    payload = {"getTransactionDetailsRequest": {
        "merchantAuthentication": cfg.auth_block(),
        "transId": str(trans_id),
    }}
    try:
        resp = post(cfg, payload, transport=transport)
    except ApiError as err:
        raise ProcessorError("STATUS FAILED - " + explain_auth_failure(err))
    txn = resp.get("transaction") or {}
    return {
        "trans_id": str(trans_id),
        "status": txn.get("transactionStatus"),
        "type": txn.get("transactionType"),
        "response_code": txn.get("responseCode"),
        "auth_amount": txn.get("authAmount"),
        "settle_amount": txn.get("settleAmount"),
    }


# ---------------------------------------------------------------- ledger recording

def _ledger():
    """Lazy import: this module stays psycopg-free at import time;
    the recording path uses the ledger package only when a charge
    actually runs inside the service."""
    from . import ledger

    return ledger


def _new_uuid():
    return str(uuid.uuid4())


def record_charge(cur, intent, attempt_id, result, amount_minor, currency,
                  event_extra=None):
    """Record a completed gateway charge through the ledger's
    existing write path, inside the caller's transaction.

    Approved: the canonical "captured" event goes to
    ledger.process_event — it claims the Authorize.net transaction
    id on the attempt, posts the capture journal under
    capture:{intent_id}, and walks the intent to CAPTURED.
    Declined: the attempt is marked DECLINED with the gateway's
    code and the intent walks to FAILED (the sandbox_authorize
    pattern). No journal is posted for a decline.
    `event_extra` merges additional markers into the captured
    event payload (eCheck carries payment_method / settlement
    state); process_event ignores keys it does not consume, so the
    canonical posting is unchanged. Returns the fresh intent row."""
    ledger = _ledger()
    if result["approved"]:
        payload = {
            "kind": "captured",
            "payment_intent_id": intent["id"],
            "attempt_id": attempt_id,
            "psp_name": PSP_NAME,
            "network_rrn": result["trans_id"],
            "amount": amount_minor,
            "currency": currency,
        }
        if event_extra:
            payload.update(event_extra)
        ledger.process_event(cur, {
            "payload": payload,
            "external_event_id": result["trans_id"],
        })
    else:
        cur.execute(
            "UPDATE payment_attempts SET status = 'DECLINED', raw_response_code = %s "
            "WHERE id = %s",
            (result.get("error_code") or result.get("response_code"), attempt_id),
        )
        ledger.move_status(cur, intent, "FAILED", attempt_id)
    return ledger.get_intent(cur, intent["id"])


def _create_intent_attempt(cur, data, amount_minor, currency, merchant_id, feats):
    """The intent + PENDING attempt every Authorize.net charge
    starts from, under idempotency key authnet:{key}. Returns
    (existing_intent, None, None) when the key repeats — a retried
    request never charges twice — else (None, intent_id,
    attempt_id)."""
    dedupe = data.get("idempotency_key") or data.get("invoice") or _new_uuid()
    intent_key = f"authnet:{dedupe}"

    cur.execute(
        "SELECT * FROM payment_intents WHERE idempotency_key = %s", (intent_key,)
    )
    existing = cur.fetchone()
    if existing:
        return existing, None, None

    intent_id = _new_uuid()
    cur.execute(
        "INSERT INTO payment_intents (id, idempotency_key, merchant_id, amount, currency, status) "
        "VALUES (%s, %s, %s, %s, %s, 'CREATED')",
        (intent_id, intent_key, merchant_id, amount_minor, currency),
    )
    cur.execute(
        "INSERT INTO payment_state_transitions (payment_intent_id, from_status, to_status) "
        "VALUES (%s, NULL, 'CREATED')",
        (intent_id,),
    )
    attempt_id = _new_uuid()
    cur.execute(
        "INSERT INTO payment_attempts (id, payment_intent_id, attempt_type, psp_name, "
        "psp_transaction_id, status, raw_response_code, features) "
        "VALUES (%s, %s, 'AUTHORIZATION', %s, NULL, 'PENDING', NULL, %s)",
        (attempt_id, intent_id, PSP_NAME, json.dumps(feats)),
    )
    return None, intent_id, attempt_id


def authnet_charge(cur, data, cfg=None, transport=None):
    """Full adapter used by POST /ledger/processor/authnet/charge.

    data: amount (minor units), nonce (Accept.js dataValue),
    descriptor?, invoice?, description?, email?, currency?,
    merchant_id?, idempotency_key? (or invoice, as the dedupe key).

    Creates the intent under idempotency key authnet:{key} and
    returns the existing intent untouched when the key repeats —
    a retried request never charges twice. Gateway-level errors
    raise ProcessorError BEFORE anything is recorded; the caller's
    rollback then leaves no partial rows.
    """
    cfg = cfg or AuthNetConfig()
    require_enabled(cfg)
    amount_minor, _ = parse_amount_minor(data.get("amount"))
    nonce = (data.get("nonce") or data.get("data_value") or "").strip()
    if not nonce:
        raise ValueError("nonce (Accept.js dataValue) is required")
    currency = (data.get("currency") or "USD").upper()
    merchant_id = data.get("merchant_id") or MERCHANT_ID
    feats = {
        "entry_mode": "CARD_NOT_PRESENT",
        "descriptor": data.get("descriptor") or DEFAULT_DESCRIPTOR,
    }
    if data.get("invoice"):
        feats["invoice"] = data["invoice"]
    existing, intent_id, attempt_id = _create_intent_attempt(
        cur, data, amount_minor, currency, merchant_id, feats)
    if existing is not None:
        return {"intent": existing, "deduplicated": True, "charged": False}

    result = execute_charge(
        cfg, amount_minor, nonce,
        descriptor=data.get("descriptor") or DEFAULT_DESCRIPTOR,
        invoice=data.get("invoice") or "",
        description=data.get("description") or "",
        email=data.get("email") or "",
        transport=transport,
    )
    intent = {"id": intent_id, "status": "CREATED", "amount": amount_minor,
              "currency": currency}
    intent = record_charge(cur, intent, attempt_id, result, amount_minor, currency)
    return {
        "intent": intent,
        "deduplicated": False,
        "charged": True,
        "approved": result["approved"],
        "trans_id": result["trans_id"],
        "auth_code": result["auth_code"],
        "message": result["message"],
    }


# ---------------------------------------------------------------- eCheck

ECHECK_TYPE = "WEB"  # the SEC code an Accept.js bank nonce spends as
SETTLEMENT_PENDING = "PENDING_SETTLEMENT"
ACCOUNT_TYPES = ("checking", "savings", "businessChecking")

# The public checkout sells exactly the member site's catalog, at
# the catalog prices — the buyer never names an amount. Keys and
# prices mirror services/member-site/app_pkg/catalog.py.
ECHECK_PRODUCTS = {
    "ccce": {
        "amount_minor": 1500,
        "invoice_prefix": "CCCE",
        "description": "CCCE course — Cultural Competence Continuing Education",
    },
    "emspr": {
        "amount_minor": 600000,
        "invoice_prefix": "EMSPR",
        "description": "EMSPR Protocols",
    },
}


def authnet_echeck_charge(cur, data, cfg=None, transport=None):
    """Full eCheck adapter used by the public checkout route.

    data: amount (minor units), nonce (Accept.js dataValue minted
    from bankData), authorization (must be exactly True — the
    buyer's WEB debit authorization from the buy-page checkbox),
    account_type?, descriptor?, invoice?, description?, email?,
    currency?, merchant_id?, idempotency_key?.

    Same intent/idempotency/recording discipline as authnet_charge;
    the differences are the gates (eCheck switch), the required
    authorization assertion, and the markers: the attempt features
    and the captured event carry payment_method=ECHECK and
    settlement_state=PENDING_SETTLEMENT, and an approved result
    reports that pending state — the gateway accepted the debit,
    the money has not settled yet.
    """
    cfg = cfg or AuthNetConfig()
    require_echeck_enabled(cfg)
    if data.get("authorization") is not True:
        raise ValueError(
            "the buyer must authorize the bank debit (the authorization "
            "checkbox on the buy page) before an eCheck charge can be "
            "submitted; nothing was charged")
    amount_minor, _ = parse_amount_minor(data.get("amount"))
    nonce = (data.get("nonce") or data.get("data_value") or "").strip()
    if not nonce:
        raise ValueError("nonce (Accept.js dataValue) is required")
    account_type = (data.get("account_type") or "checking").strip()
    if account_type not in ACCOUNT_TYPES:
        raise ValueError(
            "account_type must be one of: " + ", ".join(ACCOUNT_TYPES))
    currency = (data.get("currency") or "USD").upper()
    merchant_id = data.get("merchant_id") or MERCHANT_ID
    feats = {
        "entry_mode": "ECHECK",
        "payment_method": "ECHECK",
        "echeck_type": ECHECK_TYPE,
        "settlement_state": SETTLEMENT_PENDING,
        "account_type": account_type,
        "descriptor": data.get("descriptor") or DEFAULT_DESCRIPTOR,
    }
    if data.get("invoice"):
        feats["invoice"] = data["invoice"]
    existing, intent_id, attempt_id = _create_intent_attempt(
        cur, data, amount_minor, currency, merchant_id, feats)
    if existing is not None:
        return {"intent": existing, "deduplicated": True, "charged": False}

    result = execute_echeck_charge(
        cfg, amount_minor, nonce,
        descriptor=data.get("descriptor") or DEFAULT_DESCRIPTOR,
        invoice=data.get("invoice") or "",
        description=data.get("description") or "",
        email=data.get("email") or "",
        transport=transport,
    )
    intent = {"id": intent_id, "status": "CREATED", "amount": amount_minor,
              "currency": currency}
    intent = record_charge(
        cur, intent, attempt_id, result, amount_minor, currency,
        event_extra={
            "payment_method": "ECHECK",
            "settlement_state": SETTLEMENT_PENDING,
        },
    )
    return {
        "intent": intent,
        "deduplicated": False,
        "charged": True,
        "approved": result["approved"],
        "trans_id": result["trans_id"],
        "auth_code": result["auth_code"],
        "message": result["message"],
        "payment_method": "ECHECK",
        "settlement_state": SETTLEMENT_PENDING if result["approved"] else None,
    }


def checkout_echeck(cur, data, cfg=None, transport=None):
    """Public buy-page checkout: POST /ledger/checkout/echeck.

    data: product (catalog key), nonce, descriptor?, email,
    authorization, account_type?, idempotency_key?.

    The amount comes from the server-side catalog (ECHECK_PRODUCTS),
    never from the request. The buyer's email is required (it is the
    member account the purchase belongs to, and the gateway's
    customer email). Returns a buyer-safe result dict — no intent
    internals, no credential material.
    """
    cfg = cfg or AuthNetConfig()
    require_echeck_enabled(cfg)
    product_key = (data.get("product") or "").strip().lower()
    product = ECHECK_PRODUCTS.get(product_key)
    if product is None:
        raise ValueError(
            "unknown product for ACH checkout: "
            f"{product_key or '(missing)'} — expected one of: "
            + ", ".join(sorted(ECHECK_PRODUCTS)))
    email = (data.get("email") or "").strip()
    if not email or "@" not in email:
        raise ValueError("the buyer's account email is required")
    dedupe = (data.get("idempotency_key") or "").strip() or _new_uuid()
    invoice = f"{product['invoice_prefix']}-{dedupe[:8]}"
    charge_data = {
        "amount": product["amount_minor"],
        "nonce": data.get("nonce") or data.get("data_value") or "",
        "descriptor": data.get("descriptor") or DEFAULT_DESCRIPTOR,
        "invoice": invoice,
        "description": product["description"],
        "email": email,
        "authorization": data.get("authorization"),
        "account_type": data.get("account_type") or "checking",
        "idempotency_key": dedupe,
    }
    result = authnet_echeck_charge(cur, charge_data, cfg=cfg, transport=transport)
    out = {
        "product": product_key,
        "amount_minor": product["amount_minor"],
        "deduplicated": result["deduplicated"],
        "charged": result["charged"],
    }
    if result["charged"]:
        out.update({
            "approved": result["approved"],
            "trans_id": result["trans_id"],
            "message": result["message"],
            "payment_method": result["payment_method"],
            "settlement_state": result["settlement_state"],
        })
    else:
        out.update({
            "approved": None,
            "trans_id": None,
            "message": "this payment was already submitted — it is not "
                       "being charged twice",
            "payment_method": "ECHECK",
            "settlement_state": None,
        })
    return out
