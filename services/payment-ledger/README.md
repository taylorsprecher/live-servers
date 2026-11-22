# Payment Ledger Platform

Installed 2026-11-15 at the owner's direction from five design
documents, filed verbatim in `~/workspace/user/files/`:

1. `Payment_Database_Schema_StateMachine_Ledger_pasted_2026-11-15.txt`
2. `Payment_Ingestion_Idempotency_Pipeline_pasted_2026-11-15.txt`
3. `Payment_Reconciliation_Engine_pasted_2026-11-15.txt`
4. `Payment_Fee_Engine_ICpp_pasted_2026-11-15.txt`
5. `Payment_Routing_Orchestration_Engine_pasted_2026-11-15.txt`

## What runs here

- `paydb` — PostgreSQL 16 (own volume `payledger-data`, no published
  ports, frugal settings for this host: 64 MB shared buffers, 20
  connections).
- `payledger` — this service (Python + psycopg), published on
  127.0.0.1:18123 and routed by the gateway at
  `https://members.911transedu.net/ledger/`. It applies `schema.sql`
  idempotently at boot and runs the ingestion worker.

## What it does

- **Ledger core** — payment-intent state machine (DB trigger),
  immutable double-entry journal (balanced-journal deferred
  constraint trigger; update/delete blocked), minor units only.
- **Ingestion** — webhooks and settlement CSVs land raw in
  `inbound_events` (`UNIQUE(source_system, external_event_id)`,
  `ON CONFLICT DO NOTHING`), are acknowledged immediately, and are
  processed by a worker using `FOR UPDATE SKIP LOCKED`. Ledger
  postings use deterministic keys (`capture:{intent}`,
  `settle:{psp}:{rrn}:{type}`, `refund:{refund_id}`,
  `dispute:{id}:{stage}`), so duplicate deliveries and overlapping
  files never double-count. Settlement files are deduplicated by
  SHA-256 at the batch level and by row fingerprint; unknown RRNs
  are quarantined as `RECONCILIATION_EXCEPTION`, never blocking the
  batch. Capture posts DEBIT `acquirer_clearing_expected` / CREDIT
  `merchant_payable_main`; settlement posts DEBIT `bank_cash` +
  DEBIT `psp_processing_fee_expense` / CREDIT
  `acquirer_clearing_expected`.
- **Reconciliation** — `POST /ledger/recon/run`: EXACT_RRN →
  PSP_TX_ID → HEURISTIC (±48h) matching, bank payout rollup check,
  the three detection queries (missing settlements T+3, duplicate
  charges, fee variances with 2-minor tolerance), auto-resolution of
  sub-$0.05 fee variances via the `fee_rounding_variance` account,
  and the clearing-account aging check.
- **Fee engine** — BIN-range enrichment (smallest containing range),
  temporally versioned `fee_rules`, three layers (single best-match
  interchange with downgrade simulation when capture is >24h late or
  AVS/CVV failed; additive scheme/cross-border stack; contract
  markup, IC++ or blended), exact integer rounding via
  `calculate_line_fee`. Verified against the design's worked
  example: $100 US rewards CNP = $2.36 qualified, $2.81 downgraded.
- **Routing** — hard-constraint filter (currency/network/3DS/circuit
  breaker), Beta-posterior Thompson sampling on expected net margin
  (fee engine output + $0.05 retry penalty), ordered cascade plan,
  immutable `routing_decisions` log, and the PAN retry guard
  (hard declines 51/54/14/59/41/43 never cascade; irreversible codes
  block the PAN after one occurrence; soft-decline cap 2/24h).

## Processor layer (sandbox)

Added 2026-11-15 at the owner's direction from the payment-processor
design (`Payment_Processor_Core_Engine_pasted_2026-11-15.txt`). It is
a **sandbox**: there is no sponsor bank, no network-issued acquirer
BIN, no HSM, no PCI certification, and no socket to any card network.
The issuer is a deterministic simulator keyed to published test card
numbers (4111…/4242…/5555…4444 approve; 4000…0002 → 51, …0069 → 54,
…0127 → 59). Test PANs are used transiently and stored nowhere; the
wire log masks DE 2.

- `network_stan_sequences` — atomic circular STAN (1..999999) per
  (acquirer_bin, network); RRNs are built as
  year-digit + Julian day + hour + STAN (12 chars).
