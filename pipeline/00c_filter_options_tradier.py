"""
Filter by options availability - Tradier implementation
"""
import json
import sys
import os
import requests
from datetime import datetime, date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import TRADIER_TOKEN, TRADIER_ENV, get_tradier_session, TRADIER_BASE_URL, TRADIER_HEADERS

# BASE_URL and HEADERS are now imported from config
_session = get_tradier_session()  # SSL-verified session for Tradier API

def get_expirations(ticker):
    # Fetch all available option expiration dates for the ticker
    response = _session.get(
        f"{TRADIER_BASE_URL}/markets/options/expirations",
        headers=TRADIER_HEADERS,
        params={"symbol": ticker}
    )
    response.raise_for_status()
    data = response.json()

    dates = data.get("expirations", {}).get("date", [])
    if isinstance(dates, str):
        dates = [dates]
    return dates  # list of "YYYY-MM-DD" strings

def get_chain(ticker, expiration):
    # Fetch the full option chain for a ticker on a specific expiration date
    response = _session.get(
        f"{TRADIER_BASE_URL}/markets/options/chains",
        headers=TRADIER_HEADERS,
        params={"symbol": ticker, "expiration": expiration, "greeks": "true"}
    )
    response.raise_for_status()
    data = response.json()

    options = data.get("options", {}).get("option", [])
    if isinstance(options, dict):
        options = [options]
    return options  # list of option objects

def filter_options():
    print("="*60)
    print("STEP 0C: Filter Options")
    print("="*60)

    # Load stocks that passed the price filter in step 00b
    with open("data/filter1_passed.json", "r") as f:
        raw = json.load(f)
    # Handle both old list format and new dict format with timestamp
    stocks = raw.get("stocks", raw) if isinstance(raw, dict) else raw

    print(f"Input: {len(stocks)} stocks")

    passed = []
    failed = []
    today = datetime.now().date()

    # Process each ticker individually so one failure doesn't stop the run
    for stock_data in stocks:
        ticker = stock_data["ticker"]

        try:
            # Get expirations and find those in the 15-45 DTE window
            exp_dates = get_expirations(ticker)

            if not exp_dates:
                failed.append({"ticker": ticker, "reason": "no chain"})
                continue

            good_exps = []
            for date_str in exp_dates:
                exp_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if 15 <= dte <= 45:
                    good_exps.append({"date": date_str, "dte": dte})

            if not good_exps:
                failed.append({"ticker": ticker, "reason": "no 15-45 DTE"})
                continue

            # Check that the nearest qualifying expiration has 20+ strikes
            best_exp = good_exps[0]
            options = get_chain(ticker, best_exp["date"])

            if len(options) < 20:
                failed.append({"ticker": ticker, "reason": f"only {len(options)} strikes"})
                continue

            passed.append({
                **stock_data,
                "expirations": len(good_exps),
                "best_expiration": best_exp,
                "strikes_count": len(options)
            })

        except Exception as e:
            failed.append({"ticker": ticker, "reason": str(e)[:30]})

    return passed, failed

def save_results(passed, failed):
    # Save passing stocks to data/filter2_passed.json for downstream steps
    from datetime import datetime
    output = {
        "timestamp": datetime.now().isoformat(),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "criteria": "20+ strikes, 15-45 DTE",
        "stocks": passed
    }
    with open("data/filter2_passed.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults:")
    print(f"  Passed: {len(passed)}")
    print(f"  Failed: {len(failed)}")
    print(f"\nCriteria: 20+ strikes, 15-45 DTE")

def main():
    passed, failed = filter_options()
    save_results(passed, failed)
    print("Step 0C complete")

if __name__ == "__main__":
    main()
