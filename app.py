"""
Live trade-monitoring + PickMyTrade relay service for MNQU6 ICT_Funded_v1.

Receives three event types from TradingView on one webhook, all correlated by
"correlation_id" (the entry bar's timestamp, unique per trade since only one
position is open at a time):

  - "entry":        a new signal fired. The order-placement fields are forwarded
                     to PickMyTrade FIRST (order relay must never wait on Claude),
                     then stored, then Claude analysis runs as a background task
                     that updates the row afterward. Idempotent: if this
                     correlation_id was already successfully forwarded, this is a
                     no-op (protects against TradingView webhook retries causing a
                     duplicate real order).
  - "price_update": periodic update for an open position (current price,
                     unrealized P&L). Never forwarded anywhere. Never touches the
                     pmt_forwarded/pmt_status_code/pmt_error columns.
  - "exit":         the position closed (win or loss). Never forwarded -
                     PickMyTrade already executes its own bracket exit
                     independently once it has the entry order. Never touches the
                     pmt_forwarded/pmt_status_code/pmt_error columns.

Response status codes on POST /webhook (all in the 2xx range so TradingView never
interprets a partial failure as a delivery failure and retries it, which would
risk a duplicate order on top of the original problem):
  - 200: fully normal (entry forwarded OK, or a price_update/exit applied OK)
  - 207: entry was stored, but the PickMyTrade forward failed or is unconfigured -
         check pmt_error. This is deliberately NOT hidden as a plain 200.
  - 208: duplicate entry - this correlation_id was already forwarded previously,
         nothing was re-sent to PickMyTrade, the existing record is untouched.

Run locally:
    uvicorn app:app --reload

Deploy: see README.md in this folder.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
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


def get_pmt_forwarded(conn, correlation_id):
    """Returns True/False/None (None = no row exists yet for this correlation_id)."""
    row = conn.execute("SELECT pmt_forwarded FROM trades WHERE correlation_id=?", (correlation_id,)).fetchone()
    return bool(row[0]) if row else None


def store_entry(conn, correlation_id, payload, raw_body, forwarded, pmt_status, pmt_error):
    """Insert a new entry row, or refresh an existing one from a prior FAILED forward attempt
    (never called when pmt_forwarded is already True - the caller checks that first). Deliberately
    does not touch status/current_price/exit_price/llm_* columns on conflict, so a retry of a
    previously-failed entry can't clobber later lifecycle data that (in practice) shouldn't exist
    yet anyway, but this keeps the guarantee explicit rather than accidental."""
    conn.execute(
        """INSERT INTO trades
           (correlation_id, received_at, signal_time, direction, setup_tag, symbol,
            entry_price, sl, tp, atr, ema_distance_atr, regime_slope_pct, sweep_age_bars,
            session, status, pmt_forwarded, pmt_status_code, pmt_error, raw_entry_payload)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?,?,?, ?)
           ON CONFLICT(correlation_id) DO UPDATE SET
             received_at=excluded.received_at, signal_time=excluded.signal_time,
             direction=excluded.direction, setup_tag=excluded.setup_tag, symbol=excluded.symbol,
             entry_price=excluded.entry_price, sl=excluded.sl, tp=excluded.tp, atr=excluded.atr,
             ema_distance_atr=excluded.ema_distance_atr, regime_slope_pct=excluded.regime_slope_pct,
             sweep_age_bars=excluded.sweep_age_bars, session=excluded.session,
             pmt_forwarded=excluded.pmt_forwarded, pmt_status_code=excluded.pmt_status_code,
             pmt_error=excluded.pmt_error, raw_entry_payload=excluded.raw_entry_payload""",
        (
            correlation_id, now_iso(), payload.get("signal_time"),
            payload.get("direction"), payload.get("setup_tag"), payload.get("symbol"),
            payload.get("entry_price"), payload.get("sl"), payload.get("tp"),
            payload.get("atr"), payload.get("ema_distance_atr"), payload.get("regime_slope_pct"),
            payload.get("sweep_age_bars"), payload.get("session"),
            int(forwarded), pmt_status, pmt_error, raw_body,
        ),
    )


def run_claude_analysis(payload, correlation_id):
    """Background task - runs AFTER the HTTP response is already sent. Commentary only;
    never touches pmt_forwarded/pmt_status_code/pmt_error. Defensive at the outermost level
    (not just relying on analyze_with_claude's own try/except) so nothing from this task can
    ever propagate into the background task runner, regardless of what fails."""
    try:
        analysis, llm_error = analyze_with_claude(payload)
    except Exception as e:
        analysis, llm_error = None, str(e)
    try:
        conn = get_conn()
        conn.execute(
            "UPDATE trades SET llm_model=?, llm_analysis=?, llm_error=? WHERE correlation_id=?",
            (CLAUDE_MODEL if analysis else None, analysis, llm_error, correlation_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # analysis is commentary only - never let a DB hiccup here surface as an error


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
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
            already_forwarded = get_pmt_forwarded(conn, correlation_id)
            if already_forwarded:
                # Idempotency guard: this is a retry (TradingView redelivery, etc.) of a signal
                # that already placed a real order. Do NOT forward again. Do NOT touch the
                # existing record. Acknowledge so TradingView doesn't keep retrying.
                conn.close()
                return JSONResponse(
                    {"ok": True, "duplicate_already_forwarded": True, "correlation_id": correlation_id},
                    status_code=208,
                )

            # Order relay first, always - Claude must never delay a real order.
            forwarded, pmt_status, pmt_error = forward_to_pickmytrade(payload)

            store_entry(conn, correlation_id, payload, raw.decode("utf-8", errors="replace"),
                        forwarded, pmt_status, pmt_error)
            conn.commit()
            conn.close()

            # Claude runs after the response-critical work is done and does not block this
            # request at all - it's a background task, not part of the synchronous path.
            background_tasks.add_task(run_claude_analysis, payload, correlation_id)

            status_code = 200 if forwarded else 207
            return JSONResponse(
                {"ok": True, "pmt_forwarded": forwarded, "pmt_status_code": pmt_status, "pmt_error": pmt_error},
                status_code=status_code,
            )

        elif event_type == "price_update":
            cur = conn.execute(
                "UPDATE trades SET current_price=?, unrealized_pnl=?, last_update_at=? WHERE correlation_id=?",
                (payload.get("current_price"), payload.get("unrealized_pnl"), now_iso(), correlation_id),
            )
            matched = cur.rowcount
        elif event_type == "exit":
            status = "won" if str(payload.get("outcome", "")).upper() == "WIN" else "lost"
            cur = conn.execute(
                "UPDATE trades SET status=?, exit_price=?, realized_pnl=?, closed_at=? WHERE correlation_id=?",
                (status, payload.get("exit_price"), payload.get("realized_pnl"), now_iso(), correlation_id),
            )
            matched = cur.rowcount
        else:
            conn.close()
            return JSONResponse({"ok": False, "error": f"unknown type: {event_type}"}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"db error: {e}", "db_path": DB_PATH}, status_code=500)

    conn.commit()
    conn.close()
    if matched == 0:
        return JSONResponse(
            {"ok": True, "warning": f"no trade found for correlation_id {correlation_id}"}, status_code=200
        )
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
