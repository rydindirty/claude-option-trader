#!/usr/bin/env python3
"""
backtest_corrected.py — Backtest the CORRECTED entry structure against history.

backtest.py replays EXIT rules on the trades that were actually taken. That can
only ever re-confirm that the *current* far-OTM selection is a loser, because the
new entry rules would have REJECTED all of those trades.

This script does the thing that actually matters: it reconstructs what the
CORRECTED ENTRY RULES would have placed on the same (ticker, entry_date,
expiration, direction), then holds to expiry, and tallies the result.

Corrected rules under test:
  - Spread width = $5 (narrow, account-appropriate)
  - Short strike placed where credit >= 1/3 of width (~$1.67)  -> ~0.30 delta
  - Hold to expiry  (CONSERVATIVE: no 50% profit-taking, which would only help)

Method — self-contained, needs only daily STOCK prices:
  1. S0  = underlying close on the entry date.
  2. Back-solve ONE implied vol from the trade's RECORDED net credit at its ACTUAL
     strikes, so the model is calibrated to that day's real market pricing.
  3. Solve for short strike K* such that a $5-wide spread's BS credit = $1.667.
  4. S_T = underlying close at expiration -> terminal payoff -> P&L (1 contract).

Compares ACTUAL far-OTM trades vs the CORRECTED 1/3-width trades, same names/dates.

Caveat: uses a single skew-naive IV per trade. Good enough to demonstrate the
expectancy sign-flip, which is driven by the 1/3-width structure, not IV precision.
"""
import os
import sys
import math
import json
import sqlite3
import argparse
import time
from datetime import date, datetime, timedelta

try:
    import requests
    from scipy.stats import norm
    from scipy.optimize import brentq
except ImportError:
    print("Missing deps. Run:  pip install requests scipy")
    sys.exit(1)

RISK_FREE_RATE = 0.05
WIDTH          = 5.0          # corrected spread width ($)
CREDIT_FRAC    = 1.0 / 3.0    # require credit >= 1/3 of width
TARGET_CREDIT  = WIDTH * CREDIT_FRAC   # $1.667
API_DELAY_SEC  = 13.0
MAX_RETRIES    = 6
TODAY          = date.today()


# ── Polygon / Massive ────────────────────────────────────────────────────────
def _api_key() -> str:
    k = os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY")
    if not k:
        print("ERROR: set MASSIVE_API_KEY or POLYGON_API_KEY")
        sys.exit(1)
    return k


def _get(path: str, params: dict, key: str) -> dict:
    params["apiKey"] = key
    delay = 15.0
    for attempt in range(MAX_RETRIES):
        r = requests.get(f"https://api.polygon.io{path}", params=params, timeout=20)
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", delay))
            print(f"   429 — sleeping {wait:.0f}s ({attempt+1}/{MAX_RETRIES})")
            time.sleep(wait); delay = min(delay * 2, 120); continue
        if r.status_code in (403, 404):
            return {}
        r.raise_for_status()
        return r.json()
    return {}


def get_stock_closes(ticker: str, frm: str, to: str, key: str) -> dict:
    data = _get(f"/v2/aggs/ticker/{ticker}/range/1/day/{frm}/{to}",
                {"adjusted": "true", "sort": "asc", "limit": 400}, key)
    time.sleep(API_DELAY_SEC)
    return {datetime.fromtimestamp(r["t"] / 1000).strftime("%Y-%m-%d"): float(r["c"])
            for r in data.get("results", []) if r.get("c", 0) > 0}


# ── Black-Scholes ──────────────────────────────────────────────────────────────
def _bs(S, K, T, r, sig, is_call):
    if T <= 0 or sig <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sig**2) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if is_call:
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def _bs_delta(S, K, T, r, sig, is_call):
    if T <= 0 or sig <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sig**2) * T) / (sig * math.sqrt(T))
    return norm.cdf(d1) if is_call else norm.cdf(d1) - 1.0


def _spread_credit(S, short_k, long_k, T, r, sig, is_call):
    return _bs(S, short_k, T, r, sig, is_call) - _bs(S, long_k, T, r, sig, is_call)


