"""
Step 00I: Fetch daily OHLCV history for all filtered tickers via Tradier.
Produces data/ohlcv.json consumed by Step 01D (Kronos forecast).
Fetches ~250 trading days per ticker to fill Kronos-small's 512-token context.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_tradier_session, TRADIER_BASE_URL, TRADIER_HEADERS

_session = get_tradier_session()


def fetch_ohlcv(ticker, trading_days=250):
    """Fetch daily OHLCV from Tradier history endpoint. Returns list of dicts or None."""
    # Request ~2x calendar days to account for weekends/holidays
    end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=trading_days * 2)

    r = _session.get(
        f"{TRADIER_BASE_URL}/markets/history",
        headers=TRADIER_HEADERS,
        params={
            "symbol":   ticker,
            "interval": "daily",
            "start":    start_dt.strftime("%Y-%m-%d"),
            "end":      end_dt.strftime("%Y-%m-%d"),
        },
        timeout=15,
    )
    r.raise_for_status()
    raw = r.json()

    hist = raw.get("history")
    if not hist or hist == "null":
        return None

    day_list = hist.get("day", [])
    if isinstance(day_list, dict):   # Single-day response comes as a dict
        day_list = [day_list]
    if not day_list:
        return None

    # Keep only the most recent trading_days candles
    day_list = day_list[-trading_days:]

    return [
        {
            "date":   d["date"],
            "open":   float(d["open"]),
            "high":   float(d["high"]),
            "low":    float(d["low"]),
            "close":  float(d["close"]),
            "volume": float(d.get("volume") or 0),
        }
        for d in day_list
    ]


def main():
    print("=" * 60)
    print("STEP 00I: Fetch OHLCV History (Tradier)")
    print("=" * 60)

    with open("data/stocks.json") as f:
        stocks_data = json.load(f)

    tickers = stocks_data.get("tickers", stocks_data)
    if isinstance(tickers, list) and tickers and isinstance(tickers[0], dict):
        tickers = [t["ticker"] for t in tickers]

    print(f"\n   Fetching daily OHLCV for {len(tickers)} tickers...")

    ohlcv = {}
    failed = []

    for ticker in tickers:
        try:
            rows = fetch_ohlcv(ticker)
            if rows and len(rows) >= 20:
                ohlcv[ticker] = rows
                print(f"   ✓ {ticker}: {len(rows)} days")
            else:
                count = len(rows) if rows else 0
                print(f"   ⚠️  {ticker}: insufficient data ({count} days) — skipping")
                failed.append(ticker)
        except Exception as e:
            print(f"   ⚠️  {ticker}: {e}")
            failed.append(ticker)
        time.sleep(0.1)   # light throttle

    with open("data/ohlcv.json", "w") as f:
        json.dump(
            {
                "timestamp": datetime.now().isoformat(),
                "tickers":   list(ohlcv.keys()),
                "ohlcv":     ohlcv,
            },
            f,
            indent=2,
        )

    print(f"\n   ✓ {len(ohlcv)} tickers with OHLCV data")
    if failed:
        print(f"   ⚠️  {len(failed)} failed: {', '.join(failed)}")
    print("\n✅ Step 00I complete: ohlcv.json")


if __name__ == "__main__":
    main()
