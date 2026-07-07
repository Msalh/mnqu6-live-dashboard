CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at     TEXT NOT NULL,
    signal_time     TEXT,
    direction       TEXT NOT NULL,      -- 'long' / 'short'
    setup_tag       TEXT,               -- 'BRK' / 'RCL' / 'UNK'
    symbol          TEXT,
    entry_price     REAL,
    sl              REAL,
    tp              REAL,
    atr             REAL,
    ema_distance_atr REAL,              -- distance from EMA50, in ATR multiples
    regime_slope_pct REAL,
    sweep_age_bars  INTEGER,
    session         TEXT,               -- 'NY' / 'London' / 'NYPM'
    raw_payload     TEXT,               -- full incoming JSON, for debugging/future fields
    llm_model       TEXT,
    llm_analysis    TEXT,
    llm_error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_entries_received_at ON entries(received_at);
