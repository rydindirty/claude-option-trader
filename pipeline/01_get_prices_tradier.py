"""
Get Stock Prices: Real prices from Tradier
Reads the top candidates from step 00e and fetches current bid/ask/mid for each.
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


def load_tickers():
    # Load the top candidates produced by step 00e
    try:
        with open("data/stocks.json", "r") as f:
            data = json.load(f)
        tickers = data.get("tickers", data) if isinstance(data, dict) else data
        if not tickers:
            print("❌ No tickers found in data/stocks.json")
            sys.exit(1)
        return tickers
    except FileNotFoundError:
        print("❌ data/stocks.json not found — run step 00e first")
        sys.exit(1)


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

    # Tradier returns a single dict when one symbol, array when multiple
    raw = data.get("quotes", {}).get("quote", [])
    if isinstance(raw, dict):
        raw = [raw]
    return raw


def get_prices(tickers):
    # Fetch current bid/ask/mid for each ticker, handling per-ticker errors gracefully
    print(f"💰 Fetching prices for {len(tickers)} tickers from Tradier...")

    prices = {}
    failed = []

    # Process in batches of 50 to stay within API limits
    for i in range(0, len(tickers), 50):
        batch = tickers[i:i + 50]

        try:
            quotes = fetch_quotes(batch)
        except Exception as e:
            print(f"   ❌ Batch {i//50 + 1} API error: {e}")
            for ticker in batch:
                failed.append(ticker)
            continue

        # Index returned quotes by symbol for fast lookup
        quote_map = {q["symbol"]: q for q in quotes if "symbol" in q}

        for ticker in batch:
            if ticker not in quote_map:
                failed.append(ticker)
                print(f"   ❌ {ticker}: No quote data returned")
                continue

            q = quote_map[ticker]
            bid = q.get("bid")
            ask = q.get("ask")

            # Skip tickers with missing or zero quotes
            if not bid or not ask or float(bid) <= 0 or float(ask) <= 0:
                failed.append(ticker)
                print(f"   ❌ {ticker}: Invalid bid/ask ({bid}/{ask})")
                continue

            bid = round(float(bid), 2)
            ask = round(float(ask), 2)
            mid = round((bid + ask) / 2, 2)

            prices[ticker] = {
                "ticker": ticker,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread": round(ask - bid, 2),
                "timestamp": datetime.now().isoformat()
            }
            print(f"   ✅ {ticker}: ${mid:.2f} (bid: ${bid:.2f}, ask: ${ask:.2f})")

    return prices, failed


def save_prices(prices, failed, tickers):
    # Save results to data/stock_prices.json for downstream steps
    output = {
        "timestamp": datetime.now().isoformat(),
        "requested": len(tickers),
        "success": len(prices),
        "failed": len(failed),
        "prices": prices,
        "missing_tickers": failed
    }

    with open("data/stock_prices.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n📊 Results:")
    print(f"   Success: {len(prices)}/{len(tickers)} ({len(prices)/len(tickers)*100:.1f}%)")
    print(f"   Failed:  {len(failed)}")

    if len(prices) == 0:
        print("❌ FATAL: No prices collected — check Tradier credentials and network")
        sys.exit(1)
    elif len(prices) < len(tickers) * 0.5:
        print("⚠️  WARNING: Less than 50% success rate — pipeline may have limited results")


def main():
    print("=" * 60)
    print("STEP 01: Get Stock Prices (Tradier)")
    print("=" * 60)

    # Load ticker list from previous step
    tickers = load_tickers()

    # Fetch current market quotes
    prices, failed = get_prices(tickers)

    # Persist results for downstream steps
    save_prices(prices, failed, tickers)

    print("✅ Step 01 complete: stock_prices.json created")


if __name__ == "__main__":
    main()
