#!/usr/bin/env python3
"""
Master Pipeline - Tradier + Claude Implementation
Runs all steps and shows the data cascade
"""
import subprocess
import sys
import time
import json
from datetime import datetime

def print_header():
    print("\n" + "="*80)
    print("💎 CREDIT SPREAD FINDER - MASTER PIPELINE (Tradier + Claude)")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)

def run_step(num, script, description):
    # Run a pipeline step and report success/failure with timing
    print(f"\n{'─'*80}")
    print(f"⚡ STEP {num}: {description}")
    print(f"{'─'*80}")

    start = time.time()
    result = subprocess.run([sys.executable, script])
    elapsed = time.time() - start

    if result.returncode == 0:
        print(f"✅ Complete ({elapsed:.1f}s)")
        return True
    else:
        print(f"❌ Failed ({elapsed:.1f}s)")
        return False

def show_flow():
    # Print a summary of how many stocks survived each filter stage
    print("\n" + "="*80)
    print("📊 DATA FLOW SUMMARY")
    print("="*80)

    try:
        with open("data/sp500.json", "r") as f:
            sp500 = json.load(f)
            total = len(sp500.get("tickers", sp500))
            print(f"\n🎯 S&P 500: {total} tickers")

        with open("data/filter1_passed.json", "r") as f:
            f1 = json.load(f)
            print(f"   ↓ Price Filter: {len(f1)} passed ({len(f1)/total*100:.1f}%)")

        with open("data/filter2_passed.json", "r") as f:
            f2 = json.load(f)
            print(f"   ↓ Options Filter: {len(f2)} passed ({len(f2)/len(f1)*100:.1f}%)")

        with open("data/filter3_passed.json", "r") as f:
            f3 = json.load(f)
            print(f"   ↓ IV Filter: {len(f3)} passed ({len(f3)/len(f2)*100:.1f}%)")

        with open("data/stocks.json", "r") as f:
            stocks = json.load(f)
            tickers = stocks.get("tickers", stocks)
            print(f"   ↓ Top Scored: {len(tickers)} selected")

        with open("data/spreads.json", "r") as f:
            spreads = json.load(f)
            print(f"\n📈 Spreads Built: {spreads['total_spreads']}")

        with open("data/ranked_spreads.json", "r") as f:
            ranked = json.load(f)
            print(f"   ↓ Ranked: {ranked['summary']['total']}")
            print(f"   ↓ Top 22 (1 per ticker): {len(ranked['top_22'])}")

        with open("data/top9_analysis.json", "r") as f:
            print(f"\n🎯 Final Output: 9 trades ready")

    except Exception as e:
        print(f"⚠️  Could not load summary: {e}")

def show_audit():
    """Print a freshness audit of every data file produced by the pipeline."""
    import os
    import json
    from datetime import datetime

    print("\n" + "="*80)
    print("🔍 DATA FRESHNESS AUDIT")
    print("="*80)

    files = [
        ("data/sp500.json",               "S&P 500 tickers"),
        ("data/filter1_passed.json",       "Price filter"),
        ("data/filter2_passed.json",       "Options filter"),
        ("data/filter3_passed.json",       "IV filter"),
        ("data/stocks.json",               "Top 22 selected"),
        ("data/finnhub_news.json",         "Finnhub news"),
        ("data/stock_prices.json",         "Stock prices"),
        ("data/technicals.json",           "Technical indicators"),
        ("data/peer_zscores.json",         "Peer z-scores"),
        ("data/chains.json",               "Options chains"),
        ("data/chains_with_greeks.json",   "Greeks"),
        ("data/spreads.json",              "Spreads calculated"),
        ("data/ranked_spreads.json",       "Spreads ranked"),
        ("data/report_table.json",         "Report table"),
        ("data/macro_regime.json",         "Macro regime"),
        ("data/ohlcv.json",                "OHLCV history"),
        ("data/kronos_signals.json",       "Kronos AI forecasts"),
        ("data/top9_analysis.json",        "Claude analysis"),
    ]

    now = datetime.now()
    all_fresh = True

    print(f"\n{'File':<35} {'Age':>8}  {'Status':<10} {'Records'}")
    print("-" * 75)

    for filepath, label in files:
        try:
            with open(filepath) as f:
                data = json.load(f)

            # Handle list files (no timestamp at root level)
            if isinstance(data, list):
                age_str = "no ts"
                status = "✅ EXISTS"
                records = str(len(data))
                print(f"{label:<35} {age_str:>8}  {status:<10} {records}")
                continue

            # Get timestamp
            ts = data.get("timestamp")
            if ts:
                age = now - datetime.fromisoformat(ts)
                mins = age.total_seconds() / 60
                if mins < 60:
                    age_str = f"{mins:.0f}m ago"
                else:
                    age_str = f"{age.total_seconds()/3600:.1f}h ago"
                status = "✅ FRESH" if mins < 120 else "⚠️  STALE"
                if mins >= 120:
                    all_fresh = False
            else:
                age_str = "no ts"
                status = "⚠️  NO TS"

            # Get record count
            if isinstance(data, list):
                records = str(len(data))
            elif "stocks" in data and isinstance(data["stocks"], list):
                records = str(len(data["stocks"]))
            elif "tickers" in data:
                records = str(len(data["tickers"]))
            elif "chains_with_greeks" in data:
                records = str(len(data.get("chains_with_greeks", {}))) + " tickers"
            elif "chains" in data:
                records = str(len(data.get("chains", {}))) + " tickers"
            elif "top_22" in data:
                records = str(len(data.get("top_22", []))) + " ranked"
            elif "spreads" in data:
                records = str(data.get("total_spreads", "?")) + " spreads"
            elif "report_table" in data:
                records = str(len(data.get("report_table", []))) + " trades"
            elif "prices" in data:
                records = str(len(data.get("prices", {}))) + " stocks"
            elif "news_data" in data:
                records = str(len(data.get("news_data", {}))) + " stocks"
            else:
                records = "ok"

            print(f"{label:<35} {age_str:>8}  {status:<10} {records}")

        except FileNotFoundError:
            print(f"{label:<35} {'missing':>8}  {'❌ MISSING':<10}")
            all_fresh = False
        except Exception as e:
            print(f"{label:<35} {'error':>8}  {'❌ ERROR':<10} {str(e)[:20]}")

    print("-" * 75)
    if all_fresh:
        print("✅ All data files are fresh — safe to trade")
    else:
        print("⚠️  Some files are stale or missing — review before trading")
    print()