# ── Core reconstruction ─────────────────────────────────────────────────────────
def reconstruct(trade, stock_map):
    """Return dict describing the corrected trade + its hold-to-expiry P&L."""
    is_call  = "Bear Call" in trade["type"]
    entry    = trade["opened_at"][:10]
    expiry   = date.fromisoformat(trade["expiration"])
    S0       = stock_map.get(entry) or _nearest(stock_map, entry, after=True)
    if not S0:
        return {"skip": "no entry price"}

    T0 = max((expiry - date.fromisoformat(entry)).days, 1) / 365.0
    a_short = float(trade["short_strike"]); a_long = float(trade["long_strike"])
    a_credit = float(trade["credit_received"])

    # 1) Calibrate IV to the trade's actual recorded net credit
    try:
        iv = brentq(lambda v: _spread_credit(S0, a_short, a_long, T0, RISK_FREE_RATE, v, is_call) - a_credit,
                    1e-3, 8.0, xtol=1e-4, maxiter=200)
    except Exception:
        return {"skip": "iv solve failed"}

    # 2) Solve corrected short strike K* so that $5-wide credit == TARGET_CREDIT
    #    Put spread: long = short - WIDTH. Call spread: long = short + WIDTH.
    def credit_at(short_k):
        long_k = short_k - WIDTH if not is_call else short_k + WIDTH
        return _spread_credit(S0, short_k, long_k, T0, RISK_FREE_RATE, iv, is_call)

    lo, hi = (0.40 * S0, 1.20 * S0)   # search band for the short strike
    try:
        if is_call:
            # credit rises as short_k falls toward ATM -> search ascending
            k_star = brentq(lambda k: credit_at(k) - TARGET_CREDIT, S0 * 1.001, hi, xtol=1e-3, maxiter=200)
        else:
            k_star = brentq(lambda k: credit_at(k) - TARGET_CREDIT, lo, S0 * 0.999, xtol=1e-3, maxiter=200)
    except Exception:
        return {"skip": "strike solve failed"}

    k_star  = round(k_star)                         # snap to $1 strike grid
    long_k  = k_star - WIDTH if not is_call else k_star + WIDTH
    credit  = round(_spread_credit(S0, k_star, long_k, T0, RISK_FREE_RATE, iv, is_call), 2)
    delta   = round(_bs_delta(S0, k_star, T0, RISK_FREE_RATE, iv, is_call), 3)

    # 3) Outcome at expiry
    if expiry > TODAY:
        return {"skip": "expiry in future"}
    S_T = stock_map.get(expiry.isoformat()) or _nearest(stock_map, expiry.isoformat(), after=False)
    if not S_T:
        return {"skip": "no expiry price"}

    if is_call:
        payoff = min(max(S_T - k_star, 0.0), WIDTH)
    else:
        payoff = min(max(k_star - S_T, 0.0), WIDTH)
    pnl = round((credit - payoff) * 100, 2)

    return {
        "iv": round(iv, 3), "S0": round(S0, 2), "S_T": round(S_T, 2),
        "short_k": k_star, "long_k": long_k, "credit": credit,
        "credit_frac": round(credit / WIDTH, 3), "delta": delta,
        "payoff": round(payoff, 2), "pnl": pnl, "win": pnl > 0,
    }


def _nearest(m, target, after=True):
    days = sorted(m.keys())
    if after:
        for d in days:
            if d >= target:
                return m[d]
    else:
        for d in reversed(days):
            if d <= target:
                return m[d]
    return None


