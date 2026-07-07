"""
Live trade-monitoring + PickMyTrade relay service for MNQU6 ICT_Funded_v1.

Receives three event types from TradingView on one webhook, all correlated by
"correlation_id" (the entry bar's timestamp, unique per trade since only one
position is open at a time):

  - "entry":        a new signal fired. Stored, analyzed by Claude, and the
                     order-placement fields are forwarded to PickMyTrade.
  - "price_update": periodic update for an open position (current price,
                     unrealized P&L). Never forwarded anywhere.
  - "exit":         the position closed (win or loss). Never forwarded -
                     PickMyTrade already executes its own bracket exit
                     independently once it has the entry order.

Run locally:
    uvicorn app:app --reload

Deploy: see README.md in this folder.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

DB_PATH = os.environ.get("LIVE_DB_PATH", os.path.join(os.path.dirname(__file__), "live.db"))
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
PICKMYTRADE_WEBHOOK_URL = os.environ.get("PICKMYTRADE_WEBHOOK_URL", "")

# Fields PickMyTrade's webhook actually expects - forwarded as-is, nothing extra.
PMT_FIELDS = [
    "symbol", "strategy_name", "date", "data", "quantity", "price", "tp", "sl",
    "trail", "trail_stop", "trail_trigger", "trail_freq", "token", "pyramid",
    "same_direction_ignore", "reverse_order_close", "multiple_accounts",
]

app = FastAPI()


def get_conn():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    with open(SCHEMA_PATH, "r") as f:
        conn.executescript(f.read())
    return conn


SETUP_TAG_MEANING = {
    "BRK": "breakout/continuation setup - needs a real trend to work",
    "RCL": "liquidity reclaim setup",
    "UNK": "unrecognized setup tag",
}


def build_prompt(entry):
    tag_meaning = SETUP_TAG_MEANING.get(entry.get("setup_tag"), "unknown setup")
    return f"""You are reviewing one new trade entry from an automated ICT-style futures strategy
(MNQ, liquidity-sweep + FVG/reclaim continuation entries). Known context: this strategy only has a
real edge when the broader market is trending; it loses money in choppy/range-bound conditions. A
daily-timeframe trend regime filter is supposed to screen out chop, but it is not perfectly reliable.

New entry:
- Direction: {entry.get('direction')}
- Setup type: {entry.get('setup_tag')} ({tag_meaning})
- Entry price: {entry.get('entry_price')}
- Stop loss: {entry.get('sl')}
- Take profit: {entry.get('tp')}
- ATR: {entry.get('atr')}
- Distance from 50 EMA (in ATR multiples): {entry.get('ema_distance_atr')}
- Daily regime slope (%): {entry.get('regime_slope_pct')}
- Bars since the liquidity sweep this entry is based on: {entry.get('sweep_age_bars')}
- Session: {entry.get('session')}

In 3-4 sentences: does this entry look aligned with a genuinely trending market (steep regime
slope, not chasing too far from the EMA, a fresh sweep) or does it look like a marginal/chop-risk
entry (weak slope, stale sweep, overextended from EMA)? Be direct and specific about which factor(s)
stand out, don't hedge with generic disclaimers."""


def analyze_with_claude(entry):
    if not ANTHROPIC_API_KEY:
        return None, "ANTHROPIC_API_KEY not configured"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": build_prompt(entry)}],
        )
        return response.content[0].text, None
    except Exception as e:
        return None, str(e)


def forward_to_pickmytrade(payload):
    """Returns (forwarded: bool, status_code: int|None, error: str|None)."""
    if not PICKMYTRADE_WEBHOOK_URL:
        return False, None, "PICKMYTRADE_WEBHOOK_URL not configured"
    pmt_payload = {k: payload[k] for k in PMT_FIELDS if k in payload}
    try:
        resp = httpx.post(PICKMYTRADE_WEBHOOK_URL, json=pmt_payload, timeout=15)
        return True, resp.status_code, None if resp.status_code < 400 else f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, None, str(e)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    if WEBHOOK_SECRET and payload.get("secret") != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "bad secret"}, status_code=401)

    event_type = payload.get("type", "entry")  # default to "entry" for backward compatibility
    correlation_id = payload.get("correlation_id")
    if not correlation_id:
        return JSONResponse({"ok": False, "error": "missing correlation_id"}, status_code=400)

    try:
        conn = get_conn()
        if event_type == "entry":
            analysis, llm_error = analyze_with_claude(payload)
            forwarded, pmt_status, pmt_error = forward_to_pickmytrade(payload)
            conn.execute(
                """INSERT OR REPLACE INTO trades
                   (correlation_id, received_at, signal_time, direction, setup_tag, symbol,
                    entry_price, sl, tp, atr, ema_distance_atr, regime_slope_pct, sweep_age_bars,
                    session, status, llm_model, llm_analysis, llm_error,
                    pmt_forwarded, pmt_status_code, pmt_error, raw_entry_payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?,?,?, ?,?,?, ?)""",
                (
                    correlation_id, now_iso(), payload.get("signal_time"),
                    payload.get("direction"), payload.get("setup_tag"), payload.get("symbol"),
                    payload.get("entry_price"), payload.get("sl"), payload.get("tp"),
                    payload.get("atr"), payload.get("ema_distance_atr"), payload.get("regime_slope_pct"),
                    payload.get("sweep_age_bars"), payload.get("session"),
                    CLAUDE_MODEL if analysis else None, analysis, llm_error,
                    int(forwarded), pmt_status, pmt_error, raw.decode("utf-8", errors="replace"),
                ),
            )
        elif event_type == "price_update":
            conn.execute(
                "UPDATE trades SET current_price=?, unrealized_pnl=?, last_update_at=? WHERE correlation_id=?",
                (payload.get("current_price"), payload.get("unrealized_pnl"), now_iso(), correlation_id),
            )
        elif event_type == "exit":
            status = "won" if str(payload.get("outcome", "")).upper() == "WIN" else "lost"
            conn.execute(
                "UPDATE trades SET status=?, exit_price=?, realized_pnl=?, closed_at=? WHERE correlation_id=?",
                (status, payload.get("exit_price"), payload.get("realized_pnl"), now_iso(), correlation_id),
            )
        else:
            return JSONResponse({"ok": False, "error": f"unknown type: {event_type}"}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"db error: {e}", "db_path": DB_PATH}, status_code=500)

    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}


