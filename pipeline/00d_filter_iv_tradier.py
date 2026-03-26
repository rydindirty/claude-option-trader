"""
Filter by IV - Tradier implementation
"""
import json
import sys
import os
import requests
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import TRADIER_TOKEN, TRADIER_ENV, get_tradier_session, TRADIER_BASE_URL, TRADIER_HEADERS

# BASE_URL and HEADERS are now imported from config
_session = get_tradier_session()  # SSL-verified session for Tradier API

def get_chain_with_greeks(ticker, expiration):
    # Fetch option chain including greeks for a ticker and expiration date
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
    return options  # list of option objects with greeks attached

def get_atm_iv(options, stock_price):
    # Find the strike closest to the current stock price, then average
    # the call and put IV at that strike to get ATM IV
    calls = [o for o in options if o.get("option_type") == "call"]
    puts  = [o for o in options if o.get("option_type") == "put"]

    if not calls:
        return None

    atm_call = min(calls, key=lambda o: abs(float(o["strike"]) - stock_price))
    atm_strike = float(atm_call["strike"])

    # Pull smv_vol from greeks sub-object; it is annualized IV as a decimal
    call_iv = None
    put_iv  = None

    greeks = atm_call.get("greeks") or {}
    raw = greeks.get("smv_vol")
    if raw is not None:
        call_iv = float(raw)

    atm_puts = [o for o in puts if float(o["strike"]) == atm_strike]
    if atm_puts:
        greeks = atm_puts[0].get("greeks") or {}
        raw = greeks.get("smv_vol")
        if raw is not None:
            put_iv = float(raw)

    # Average whichever IVs are available
    ivs = [v for v in [call_iv, put_iv] if v is not None and v > 0]
    if not ivs:
        return None
    return sum(ivs) / len(ivs)

def filter_iv():
    print("="*60)
    print("STEP 0D: Filter IV")
    print("="*60)

    # Load stocks that passed the options availability filter in step 00c
    with open("data/filter2_passed.json", "r") as f:
        raw = json.load(f)
    # Handle both old list format and new dict format with timestamp
    stocks = raw.get("stocks", raw) if isinstance(raw, dict) else raw

    print(f"Input: {len(stocks)} stocks")

    passed = []
    failed = []

    # Process each ticker individually so one failure doesn't stop the run
    for stock_data in stocks:
        ticker = stock_data["ticker"]
        exp_date_str = stock_data["best_expiration"]["date"]
        stock_price = stock_data["mid"]

        try:
            options = get_chain_with_greeks(ticker, exp_date_str)

            if not options:
                failed.append({"ticker": ticker, "reason": "no chain"})
                continue

            # Compute ATM IV by averaging call and put smv_vol at the ATM strike
            iv = get_atm_iv(options, stock_price)

            if iv is None:
                failed.append({"ticker": ticker, "reason": "no IV data"})
                continue

            iv_pct = iv * 100

            if 15 <= iv_pct <= 80:
                passed.append({
                    **stock_data,
                    "iv": round(iv, 4),
                    "iv_pct": round(iv_pct, 1)
                })
            else:
                failed.append({"ticker": ticker, "reason": f"IV {iv_pct:.1f}% out of range"})

        except Exception as e:
            failed.append({"ticker": ticker, "reason": str(e)[:30]})

    return passed, failed

def save_results(passed, failed):
    # Save passing stocks to data/filter3_passed.json for downstream steps
    from datetime import datetime
    output = {
        "timestamp": datetime.now().isoformat(),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "criteria": "IV 15-80%",
        "stocks": passed
    }
    with open("data/filter3_passed.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults:")
    print(f"  Passed: {len(passed)}")
    print(f"  Failed: {len(failed)}")
    print(f"\nCriteria: IV 15-80%")

def main():
    passed, failed = filter_iv()
    save_results(passed, failed)
    print("Step 0D complete")

if __name__ == "__main__":
    main()
