# MNQU6 live entry dashboard

A small FastAPI service: receives a webhook from TradingView on every new entry, stores it,
asks Claude for a quick read on the entry, and serves a live-updating dashboard page.

## Files
- `app.py` - the service (webhook receiver + dashboard, one process)
- `schema.sql` - SQLite schema
- `requirements.txt`, `Procfile` - deployment
- `.env.example` - required environment variables (copy to `.env` for local testing only)

## 1. Get an Anthropic API key
https://console.anthropic.com/settings/keys - create a key, you'll paste it into your host's
environment variables (never into the code).

## 2. Deploy (recommended: Railway, for persistent storage)
Render's free tier wipes local disk on every redeploy/restart, which would silently lose your
entry history - avoid it for this unless you attach a paid persistent disk. Railway supports a
persistent volume on its base plan, which is what this needs.

Steps (Railway):
1. Push this `live/` folder to a GitHub repo (or a repo containing it).
2. In Railway: New Project -> Deploy from GitHub repo -> pick the repo.
3. Add a **Volume**, mount it at `/data`.
4. Set environment variables (Railway project settings -> Variables):
   - `ANTHROPIC_API_KEY` = your key from step 1
   - `WEBHOOK_SECRET` = make up a password, e.g. a long random string
   - `LIVE_DB_PATH` = `/data/live.db`
5. Railway will detect `Procfile` and run `uvicorn app:app --host 0.0.0.0 --port $PORT` automatically.
6. Once deployed, Railway gives you a public URL like `https://your-app.up.railway.app`.
   - Dashboard: that URL directly (`/`)
   - Webhook endpoint for TradingView: `https://your-app.up.railway.app/webhook`

(Render works too if you're fine with occasional data loss on redeploy, or if you pair it with
Render's managed Postgres instead of SQLite - not set up here by default.)

## 3. Point a TradingView alert at the webhook
The Pine script already fires a second `alert()` with the diagnostic payload on every entry
(see `pine/MNQU6_ICT_Funded_v1.pine`). In TradingView:
1. Create an alert on the strategy, condition = the strategy's alert condition (or "Any alert() function call").
2. In the alert's **Notifications** tab, check **Webhook URL** and paste `https://your-app.up.railway.app/webhook`.
3. The message body is already set by the script's `alert()` call - leave the message field as `{{strategy.order.alert_message}}` (TradingView's default) so it passes through what Pine sends.

## 4. Test it
Send a manual test payload to confirm the service works before relying on a live alert:

```bash
curl -X POST https://your-app.up.railway.app/webhook \
  -H "Content-Type: application/json" \
  -d '{"secret":"<your WEBHOOK_SECRET>","direction":"long","setup_tag":"BRK","entry_price":30000,"sl":29950,"tp":30050,"atr":42.5,"ema_distance_atr":0.8,"regime_slope_pct":1.2,"sweep_age_bars":6,"session":"NY","signal_time":"2026-07-07T17:35:00Z"}'
```

Then open the dashboard URL - you should see the entry with Claude's analysis underneath it
within a few seconds.

## Notes / limitations
- No authentication on the dashboard page itself (`/`) - anyone with the URL can view it. The
  `WEBHOOK_SECRET` only protects the `/webhook` endpoint from fake entries, not the dashboard's
  visibility. Don't put anything sensitive in the entries beyond trade parameters.
- Claude is called synchronously inside the webhook handler - fine for this strategy's low
  signal frequency (a few per week), would need a background queue for high-frequency use.
- If `ANTHROPIC_API_KEY` is missing or the API call fails, the entry is still stored - the
  dashboard just shows the error instead of an analysis, so you never lose entry data because
  of an LLM hiccup.