def load_closed(db_path):
    c = sqlite3.connect(db_path); c.row_factory = sqlite3.Row
    rows = c.execute("""SELECT id,ticker,type,short_strike,long_strike,expiration,
                        opened_at,credit_received,contracts,close_reason,total_profit
                        FROM trades WHERE status='closed' ORDER BY opened_at""").fetchall()
    c.close()
    return [dict(r) for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/trades.db")
    ap.add_argument("--out", default="data/backtest_corrected_results.json")
    args = ap.parse_args()
    key = _api_key()

    trades = load_closed(args.db)
    print(f"Loaded {len(trades)} closed trades\n")

    # Fetch one wide stock range per ticker (cache), then slice per trade
    by_ticker = {}
    for t in trades:
        by_ticker.setdefault(t["ticker"], []).append(t)
    stock = {}
    for tk, ts in by_ticker.items():
        frm = min(t["opened_at"][:10] for t in ts)
        to  = (min(max(date.fromisoformat(t["expiration"]) for t in ts), TODAY)
               + timedelta(days=3)).isoformat()
        print(f"  fetching {tk} {frm}..{to}")
        stock[tk] = get_stock_closes(tk, frm, to, key)

    rows = []
    for t in trades:
        r = reconstruct(t, stock.get(t["ticker"], {}))
        r["ticker"] = t["ticker"]; r["type"] = t["type"]
        r["entry"] = t["opened_at"][:10]; r["actual_pnl"] = t.get("total_profit") or 0
        rows.append(r)

    ok = [r for r in rows if "skip" not in r]
    skipped = [r for r in rows if "skip" in r]

    print("\n" + "=" * 104)
    print("CORRECTED STRUCTURE  ($5 width, credit >= 1/3 width, hold to expiry)  vs  ACTUAL")
    print("=" * 104)
    hdr = (f"{'Ticker':<6} {'Type':<10} {'Entry':<11} {'shortK':>7} {'cr':>5} {'c/w':>5} "
           f"{'Δ':>6} {'S0':>7} {'S_T':>7} {'CORR P&L':>9}   {'ACTUAL P&L':>10}")
    print(hdr); print("-" * len(hdr))
    for r in rows:
        if "skip" in r:
            print(f"{r['ticker']:<6} {r['type']:<10} {r['entry']:<11} "
                  f"{'— skipped: ' + r['skip']:>55}   ${r['actual_pnl']:>9.2f}")
            continue
        print(f"{r['ticker']:<6} {r['type']:<10} {r['entry']:<11} {r['short_k']:>7.0f} "
              f"{r['credit']:>5.2f} {r['credit_frac']*100:>4.0f}% {r['delta']:>+6.2f} "
              f"{r['S0']:>7.1f} {r['S_T']:>7.1f} ${r['pnl']:>8.2f}   ${r['actual_pnl']:>9.2f}")

    # Aggregates
    def stats(pnls):
        n = len(pnls); wins = [p for p in pnls if p > 0]; loss = [p for p in pnls if p <= 0]
        aw = sum(wins)/len(wins) if wins else 0; al = sum(loss)/len(loss) if loss else 0
        wr = len(wins)/n if n else 0
        return n, len(wins), 100*wr, sum(pnls), aw, al, (wr*aw + (1-wr)*al)

    corr_pnls = [r["pnl"] for r in ok]
    # actual P&L for the SAME trades we could reconstruct (apples-to-apples)
    act_match = [r["actual_pnl"] for r in ok]
    act_all   = [t.get("total_profit") or 0 for t in trades]

    print("\n" + "=" * 104)
    print(f"Reconstructed {len(ok)} of {len(trades)} trades ({len(skipped)} skipped)\n")
    fmt = "{:<34} {:>4} {:>5} {:>7} {:>11} {:>9} {:>9} {:>12}"
    print(fmt.format("Set", "N", "Wins", "Win%", "Total P&L", "AvgWin", "AvgLoss", "EV/trade"))
    print("-" * 104)
    for label, pnls in [("CORRECTED (1/3 width, hold)", corr_pnls),
                        ("ACTUAL (same trades)", act_match),
                        ("ACTUAL (all 36 closed)", act_all)]:
        n, w, wr, tot, aw, al, ev = stats(pnls)
        print(fmt.format(label, n, w, f"{wr:.1f}%", f"${tot:,.0f}", f"${aw:,.0f}",
                         f"${al:,.0f}", f"${ev:,.0f}"))

    if ok:
        avg_cw = sum(r["credit_frac"] for r in ok) / len(ok)
        avg_d  = sum(abs(r["delta"]) for r in ok) / len(ok)
        print(f"\nCorrected avg credit/width = {avg_cw*100:.0f}%   avg |Δshort| = {avg_d:.2f}   "
              f"(break-even win rate = {(1-avg_cw)*100:.0f}%)")

    json.dump({"run_at": datetime.now().isoformat(), "rows": rows}, open(args.out, "w"),
              indent=2, default=str)
    print(f"\nSaved -> {args.out}")


if __name__ == "__main__":
    main()
