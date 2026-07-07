import os
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402

TEST_SECRET = "test-secret"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient wired to a fresh temp SQLite DB and a fixed webhook secret,
    with PickMyTrade/Claude untouched by default (each test patches those two
    functions explicitly to control their behavior)."""
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "test_live.db"))
    monkeypatch.setattr(app_module, "WEBHOOK_SECRET", TEST_SECRET)
    return TestClient(app_module.app)


@pytest.fixture
def get_trade():
    """Returns a helper that reads back a trade row as a dict, given a correlation_id."""
    def _get(correlation_id):
        conn = sqlite3.connect(app_module.DB_PATH)
        cols = [d[0] for d in conn.execute("SELECT * FROM trades LIMIT 0").description]
        row = conn.execute("SELECT * FROM trades WHERE correlation_id=?", (correlation_id,)).fetchone()
        conn.close()
        return dict(zip(cols, row)) if row else None
    return _get


def entry_payload(correlation_id="corr-1", **overrides):
    payload = {
        "type": "entry",
        "correlation_id": correlation_id,
        "secret": TEST_SECRET,
        "symbol": "MNQU6",
        "strategy_name": "NQ RECLAIM NY LONG",
        "data": "BUY",
        "quantity": 12,
        "price": 30000,
        "tp": 30050,
        "sl": 29950,
        "token": "x",
        "direction": "long",
        "setup_tag": "BRK",
        "entry_price": 30000,
        "atr": 42.5,
        "ema_distance_atr": 0.8,
        "regime_slope_pct": 1.2,
        "sweep_age_bars": 6,
        "session": "NY",
        "signal_time": "2026-07-07T17:35:00Z",
    }
    payload.update(overrides)
    return payload
