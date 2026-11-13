# Divergence Screener (Stochastic 5,3,3, top 100)

Static dashboard + one scheduled scan per day. No database: the history is a JSON file committed to the repo.

- `scanner/scan.py` downloads closed daily/weekly candles for the top-100 coins (CoinGecko ranking, Binance USDT pairs), runs the
  detector (`scanner/divergence.py`) with two rule sets and records every new signal as a paper trade in `public/data/history.json`.
  - tab **Divergence sudah cross**: divergence confirmed at the K/D crossing close.
  - tab **Potensial crossing**: divergence visible, K/D not crossed yet, |K-D| <= 2 and shrinking, K turning toward D.
  - Each signal is stored once (first seen). Entry = open of the next candle, stop = peak-2 extreme, TP = 2R, time stop 30 daily / 12 weekly candles.
  - Open trades are re-evaluated every day, so a signal that leaves the screener stays in the history with its result.
  - The first run back-fills the last 120 daily / 26 weekly candles (records flagged `live: false`).
- `public/index.html` is the dashboard: Aktif / Histori Daily / Histori Weekly per tab, filters, chart with divergence line, entry, stop, TP,
  watch/skip decisions (saved per browser in localStorage).
- `.github/workflows/scan.yml` runs once a day at 00:10 UTC (07:10 WIB), right after the daily close, and commits the new JSON.
  Vercel redeploys on every push.

## Making the daily run reliable

GitHub's own `schedule:` trigger can be hours late or skipped. So the wake-up call comes from **Vercel Cron** (`vercel.json`, 00:20 UTC):
it calls `api/trigger.js`, which asks GitHub to run the workflow (`workflow_dispatch`). GitHub Actions still does the scan and the commit.
The workflow also keeps its own `schedule:` as a backup. Setup:

1. GitHub: Settings, Developer settings, Personal access tokens, Fine-grained tokens. Repository access: only this repo.
   Permission: **Actions: Read and write**. Copy the token.
2. Vercel project, Settings, Environment Variables (Production): `GITHUB_TOKEN` = the token, `CRON_SECRET` = any long random string.
3. Redeploy once. Check Vercel, project, Settings, Cron Jobs, and press "Run" to test.

The dashboard header turns red when the data is older than about 30 hours, so a missed run is visible.

## Why no database

History is a few hundred KB (about 5-10 new signals a day). A JSON file in git is versioned, free and good enough. A database
(Supabase / Vercel KV) only becomes worth it if you want watch/skip decisions shared between devices or the file grows past a few MB.

## Run locally

```bash
pip install -r requirements.txt
python scanner/scan.py
python -m http.server 8000 --directory public   # open http://localhost:8000
```

## Deploy

1. Push this folder to a GitHub repository (the folder itself is the repo root).
2. Vercel: import the repo (no framework, no build command; `vercel.json` sets the output directory to `public`).
3. GitHub Actions tab: run "scan-divergence" once with "Run workflow".
