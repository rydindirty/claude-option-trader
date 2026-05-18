#!/usr/bin/env python3
"""
Options credit spread strategy backtester.

Replays all closed paper trades with three exit rule sets side-by-side:
  A) Old: 1.5x credit price stop (what actually happened)
  B) New: delta stop when abs(short delta) >= 0.50
  C) Hold: no stop — only profit target, width cap, and 21-DTE time stop

Uses Polygon.io / Massive API for historical daily option and stock prices.
Delta is back-calculated daily via Black-Scholes from market prices.

Setup:
  pip install requests scipy
  export MASSIVE_API_KEY=your_key   (or POLYGON_API_KEY)

Get the DB from EC2:
  scp -i "~/Downloads/AWS Key Pair.pem" ubuntu@3.145.122.245:/home/ubuntu/News_Spread_Engine/data/trades.db data/trades.db

Usage:
  python3 backtest.py                        # replay all 28 closed trades
  python3 backtest.py --db path/to/trades.db
  python3 backtest.py --id 5                 # replay a single trade by DB id
"""
import os
import sys
import math
import json
import sqlite3
import argparse
import time
from datetime import date, datetime, timedelta
from typing import Optional

try:
    import requests
    from scipy.stats import norm
    from scipy.optimize import brentq
except ImportError:
    print("Missing dependencies. Run:  pip install requests scipy")
    sys.exit(1)

# ── Strategy constants (must match pipeline) ─────────────────────────────────
RISK_FREE_RATE = 0.05   # ~5% for 2025
PROFIT_TARGET  = 0.50   # close when spread value ≤ 50% of credit
WIDTH_CAP_PCT  = 0.80   # hard close when spread value ≥ 80% of width
DTE_TIME_STOP  = 21     # hard close at this DTE
API_DELAY_SEC  = 0.15   # pause between Polygon calls (rate limit: 5 req/s free, 100/s paid)

# ── Rule sets to compare ──────────────────────────────────────────────────────
RULE_SETS = {
    "A: old stop (1.5x)":    {"stop_mult": 1.5,  "delta_stop": None},
    "B: delta stop (≥0.50)": {"stop_mult": None, "delta_stop": 0.50},
    "C: no stop (hold)":     {"stop_mult": None, "delta_stop": None},
}


# ── Polygon / Massive API ─────────────────────────────────────────────────────
def _api_key() -> str:
    key = os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY")
    if not key:
        print("ERROR: set MASSIVE_API_KEY or POLYGON_API_KEY in your environment")
        sys.exit(1)
    return key


def _get(path: str, params: dict, key: str) -> dict:
    params["apiKey"] = key
    r = requests.get(f"https://api.polygon.io{path}", params=params, timeout=20)
    if r.status_code == 403:
        print(f"   ⚠️  403 Forbidden on {path} — check your plan includes options history")
        return {}
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()


def get_option_aggs(occ_symbol: str, from_date: str, to_date: str, key: str) -> list:
    """
    Fetch daily OHLCV for an option contract.
    occ_symbol — OCC format WITHOUT the 'O:' prefix (e.g. ORCL250620P00155000).
    Returns list of {date, close, volume}.
    """
    polygon_sym = f"O:{occ_symbol}"
    try:
        data = _get(
            f"/v2/aggs/ticker/{polygon_sym}/range/1/day/{from_date}/{to_date}",
            {"adjusted": "false", "sort": "asc", "limit": 300},
            key,
        )
        time.sleep(API_DELAY_SEC)
        return [
            {
                "date":   datetime.fromtimestamp(r["t"] / 1000).strftime("%Y-%m-%d"),
                "close":  float(r.get("c") or 0),
                "volume": float(r.get("v") or 0),
            }
            for r in data.get("results", [])
            if r.get("c", 0) > 0
        ]
    except Exception as e:
        print(f"   ⚠️  Option aggs failed ({occ_symbol}): {e}")
        return []


def get_stock_aggs(ticker: str, from_date: str, to_date: str, key: str) -> dict:
    """
    Fetch daily close prices for a stock.
    Returns {date_str: close_price} dict.
    """
    try:
        data = _get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}",
            {"adjusted": "true", "sort": "asc", "limit": 300},
            key,
        )
        time.sleep(API_DELAY_SEC)
        return {
            datetime.fromtimestamp(r["t"] / 1000).strftime("%Y-%m-%d"): float(r.get("c") or 0)
            for r in data.get("results", [])
            if r.get("c", 0) > 0
        }
    except Exception as e:
        print(f"   ⚠️  Stock aggs failed ({ticker}): {e}")
        return {}


