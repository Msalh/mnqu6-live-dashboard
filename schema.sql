CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id      TEXT UNIQUE NOT NULL,
    received_at         TEXT NOT NULL,
    signal_time         TEXT,
    direction           TEXT,
    setup_tag           TEXT,
    symbol              TEXT,
    entry_price         REAL,
    sl                  REAL,
    tp                  REAL,
    atr                 REAL,
    ema_distance_atr    REAL,
    regime_slope_pct    REAL,
    sweep_age_bars      INTEGER,
    session             TEXT,

    status              TEXT NOT NULL DEFAULT 'open',  -- 'open' / 'won' / 'lost'
    current_price       REAL,
    unrealized_pnl      REAL,
    last_update_at      TEXT,
    exit_price          REAL,
    realized_pnl        REAL,
    closed_at           TEXT,

    llm_model           TEXT,
    llm_analysis        TEXT,
    llm_error           TEXT,

    pmt_forwarded       INTEGER NOT NULL DEFAULT 0,
    pmt_status_code     INTEGER,
    pmt_error           TEXT,

    raw_entry_payload   TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_correlation_id ON trades(correlation_id);
CREATE INDEX IF NOT EXISTS idx_trades_received_at ON trades(received_at);
