# Divergence Screener (Stochastic 5,3,3, top 100)

Static dashboard + scheduled scanner.

- `scanner/scan.py` downloads closed daily/weekly candles for the top-100 coins (CoinGecko ranking, Binance USDT pairs),
  runs the divergence detector (`scanner/divergence.py`) and writes `public/data/signals.json`.
- `public/index.html` is the dashboard (filters, sortable table, chart with divergence lines, watch/skip decisions saved in the browser).
- `.github/workflows/scan.yml` runs the scanner every day at 00:10 UTC (and 12:10 UTC as a retry) and commits the new JSON.
  Vercel redeploys automatically on every push, so the dashboard updates itself.

## Run locally

```bash
pip install -r requirements.txt
python scanner/scan.py
python -m http.server 8000 --directory public   # open http://localhost:8000
```

## Deploy

1. Push this folder to a GitHub repository (the folder itself is the repo root).
2. In Vercel: Add New Project, import the repo. No framework, no build command; `vercel.json` sets the output directory to `public`.
3. In the GitHub repo: Actions tab, enable workflows, run "scan-divergence" once with "Run workflow".

Watch/skip decisions are stored in each browser's localStorage (not shared between devices).
