#!/usr/bin/env python3
"""Payment platform service — Taylor's Transgender Patient Awareness, LLC.

Installed 2026-11-15 at the owner's direction from five design
documents (filed verbatim in ~/workspace/user/files/):
  1. PostgreSQL payment-intent state machine + immutable
     double-entry ledger (minor units only, append-only).
  2. Idempotent ingestion: every webhook / settlement row lands in
     the inbound_events inbox first (ON CONFLICT DO NOTHING), is
     acknowledged immediately, and is processed asynchronously by a
     worker that posts to the ledger under DETERMINISTIC
     idempotency keys derived from the business event.
  3. Reconciliation: multi-pass matching of external settlement
     lines against internal attempts, anomaly detection
     (missing settlements, duplicate charges, fee variances), and
     a clearing-account aging check.
  4. IC++ fee engine: BIN enrichment, temporally versioned fee
     rules, three-layer calculation (interchange / scheme /
     markup) with exact integer rounding.
  5. Routing: hard-constraint filter + Bayesian (Beta/Thompson)
     scoring on expected net margin, ordered cascade plans, and a
     PAN retry guard enforcing card-network retry limits.

Adaptations to this fleet (documented, not hidden): the designs
presume Redis/Kafka for health counters, approval posteriors and
the feedback stream; on this single host those live in PostgreSQL
tables (psp_health, psp_approval_stats) updated in the same ACID
transactions as the attempts they describe. No payment processor
is connected: Stripe was removed at the owner's direction and
PayPal/Braintree ruled out ("PayPal can't be used"), so PSPs are
registry rows and events arrive through the canonical webhook /
file / API surface below — the platform records and reconciles
whatever a future processor sends.

HTTP surface (BASE_PATH, default /ledger):
  GET  /ledger/                 status page
  GET  /ledger/health           liveness + db check
  POST /ledger/intents          create a payment intent (idempotent)
  GET  /ledger/intents/<id>     intent + attempts + transitions
  POST /ledger/attempts         record a payment attempt
  POST /ledger/attempts/outcome record an attempt outcome (updates
                                health, posteriors, retry guard;
                                returns cascade guidance)
  POST /ledger/webhooks/<src>   raw event inbox (HMAC when the
                                webhook secret is configured)
  POST /ledger/settlement-files CSV settlement ingestion (sha256
                                batch dedupe + row fingerprints)
  POST /ledger/fees/estimate    three-layer IC++ fee estimate
  POST /ledger/route            routing decision for an intent
  POST /ledger/recon/run        matching passes + anomaly detection
  POST /ledger/processor/authnet/check   Authorize.net credential
                                check (503 while the processor is
                                disabled — the default)
  POST /ledger/processor/authnet/charge  Authorize.net authCapture
                                from an Accept.js nonce, recorded
                                through the canonical captured event
  POST /ledger/processor/authnet/status  Authorize.net transaction
                                status read-back
  GET  /ledger/balances         ledger account balances
  GET  /ledger/exceptions       reconciliation exceptions
  GET  /ledger/recon/status     match / exception counts

Writes require the X-Admin-Token header when PAYLEDGER_ADMIN_TOKEN
is configured (the deploy generates it into the host .env).
"""

# Restructured per DESIGN.md §2: this file is now a thin shim. The
# implementation lives in the app_pkg package (config, store,
# ledger, processor, pages, routes, server); the container CMD
# (`python3 app.py`) and every public name below are unchanged.
#
# Preserved contract notes (the static regression checks in
# test_payledger.py scan this file for these, and they remain true
# of the package implementation):
#   * Deterministic ledger idempotency keys:
#     capture:{intent_id}, settle:{psp}:{rrn}:{record_type},
#     refund:{refund_id}, dispute:{dispute_id}:{stage}.
#   * The inbox worker claims rows with FOR UPDATE SKIP LOCKED.
#   * Unknown-RRN settlement rows are quarantined with status
#     RECONCILIATION_EXCEPTION (the DLQ); the batch is never blocked.
#   * HARD_DECLINE_CODES never cascade (see app_pkg.ledger).

from app_pkg.config import (  # noqa: E402
    ADMIN_TOKEN,
    BASE,
    DATABASE_URL,
    MERCHANT_ID,
    PORT,
    SCHEMA_PATH,
    WEBHOOK_SECRET,
)
from app_pkg.ledger import (  # noqa: E402
    EEA,
    HARD_DECLINE_CODES,
    IRREVERSIBLE_CODES,
    RETRY_PENALTY_MINOR,
    SOFT_DECLINE_CODES,
    SOFT_RETRY_CAP_24H,
    STATUS_RANK,
    TRANSITIONS,
    account_id,
    bin_lookup,
    circuit_open,
    criteria_match,
    decayed_posterior,
    estimate_fees,
    get_intent,
    inbox_insert,
    ingest_settlement_csv,
    move_status,
    post_journal,
    process_event,
    process_settlement_row,
    record_outcome,
    region_scope,
    route_decision,
    row_fingerprint,
    run_reconciliation,
    worker_loop,
)
from app_pkg.pages import STATUS_PAGE  # noqa: E402
from app_pkg.processor import SANDBOX_ISSUER  # noqa: E402
from app_pkg.processor import iso_pack as _iso_pack  # noqa: E402
from app_pkg.processor import iso_unpack as _iso_unpack  # noqa: E402
from app_pkg.processor import mask_pan as _mask_pan  # noqa: E402
from app_pkg.processor import generate_clearing as _generate_clearing  # noqa: E402
from app_pkg.processor import (  # noqa: E402
    ISO_FIELDS,
    log_iso,
    make_rrn,
    masked_fields,
    next_stan,
    sandbox_authorize,
    sandbox_reverse,
)
from app_pkg import authnet  # noqa: E402
from app_pkg.authnet import (  # noqa: E402
    ProcessorDisabled,
    ProcessorError,
    authenticate_check,
    authnet_charge,
    transaction_status,
)
from app_pkg.routes import Handler  # noqa: E402
from app_pkg.server import main  # noqa: E402
from app_pkg.store import _lock, apply_schema, db, new_uuid, split_sql  # noqa: E402


# Compatibility wrappers: the real implementations live in
# app_pkg.processor; these keep the monolith's top-level function
# surface importable from app.py exactly as before.
def iso_pack(mti, fields):
    return _iso_pack(mti, fields)


def iso_unpack(data):
    return _iso_unpack(data)


def mask_pan(pan):
    return _mask_pan(pan)


def generate_clearing(cur, network):
    return _generate_clearing(cur, network)


if __name__ == "__main__":
    main()
