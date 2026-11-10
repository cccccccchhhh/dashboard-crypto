"""
Stochastic (5,3,3) divergence detector: regular ("strong") + hidden, bull + bear.

Definitions
-----------
Stoch 5,3,3  : raw = (C-LL5)/(HH5-LL5)*100 ; K = SMA3(raw) ; D = SMA3(K)
Peak (bear)  : a run of bars with K > D. It is CONFIRMED on the close where K crosses back below D ("crossing close").
               Peak oscillator value = max K in the run, peak price = max High in the run. Only peaks with K >= OB count.
Trough (bull): mirror image (K < D run, confirmed when K crosses above D, K <= 100-OB, price = min Low).
Pairing      : the new peak is compared with the PREVIOUS qualified peak (peak 1 -> peak 2, always preferred).
               If that gives nothing, peak 1 -> peak 3 is allowed only when the in-between peak 2 is "minor":
               its price extreme is within +-MINOR_ATR * ATR(14) of peak 1's. Never peak 1 -> peak 4.
Types        : regular bear  price HH, stoch LH      hidden bear  price LH, stoch HH
               regular bull  price LL, stoch HL      hidden bull  price HL, stoch LL
Signal bar   : the crossing close. No look-ahead: everything is known at that bar's close.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Params:
    ob: float = 60.0          # peak K must be >= ob (bear) / <= 100-ob (bull)
    min_k_diff: float = 3.0   # min oscillator difference between the 2 peaks
    max_gap: int = 60         # max bars between the price extremes of the two peaks
    minor_atr: float = 0.5    # peak 2 "minor" if price extreme within this many ATR of peak 1
    pre_proj: bool = False        # early entry also when the gap would reach 0 next bar at the current shrink rate
    pre_gap: float | None = None  # EARLY entry: fire before the K/D cross once |K-D| has narrowed to <= pre_gap (None = wait for the cross close)


def stoch(df: pd.DataFrame, k_len=5, k_smooth=3, d_smooth=3):
    ll = df["low"].rolling(k_len).min()
    hh = df["high"].rolling(k_len).max()
    raw = (df["close"] - ll) / (hh - ll).replace(0, np.nan) * 100
    k = raw.rolling(k_smooth).mean()
    d = k.rolling(d_smooth).mean()
    return k.to_numpy(), d.to_numpy()


def atr(df: pd.DataFrame, n=14) -> np.ndarray:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean().to_numpy()


def detect(df: pd.DataFrame, p: Params = Params()) -> list[dict]:
    """Return a list of divergence signals: {bar, side, kind, stop, p1_bar, p2_bar, skipped}."""
    k, d = stoch(df)
    a = atr(df)
    hi, lo = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    signals = []
    for side in ("bear", "bull"):
        sign = 1 if side == "bear" else -1
        peaks: list[dict] = []          # qualified, confirmed peaks (oldest -> newest)
        start, fired = None, None
        for i in range(1, n):
            if np.isnan(k[i]) or np.isnan(d[i]):
                start, fired = None, None
                continue
            inside = sign * (k[i] - d[i]) > 0
            if inside:
                if start is None:
                    start, fired = i, None
                if p.pre_gap is not None and fired is None and i >= 2 and not np.isnan(k[i - 2]) and not np.isnan(d[i - 2]):
                    gap, gap2 = sign * (k[i] - d[i]), sign * (k[i - 2] - d[i - 2])
                    # gap shrinking (smaller than 2 bars ago), small enough, and K already turning back toward D
                    gap1 = sign * (k[i - 1] - d[i - 1])
                    close_enough = gap <= p.pre_gap or (p.pre_proj and gap <= gap1 - gap)
                    if gap < gap2 and close_enough and sign * (k[i] - k[i - 1]) < 0:
                        seg = slice(start, i + 1)
                        if side == "bear":
                            kv = float(np.max(k[seg])); pv = float(np.max(hi[seg])); pb = start + int(np.argmax(hi[seg])); kb = start + int(np.argmax(k[seg]))
                            ok = kv >= p.ob
                        else:
                            kv = float(np.min(k[seg])); pv = float(np.min(lo[seg])); pb = start + int(np.argmin(lo[seg])); kb = start + int(np.argmin(k[seg]))
                            ok = kv <= 100 - p.ob
                        if ok:
                            sig = _compare(side, peaks, {"bar": i, "k": kv, "px": pv, "pbar": pb, "kbar": kb}, a, p)
                            if sig:
                                sig["pre"] = True
                                signals.append(sig)
                                fired = sig
                continue
            if start is None:
                continue
            # run [start, i-1] just ended: K crossed to the other side of D at close i
            seg = slice(start, i)
            if side == "bear":
                kv = float(np.max(k[seg])); pv = float(np.max(hi[seg])); pb = start + int(np.argmax(hi[seg])); kb = start + int(np.argmax(k[seg]))
                ok = kv >= p.ob
            else:
                kv = float(np.min(k[seg])); pv = float(np.min(lo[seg])); pb = start + int(np.argmin(lo[seg])); kb = start + int(np.argmin(k[seg]))
                ok = kv <= 100 - p.ob
            was_fired, start, fired = fired, None, None
            if not ok:
                continue
            new = {"bar": i, "k": kv, "px": pv, "pbar": pb, "kbar": kb}
            if was_fired is not None:
                was_fired["cross_bar"] = i          # the early signal's cross-close confirmation came at this bar
                sig = None
            else:
                sig = _compare(side, peaks, new, a, p)
            if sig:
                signals.append(sig)
            peaks.append(new)
            peaks = peaks[-3:]
    signals.sort(key=lambda s: s["bar"])
    return signals


def _compare(side, peaks, new, a, p):
    def classify(prev):
        kd = new["k"] - prev["k"]
        if side == "bear":
            if new["px"] > prev["px"] and kd < -p.min_k_diff:
                return "regular"
            if new["px"] < prev["px"] and kd > p.min_k_diff:
                return "hidden"
        else:
            if new["px"] < prev["px"] and kd > p.min_k_diff:
                return "regular"
            if new["px"] > prev["px"] and kd < -p.min_k_diff:
                return "hidden"
        return None

    # 1) peak 1 -> peak 2 (consecutive)
    if peaks:
        prev = peaks[-1]
        if new["pbar"] - prev["pbar"] <= p.max_gap:
            kind = classify(prev)
            if kind:
                return {"bar": new["bar"], "side": side, "kind": kind, "stop": new["px"],
                        "p1_bar": prev["pbar"], "p2_bar": new["pbar"], "skipped": False, "p1": prev, "p2": new}
    # 2) peak 1 -> peak 3, only if in-between peak 2 is minor
    if len(peaks) >= 2:
        p1, mid = peaks[-2], peaks[-1]
        tol = p.minor_atr * a[mid["pbar"]] if not np.isnan(a[mid["pbar"]]) else 0
        if abs(mid["px"] - p1["px"]) <= tol and new["pbar"] - p1["pbar"] <= p.max_gap:
            kind = classify(p1)
            if kind:
                return {"bar": new["bar"], "side": side, "kind": kind, "stop": new["px"],
                        "p1_bar": p1["pbar"], "p2_bar": new["pbar"], "skipped": True, "p1": p1, "p2": new, "mid": mid}
    return None