- ISO 8583 codec — MTI + primary/secondary bitmaps; DE 2 LLVAR,
  DE 3/4/7/11/12/13/14/22/25 numeric, DE 37/38/39/41/42/49 fixed,
  DE 55 LLLVAR (hex-carried TLV), DE 70, DE 90. Round-trip verified.
- `POST /ledger/processor/authorize` — builds MTI 0100, logs OUT,
  simulates the issuer, logs MTI 0110 IN, and records the outcome on
  a real ledger payment intent (AUTHORIZED or FAILED).
- `POST /ledger/processor/reverse` — MTI 0420 with DE 90 original
  data elements; intent → VOIDED. `POST /ledger/processor/echo` —
  MTI 0800/0810 (DE 70 = 301), simulated.
- `iso8583_message_log` — immutable wire audit (UPDATE/DELETE
  blocked by trigger). The stored `raw_hex` is re-packed from the
  masked fields, so DE 2 is masked in the stored bytes exactly as
  in `parsed_de`; the unmasked message exists only transiently in
  memory during the simulated send.
- Clearing — `POST /ledger/processor/clearing/generate` sweeps
  CAPTURED sandbox intents into a Base II-style fixed-width file
  (HDR / TC05 records with masked PAN, RRN, amount, currency, CPS
  flag / TRL with count + gross), tracked in `clearing_batches` +
  `clearing_batch_items`; `transmit` marks GENERATED → ACKED
  (simulated; no endpoint exists).
- FBO wires — `POST /ledger/processor/fbo-wires` records a sponsor
  bank net wire and reconciles it against the expected net (ACKED
  batch gross minus fee estimates) for that network/date.

## Adaptations (documented, per the owner's no-hiding rule)

- The designs presume Redis/Kafka for circuit breakers, approval
  posteriors, and the feedback stream. On this single host those
  live in PostgreSQL (`psp_health`, `psp_approval_stats`), updated
  in the same ACID transactions as the attempts they describe.
- `bin_ranges` uses a GiST index with smallest-containing-range
  lookup rather than an exclusion constraint, because real BIN
  tables nest ranges (an exclusion constraint would forbid that).
- Authorization holds change state only; no journal is posted until
  capture moves money (the designs name no accounts for a hold).
- `payment_attempts.features` (JSONB) carries the enrichment
  attributes the posteriors key off.
- **No payment processor is connected.** Stripe was removed at the
  owner's direction and PayPal/Braintree ruled out ("PayPal can't be
  used"). PSPs are rows in the `psps` registry; events arrive via
  the canonical webhook endpoint, settlement-file upload, or API.
  Buyer-facing sales continue on the member site's Cash App +
  GoDaddy Pay Links rails; this platform records and reconciles.

## Access

- Reads (`/ledger/`, `/ledger/health`, `/ledger/balances`,
  `/ledger/exceptions`, `/ledger/recon/status`, intent detail) are
  public like the fleet's other status pages.
- Writes require header `X-Admin-Token` matching
  `PAYLEDGER_ADMIN_TOKEN`; webhooks verify `X-Signature-256`
  (HMAC-SHA256 of the raw body) when `PAYLEDGER_WEBHOOK_SECRET` is
  set. Both secrets, plus `PAYLEDGER_DB_PASSWORD`, are generated by
  `ops/deploy-payledger.sh` into the host `.env` only — never in
  git, never printed.

## Authorize.net processor (built 2026-11-20, DISABLED by default)

`app_pkg/authnet.py` is the platform's first real PSP adapter, for
the owner's Authorize.net merchant account (acquirer Global
Payments, Gateway 2886423; approved 2026-11-20, Acquirer Agreement
accepted the same day). It adapts the standalone pipeline script
(`~/workspace/user/files/authorize_net_pipeline.py`): same payload
shapes, same plain-language error wording, same account caps —
largest single transaction **$6,000**, monthly sales **$6,000**
(enforced in code; an EMSPR sale at $6,000 exactly hits both).

- **Enable gate (fail closed):** the processor runs only when
  `AUTHNET_ENABLED=1` AND both `AUTHNET_API_LOGIN_ID` and
  `AUTHNET_TRANSACTION_KEY` are present in the service environment.
  In every other state the routes answer **503** with the plain
  reason, the boot log prints one `authnet: DISABLED (...)` line,
  and `/ledger/health` reports the state under its `authnet` key.
  Credential values are never logged or stored anywhere.