# ── Black-Scholes helpers ─────────────────────────────────────────────────────
def _bs_price(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def _bs_delta(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return norm.cdf(d1) if is_call else (norm.cdf(d1) - 1)  # negative for puts


def _implied_vol(
    market_price: float, S: float, K: float, T: float, r: float, is_call: bool
) -> Optional[float]:
    """Back-solve IV from market price using Brent's method."""
    if market_price <= 0 or T <= 0 or S <= 0:
        return None
    intrinsic = max(0.0, (S - K) if is_call else (K - S))
    if market_price < intrinsic:
        return None
    try:
        return brentq(
            lambda v: _bs_price(S, K, T, r, v, is_call) - market_price,
            1e-4, 20.0,
            xtol=1e-4, maxiter=100,
        )
    except Exception:
        return None


# ── DB loader ─────────────────────────────────────────────────────────────────
def load_closed_trades(db_path: str, trade_id: Optional[int] = None) -> list:
    if not os.path.exists(db_path):
        print(f"ERROR: DB not found at {db_path}")
        print("Copy it from EC2 first:")
        print('  scp -i "~/Downloads/AWS Key Pair.pem" ubuntu@3.145.122.245:'
              '/home/ubuntu/News_Spread_Engine/data/trades.db data/trades.db')
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    q = """
        SELECT id, ticker, type, short_strike, long_strike, expiration,
               short_symbol, long_symbol, opened_at, closed_at,
               credit_received, max_loss, contracts,
               close_reason, total_profit, profit_pct
        FROM trades WHERE status = 'closed'
    """
    if trade_id:
        q += f" AND id = {trade_id}"
    q += " ORDER BY opened_at"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Simulation core ───────────────────────────────────────────────────────────
def simulate_trade(
    trade: dict,
    short_aggs: list,
    long_aggs: list,
    stock_prices: dict,
    rules: dict,
) -> dict:
    """
    Walk forward through the option price history and apply exit rules.
    Uses daily close prices — "check at end of each trading day" model.

    Returns dict: exit_date, exit_reason, exit_spread_value, pnl, pnl_pct,
                  days_held, exit_delta (if delta stop fired).
    """
    credit     = float(trade["credit_received"])
    width      = abs(float(trade["short_strike"]) - float(trade["long_strike"]))
    expiry     = date.fromisoformat(trade["expiration"])
    is_call    = "Bear Call" in trade["type"]
    short_k    = float(trade["short_strike"])
    contracts  = int(trade["contracts"])

    short_map = {r["date"]: r["close"] for r in short_aggs}
    long_map  = {r["date"]: r["close"] for r in long_aggs}

    entry_dt       = datetime.fromisoformat(trade["opened_at"])
    entry_date_str = entry_dt.strftime("%Y-%m-%d")

    trading_days = sorted(set(short_map.keys()) & set(long_map.keys()))
    trading_days = [d for d in trading_days if d > entry_date_str]

    if not trading_days:
        return _no_data_result()

    stop_mult  = rules.get("stop_mult")
    delta_stop = rules.get("delta_stop")

    for day_str in trading_days:
        day  = date.fromisoformat(day_str)
        dte  = (expiry - day).days

        short_px   = short_map[day_str]
        long_px    = long_map[day_str]
        spread_val = max(0.0, round(short_px - long_px, 2))
        pct        = (credit - spread_val) / credit * 100
        days_held  = (day - entry_dt.date()).days

        # ── 1. Profit target ──────────────────────────────────────
        if pct >= PROFIT_TARGET * 100:
            return _result("profit_target", spread_val, credit, contracts, pct, days_held)

        # ── 2. Price-based stop loss (old rule) ───────────────────
        if stop_mult and spread_val >= credit * stop_mult:
            return _result("stop_loss", spread_val, credit, contracts, pct, days_held)

        # ── 3. Delta stop (new rule) ──────────────────────────────
        if delta_stop:
            stock_px = stock_prices.get(day_str, 0)
            if stock_px > 0:
                T  = max(dte, 1) / 365.0
                iv = _implied_vol(short_px, stock_px, short_k, T, RISK_FREE_RATE, is_call)
                if iv:
                    delta = _bs_delta(stock_px, short_k, T, RISK_FREE_RATE, iv, is_call)
                    if abs(delta) >= delta_stop:
                        r = _result("delta_stop", spread_val, credit, contracts, pct, days_held)
                        r["exit_delta"] = round(delta, 3)
                        return r

        # ── 4. Width hard cap ─────────────────────────────────────
        if spread_val >= width * WIDTH_CAP_PCT:
            return _result("width_cap", spread_val, credit, contracts, pct, days_held)

        # ── 5. DTE time stop ──────────────────────────────────────
        if dte < DTE_TIME_STOP:
            return _result("time_stop", spread_val, credit, contracts, pct, days_held)

    # Held to expiry (no exit rule fired) — assume expires worthless if OTM
    return _result("held_to_expiry", 0.0, credit, contracts, 100.0,
                   (expiry - entry_dt.date()).days, exit_date=expiry.isoformat())


def _result(reason, spread_val, credit, contracts, pct, days, exit_date=None) -> dict:
    pnl = round((credit - spread_val) * contracts * 100, 2)
    return {
        "exit_reason":       reason,
        "exit_date":         exit_date,
        "exit_spread_value": spread_val,
        "pnl":               pnl,
        "pnl_pct":           round(pct, 1),
        "days_held":         days,
        "exit_delta":        None,
    }


def _no_data_result() -> dict:
    return {"exit_reason": "no_data", "exit_date": None,
            "exit_spread_value": None, "pnl": None,
            "pnl_pct": None, "days_held": None, "exit_delta": None}


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_summary(results_by_ruleset: dict, trades: list):
    width_sep = 100
    print("\n" + "=" * width_sep)
    print("BACKTEST RESULTS — EXIT RULE COMPARISON (daily close prices)")
    print("=" * width_sep)

    # Aggregate stats
    print(f"\n{'Rule Set':<28} {'N':>4} {'Wins':>5} {'Win%':>6}  "
          f"{'Total P&L':>10}  {'Avg P&L':>8}  {'Avg Days':>9}  {'Expectancy':>11}")
    print("─" * width_sep)

    for name, results in results_by_ruleset.items():
        valid     = [r for r in results if r["pnl"] is not None]
        wins      = [r for r in valid if r["pnl"] > 0]
        total     = sum(r["pnl"] for r in valid)
        avg       = total / len(valid) if valid else 0
        win_pct   = len(wins) / len(valid) * 100 if valid else 0
        avg_days  = (sum(r["days_held"] for r in valid if r["days_held"]) / len(valid)
                     if valid else 0)
        # Expectancy = (win_rate × avg_win) + (loss_rate × avg_loss)
        avg_win  = sum(r["pnl"] for r in valid if r["pnl"] > 0) / max(len(wins), 1)
        losses   = [r for r in valid if r["pnl"] <= 0]
        avg_loss = sum(r["pnl"] for r in valid if r["pnl"] <= 0) / max(len(losses), 1)
        exp = (len(wins)/len(valid) * avg_win + len(losses)/len(valid) * avg_loss) if valid else 0
        print(f"{name:<28} {len(valid):>4} {len(wins):>5} {win_pct:>5.1f}%  "
              f"${total:>9.2f}  ${avg:>7.2f}  {avg_days:>8.1f}d  ${exp:>10.2f}")

    # Exit reason breakdown
    print(f"\n{'Exit reasons':}")
    for name, results in results_by_ruleset.items():
        reasons: dict = {}
        for r in results:
            k = r.get("exit_reason", "no_data")
            reasons[k] = reasons.get(k, 0) + 1
        sorted_r = dict(sorted(reasons.items(), key=lambda x: -x[1]))
        print(f"  {name}: {sorted_r}")

    # Per-trade detail — first two rule sets vs actual
    rule_names = list(results_by_ruleset.keys())
    r_old = results_by_ruleset[rule_names[0]]
    r_new = results_by_ruleset[rule_names[1]]

    print(f"\n{'Per-trade: actual vs simulated':}")
    hdr = (f"  {'#':>2}  {'Ticker':<6} {'Type':<10} {'Actual Exit':<16} {'Actual P&L':>10}  "
           f"{'Old Stop P&L':>12}  {'Delta Stop P&L':>14}  {'No Stop P&L':>12}  {'Δ best':>8}")
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))

    r_hold = results_by_ruleset[rule_names[2]]
    for i, (t, old, new, hold) in enumerate(zip(trades, r_old, r_new, r_hold), 1):
        actual_pnl = t.get("total_profit") or 0
        old_pnl    = old.get("pnl") or 0
        new_pnl    = new.get("pnl") or 0
        hold_pnl   = hold.get("pnl") or 0
        best_sim   = max(old_pnl, new_pnl, hold_pnl)
        improvement = best_sim - actual_pnl
        imp_str = f"+${improvement:.0f}" if improvement >= 0 else f"-${abs(improvement):.0f}"
        print(
            f"  {i:>2}  {t['ticker']:<6} {t['type']:<10} "
            f"{(t.get('close_reason') or '?'):<16} ${actual_pnl:>8.2f}  "
            f"${old_pnl:>10.2f}  ${new_pnl:>12.2f}  ${hold_pnl:>10.2f}  {imp_str:>8}"
        )

    total_actual = sum(t.get("total_profit") or 0 for t in trades)
    total_old    = sum(r.get("pnl") or 0 for r in r_old)
    total_new    = sum(r.get("pnl") or 0 for r in r_new)
    total_hold   = sum(r.get("pnl") or 0 for r in r_hold)
    print(f"\n  {'TOTAL':>38}  ${total_actual:>8.2f}  "
          f"${total_old:>10.2f}  ${total_new:>12.2f}  ${total_hold:>10.2f}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Options spread backtester")
    parser.add_argument("--db",  default="data/trades.db", help="Path to trades.db")
    parser.add_argument("--id",  type=int, default=None,   help="Replay a single trade by DB id")
    parser.add_argument("--out", default="data/backtest_results.json", help="Output JSON path")
    args = parser.parse_args()

    key = _api_key()

    print("=" * 60)
    print("OPTIONS SPREAD BACKTESTER")
    print(f"  DB:   {args.db}")
    print(f"  Out:  {args.out}")
    print(f"  Rule sets: {list(RULE_SETS.keys())}")
    print("=" * 60)

    trades = load_closed_trades(args.db, args.id)
    if not trades:
        print("No closed trades found.")
        sys.exit(0)
    print(f"\nLoaded {len(trades)} closed trade(s)\n")

    # Cache fetched data to avoid re-fetching the same symbol multiple times
    _cache: dict = {}

    def _cached_option(sym, from_d, to_d):
        k = ("opt", sym, from_d, to_d)
        if k not in _cache:
            _cache[k] = get_option_aggs(sym, from_d, to_d, key)
        return _cache[k]

    def _cached_stock(ticker, from_d, to_d):
        k = ("stk", ticker, from_d, to_d)
        if k not in _cache:
            _cache[k] = get_stock_aggs(ticker, from_d, to_d, key)
        return _cache[k]

    results_by_ruleset: dict = {name: [] for name in RULE_SETS}

    for i, trade in enumerate(trades, 1):
        ticker     = trade["ticker"]
        entry_date = trade["opened_at"][:10]
        expiry     = trade["expiration"]
        # Fetch a few extra days past expiry in case of data lag
        to_date    = (date.fromisoformat(expiry) + timedelta(days=5)).isoformat()

        print(f"[{i:>2}/{len(trades)}] {ticker:<6} {trade['type']:<10}  "
              f"entry={entry_date}  expiry={expiry}  "
              f"credit=${trade['credit_received']:.2f}  "
              f"actual={trade.get('close_reason','?'):<15} "
              f"P&L=${trade.get('total_profit') or 0:+.2f}")

        short_aggs   = _cached_option(trade["short_symbol"], entry_date, to_date)
        long_aggs    = _cached_option(trade["long_symbol"],  entry_date, to_date)
        stock_prices = _cached_stock(ticker, entry_date, to_date)

        if not short_aggs or not long_aggs:
            print(f"        ⚠️  No option data — skipping (check your Polygon plan)")
            for name in RULE_SETS:
                results_by_ruleset[name].append(_no_data_result())
            continue

        print(f"        Data: short={len(short_aggs)}d  long={len(long_aggs)}d  "
              f"stock={len(stock_prices)}d")

        for name, rules in RULE_SETS.items():
            result = simulate_trade(trade, short_aggs, long_aggs, stock_prices, rules)
            results_by_ruleset[name].append(result)
            delta_str = (f"  δ={result['exit_delta']:+.3f}"
                         if result.get("exit_delta") else "")
            print(f"        {name:<28}  "
                  f"{(result.get('exit_reason') or 'no_data'):<16}  "
                  f"P&L=${result.get('pnl') or 0:>7.2f}  "
                  f"days={result.get('days_held') or '?'}{delta_str}")

    print_summary(results_by_ruleset, trades)

    # Save full results to JSON
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    output = {
        "run_at":       datetime.now().isoformat(),
        "rule_sets":    list(RULE_SETS.keys()),
        "trade_count":  len(trades),
        "trades": [
            {
                "trade":   t,
                "results": {name: results_by_ruleset[name][j] for name in RULE_SETS},
            }
            for j, t in enumerate(trades)
        ],
    }
    with open(args.out, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n✅ Full results saved to {args.out}")


if __name__ == "__main__":
    main()
