"""
Daily scanner + trade history for the Stochastic 5,3,3 divergence dashboard (top-100 crypto, Weekly + Daily).

Run ONCE per day right after the daily candle close (00:10 UTC). Only CLOSED candles are used, so a signal never flips
after it is recorded. Two rule sets (tabs):
  cross : divergence confirmed at the K/D crossing close.
  pre   : "potential crossing": the divergence is already visible, K and D have NOT crossed yet, |K-D| <= PRE_GAP and shrinking,
          K already turning back toward D (see backtest_divergence_precross.py / brute_force_cross_gap.py).
Every signal is stored once (first seen) in public/data/history.json as a paper trade (rules v2, from brute_force_sr.py):
entry = open of the candle after the signal, stop = peak-2 extreme, NO take profit, the trade is closed at the close of the
HOLD-th candle (4 weekly / 10 daily) unless the stop hits first. Each record also carries `sr`: how many earlier times price tested
the peak-2 level and failed to break it (strong S/R = 3 or more).
Core rule (weekly, cross, bull, sr >= 3) is decided in the dashboard; daily is kept for observation only (no edge in the backtest).
Open trades are re-evaluated on every run, so history keeps the outcome (stop / time) after a signal leaves the screener.
When RULES_VERSION increases, every record is replayed with the new exit rule on the first run.

First run (no history.json yet) back-fills the last BACKFILL candles so the history tabs are not empty (records flagged live=false).
    python scanner/scan.py
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
OUT = ROOT / "public" / "data" / "history.json"
CACHE = ROOT / "scanner" / "universe.json"
KL = "https://data-api.binance.vision/api/v3/klines"
INFO = "https://data-api.binance.vision/api/v3/exchangeInfo"
CG = "https://api.coingecko.com/api/v3/coins/markets"
TOP_N = 100
PRE_GAP = 2.0
HOLD = {"1w": 4, "1d": 10}                    # rules v2: no take profit, close the trade after this many candles unless the stop hits first
SR_LB, SR_TOL, SR_MIN = 250, 0.5, 3           # strong S/R = at least SR_MIN earlier rejections of the peak-2 level (250 candles, 0.5 ATR)
RULES_VERSION = 2
MAX_GAP = {"1w": 26, "1d": 60}
PERIOD = {"1w": 604800, "1d": 86400}
NEW_AGE = 1                                   # a live run records signals whose signal candle is at most this old
BACKFILL = {"1w": 26, "1d": 120}              # candles back-filled on the very first run
HISTORY = 500
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
                out.append({"symbol": s, "name": c["name"]})
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


def klines(sym: str, tf: str):
    """Closed candles as a DataFrame + the open price of the candle that is forming right now (None if unavailable)."""
    rows = get(KL, symbol=f"{sym}USDT", interval=tf, limit=HISTORY)
    now = int(time.time() * 1000)
    closed = [r for r in rows if int(r[6]) < now]
    forming = [r for r in rows if int(r[6]) >= now]
    df = pd.DataFrame([r[:6] for r in closed], columns=["open_time", "open", "high", "low", "close", "volume"])
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    return df, (float(forming[0][1]) if forming else None)


def pk(t, q):
    return [int(t[q["pbar"]]), float(q["px"]), int(t[q["kbar"]]), float(q["k"])]


def rejections(h, l, c, level, resistance, tol, excl_from, end):
    """How many EARLIER times (distinct clusters >= 5 candles apart, within SR_LB candles) price tested `level` and failed to
    break it: resistance = high reached level-tol but the candle closed below it; support = mirror image."""
    lo = max(0, end - SR_LB)
    hi = min(excl_from, end)
    if hi <= lo:
        return 0
    m = (h[lo:hi] >= level - tol) & (c[lo:hi] < level) if resistance else (l[lo:hi] <= level + tol) & (c[lo:hi] > level)
    idx = np.flatnonzero(m)
    return 0 if not len(idx) else 1 + int(np.count_nonzero(np.diff(idx) >= 5))


def resolve(rec: dict, df: pd.DataFrame, next_open: float | None) -> None:
    """Advance one paper trade using closed candles only (identical rules to backtest_divergence.trades_for)."""
    tf = rec["tf"]
    t = (df["open_time"].to_numpy() // 1000).astype(np.int64)
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    n = len(df)
    hit = np.where(t == rec["signal_time"])[0]
    if not len(hit):
        return
    si = int(hit[0])
    up = rec["side"] == "bull"
    ei = si + 1
    if rec["entry"] is None:
        if ei < n:
            entry, et = float(o[ei]), int(t[ei])
        elif next_open is not None:
            entry, et = next_open, int(t[si]) + PERIOD[tf]
        else:
            return                                                    # entry candle not open yet
        stop = rec["stop"]
        risk = (entry - stop) if up else (stop - entry)
        if risk <= 0 or risk / entry > 0.5:
            rec.update(entry=entry, entry_time=et, status="skipped",
                       skip_reason="harga entry sudah melewati stop" if risk <= 0 else "jarak stop > 50%")
            return
        rec.update(entry=entry, entry_time=et, tp=None, status="open")
    entry, stop = rec["entry"], rec["stop"]
    rec["last_price"], rec["last_time"] = float(c[-1]), int(t[-1])
    if ei >= n:                                                        # the entry candle is still forming
        rec["bars"] = 0
        return
    hold = HOLD[tf]
    for j in range(ei, min(ei + hold, n)):
        px, why = None, None
        if up:
            if o[j] <= stop: px, why = float(o[j]), "stop"
            elif l[j] <= stop: px, why = stop, "stop"
        else:
            if o[j] >= stop: px, why = float(o[j]), "stop"
            elif h[j] >= stop: px, why = stop, "stop"
        if why is None and j == ei + hold - 1:
            px, why = float(c[j]), "time"
        if why:
            rec.update(status=why, exit_time=int(t[j]), exit_price=px, bars=j - ei + 1,
                       gross=(px / entry - 1) * (1 if up else -1))
            return
    rec["bars"] = n - ei


def process(info: dict, rank: int, tf: str, hist: dict, backfill: bool, now_iso: str) -> None:
    df, next_open = klines(info["symbol"], tf)
    n = len(df)
    if n < 60:
        return
    t = (df["open_time"].to_numpy() // 1000).astype(np.int64)
    sym = info["symbol"]
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    atr = dv.atr(df)
    for tab, p in (("cross", dv.Params(ob=60, max_gap=MAX_GAP[tf])), ("pre", dv.Params(ob=60, max_gap=MAX_GAP[tf], pre_gap=PRE_GAP))):
        for s in dv.detect(df, p):
            if tab == "pre" and not s.get("pre"):
                continue                                              # fired at the cross close: that is the "cross" tab
            age = n - 1 - s["bar"]
            rid = f"{tab}|{sym}|{tf}|{int(t[s['bar']])}|{s['side']}|{s['kind']}"
            rec = hist.get(rid)
            if rec is None:
                if age > (BACKFILL[tf] if backfill else NEW_AGE):
                    continue
                rec = hist[rid] = {
                    "id": rid, "tab": tab, "symbol": sym, "name": info["name"], "rank": rank, "tf": tf, "side": s["side"],
                    "kind": s["kind"], "skip": bool(s["skipped"]), "signal_time": int(t[s["bar"]]), "p1": pk(t, s["p1"]),
                    "p2": pk(t, s["p2"]), "mid": pk(t, s["mid"]) if s["skipped"] else None, "stop": float(s["stop"]),
                    "live": not backfill, "first_seen": now_iso, "status": "pending", "entry": None, "entry_time": None,
                    "tp": None, "exit_time": None, "exit_price": None, "gross": None, "bars": 0, "last_price": None,
                    "last_time": None, "crossed_time": None, "skip_reason": None, "sr": None,
                }
            if tab == "pre" and s.get("cross_bar") is not None and rec.get("crossed_time") is None:
                rec["crossed_time"] = int(t[s["cross_bar"]])
    for rec in hist.values():
        if rec["symbol"] != sym or rec["tf"] != tf:
            continue
        if rec.get("sr") is None:                                     # earlier rejections of the peak-2 level (known at signal time)
            hit, p2h = np.where(t == rec["signal_time"])[0], np.where(t == rec["p2"][0])[0]
            if len(hit) and len(p2h) and not np.isnan(atr[hit[0]]):
                rec["sr"] = rejections(h, l, c, rec["stop"], rec["side"] == "bear", SR_TOL * atr[hit[0]], int(p2h[0]) - 2, int(hit[0]))
        if rec["status"] in ("pending", "open"):
            resolve(rec, df, next_open)


def main() -> int:
    hist: dict[str, dict] = {}
    backfill = not OUT.exists()
    migrated = False
    if not backfill:
        old = json.loads(OUT.read_text(encoding="utf-8"))
        hist = {r["id"]: r for r in old["records"]}
        if old["meta"].get("rules_version", 1) < RULES_VERSION:      # exit rule changed (no TP, shorter hold): replay every record
            migrated = True
            for r in hist.values():
                r.update(status="pending", entry=None, entry_time=None, tp=None, exit_time=None, exit_price=None, gross=None,
                         bars=0, last_price=None, last_time=None, skip_reason=None, sr=None)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    uni = universe()
    jobs = [(info, rank) for rank, info in enumerate(uni, 1)]
    have = {i["symbol"] for i in uni}
    for r in hist.values():                                          # coins that dropped out of the top 100 but still hold an open trade
        if r["status"] in ("pending", "open") and r["symbol"] not in have:
            have.add(r["symbol"])
            jobs.append(({"symbol": r["symbol"], "name": r["name"]}, r["rank"]))
    failed = 0
    for info, rank in jobs:
        for tf in ("1w", "1d"):
            try:
                process(info, rank, tf, hist, backfill, now_iso)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"{info['symbol']} {tf}: {e}", file=sys.stderr)
            time.sleep(0.05)
    if failed > len(jobs) * 2 * 0.2:
        print(f"too many failures ({failed}); keeping old file", file=sys.stderr)
        return 1
    recs = sorted(hist.values(), key=lambda r: (-r["signal_time"], r["rank"]))
    btc, _ = klines("BTC", "1d")
    meta = {
        "generated_at": now_iso, "universe": len(uni), "failed": failed, "backfilled": backfill, "migrated": migrated,
        "rules_version": RULES_VERSION,
        "last_daily_close": datetime.fromtimestamp(int(btc["open_time"].iloc[-1]) // 1000 + 86400, timezone.utc).isoformat(timespec="seconds"),
        "rules": {"stoch": "5,3,3", "ob": 60, "pre_gap": PRE_GAP, "tp": None, "hold": HOLD, "fee_per_side": 0.0005,
                  "sr": {"lookback": SR_LB, "tol_atr": SR_TOL, "min_rejections": SR_MIN}},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"meta": meta, "records": recs}, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT)
    by = pd.Series([f"{r['tab']}/{r['tf']}/{r['status']}" for r in recs]).value_counts().to_dict()
    print(f"{len(recs)} records ({'backfill' if backfill else 'incremental'}), {failed} failed downloads -> {OUT}")
    print(by)
    return 0


if __name__ == "__main__":
    sys.exit(main())