def render_trade(t):
    color = "#1a7f37" if t["direction"] == "long" else "#c9302c"
    status_badge = {
        "open": ("#3b82f6", "OPEN"),
        "won": ("#1a7f37", "WON"),
        "lost": ("#c9302c", "LOST"),
    }.get(t["status"], ("#888", t["status"]))

    if t["status"] == "open":
        price_line = f"Current: <b>{t['current_price'] if t['current_price'] is not None else '-'}</b> &nbsp; Unrealized: <b>{t['unrealized_pnl'] if t['unrealized_pnl'] is not None else '-'}</b> &nbsp; (as of {t['last_update_at'] or 'no update yet'})"
    else:
        price_line = f"Exit: <b>{t['exit_price']}</b> &nbsp; Realized P&amp;L: <b>{t['realized_pnl']}</b> &nbsp; (closed {t['closed_at']})"

    if t["pmt_forwarded"]:
        pmt_line = f'<span style="color:#1a7f37;">forwarded to PickMyTrade (HTTP {t["pmt_status_code"]})</span>'
    else:
        pmt_line = f'<span style="color:#c9302c;">NOT forwarded to PickMyTrade{": " + t["pmt_error"] if t["pmt_error"] else ""}</span>'

    analysis = t["llm_analysis"] or (f"(analysis failed: {t['llm_error']})" if t["llm_error"] else "(pending)")

    return f"""
    <div style="border:1px solid #333;border-radius:8px;padding:12px;margin-bottom:12px;background:#111;">
      <div style="display:flex;justify-content:space-between;">
        <span style="color:{color};font-weight:bold;">{(t['direction'] or '?').upper()} - {t['setup_tag'] or '?'}
          <span style="background:{status_badge[0]};color:#fff;border-radius:4px;padding:1px 6px;font-size:11px;margin-left:8px;">{status_badge[1]}</span>
        </span>
        <span style="color:#888;">{t['received_at']}</span>
      </div>
      <div style="color:#ccc;margin-top:6px;">
        Entry: <b>{t['entry_price']}</b> &nbsp; SL: <b>{t['sl']}</b> &nbsp; TP: <b>{t['tp']}</b> &nbsp;
        ATR: {t['atr']} &nbsp; EMA dist (ATR): {t['ema_distance_atr']} &nbsp;
        Regime slope: {t['regime_slope_pct']}% &nbsp; Sweep age: {t['sweep_age_bars']} bars &nbsp; Session: {t['session']}
      </div>
      <div style="color:#ccc;margin-top:6px;">{price_line}</div>
      <div style="margin-top:6px;font-size:13px;">{pmt_line}</div>
      <div style="color:#9cdcfe;margin-top:8px;font-style:italic;">{analysis}</div>
    </div>
    """


@app.get("/", response_class=HTMLResponse)
def dashboard():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 100").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM trades LIMIT 0").description]
    conn.close()

    trades_html = "".join(render_trade(dict(zip(cols, row))) for row in rows)

    return f"""<!DOCTYPE html>
<html>
<head>
  <title>MNQU6 Live Trades</title>
  <meta http-equiv="refresh" content="15">
  <style>
    body {{ background:#0a0a0a; color:#eee; font-family: -apple-system, sans-serif; padding: 20px; max-width: 900px; margin: 0 auto; }}
    h1 {{ font-size: 20px; }}
  </style>
</head>
<body>
  <h1>MNQU6 ICT_Funded_v1 - Live Trades</h1>
  <p style="color:#888;">Auto-refreshes every 15s. Showing latest 100 trades.</p>
  {trades_html or '<p style="color:#888;">No trades yet.</p>'}
</body>
</html>"""