def main():
    print_header()

    # All steps now point to Tradier/Claude migrated versions
    steps = [
        ("00A", "pipeline/00a_get_sp500.py",                "Get S&P 500 tickers"),
        ("00B", "pipeline/00b_filter_price_tradier.py",      "Filter by price & spread"),
        ("00C", "pipeline/00c_filter_options_tradier.py",    "Filter by options chains"),
        ("00D", "pipeline/00d_filter_iv_tradier.py",         "Filter by IV range"),
        ("00E", "pipeline/00e_select_22_tradier.py",         "Score & select top 22"),
        ("00F", "pipeline/00f_get_news_tradier.py",          "Fetch news headlines"),
        ("00G", "pipeline/00g_claude_sentiment_filter.py",   "Claude sentiment filter"),
        ("00H", "pipeline/00h_macro_regime.py",              "Macro regime classification"),
        ("00I", "pipeline/00i_fetch_ohlcv.py",               "Fetch OHLCV history (Tradier)"),
        ("01D", "pipeline/01d_kronos_forecast.py",           "Kronos AI price forecast"),
        ("01",  "pipeline/01_get_prices_tradier.py",         "Get real-time prices"),
        ("01B", "pipeline/01b_get_technicals.py",            "Compute technical indicators"),
        ("02",  "pipeline/02_get_chains_tradier.py",         "Get options chains"),
        ("03",  "pipeline/03_check_liquidity_tradier.py",    "Check liquidity"),
        ("04",  "pipeline/04_get_greeks_tradier.py",         "Extract Greeks"),
        ("01C", "pipeline/01c_peer_zscores.py",              "Peer group z-scoring"),
        ("05",  "pipeline/05_calculate_spreads_tradier.py",  "Calculate spreads"),
        ("06",  "pipeline/06_rank_spreads_tradier.py",       "Rank by score"),
        ("07",  "pipeline/07_build_report_tradier.py",       "Build report table"),
        ("08",  "pipeline/08_claude_analysis.py",            "Claude 5W1H analysis"),
        ("09",  "pipeline/09_format_trades_tradier.py",      "Format final trades")
    ]

    pipeline_start = time.time()
    failed_at = None

    for num, script, desc in steps:
        if not run_step(num, script, desc):
            failed_at = num
            break
        time.sleep(0.5)

    elapsed = time.time() - pipeline_start

    if not failed_at:
        show_flow()
        show_audit()
        print(f"\n{'='*80}")
        print(f"✅ PIPELINE COMPLETE ({elapsed:.1f}s total)")
        print(f"{'='*80}")

        print("\n📊 TODAY'S TRADE RECOMMENDATIONS:")
        print("="*80)
        subprocess.run([sys.executable,
                       "pipeline/09_format_trades_tradier.py"])

        print("\n")
        print("⏳ Launching trade approval screen in 5 seconds...")
        print("   Press Ctrl+C to cancel and run manually later:")
        print("   python3 pipeline/11_place_trades.py")
        time.sleep(5)
        subprocess.run([sys.executable,
                       "pipeline/11_place_trades.py"])

        print("\n🔍 Starting position monitor...")
        print("   Press Ctrl+C to stop monitoring")
        subprocess.run([sys.executable, "pipeline/12_position_monitor.py"])
    else:
        print(f"\n❌ Pipeline stopped at step {failed_at}")
        print(f"   Total time: {elapsed:.1f}s")

if __name__ == "__main__":
    main()
