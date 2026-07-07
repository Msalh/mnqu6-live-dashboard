# MNQU6 live trade dashboard + PickMyTrade relay

A FastAPI service that sits between TradingView and PickMyTrade: it receives every entry,
price update, and exit from the Pine script, stores the full lifecycle of each trade, asks
Claude for a quick read on entries, forwards the order-placement fields to PickMyTrade, and
serves a live-updating dashboard showing it all.

## Event types (all POSTed to the same `/webhook` endpoint, distinguished by `"type"`)
- **`entry`** - a new signal fired. Stored, sent to Claude for analysis, and its order fields
  are forwarded to `PICKMYTRADE_WEBHOOK_URL`.
- **`price_update`** - periodic update for an open position (current price, unrealized P&L).
  Matched to its trade by `correlation_id`. Never forwarded anywhere.
- **`exit`** - the position closed (win or loss). Matched by `correlation_id`, updates the
  trade's final status. Never forwarded - PickMyTrade already executes its own bracket exit
  independently once it has the entry order, so re-notifying it would be redundant.

`correlation_id` is the entry bar's timestamp (unique per trade, since only one position is
open at a time) - it's how price_update/exit events find the right trade row to update.

## Files
- `app.py` - the service (webhook receiver + PickMyTrade relay + dashboard, one process)
- `schema.sql` - SQLite schema (`trades` table, one row per trade, updated over its lifecycle)
- `requirements.txt`, `Procfile` - deployment
- `.env.example` - required environment variables (copy to `.env` for local testing only)

## Environment variables
- `ANTHROPIC_API_KEY` - from https://console.anthropic.com/settings/keys
- `WEBHOOK_SECRET` - a password you make up; must match the Pine script's `webhookSecret` input
- `LIVE_DB_PATH` - where to persist SQLite, e.g. `/data/live.db` on Railway's mounted volume
- `PICKMYTRADE_WEBHOOK_URL` - the webhook URL PickMyTrade gave you for receiving orders. If
  unset, entries are still stored/analyzed, just not forwarded (the dashboard will show
  "NOT forwarded to PickMyTrade" with the reason).
- `CLAUDE_MODEL` - optional, defaults to `claude-haiku-4-5-20251001`

## Deploy (Railway, for persistent storage)
Render's free tier wipes local disk on every redeploy/restart, which would silently lose trade
history - avoid it unless you attach a paid persistent disk. Railway supports a persistent
volume on its base plan.

1. Push this `live/` folder to a GitHub repo.
2. Railway: New Project -> Deploy from GitHub repo -> pick the repo.
3. Settings -> Volumes -> add one, mount at `/data`.
4. Variables tab -> add the four environment variables above.
5. Railway detects `Procfile` and runs `uvicorn app:app --host 0.0.0.0 --port $PORT` automatically.
6. Settings -> Networking -> generate a public domain if one isn't already assigned.

## Point TradingView at it
In `MNQU6_ICT_Funded_v1.pine`, enable `Send Entries To Live Dashboard` and set
`Live Dashboard Webhook Secret` to match `WEBHOOK_SECRET`. Then in TradingView, create an alert
on the strategy (condition = the script, "alert() function calls only"), Notifications tab ->
Webhook URL -> `https://your-app.up.railway.app/webhook`, message left as the default
`{{strategy.order.alert_message}}`.

If you previously had a separate alert pointed directly at PickMyTrade's webhook, you can
remove it now - this service forwards entries to PickMyTrade for you.

## Test it manually
An entry event (this also attempts to forward to PickMyTrade if configured):
```bash
curl -X POST https://your-app.up.railway.app/webhook \
  -H "Content-Type: application/json" \
  -d '{"type":"entry","correlation_id":"test-1","secret":"<WEBHOOK_SECRET>","symbol":"MNQU6","strategy_name":"NQ RECLAIM NY LONG","data":"BUY","quantity":12,"price":30000,"tp":30050,"sl":29950,"token":"x","direction":"long","setup_tag":"BRK","entry_price":30000,"atr":42.5,"ema_distance_atr":0.8,"regime_slope_pct":1.2,"sweep_age_bars":6,"session":"NY","signal_time":"2026-07-07T17:35:00Z"}'
```

A price update for that same trade:
```bash
curl -X POST https://your-app.up.railway.app/webhook \
  -H "Content-Type: application/json" \
  -d '{"type":"price_update","correlation_id":"test-1","secret":"<WEBHOOK_SECRET>","current_price":30025,"unrealized_pnl":150}'
```

An exit for that same trade:
```bash
curl -X POST https://your-app.up.railway.app/webhook \
  -H "Content-Type: application/json" \
  -d '{"type":"exit","correlation_id":"test-1","secret":"<WEBHOOK_SECRET>","outcome":"WIN","exit_price":30050,"realized_pnl":600}'
```

Open the dashboard URL after each - you should see the trade appear, then its price update, then
its final status.

## Notes / limitations
- No authentication on the dashboard page itself (`/`) - anyone with the URL can view it. The
  `WEBHOOK_SECRET` only protects the `/webhook` endpoint from fake entries, not the dashboard's
  visibility. Don't put anything sensitive in the entries beyond trade parameters.
- Claude and the PickMyTrade forward are both called synchronously inside the webhook handler -
  fine for this strategy's low signal frequency, would need a background queue for
  high-frequency use.
- If `ANTHROPIC_API_KEY` or `PICKMYTRADE_WEBHOOK_URL` are missing or a call fails, the entry is
  still stored - the dashboard shows the specific error instead of silently losing the trade.
