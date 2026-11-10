-- USB-SOVEREIGN — ClickHouse analytics-layer schema.
--
-- This is the schema of the spec's analytical engine: the telemetry plane
-- lands in `serial_events`, and frames are classified against the
-- `serial_protocol_signatures` catalog. In this local build nothing talks
-- to a ClickHouse server — the stand-in is the append-only JSONL store and
-- JSON signature registry in event_store.py, which mirrors these two
-- tables (same column set, same signature codes and names). This file is
-- the authoritative DDL for when the real ClickHouse layer is provisioned.

CREATE TABLE serial_events
(
    timestamp   DateTime         CODEC(DoubleDelta, ZSTD),
    source      LowCardinality(String),  -- 'COM1', 'COM2', 'LH', 'FSH'
    data_hex    String           CODEC(ZSTD)
)
ENGINE = MergeTree
PARTITION BY toDate(timestamp)
ORDER BY (timestamp, source)
TTL timestamp + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192;

-- Protocol signature catalog: `header` is the frame-header key a
-- signature matches on. Ids 1-9 are the original spec catalog (sport /
-- coort / LAN / COM-pair / baseline LH / baseline FSH / BT / WiFi / cell /
-- ZilTB frames, held in event_store.py's registry under their notation
-- codes); ids 10 and 11 are the bio-telemetry additions below. The two
-- additions are keyed by their raw hex header ('41A0' / '41B0') because
-- the second header byte is not ASCII and cannot be written in the
-- registry's hex+ASCII notation.
CREATE TABLE serial_protocol_signatures
(
    id          UInt16,
    name        String,
    header      String,
    description String
)
ENGINE = MergeTree
ORDER BY id;

-- Bio-telemetry signature additions (LH/FSH expansion).
INSERT INTO serial_protocol_signatures VALUES
(10, 'lh_surge',  '41A0', 'Luteinizing Hormone peak threshold signature'),
(11, 'fsh_pulse', '41B0', 'Follicle Stimulating Hormone baseline pulse signature');
