"""
Calculate Credit Spreads using Black-Scholes PoP
Professional-grade probability calculations
"""
import json
import sys
import os
import math
from datetime import datetime
from scipy.stats import norm

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PARAMS_FILE = os.path.join(_PROJECT_ROOT, "data", "strategy_params.json")
# Defaults reflect the structural fix: sell ~30-delta so credit is a real
# fraction of width. min_credit_to_width is the single most important gate —
# break-even win rate = 1 - (credit/width), so 0.33 implies a 67% break-even.
# max_width caps catastrophic per-trade loss (the DDOG/AXON wide-spread problem).
_PARAM_DEFAULTS = {
    "min_delta": 0.15,
    "max_delta": 0.45,
    "min_credit": 0.30,           # absolute $ floor; real gate is credit/width below
    "min_credit_to_width": 0.33,  # require credit >= 1/3 of width  (positive expectancy)
    "max_width": 5.0,             # hard cap on spread width (per-trade max-loss control)
    "min_pop": 60,                # PoP floor; ~30-delta shorts land ~62-70%
}

def _load_params() -> dict:
    try:
        with open(_PARAMS_FILE) as f:
            p = json.load(f)
        return {**_PARAM_DEFAULTS, **p}
    except Exception:
        return _PARAM_DEFAULTS

def black_scholes_pop(stock_price, strike, dte, iv, is_call, delta=None):
    # Fall back to delta-based PoP if IV is not available
    if iv <= 0 or dte <= 0:
        if delta is not None:
            return (1 - abs(delta)) * 100
        return 0

    T = dte / 365.0
    r = 0.05
    d1 = (math.log(stock_price / strike) + (r + 0.5 * iv**2) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)

    if is_call:
        pop = norm.cdf(-d2) * 100
    else:
        pop = norm.cdf(d2) * 100

    return pop

