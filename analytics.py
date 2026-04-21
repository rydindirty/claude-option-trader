#!/usr/bin/env python3
"""
Trade analytics for News Spread Engine.

Usage:
  python3 analytics.py          # full report
  python3 analytics.py --recent # last 20 trades only
  python3 analytics.py --all    # include sandbox/test trades
"""
import sys
import os
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline"))
import db

SANDBOX_REASONS = {"manual_close_sandbox"}

# ── Helpers ──────────────────────────────────────────────────────────────────

def _pnl(v):
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):.2f}"
def _pct(v): return f"{v:.1f}%"
def _bar(wins, total, width=20):
    if total == 0:
        return "[" + "-" * width + "]"
    filled = round(wins / total * width)
    return "[" + "█" * filled + "─" * (width - filled) + "]"


def _breakdown(trades, key_fn, label):
    """Print a breakdown table grouped by key_fn(trade)."""
    groups = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)

    print(f"\n{label}")
    print(f"  {'Group':<22} {'Trades':>6} {'Win%':>7} {'Total P&L':>11} {'Avg P&L':>10} {'Avg DTE':>8}")
    print("  " + "─" * 70)

    rows = []
    for group, ts in groups.items():
        wins = sum(1 for t in ts if (t["total_profit"] or 0) > 0)
        total_pnl = sum(t["total_profit"] or 0 for t in ts)
        avg_pnl = total_pnl / len(ts)
        avg_dte = sum(t["dte_at_entry"] for t in ts) / len(ts)
        win_pct = wins / len(ts) * 100
        rows.append((group, len(ts), win_pct, total_pnl, avg_pnl, avg_dte))

    rows.sort(key=lambda r: r[3], reverse=True)
    for group, count, win_pct, total_pnl, avg_pnl, avg_dte in rows:
        print(f"  {str(group):<22} {count:>6}  {win_pct:>5.1f}%  "
              f"{_pnl(total_pnl):>10}  {_pnl(avg_pnl):>9}  {avg_dte:>7.1f}")


def _recent_table(trades, n=20):
    print(f"\nRECENT TRADES (last {min(n, len(trades))})")
    print(f"  {'Date':<12} {'Ticker':<6} {'Type':<10} {'Strikes':<10} "
          f"{'DTE':>4} {'Credit':>7} {'Close':>7} {'P&L':>9} {'Regime':<12} {'Reason'}")
    print("  " + "─" * 95)
    for t in trades[-n:][::-1]:
        date = (t["closed_at"] or t["opened_at"] or "")[:10]
        strikes = f"{t['short_strike']:.0f}/{t['long_strike']:.0f}"
        pnl = _pnl(t["total_profit"] or 0)
        regime = t.get("regime") or "—"
        close_v = f"${t['close_value']:.2f}" if t["close_value"] else "open"
        print(f"  {date:<12} {t['ticker']:<6} {t['type']:<10} {strikes:<10} "
              f"{t['dte_at_entry']:>4}  ${t['credit_received']:.2f}  "
              f"{close_v:>7}  {pnl:>8}  {regime:<12} {t['close_reason'] or ''}")


# ── Main report ───────────────────────────────────────────────────────────────

def run(include_sandbox=False, recent_only=False):
    db.init_db()
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT * FROM trades WHERE status = 'closed' ORDER BY id"
    ).fetchall()
    conn.close()

    all_trades = [dict(r) for r in rows]

    if not include_sandbox:
        trades = [t for t in all_trades
                  if t.get("close_reason") not in SANDBOX_REASONS]
        excluded = len(all_trades) - len(trades)
    else:
        trades = all_trades
        excluded = 0

    print("\n" + "=" * 65)
    print("TRADE ANALYTICS — News Spread Engine")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sandbox_note = f"  |  Sandbox excluded: {excluded}" if excluded else ""
    print(f"Generated: {now}  |  Real trades: {len(trades)}{sandbox_note}")
    print("=" * 65)

    if not trades:
        print("\nNo closed trades to analyze yet.")
        return

    if recent_only:
        _recent_table(trades)
        return

    # ── Overall summary ───────────────────────────────────────────
    winners = [t for t in trades if (t["total_profit"] or 0) > 0]
    losers  = [t for t in trades if (t["total_profit"] or 0) <= 0]
    win_rate = len(winners) / len(trades) * 100
    total_pnl = sum(t["total_profit"] or 0 for t in trades)
    avg_pnl = total_pnl / len(trades)
    avg_dte = sum(t["dte_at_entry"] for t in trades) / len(trades)
    avg_credit = sum(t["credit_received"] for t in trades) / len(trades)

    best  = max(trades, key=lambda t: t["total_profit"] or 0)
    worst = min(trades, key=lambda t: t["total_profit"] or 0)

    print(f"\nOVERALL SUMMARY")
    print(f"  Total closed trades:   {len(trades)}")
    print(f"  Winners:               {len(winners)}  "
          f"({_pct(win_rate)} win rate)  {_bar(len(winners), len(trades))}")
    print(f"  Losers:                {len(losers)}")
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  Total P&L:             {_pnl(total_pnl)}")
    print(f"  Avg P&L per trade:     {_pnl(avg_pnl)}")
    best_lbl  = f"{best['ticker']} {best['type']} ({best['close_reason']})"
    worst_lbl = f"{worst['ticker']} {worst['type']} ({worst['close_reason']})"
    print(f"  Best trade:            {_pnl(best['total_profit'] or 0):>8}  {best_lbl}")
    print(f"  Worst trade:           {_pnl(worst['total_profit'] or 0):>8}  {worst_lbl}")
    print(f"  Avg DTE at entry:      {avg_dte:.1f}")
    print(f"  Avg credit collected:  ${avg_credit:.2f}")

    # ── Breakdowns ────────────────────────────────────────────────
    _breakdown(trades, lambda t: t["type"],         "BY SPREAD TYPE")
    _breakdown(trades, lambda t: t["close_reason"], "BY CLOSE REASON")
    _breakdown(trades, lambda t: t["ticker"],       "BY TICKER")

    regime_trades = [t for t in trades if t.get("regime")]
    if regime_trades:
        _breakdown(regime_trades, lambda t: t["regime"], "BY REGIME")
    else:
        print(f"\nBY REGIME")
        print(f"  (No regime data yet — new trades will capture regime at entry)")

    # ── Recent trades ─────────────────────────────────────────────
    _recent_table(trades)
    print()


if __name__ == "__main__":
    include_sandbox = "--all" in sys.argv
    recent_only     = "--recent" in sys.argv
    run(include_sandbox=include_sandbox, recent_only=recent_only)
