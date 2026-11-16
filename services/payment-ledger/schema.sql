-- Payment platform schema — installed 2026-11-15 at the owner's
-- direction from five design documents (filed verbatim in
-- ~/workspace/user/files/Payment_*_pasted_2026-11-15.txt):
--   1. Ledger core: payment-intent state machine + immutable
--      double-entry ledger (minor units only, append-only).
--   2. Idempotent ingestion: inbound_events inbox + settlement
--      batches; deterministic ledger idempotency keys.
--   3. Reconciliation: external lines, matches, exceptions.
--   4. IC++ fee engine: BIN ranges, versioned fee rules, integer
--      rounding function, itemized estimates, merchant contracts.
--   5. Routing: PSP registry + health + approval posteriors,
--      routing rules/decisions, PAN retry guard.
-- Idempotent: safe to apply on every boot.

-- gen_random_uuid() is core PostgreSQL (13+); no extensions needed.

-- =====================================================================
-- 1. OPERATIONAL STATE MACHINE
-- =====================================================================
CREATE TABLE IF NOT EXISTS payment_intents (
    id UUID PRIMARY KEY,
    idempotency_key VARCHAR(128) UNIQUE NOT NULL,
    merchant_id UUID NOT NULL,
    amount BIGINT NOT NULL CHECK (amount > 0),
    currency CHAR(3) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'CREATED',
    version INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_attempts (
    id UUID PRIMARY KEY,
    payment_intent_id UUID NOT NULL REFERENCES payment_intents(id),
    attempt_type VARCHAR(24) NOT NULL,
    psp_name VARCHAR(64) NOT NULL,
    psp_transaction_id VARCHAR(128) UNIQUE,
    status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
    raw_response_code VARCHAR(32),
    -- Enrichment features (design doc 4, section 1) captured at
    -- attempt time; the routing posteriors key off these.
    features JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_attempts_intent ON payment_attempts(payment_intent_id);
CREATE INDEX IF NOT EXISTS idx_attempts_psp_tx ON payment_attempts(psp_transaction_id);

CREATE TABLE IF NOT EXISTS payment_state_transitions (
    id BIGSERIAL PRIMARY KEY,
    payment_intent_id UUID NOT NULL REFERENCES payment_intents(id),
    from_status VARCHAR(32),
    to_status VARCHAR(32) NOT NULL,
    trigger_attempt_id UUID REFERENCES payment_attempts(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_transitions_intent ON payment_state_transitions(payment_intent_id);

-- =====================================================================
-- 2. DOUBLE-ENTRY LEDGER
-- =====================================================================
CREATE TABLE IF NOT EXISTS ledger_accounts (
    id UUID PRIMARY KEY,
    code VARCHAR(64) UNIQUE NOT NULL,
    account_type VARCHAR(16) NOT NULL,
    normal_balance VARCHAR(8) NOT NULL CHECK (normal_balance IN ('DEBIT', 'CREDIT')),
    currency CHAR(3) NOT NULL,
    allow_negative BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS ledger_transactions (
    id UUID PRIMARY KEY,
    idempotency_key VARCHAR(128) UNIQUE NOT NULL,
    payment_intent_id UUID REFERENCES payment_intents(id),
    description TEXT NOT NULL,
    posted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id UUID PRIMARY KEY,
    ledger_transaction_id UUID NOT NULL REFERENCES ledger_transactions(id),
    account_id UUID NOT NULL REFERENCES ledger_accounts(id),
    direction VARCHAR(8) NOT NULL CHECK (direction IN ('DEBIT', 'CREDIT')),
    amount BIGINT NOT NULL CHECK (amount > 0),
    currency CHAR(3) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_tx ON ledger_entries(ledger_transaction_id);
CREATE INDEX IF NOT EXISTS idx_entries_account ON ledger_entries(account_id);

-- =====================================================================
-- 3. INGESTION INBOX + SETTLEMENT BATCHES
-- =====================================================================
CREATE TABLE IF NOT EXISTS inbound_events (
    id UUID PRIMARY KEY,
    source_system VARCHAR(32) NOT NULL,
    external_event_id VARCHAR(128) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    last_error TEXT,
    CONSTRAINT uq_inbound_source_event UNIQUE (source_system, external_event_id)
);
CREATE INDEX IF NOT EXISTS idx_inbound_status ON inbound_events(status, received_at);

CREATE TABLE IF NOT EXISTS settlement_batches (
    id UUID PRIMARY KEY,
    filename TEXT NOT NULL,
    file_sha256 CHAR(64) UNIQUE NOT NULL,
    source_system VARCHAR(32) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'RECEIVED',
    row_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

-- =====================================================================
-- 4. RECONCILIATION
-- =====================================================================
CREATE TABLE IF NOT EXISTS external_settlement_lines (
    id UUID PRIMARY KEY,
    batch_id UUID NOT NULL REFERENCES settlement_batches(id),
    psp_name VARCHAR(64) NOT NULL,
    network_rrn VARCHAR(64),
    psp_transaction_id VARCHAR(128),
    record_type VARCHAR(24) NOT NULL,
    gross_amount BIGINT NOT NULL,
    fee_amount BIGINT NOT NULL,
    net_amount BIGINT NOT NULL,
    expected_fee_amount BIGINT,
    currency CHAR(3) NOT NULL,
    settlement_date DATE NOT NULL,
    recon_status VARCHAR(24) NOT NULL DEFAULT 'UNMATCHED'
);
CREATE INDEX IF NOT EXISTS idx_esl_rrn ON external_settlement_lines(network_rrn);
CREATE INDEX IF NOT EXISTS idx_esl_psp_tx ON external_settlement_lines(psp_transaction_id);
CREATE INDEX IF NOT EXISTS idx_esl_status ON external_settlement_lines(recon_status);

CREATE TABLE IF NOT EXISTS reconciliation_matches (
    id UUID PRIMARY KEY,
    payment_attempt_id UUID REFERENCES payment_attempts(id),
    ledger_transaction_id UUID REFERENCES ledger_transactions(id),
    external_line_id UUID NOT NULL UNIQUE REFERENCES external_settlement_lines(id),
    match_rule VARCHAR(32) NOT NULL,
    matched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reconciliation_exceptions (
    id UUID PRIMARY KEY,
    exception_type VARCHAR(32) NOT NULL,
    severity VARCHAR(16) NOT NULL,
    payment_intent_id UUID REFERENCES payment_intents(id),
    external_line_id UUID REFERENCES external_settlement_lines(id),
    discrepancy_amount BIGINT NOT NULL,
    currency CHAR(3) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'OPEN',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_exceptions_status ON reconciliation_exceptions(status, exception_type);

-- =====================================================================
-- 5. FEE ENGINE (IC++)
-- =====================================================================
CREATE TABLE IF NOT EXISTS bin_ranges (
    bin_range INT8RANGE PRIMARY KEY,
    network VARCHAR(16) NOT NULL,
    funding_type VARCHAR(16) NOT NULL,
    card_product VARCHAR(32) NOT NULL,
    issuer_country CHAR(2) NOT NULL,
    is_regulated BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_bin_ranges_gist ON bin_ranges USING GIST (bin_range);

CREATE TABLE IF NOT EXISTS fee_rules (
    id UUID PRIMARY KEY,
    fee_category VARCHAR(24) NOT NULL,
    program_code VARCHAR(64) NOT NULL,
    network VARCHAR(16) NOT NULL,
    priority INT NOT NULL,
    match_criteria JSONB NOT NULL DEFAULT '{}',
    rate_bps INT NOT NULL DEFAULT 0,
    fixed_fee_minor BIGINT NOT NULL DEFAULT 0,
    cap_amount_minor BIGINT,
    effective_from TIMESTAMPTZ NOT NULL,
    effective_to TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_fee_rules_lookup
    ON fee_rules(fee_category, network, effective_from);

CREATE TABLE IF NOT EXISTS merchant_contracts (
    id UUID PRIMARY KEY,
    merchant_id UUID NOT NULL,
    psp_name VARCHAR(64) NOT NULL,
    contract_type VARCHAR(16) NOT NULL DEFAULT 'IC_PLUS_PLUS',
    markup_bps INT NOT NULL DEFAULT 0,
    markup_fixed_minor BIGINT NOT NULL DEFAULT 0,
    blended_bps INT NOT NULL DEFAULT 0,
    blended_fixed_minor BIGINT NOT NULL DEFAULT 0,
    effective_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    effective_to TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS transaction_fee_estimates (
    id UUID PRIMARY KEY,
    payment_attempt_id UUID NOT NULL REFERENCES payment_attempts(id),
    interchange_program VARCHAR(64),
    interchange_minor BIGINT NOT NULL DEFAULT 0,
    scheme_minor BIGINT NOT NULL DEFAULT 0,
    markup_minor BIGINT NOT NULL DEFAULT 0,
    total_minor BIGINT NOT NULL DEFAULT 0,
    blended_override BOOLEAN NOT NULL DEFAULT FALSE,
    breakdown JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_fee_est_attempt ON transaction_fee_estimates(payment_attempt_id);

-- Deterministic integer fee arithmetic (design doc 4, verbatim logic).
CREATE OR REPLACE FUNCTION calculate_line_fee(
    p_amount_minor BIGINT,
    p_rate_bps_x100 INT,
    p_fixed_minor BIGINT,
    p_cap_minor BIGINT DEFAULT NULL
) RETURNS BIGINT AS $$
DECLARE
    v_raw_fee BIGINT;
BEGIN
    v_raw_fee := ((p_amount_minor * p_rate_bps_x100 + 500000) / 1000000) + p_fixed_minor;
    IF p_cap_minor IS NOT NULL AND v_raw_fee > p_cap_minor THEN
        RETURN p_cap_minor;
    END IF;
    RETURN v_raw_fee;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- =====================================================================
-- 6. ROUTING
-- =====================================================================
CREATE TABLE IF NOT EXISTS psps (
    id UUID PRIMARY KEY,
    name VARCHAR(64) UNIQUE NOT NULL,
    supported_currencies TEXT[] NOT NULL DEFAULT '{}',
    supported_networks TEXT[] NOT NULL DEFAULT '{}',
    acquirer_countries TEXT[] NOT NULL DEFAULT '{}',
    supports_3ds BOOLEAN NOT NULL DEFAULT TRUE,
    active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS psp_health (
    psp_id UUID PRIMARY KEY REFERENCES psps(id),
    window_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_count INT NOT NULL DEFAULT 0,
    timeout_count INT NOT NULL DEFAULT 0,
    error_count INT NOT NULL DEFAULT 0,
    circuit_state VARCHAR(8) NOT NULL DEFAULT 'CLOSED',
    opened_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS psp_approval_stats (
    psp_id UUID NOT NULL REFERENCES psps(id),
    bucket_key TEXT NOT NULL,
    alpha DOUBLE PRECISION NOT NULL DEFAULT 1,
    beta DOUBLE PRECISION NOT NULL DEFAULT 1,
    sample_count INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (psp_id, bucket_key)
);

CREATE TABLE IF NOT EXISTS routing_rules (
    id UUID PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    priority INT NOT NULL,
    predicate JSONB NOT NULL DEFAULT '{}',
    allowed_psp_ids UUID[] NOT NULL,
    target_shares JSONB,
    active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS routing_decisions (
    id UUID PRIMARY KEY,
    payment_intent_id UUID NOT NULL UNIQUE REFERENCES payment_intents(id),
    chosen_psp_id UUID NOT NULL,
    fallback_psp_ids UUID[] NOT NULL DEFAULT '{}',
    decision_mode VARCHAR(16) NOT NULL,
    candidate_scores JSONB NOT NULL,
    latency_ms INT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pan_retry_tracker (
    card_fingerprint VARCHAR(128) PRIMARY KEY,
    hard_decline_count_30d INT NOT NULL DEFAULT 0,
    soft_decline_count_24h INT NOT NULL DEFAULT 0,
    last_decline_code VARCHAR(16),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =====================================================================
-- 7. ENFORCEMENT TRIGGERS (design doc 1)
-- =====================================================================
CREATE OR REPLACE FUNCTION enforce_payment_state_transition()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status = NEW.status THEN
        RETURN NEW;
    END IF;

    IF NOT (
        (OLD.status = 'CREATED'     AND NEW.status IN ('AUTHORIZING', 'FAILED')) OR
        (OLD.status = 'AUTHORIZING' AND NEW.status IN ('AUTHORIZED', 'CAPTURED', 'FAILED')) OR
        (OLD.status = 'AUTHORIZED'  AND NEW.status IN ('CAPTURING', 'VOIDED', 'EXPIRED')) OR
        (OLD.status = 'CAPTURING'   AND NEW.status IN ('CAPTURED', 'FAILED')) OR
        (OLD.status = 'CAPTURED'    AND NEW.status IN ('SETTLED', 'REFUNDED')) OR
        (OLD.status = 'SETTLED'     AND NEW.status IN ('REFUNDED', 'DISPUTED'))
    ) THEN
        RAISE EXCEPTION 'Invalid payment transition from % to %', OLD.status, NEW.status;
    END IF;

    NEW.version := OLD.version + 1;
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_payment_state_machine ON payment_intents;
CREATE TRIGGER trg_payment_state_machine
BEFORE UPDATE OF status ON payment_intents
FOR EACH ROW EXECUTE FUNCTION enforce_payment_state_transition();

CREATE OR REPLACE FUNCTION verify_balanced_journal()
RETURNS TRIGGER AS $$
DECLARE
    net_balance BIGINT;
BEGIN
    SELECT COALESCE(SUM(
        CASE WHEN direction = 'DEBIT' THEN amount ELSE -amount END
    ), 0)
    INTO net_balance
    FROM ledger_entries
    WHERE ledger_transaction_id = NEW.id;

    IF net_balance <> 0 THEN
        RAISE EXCEPTION 'Unbalanced ledger transaction %: net diff is %', NEW.id, net_balance;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_verify_balanced_journal ON ledger_transactions;
CREATE CONSTRAINT TRIGGER trg_verify_balanced_journal
AFTER INSERT ON ledger_transactions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION verify_balanced_journal();

CREATE OR REPLACE FUNCTION prevent_ledger_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Ledger entries are immutable. Post a reversing entry instead.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_immutable_ledger_entries ON ledger_entries;
CREATE TRIGGER trg_immutable_ledger_entries
BEFORE UPDATE OR DELETE ON ledger_entries
FOR EACH ROW EXECUTE FUNCTION prevent_ledger_mutation();

DROP TRIGGER IF EXISTS trg_immutable_ledger_transactions ON ledger_transactions;
CREATE TRIGGER trg_immutable_ledger_transactions
BEFORE UPDATE OR DELETE ON ledger_transactions
FOR EACH ROW EXECUTE FUNCTION prevent_ledger_mutation();

-- =====================================================================
-- 8. SEED CHART OF ACCOUNTS (USD)
-- =====================================================================
INSERT INTO ledger_accounts (id, code, account_type, normal_balance, currency, allow_negative)
VALUES
    (gen_random_uuid(), 'acquirer_clearing_expected',    'ASSET',   'DEBIT',  'USD', TRUE),
    (gen_random_uuid(), 'acquirer_settlement_receivable','ASSET',   'DEBIT',  'USD', TRUE),
    (gen_random_uuid(), 'bank_cash',                     'ASSET',   'DEBIT',  'USD', TRUE),
    (gen_random_uuid(), 'psp_processing_fee_expense',    'EXPENSE', 'DEBIT',  'USD', TRUE),
    (gen_random_uuid(), 'fee_rounding_variance',         'EXPENSE', 'DEBIT',  'USD', TRUE),
    (gen_random_uuid(), 'platform_fee_revenue',          'REVENUE', 'CREDIT', 'USD', TRUE),
    (gen_random_uuid(), 'merchant_payable_main',         'LIABILITY','CREDIT','USD', TRUE)
ON CONFLICT (code) DO NOTHING;

-- =====================================================================
-- 9. PROCESSOR LAYER (SANDBOX) — network & clearing state
-- From the payment-processor design (filed verbatim in
-- ~/workspace/user/files/Payment_Processor_Core_Engine_pasted_2026-11-15.txt),
-- built as a sandbox at the owner's direction 2026-11-15: no real
-- card-network connection exists or is implied. PANs are never
-- stored; the message log keeps DE 2 masked.
-- =====================================================================
CREATE TABLE IF NOT EXISTS network_stan_sequences (
    acquirer_bin VARCHAR(16) NOT NULL,
    network VARCHAR(16) NOT NULL,
    current_stan INT NOT NULL DEFAULT 0,
    PRIMARY KEY (acquirer_bin, network)
);

CREATE TABLE IF NOT EXISTS iso8583_message_log (
    id UUID PRIMARY KEY,
    rrn VARCHAR(12),
    stan VARCHAR(6),
    mti VARCHAR(4) NOT NULL,
    direction VARCHAR(3) NOT NULL,
    raw_hex TEXT NOT NULL,
    parsed_de JSONB NOT NULL DEFAULT '{}',
    latency_ms INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_iso_log_rrn ON iso8583_message_log(rrn);

DROP TRIGGER IF EXISTS trg_immutable_iso_log ON iso8583_message_log;
CREATE TRIGGER trg_immutable_iso_log
BEFORE UPDATE OR DELETE ON iso8583_message_log
FOR EACH ROW EXECUTE FUNCTION prevent_ledger_mutation();

CREATE TABLE IF NOT EXISTS clearing_batches (
    id UUID PRIMARY KEY,
    network VARCHAR(16) NOT NULL,
    file_name TEXT NOT NULL,
    record_count INT NOT NULL DEFAULT 0,
    gross_total BIGINT NOT NULL DEFAULT 0,
    file_body TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'GENERATED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    transmitted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS clearing_batch_items (
    batch_id UUID NOT NULL REFERENCES clearing_batches(id),
    payment_intent_id UUID NOT NULL UNIQUE REFERENCES payment_intents(id),
    PRIMARY KEY (batch_id, payment_intent_id)
);

CREATE TABLE IF NOT EXISTS fbo_settlement_wires (
    id UUID PRIMARY KEY,
    sponsor_bank_ref VARCHAR(64) NOT NULL,
    network VARCHAR(16) NOT NULL,
    settlement_date DATE NOT NULL,
    net_wire_amount BIGINT NOT NULL,
    currency CHAR(3) NOT NULL DEFAULT 'USD',
    expected_amount BIGINT,
    reconciled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
