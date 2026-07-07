"""
Live entry-monitoring service for MNQU6 ICT_Funded_v1.

Receives a webhook from TradingView on every new entry, stores it, asks Claude
for a quick read on the entry's quality, and serves a live-updating dashboard.

Run locally:
    uvicorn app:app --reload

Deploy: see README.md in this folder.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

DB_PATH = os.environ.get("LIVE_DB_PATH", os.path.join(os.path.dirname(__file__), "live.db"))
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

app = FastAPI()


def get_conn():
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


@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    if WEBHOOK_SECRET and payload.get("secret") != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "bad secret"}, status_code=401)

    analysis, error = analyze_with_claude(payload)

    conn = get_conn()
    conn.execute(
        """INSERT INTO entries
           (received_at, signal_time, direction, setup_tag, symbol, entry_price, sl, tp,
            atr, ema_distance_atr, regime_slope_pct, sweep_age_bars, session,
            raw_payload, llm_model, llm_analysis, llm_error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            payload.get("signal_time"),
            payload.get("direction"),
            payload.get("setup_tag"),
            payload.get("symbol"),
            payload.get("entry_price"),
            payload.get("sl"),
            payload.get("tp"),
            payload.get("atr"),
            payload.get("ema_distance_atr"),
            payload.get("regime_slope_pct"),
            payload.get("sweep_age_bars"),
            payload.get("session"),
            raw.decode("utf-8", errors="replace"),
            CLAUDE_MODEL if analysis else None,
            analysis,
            error,
        ),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM entries ORDER BY id DESC LIMIT 100").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM entries LIMIT 0").description]
    conn.close()

    entries_html = ""
    for row in rows:
        e = dict(zip(cols, row))
        color = "#1a7f37" if e["direction"] == "long" else "#c9302c"
        analysis = e["llm_analysis"] or (f"(analysis failed: {e['llm_error']})" if e["llm_error"] else "(pending)")
        entries_html += f"""
        <div style="border:1px solid #333;border-radius:8px;padding:12px;margin-bottom:12px;background:#111;">
          <div style="display:flex;justify-content:space-between;">
            <span style="color:{color};font-weight:bold;">{e['direction'].upper() if e['direction'] else '?'} - {e['setup_tag'] or '?'}</span>
            <span style="color:#888;">{e['received_at']}</span>
          </div>
          <div style="color:#ccc;margin-top:6px;">
            Entry: <b>{e['entry_price']}</b> &nbsp; SL: <b>{e['sl']}</b> &nbsp; TP: <b>{e['tp']}</b> &nbsp;
            ATR: {e['atr']} &nbsp; EMA dist (ATR): {e['ema_distance_atr']} &nbsp;
            Regime slope: {e['regime_slope_pct']}% &nbsp; Sweep age: {e['sweep_age_bars']} bars &nbsp; Session: {e['session']}
          </div>
          <div style="color:#9cdcfe;margin-top:8px;font-style:italic;">{analysis}</div>
        </div>
        """

    return f"""<!DOCTYPE html>
<html>
<head>
  <title>MNQU6 Live Entries</title>
  <meta http-equiv="refresh" content="30">
  <style>
    body {{ background:#0a0a0a; color:#eee; font-family: -apple-system, sans-serif; padding: 20px; max-width: 900px; margin: 0 auto; }}
    h1 {{ font-size: 20px; }}
  </style>
</head>
<body>
  <h1>MNQU6 ICT_Funded_v1 - Live Entries</h1>
  <p style="color:#888;">Auto-refreshes every 30s. Showing latest 100 entries.</p>
  {entries_html or '<p style="color:#888;">No entries yet.</p>'}
</body>
</html>"""