- **Credential delivery:** `deploy-payledger.yml` pipes the two
  GitHub repo secrets over SSH stdin into the host `.env`
  (`ops/set-authnet-creds.sh`, 0600 upsert — the zillion/proxy
  pattern) and compose passes them through. Delivery alone does
  **not** enable anything; `AUTHNET_ENABLED=1` in the host `.env`
  plus a redeploy is the separate, deliberate on-switch. Charges
  additionally cannot succeed until Global Payments enables
  Transaction Processing on the account (acquirer-side; the portal
  toggle was still greyed out when the agreement was accepted).
- **Routes** (all behind the same `X-Admin-Token` gate as every
  write): `POST /ledger/processor/authnet/check` (credential check,
  no money), `POST /ledger/processor/authnet/status` (gateway
  read-back of one transaction), `POST /ledger/processor/authnet/charge`
  — body `{amount (minor units), nonce, descriptor?, invoice?,
  description?, email?, currency?, idempotency_key?}`.
- **How a charge records:** the buyer's browser tokenizes the card
  with Accept.js; this service only ever sees the nonce
  (opaqueData) — no PAN touches the fleet. The adapter creates the
  intent under idempotency key `authnet:{key}` (a repeat never
  charges twice) plus a `PENDING` attempt, submits
  `authCaptureTransaction`, and on approval hands the canonical
  **captured** event to `ledger.process_event` — the existing write
  path — which claims the gateway transaction id on the attempt,
  posts the capture journal under `capture:{intent_id}`, and walks
  the intent to `CAPTURED`. Declines mark the attempt `DECLINED`
  and the intent `FAILED`; nothing is posted. Settlement then
  arrives through the canonical surfaces like any other PSP's
  (webhook / settlement file carrying the transaction id) and posts
  `settle:authorizenet:{transId}:SALE`. **Open decision:** the
  Authorize.net webhook signature key (a third credential) is not
  wired — settlement can equally arrive via the settlement-file
  route, which needs no new secret.
- **Tests:** `test_authnet.py` (offline: payload shape, cap guard,
  disabled/missing-credential behavior, recording seam against a
  fake ledger) runs anywhere with plain `python3`; the static
  checks in `test_payledger.py` and the scratch-DB behavioral gate
  (`ops/payledger_behavioral.py` §1b: dummy-nonce charge never
  approves, never posts) cover the wiring at deploy time.

## eCheck (ACH) checkout (built 2026-11-21, DISABLED by default)

eCheck.Net was activated on the merchant account 2026-11-21
(limits identical to cards: $6,000 per transaction / $6,000 per
month; types PPD/WEB/CCD/TEL; funds hold 5 business days). The
adapter's card path spends Accept.js nonces; Accept.js tokenizes
bank accounts identically (`secureData.bankData` → `opaqueData`,
per the Accept.js documentation), and a bank nonce spent in
`createTransactionRequest` is an eCheck debit (SEC code WEB).
Bank routing/account numbers therefore never reach this service,
exactly like card numbers.

- **Second switch (fail closed):** eCheck charges require
  `AUTHNET_ENABLED=1` AND `AUTHNET_ECHECK_ENABLED=1` AND the
  credentials. `/ledger/health` reports `echeck_enabled` /
  `echeck_disabled_reason` alongside the card state. The
  deliberate flip is `deploy-payledger.yml`'s `enable_echeck`
  input (`ops/set-authnet-echeck-enabled.sh`) — a plain deploy
  changes neither switch.
- **Public route:** `POST /ledger/checkout/echeck` — the ledger's
  one public write, called by the member site's buy pages. It is
  not behind the admin token; safety is the fail-closed gates,
  a **server-side catalog** (`authnet.ECHECK_PRODUCTS`: ccce
  $15.00, emspr $6,000.00 — the buyer never names an amount), a
  required buyer **authorization assertion** (the WEB debit
  authorization checkbox on the buy page; without it the charge
  is refused before anything is created), and the same
  `authnet:{key}` idempotency discipline as card charges.
- **Recording:** through the same canonical captured event as
  cards, with markers: the attempt features and the captured
  event carry `payment_method=ECHECK`, `echeck_type=WEB`,
  `settlement_state=PENDING_SETTLEMENT` — an approved eCheck
  response means the gateway accepted the debit; the money
  settles ~5 business days later through the canonical
  settlement surfaces, and a returned debit surfaces through
  reconciliation like any other exception.
