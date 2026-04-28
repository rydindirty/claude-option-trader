#!/usr/bin/env python3
"""
Master Pipeline Runner - Complete Data Flow (Tradier + Claude)
Called by the web app's Re-run Pipeline button.
"""
import os
import subprocess
import sys
import time
from datetime import datetime

# Re-exec inside the project venv if we're running outside it
_venv_python = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "python3")
if os.path.exists(_venv_python) and os.path.abspath(sys.executable) != os.path.abspath(_venv_python):
    os.execv(_venv_python, [_venv_python] + sys.argv)

def run_step(step_name, script_path, description):
    print("\n" + "="*80)
    print(f"▶ {step_name}: {description}")
    print("="*80)

    start = time.time()
    result = subprocess.run([sys.executable, script_path], text=True)
    elapsed = time.time() - start

    if result.returncode == 0:
        print(f"\n✅ {step_name} complete ({elapsed:.1f}s)")
        return True
    else:
        print(f"\n❌ {step_name} FAILED ({elapsed:.1f}s)")
        return False

def main():
    print("\n" + "█"*80)
    print("█" + "  CREDIT SPREAD FINDER - FULL PIPELINE".center(78) + "█")
    print("█" + f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}".center(78) + "█")
    print("█"*80)

    start = time.time()

    steps = [
        ("00A", "pipeline/00a_get_sp500.py",                "Get S&P 500 tickers"),
        ("00B", "pipeline/00b_filter_price_tradier.py",     "Filter by price & spread"),
        ("00C", "pipeline/00c_filter_options_tradier.py",   "Filter by options chains"),
        ("00D", "pipeline/00d_filter_iv_tradier.py",        "Filter by IV range"),
        ("00E", "pipeline/00e_select_22_tradier.py",        "Score & select top 22"),
        ("00F", "pipeline/00f_get_news_tradier.py",         "Fetch news headlines"),
        ("00G", "pipeline/00g_claude_sentiment_filter.py",  "Claude sentiment filter"),
        ("00H", "pipeline/00h_macro_regime.py",             "Macro regime classification"),
        ("00I", "pipeline/00i_fetch_ohlcv.py",              "Fetch OHLCV history"),
        ("01",  "pipeline/01_get_prices_tradier.py",        "Get real-time prices"),
        ("01B", "pipeline/01b_get_technicals.py",           "Compute technical indicators"),
        ("02",  "pipeline/02_get_chains_tradier.py",        "Get options chains"),
        ("03",  "pipeline/03_check_liquidity_tradier.py",   "Check liquidity"),
        ("04",  "pipeline/04_get_greeks_tradier.py",        "Extract Greeks"),
        ("01C", "pipeline/01c_peer_zscores.py",             "Peer group z-scoring"),
        ("01D", "pipeline/01d_kronos_forecast.py",          "Kronos AI price forecast"),
        ("05",  "pipeline/05_calculate_spreads_tradier.py", "Calculate spreads"),
        ("06",  "pipeline/06_rank_spreads_tradier.py",      "Rank by score"),
        ("07",  "pipeline/07_build_report_tradier.py",      "Build report table"),
        ("08",  "pipeline/08_claude_analysis.py",           "Claude 5W1H analysis"),
        ("09",  "pipeline/09_format_trades_tradier.py",     "Format final trades"),
    ]

    completed = 0
    for step_name, script, desc in steps:
        if run_step(step_name, script, desc):
            completed += 1
            time.sleep(0.3)
        else:
            break

    elapsed = time.time() - start
    print("\n" + "="*80)
    print(f"{'✅ COMPLETE' if completed == len(steps) else '❌ STOPPED'}: {completed}/{len(steps)} ({elapsed:.1f}s)")
    print("="*80)

if __name__ == "__main__":
    main()
