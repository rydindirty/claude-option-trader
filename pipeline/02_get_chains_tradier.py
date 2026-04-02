"""
Get Options Chains: Expirations and strikes from Tradier
For each ticker, fetches expirations in the 0-45 DTE window then pulls
the full chain (with Greeks) for each qualifying expiration.
"""
import json
import sys
import os
import requests
from datetime import datetime, date, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import TRADIER_TOKEN, TRADIER_ENV, get_tradier_session, TRADIER_BASE_URL, TRADIER_HEADERS

# BASE_URL and HEADERS are now imported from config
_session = get_tradier_session()  # SSL-verified session for Tradier API


def load_stock_prices():
    # Load current prices produced by step 01
    try:
        with open("data/stock_prices.json", "r") as f:
            data = json.load(f)
        return data["prices"]
    except FileNotFoundError:
        print("❌ data/stock_prices.json not found — run step 01 first")
        sys.exit(1)


def fetch_expirations(ticker):
    # Fetch all option expiration dates for a ticker from Tradier
    response = _session.get(
        f"{TRADIER_BASE_URL}/markets/options/expirations",
        headers=TRADIER_HEADERS,
        params={"symbol": ticker, "includeAllRoots": "true"}
    )
    response.raise_for_status()
    data = response.json()

    # Returns {"expirations": {"date": ["2026-04-17", ...]}} or null when no options
    expirations = data.get("expirations") or {}
    dates = expirations.get("date", [])
    if isinstance(dates, str):
        dates = [dates]
    return dates


def fetch_chain(ticker, expiration):
    # Fetch the full option chain for a single ticker + expiration, with Greeks
    response = _session.get(
        f"{TRADIER_BASE_URL}/markets/options/chains",
        headers=TRADIER_HEADERS,
        params={"symbol": ticker, "expiration": expiration, "greeks": "true"}
    )
    response.raise_for_status()
    data = response.json()

    # Tradier returns a single dict when one contract, list when multiple
    raw = (data.get("options") or {}).get("option", [])
    if isinstance(raw, dict):
        raw = [raw]
    return raw


def build_strikes(options, stock_price):
    # Organise raw option contracts into a dict keyed by strike price,
    # merging call and put data for each strike into a single record.
    strikes = {}

    for opt in options:
        strike = float(opt.get("strike", 0))

        # Keep only strikes within 70-130% of the current stock price
        if not (stock_price * 0.70 <= strike <= stock_price * 1.30):
            continue

        if strike not in strikes:
            strikes[strike] = {
                "strike": strike,
                "call_symbol": None,
                "put_symbol": None,
                "call_bid": 0,
                "call_ask": 0,
                "put_bid": 0,
                "put_ask": 0,
                "call_volume": 0,
                "put_volume": 0,
                "call_open_interest": 0,
                "put_open_interest": 0
            }

        opt_type = (opt.get("option_type") or "").lower()
        bid = float(opt.get("bid") or 0)
        ask = float(opt.get("ask") or 0)
        volume = int(opt.get("volume") or 0)
        oi = int(opt.get("open_interest") or 0)
        symbol = opt.get("symbol", "")

        if opt_type == "call":
            strikes[strike]["call_symbol"] = symbol
            strikes[strike]["call_bid"] = bid
            strikes[strike]["call_ask"] = ask
            strikes[strike]["call_volume"] = volume
            strikes[strike]["call_open_interest"] = oi
            g = opt.get("greeks") or {}
            if g:
                # mid_iv is Tradier's mid-price IV; smv_vol is smoothed fallback
                iv_val = float(g.get("mid_iv") or g.get("smv_vol") or 0)
                strikes[strike]["call_greeks"] = {
                    "iv":    iv_val,
                    "delta": float(g.get("delta") or 0),
                    "gamma": float(g.get("gamma") or 0),
                    "theta": float(g.get("theta") or 0),
                    "vega":  float(g.get("vega") or 0)
                }
        elif opt_type == "put":
            strikes[strike]["put_symbol"] = symbol
            strikes[strike]["put_bid"] = bid
            strikes[strike]["put_ask"] = ask
            strikes[strike]["put_volume"] = volume
            strikes[strike]["put_open_interest"] = oi
            g = opt.get("greeks") or {}
            if g:
                iv_val = float(g.get("mid_iv") or g.get("smv_vol") or 0)
                strikes[strike]["put_greeks"] = {
                    "iv":    iv_val,
                    "delta": float(g.get("delta") or 0),
                    "gamma": float(g.get("gamma") or 0),
                    "theta": float(g.get("theta") or 0),
                    "vega":  float(g.get("vega") or 0)
                }

    return strikes


def get_chains(prices):
    # Iterate over each ticker, fetch qualifying expirations, and build chain data
    today = date.today()
    chains = {}

    for ticker, price_data in prices.items():
        stock_price = price_data["mid"]

        try:
            # Fetch all available expiration dates for this ticker
            all_expirations = fetch_expirations(ticker)
        except Exception as e:
            print(f"   ❌ {ticker}: expirations error — {e}")
            continue

        ticker_expirations = []

        for exp_str in all_expirations:
            # Calculate days to expiration; skip anything outside 0-45 DTE
            try:
                exp_date = date.fromisoformat(exp_str)
            except ValueError:
                continue

            dte = (exp_date - today).days
            if not (0 <= dte <= 45):
                continue

            # Fetch the option chain for this expiration
            try:
                options = fetch_chain(ticker, exp_str)
            except Exception as e:
                print(f"   ❌ {ticker} {exp_str}: chain error — {e}")
                continue

            if not options:
                continue

            # Build per-strike records filtered to the ±30% price band
            strikes = build_strikes(options, stock_price)

            if strikes:
                ticker_expirations.append({
                    "expiration_date": exp_str,
                    "dte": dte,
                    "strikes": sorted(strikes.values(), key=lambda x: x["strike"])
                })

        if ticker_expirations:
            chains[ticker] = ticker_expirations
            print(f"   ✅ {ticker}: {len(ticker_expirations)} expirations")
        else:
            print(f"   ❌ {ticker}: no qualifying expirations in 0-45 DTE window")

    return chains


def save_chains(chains, prices):
    # Persist chain data to data/chains.json for downstream steps
    total_exp = sum(len(exps) for exps in chains.values())
    total_strikes = sum(len(exp["strikes"]) for exps in chains.values() for exp in exps)

    output = {
        "timestamp": datetime.now().isoformat(),
        "requested": len(prices),
        "success": len(chains),
        "total_expirations": total_exp,
        "total_strikes": total_strikes,
        "chains": chains
    }

    with open("data/chains.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n{'='*60}")
    print(f"✅ Chains complete: {len(chains)}/{len(prices)} stocks")
    print(f"   Expirations: {total_exp}")
    print(f"   Strikes:     {total_strikes}")


def main():
    print("=" * 60)
    print("STEP 02: Get Options Chains (Tradier)")
    print("=" * 60)

    # Load ticker prices from previous step
    prices = load_stock_prices()
    print(f"\n📊 Collecting chains for {len(prices)} tickers...")

    # Fetch chains from Tradier
    chains = get_chains(prices)

    # Persist results for downstream steps
    save_chains(chains, prices)

    print("✅ Step 02 complete: chains.json created")


if __name__ == "__main__":
    main()