- **Buy pages:** the member site renders an "Or pay by bank
  account — ACH" section only while configured (the GoDaddy
  section's pattern): `AUTHNET_ECHECK_ENABLED=1` plus the API
  Login ID plus the **Public Client Key** (`AUTHNET_CLIENT_KEY`)
  in the members environment. The Login ID and Client Key are
  browser-public by design; the Transaction Key is never passed
  to the member site. Go-live order: fix the stored Transaction
  Key (the E00007 fix), deploy payledger with `enable_echeck`,
  generate the Public Client Key in the portal (Account →
  Settings → Security Settings → Manage Public Client Key) into
  the host `.env`, redeploy members.

## SecureStore checkout-receipt vault (added 2026-11-21, dark)

`app_pkg/vault.py` applies the owner's SecureStore engine
(`app_pkg/securestore.py` — the module filed 2026-11-10, with its
appdirs dependency vendored as `app_pkg/appdirs.py`) to the public
checkout: after a `POST /ledger/checkout/echeck` commits, a
buyer-safe receipt (product, amount, buyer email, transaction id,
approval and settlement state — never the payment nonce, never bank
numbers) is written as a Fernet-encrypted file
(`receipt-<trans_id>.enc`) on the `payledger-vault` volume, giving
the owner an encrypted, tamper-evident copy of each sale independent
of Postgres. Recording is best-effort and can never affect the
buyer's result. **Third switch (fail closed):** the vault records
only while the host `.env` sets `PAYLEDGER_SECURESTORE_ENABLED=1`
AND delivers a valid Fernet key in `PAYLEDGER_SECURESTORE_KEY`
(host `.env` only, never in git); a plain deploy changes neither.
`/ledger/health` reports `securestore_enabled`. Checks:
`test_vault.py` (17 offline checks: gates, ciphertext at rest,
round-trip, never-raise).

## Auto-renewal (added 2026-11-21, dark)

`app_pkg/renewal.py` adds opt-in auto-renewal for the checkout
catalog (CCCE $15 every two years, EMSPR $6,000 every year — the
catalog's own cadences). Authorize.net's ARB cannot express a
24-month cycle, so the platform is the scheduler: a buyer who
checks the buy page's auto-renew box (the recurring WEB debit
authorization, version `recurring-web-v1`) is enrolled FROM their
completed first transaction into an Authorize.net Customer Profile
— the bank account lives only at the gateway; this service stores
profile ids, the consent timestamp, and the SHA-256 hash of a
one-time cancel token the buyer is shown once. A daemon loop
(server.py, beside the ingestion worker) charges due profiles
through the same intent + canonical captured-event path as a first
charge (ECHECK / PENDING_SETTLEMENT / is_recurring markers,
deterministic per-period idempotency, one commit per profile), and
files a SecureStore receipt per renewal. Declines retry twice more
at 3-day intervals, then park at NEEDS_ATTENTION; gateway errors
record nothing and retry the next day. Cancellation: the buyer via
`POST /ledger/renewals/cancel` (email + product + token), the owner
via the admin routes (`GET /ledger/admin/renewals`,
`POST /ledger/admin/renewals/cancel`). **Fourth switch (fail
closed):** everything refuses unless the host `.env` sets
`AUTHNET_RENEWAL_ENABLED=1` on top of the processor and eCheck
switches (deploy-payledger's `enable_renewal` input upserts it;
the same entry arms the buy-page checkbox after a members
redeploy). `/ledger/health` reports `renewal_enabled`. Checks:
`test_renewal.py` (31 offline checks: gates, anniversary math, CIM
payloads, enrollment/cancel, all four scheduler outcomes).

## Verified before deploy (local PostgreSQL 16, 2026-11-15)

Schema applies cleanly; invalid state transition rejected;
unbalanced journal rejected at commit; ledger mutation rejected;
intent + webhook dedupe; capture → settle lifecycle lands bank
9710 / fee 290 / clearing 0 on a $100 capture with a $2.90 fee;
file re-upload deduplicated; EXACT_RRN match; clearing check
difference 0; fee example reproduces $2.36 / $2.81; routing picks
the higher-EV PSP in 3 ms; hard decline blocks cascade, soft
decline permits it; unknown-RRN row quarantined without blocking.
Processor layer (same day): ISO codec round-trip incl. secondary
bitmap; STAN sequence increments; sandbox authorize approves 4111…
(intent AUTHORIZED) and declines 4000…0002 with code 51 (FAILED);
message log stores masked DE 2 only (byte-checked — no PAN in the
stored hex); capture → clearing batch with correct trailer totals;
FBO wire reconciles at the expected net and flags a wrong amount;
reversal posts MTI 0420 and voids the intent.
