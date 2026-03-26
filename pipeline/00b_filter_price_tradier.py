"""
Filter by price and spread - Tradier implementation
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

def fetch_quotes(tickers):
    # Fetch quotes for a batch of tickers from Tradier /markets/quotes
    symbols = ",".join(tickers)
    response = _session.get(
        f"{TRADIER_BASE_URL}/markets/quotes",
        headers=TRADIER_HEADERS,
        params={"symbols": symbols}
    )
    response.raise_for_status()
    data = response.json()

    # Tradier returns a single object when one symbol, array when multiple
    raw = data.get("quotes", {}).get("quote", [])
    if isinstance(raw, dict):
        raw = [raw]
    return raw

def filter_price_liquidity():
    print("="*60)
    print("STEP 0B: Filter Price")
    print("="*60)

    # Load the S&P 500 universe produced by step 00a
    with open("data/sp500.json", "r") as f:
        tickers = json.load(f)["tickers"]

    print(f"Input: {len(tickers)} stocks")

    passed = []
    failed = []

    # Process tickers in batches of 50 to stay within API limits
    for i in range(0, len(tickers), 50):
        batch = tickers[i:i+50]

        try:
            quotes = fetch_quotes(batch)
        except Exception as e:
            for ticker in batch:
                failed.append({"ticker": ticker, "reason": f"API error: {e}"})
            continue

        # Index returned quotes by symbol for fast lookup
        quote_map = {q["symbol"]: q for q in quotes if "symbol" in q}

        # Apply price ($30-$400) and spread (<2%) filters
        for ticker in batch:
            if ticker not in quote_map:
                failed.append({"ticker": ticker, "reason": "no quote data"})
                continue

            q = quote_map[ticker]
            bid = q.get("bid")
            ask = q.get("ask")

            if not bid or not ask or float(bid) <= 0 or float(ask) <= 0:
                failed.append({"ticker": ticker, "reason": "no quote data"})
                continue

            bid = float(bid)
            ask = float(ask)
            mid = (bid + ask) / 2
            spread_pct = ((ask - bid) / mid) * 100

            if 30 <= mid <= 400 and spread_pct < 2.0:
                passed.append({
                    "ticker": ticker,
                    "bid": round(bid, 2),
                    "ask": round(ask, 2),
                    "mid": round(mid, 2),
                    "spread_pct": round(spread_pct, 2)
                })
            else:
                reason = "price out of range" if mid < 30 or mid > 400 else f"spread {spread_pct:.2f}%"
                failed.append({"ticker": ticker, "reason": reason})

    return passed, failed

def save_results(passed, failed):
    # Save passing stocks to data/filter1_passed.json for downstream steps
    # Wrap in dict with timestamp for freshness auditing
    output = {
        "timestamp": datetime.now().isoformat(),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "criteria": "$30-400, spread <2%",
        "stocks": passed
    }
    with open("data/filter1_passed.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults:")
    print(f"  Passed: {len(passed)}")
    print(f"  Failed: {len(failed)}")
    print(f"\nCriteria: $30-400, spread <2%")

def main():
    passed, failed = filter_price_liquidity()
    save_results(passed, failed)
    print("Step 0B complete")

if __name__ == "__main__":
    main()
