"""
Scan the top-100 crypto (daily + weekly) for fresh Stochastic 5,3,3 divergences and write public/data/signals.json.

Only CLOSED candles are used (the still-forming candle is dropped), so a signal is final: it is the crossing close.
Run:  python scanner/scan.py
Exit code is non-zero (and the old signals.json is left untouched) if too many coins fail to download.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import divergence as dv

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "public" / "data" / "signals.json"
CACHE = ROOT / "scanner" / "universe.json"
KL = "https://data-api.binance.vision/api/v3/klines"
INFO = "https://data-api.binance.vision/api/v3/exchangeInfo"
CG = "https://api.coingecko.com/api/v3/coins/markets"
TOP_N = 100
MAX_AGE = {"1w": 4, "1d": 10}          # keep signals whose crossing close is at most this many candles old
CHART_BARS = {"1w": 150, "1d": 220}
MAX_GAP = {"1w": 26, "1d": 60}
HISTORY = 500                           # candles downloaded per coin / timeframe
EXCLUDE = {
    "USDT", "USDC", "FDUSD", "DAI", "TUSD", "USDE", "USDS", "USDD", "PYUSD", "BUSD", "USD1", "USDG", "BFUSD", "RLUSD",
    "WBTC", "WETH", "STETH", "WSTETH", "WBETH", "WEETH", "CBBTC", "BTCB", "WBNB", "BSC-USD", "SUSDE", "SUSDS", "BUIDL",
    "JITOSOL", "METH", "RETH", "CBETH", "LSETH", "SOLVBTC", "XAUT", "PAXG", "FIGR_HELOC", "USDT0", "USDTB", "SYRUPUSDC",
}
S = requests.Session()


def get(url, **params):
    for attempt in range(4):
        try:
            r = S.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed: {url}")


def universe() -> list[dict]:
    try:
        info = get(INFO)
        tradable = {s["baseAsset"] for s in info["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        out, seen = [], set()
        for page in (1, 2):
            for c in get(CG, vs_currency="usd", order="market_cap_desc", per_page=100, page=page):
                s = c["symbol"].upper()
                if s in EXCLUDE or s not in tradable or s in seen or not s.isascii():
                    continue
                seen.add(s)
                out.append({"symbol": s, "name": c["name"], "cap_rank": c["market_cap_rank"]})
            time.sleep(1.5)
        out = out[:TOP_N]
        if len(out) >= 80:
            CACHE.write_text(json.dumps(out), encoding="utf-8")
            return out
    except Exception as e:  # noqa: BLE001
        print("universe fetch failed:", e, file=sys.stderr)
    if CACHE.exists():
        print("using cached universe", file=sys.stderr)
        return json.loads(CACHE.read_text(encoding="utf-8"))
    raise RuntimeError("no universe")


def klines(sym: str, tf: str) -> pd.DataFrame:
    rows = get(KL, symbol=f"{sym}USDT", interval=tf, limit=HISTORY)
    now = int(time.time() * 1000)
    rows = [r for r in rows if int(r[6]) < now]                 # closed candles only
    df = pd.DataFrame([r[:6] for r in rows], columns=["open_time", "open", "high", "low", "close", "volume"])
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    return df


def scan_one(info: dict, rank: int, tf: str) -> tuple[list[dict], dict | None]:
    df = klines(info["symbol"], tf)
    n = len(df)
    if n < 60:
        return [], None
    t = (df["open_time"].to_numpy() // 1000).astype(np.int64)
    k, d = dv.stoch(df)
    close, hi, lo = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    out = []
    for s in dv.detect(df, dv.Params(ob=60, max_gap=MAX_GAP[tf])):
        age = n - 1 - s["bar"]
        if age > MAX_AGE[tf]:
            continue
        up = s["side"] == "bull"
        stop = s["stop"]
        after = slice(s["bar"] + 1, n)
        invalid = bool((lo[after] <= stop).any()) if up else bool((hi[after] >= stop).any())
        ref = close[s["bar"]]
        pk = lambda q: [int(t[q["pbar"]]), q["px"], int(t[q["kbar"]]), q["k"]]  # noqa: E731
        out.append({
            "symbol": info["symbol"], "name": info["name"], "rank": rank, "tf": tf, "side": s["side"], "kind": s["kind"],
            "skip": bool(s["skipped"]), "signal_time": int(t[s["bar"]]), "age": int(age), "price": float(close[-1]),
            "ref": float(ref), "move_pct": float((close[-1] / ref - 1) * (1 if up else -1) * 100),
            "stop": float(stop), "stop_dist_pct": float(abs(close[-1] - stop) / close[-1] * 100),
            "invalid": invalid, "p1": pk(s["p1"]), "p2": pk(s["p2"]), "mid": pk(s["mid"]) if s["skipped"] else None,
            "k_now": float(k[-1]), "d_now": float(d[-1]),
        })
    chart = None
    if out:
        m = CHART_BARS[tf]
        sl = slice(max(0, n - m), n)
        r = lambda a, p: [None if np.isnan(x) else round(float(x), p) for x in a[sl]]  # noqa: E731
        sig_fig = lambda a: [float(f"{x:.6g}") for x in a[sl]]  # noqa: E731
        chart = {"t": t[sl].tolist(), "o": sig_fig(df["open"].to_numpy()), "h": sig_fig(hi), "l": sig_fig(lo),
                 "c": sig_fig(close), "k": r(k, 1), "d": r(d, 1)}
    return out, chart


def main() -> int:
    uni = universe()
    signals, charts, failed = [], {}, 0
    for rank, info in enumerate(uni, 1):
        for tf in ("1w", "1d"):
            try:
                sg, ch = scan_one(info, rank, tf)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"{info['symbol']} {tf}: {e}", file=sys.stderr)
                continue
            signals.extend(sg)
            if ch:
                charts[f"{info['symbol']}|{tf}"] = ch
            time.sleep(0.05)
    if failed > len(uni) * 2 * 0.2:
        print(f"too many failures ({failed}); keeping old file", file=sys.stderr)
        return 1
    signals.sort(key=lambda x: (x["age"], x["tf"] != "1w", x["rank"]))
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "universe": len(uni), "failed": failed,
        "params": {"stoch": "5,3,3", "ob": 60, "max_age": MAX_AGE, "stop": "peak-2 extreme"},
        "signals": signals, "charts": charts,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"{len(signals)} signals from {len(uni)} coins ({failed} failed downloads) -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