def calculate_spreads():
    print("="*60)
    print("STEP 5: Calculate Spreads (Black-Scholes)")
    print("="*60)

    params = _load_params()
    MIN_DELTA            = params["min_delta"]
    MAX_DELTA            = params["max_delta"]
    MIN_CREDIT           = params["min_credit"]
    MIN_CREDIT_TO_WIDTH  = params["min_credit_to_width"]
    MAX_WIDTH            = params["max_width"]
    MIN_POP              = params["min_pop"]
    print(f"   Params: delta {MIN_DELTA}–{MAX_DELTA} | min_credit ${MIN_CREDIT:.2f} | "
          f"credit/width ≥ {MIN_CREDIT_TO_WIDTH:.0%} | max_width ${MAX_WIDTH:.0f} | PoP ≥ {MIN_POP}%")

    with open("data/chains_with_greeks.json", "r") as f:
        data = json.load(f)
    chains = data["chains_with_greeks"]
    
    with open("data/stock_prices.json", "r") as f:
        prices = json.load(f)["prices"]
    
    print("\n📊 Building spreads with Black-Scholes PoP...")

    # Audit: check how many strikes have real IV vs zero IV
    total_strikes = sum(
        len(exp["strikes"])
        for exps in chains.values()
        for exp in exps
    )
    zero_iv = sum(
        1
        for exps in chains.values()
        for exp in exps
        for strike in exp["strikes"]
        if strike.get("put_greeks", {}).get("iv", 0) == 0
        and strike.get("call_greeks", {}).get("iv", 0) == 0
    )
    iv_pct = 100 - (zero_iv / total_strikes * 100) if total_strikes else 0
    if iv_pct < 50:
        print(f"⚠️  IV AUDIT: {iv_pct:.1f}% real IV — using delta-based PoP (normal outside market hours)")
    else:
        print(f"✅ IV AUDIT: {iv_pct:.1f}% real IV — Black-Scholes active")

    all_spreads = []

    for ticker, expirations in chains.items():
        if ticker not in prices:
            continue

        stock_price = prices[ticker]["mid"]
        ticker_iv = sum(
            1 for exp in chains[ticker]
            for strike in exp["strikes"]
            if strike.get("put_greeks", {}).get("iv", 0) > 0
        )
        iv_note = "live IV" if ticker_iv > 0 else "delta PoP"
        print(f"\n{ticker}: ${stock_price:.2f} [{iv_note}]")
        
        for exp_data in expirations:
            dte = exp_data["dte"]
            
            if dte < 21 or dte > 45:
                continue  # Only trade spreads with 21-45 DTE
            
            strikes = exp_data["strikes"]
            
            # Bull Put Spreads
            for i in range(len(strikes)):
                for j in range(i):
                    short_strike = strikes[i]
                    long_strike = strikes[j]
                    
                    if "put_greeks" not in short_strike or "put_greeks" not in long_strike:
                        continue
                    
                    short_iv = short_strike["put_greeks"]["iv"]
                    short_delta = abs(short_strike["put_greeks"]["delta"])

                    if short_delta < MIN_DELTA or short_delta > MAX_DELTA:
                        continue

                    short_bid = short_strike.get("put_bid", 0)
                    long_ask = long_strike.get("put_ask", 0)

                    if short_bid <= 0 or long_ask <= 0:
                        continue

                    net_credit = short_bid - long_ask
                    width = short_strike["strike"] - long_strike["strike"]

                    if net_credit <= 0 or width <= 0:
                        continue

                    if width > MAX_WIDTH:
                        continue

                    credit_pct = net_credit / width  # stored for display/ranking
                    if credit_pct < MIN_CREDIT_TO_WIDTH:
                        continue  # structural EV gate: break-even win rate = 1 - credit/width
                    if net_credit < MIN_CREDIT:
                        continue

                    max_loss = width - net_credit
                    roi = (net_credit / max_loss) * 100

                    pop = black_scholes_pop(
                        stock_price,
                        short_strike["strike"],
                        dte,
                        short_iv,
                        is_call=False,
                        delta=short_strike["put_greeks"]["delta"]
                    )

                    if roi >= 5 and roi <= 150 and pop >= MIN_POP:
                        spread = {
                            "ticker": ticker,
                            "type": "Bull Put",
                            "stock_price": round(stock_price, 2),
                            "short_strike": short_strike["strike"],
                            "long_strike": long_strike["strike"],
                            "width": round(width, 2),
                            "net_credit": round(net_credit, 2),
                            "credit_pct": round(credit_pct * 100, 1),
                            "max_loss": round(max_loss, 2),
                            "roi": round(roi, 1),
                            "pop": round(pop, 1),
                            "short_iv": round(short_iv * 100, 1),
                            "short_delta": round(short_delta, 2),
                            "expiration": {"date": exp_data["expiration_date"], "dte": dte}
                        }
                        all_spreads.append(spread)
            
            # Bear Call Spreads
            for i in range(len(strikes)):
                for j in range(i + 1, len(strikes)):
                    short_strike = strikes[i]
                    long_strike = strikes[j]
                    
                    if "call_greeks" not in short_strike or "call_greeks" not in long_strike:
                        continue
                    
                    short_iv = short_strike["call_greeks"]["iv"]
                    short_delta = abs(short_strike["call_greeks"]["delta"])

                    if short_delta < MIN_DELTA or short_delta > MAX_DELTA:
                        continue

                    short_bid = short_strike.get("call_bid", 0)
                    long_ask = long_strike.get("call_ask", 0)

                    if short_bid <= 0 or long_ask <= 0:
                        continue

                    net_credit = short_bid - long_ask
                    width = long_strike["strike"] - short_strike["strike"]

                    if net_credit <= 0 or width <= 0:
                        continue

                    if width > MAX_WIDTH:
                        continue

                    credit_pct = net_credit / width  # stored for display/ranking
                    if credit_pct < MIN_CREDIT_TO_WIDTH:
                        continue  # structural EV gate: break-even win rate = 1 - credit/width
                    if net_credit < MIN_CREDIT:
                        continue

                    max_loss = width - net_credit
                    roi = (net_credit / max_loss) * 100

                    pop = black_scholes_pop(
                        stock_price,
                        short_strike["strike"],
                        dte,
                        short_iv,
                        is_call=True,
                        delta=short_strike["call_greeks"]["delta"]
                    )

                    if roi >= 5 and roi <= 150 and pop >= MIN_POP:
                        spread = {
                            "ticker": ticker,
                            "type": "Bear Call",
                            "stock_price": round(stock_price, 2),
                            "short_strike": short_strike["strike"],
                            "long_strike": long_strike["strike"],
                            "width": round(width, 2),
                            "net_credit": round(net_credit, 2),
                            "credit_pct": round(credit_pct * 100, 1),
                            "max_loss": round(max_loss, 2),
                            "roi": round(roi, 1),
                            "pop": round(pop, 1),
                            "short_iv": round(short_iv * 100, 1),
                            "short_delta": round(short_delta, 2),
                            "expiration": {"date": exp_data["expiration_date"], "dte": dte}
                        }
                        all_spreads.append(spread)
        
        ticker_spreads = len([s for s in all_spreads if s["ticker"] == ticker])
        print(f"   ✅ {ticker_spreads} quality spreads")
    
    output = {
        "timestamp": datetime.now().isoformat(),
        "total_spreads": len(all_spreads),
        "spreads": all_spreads
    }
    
    with open("data/spreads.json", "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"\n✅ Total spreads: {len(all_spreads)}")
    print(f"   Bull Puts: {len([s for s in all_spreads if s['type'] == 'Bull Put'])}")
    print(f"   Bear Calls: {len([s for s in all_spreads if s['type'] == 'Bear Call'])}")

if __name__ == "__main__":
    calculate_spreads()
