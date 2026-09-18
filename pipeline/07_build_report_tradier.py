"""
Build Report Table: Top 9 spreads for GPT analysis
"""
import json
import sys
import os
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def build_report_table():
    print("="*60)
    print("STEP 7: Build Report (Top 9)")
    print("="*60)
    
    with open("data/ranked_spreads.json", "r") as f:
        data = json.load(f)
    
    # Prioritize actionable spreads: ENTER first, then WATCH, then SKIP to fill
    enter = [s for s in data["ranked_spreads"] if s.get("decision") == "ENTER"]
    watch = [s for s in data["ranked_spreads"] if s.get("decision") == "WATCH"]
    skip  = [s for s in data["ranked_spreads"] if s.get("decision") == "SKIP"]
    spreads = (enter + watch + skip)[:9]
    
    try:
        from data.stocks import EDGE_REASON
    except ImportError:
        EDGE_REASON = {}
    
    sector_map = {
        "INTC": "XLK", "AMD": "XLK", "AVGO": "XLK", "CEG": "XLU", "NVDA": "XLK",
        "ORCL": "XLK", "PLTR": "XLK", "SMCI": "XLK", "GOOGL": "XLC", "AMZN": "XLY",
        "APO": "XLF", "XYZ": "XLK", "DAL": "XLI", "ETN": "XLI", "FCX": "XLB",
        "IBM": "XLK", "LULU": "XLY", "MS": "XLF", "TTD": "XLC", "UNH": "XLV",
        "TGT": "XLY", "ABT": "XLV"
    }
    
    report_entries = []
    
    for spread in spreads:
        ticker = spread["ticker"]
        is_debit = spread["type"] in ("Long Call", "Long Put")

        ic = spread.get("ic")
        if spread["type"] == "Iron Condor" and ic:
            legs = (f"P${ic['short_put']:.0f}/${ic['long_put']:.0f} "
                    f"C${ic['short_call']:.0f}/${ic['long_call']:.0f}")
        else:
            # Keep the "$short/$long" position convention for every type (short =
            # sold leg, long = bought leg) so downstream parsers that split on "/"
            # (08_claude_analysis.py, 11_place_trades.py) don't need to know which
            # strategy built the trade.
            legs = f"${spread['short_strike']:.0f}/${spread['long_strike']:.0f}"

        entry = {
            "rank": spread["rank"],
            "sector": sector_map.get(ticker, "Unknown"),
            "ticker": ticker,
            "type": spread["type"],
            "legs": legs,
            "exp_date": spread["expiration"]["date"],
            "dte": spread["expiration"]["dte"],
            "roi": f"{spread['roi']}%",
            "pop": f"{spread['pop']}%",
            # debit spreads pay a net_debit (cost), credit spreads collect a
            # net_credit — only one of these is populated per trade.
            "net_credit": f"${spread['net_credit']:.2f}" if not is_debit else None,
            "net_debit": f"${spread['net_debit']:.2f}" if is_debit else None,
            "breakeven": f"${spread['breakeven']:.2f}" if is_debit else None,
            # for credit spreads max_profit == net_credit (not duplicated here);
            # for debit spreads max_profit = width - debit, a real separate figure
            "max_profit": f"${spread['max_profit']:.2f}" if is_debit else None,
            "max_loss": f"${spread['max_loss']:.2f}",
            "decision": spread["decision"],
            "edge_reason": EDGE_REASON.get(ticker, ""),
            "iv": spread.get("long_iv", spread.get("short_iv", 0.0)),
            "delta": spread.get("long_delta", spread.get("short_delta", 0.0)),
            "score": spread["score"],
            "kronos_direction": spread.get("kronos_direction", "n/a"),
            "kronos_forecast_pct": spread.get("kronos_forecast_pct", 0.0),
            "ic": ic,  # full 4-leg detail for the auto-trader (None for verticals)
        }
        report_entries.append(entry)
    
    output = {
        "timestamp": datetime.now().isoformat(),
        "total_entries": len(report_entries),
        "report_table": report_entries
    }
    
    with open("data/report_table.json", "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"\nReport: {len(report_entries)} trades")
    print(f"\n{'Rank':<5} {'Ticker':<8} {'Type':<12} {'ROI':<8} {'PoP':<8}")
    print("-" * 45)
    
    for entry in report_entries:
        print(f"{entry['rank']:<5} {entry['ticker']:<8} {entry['type']:<12} {entry['roi']:<8} {entry['pop']:<8}")
    
    print("\n✅ Step 7 complete: report_table.json")

if __name__ == "__main__":
    build_report_table()
