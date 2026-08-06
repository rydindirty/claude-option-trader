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
#
# max_width was a FLAT dollar cap for every ticker regardless of price (2026-07
# audit: on a normal day 20/21 candidates produced zero spreads because a $5
# width forces >=$1.65 credit at 35-45 DTE/15-45 delta — only a handful of
# high-price, high-IV megacaps can generate that much premium in a fixed $5
# window). max_width_pct scales the effective cap to each ticker's own price
# so cheaper/lower-vol names get a proportionally reachable width+credit
# target instead of being structurally excluded; max_width remains the
# absolute ceiling (so expensive names don't get an oversized width), and
# paper_auto_trader's max_risk_pct gate still blocks any trade too large for
# the account regardless of how wide effective_max_width computes.
_PARAM_DEFAULTS = {
    "min_delta": 0.15,
    "max_delta": 0.45,
    "min_credit": 0.30,           # absolute $ floor; real gate is credit/width below
    "min_credit_to_width": 0.33,  # require credit >= 1/3 of width  (positive expectancy)
    "max_width": 5.0,             # absolute ceiling on spread width regardless of price
    "max_width_pct": 0.03,        # width also scales with price: effective cap =
                                  #   clamp(price * max_width_pct, min_width, max_width).
                                  #   Live-tuned 2026-08-06: below ~2.5% too few strikes
                                  #   fit inside the width to ever clear credit/width;
                                  #   above ~6% most $80+ names just hit the $5 ceiling
                                  #   anyway (no different from the flat cap this fixes).
                                  #   3% keeps names up to ~$167 meaningfully below the
                                  #   ceiling. NOTE: on a thin-IV day even this can still
                                  #   leave only 1-2 tickers clearing min_pop as well —
                                  #   that floor, not width, is what's binding that day.
    "min_width": 1.0,             # floor so very cheap tickers still get a workable width
    "min_pop": 60,                # PoP floor; ~30-delta shorts land ~62-70%
    "min_dte": 35,                # enter far enough out that theta can work BEFORE the
    "max_dte": 45,                #   monitor's 21-DTE time stop (was 21 → trades entered
                                  #   at ~22 DTE and got time-stopped ~1 day later).
    "enable_iron_condors": True,  # also generate directionally-neutral iron condors
    "ic_min_delta": 0.10,         # per-side short delta band for IC legs (lower than a
    "ic_max_delta": 0.25,         #   single vertical — two premiums clear credit/width)
    "ic_min_pop": 50,             # IC between-shorts PoP floor. Lower than a vertical's:
                                  #   an IC's edge is 2 premiums + 50% mgmt + vol premium,
                                  #   not high PoP. credit/width ≥ 1/3 stays the EV gate.
    "ic_min_leg_credit_pct": 0.12,# EACH IC leg must clear this credit/width. Prevents a
                                  #   lopsided condor (e.g. a 4%-credit throwaway put +
                                  #   a 30%-credit call = a directional bet, not neutral).
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

def effective_max_width(stock_price, params):
    """
    Per-ticker width cap: scales with the underlying's price so a $50 stock
    and a $400 stock aren't held to the same flat dollar width. Clamped
    between min_width (floor, so cheap names still get a usable width) and
    max_width (ceiling, so expensive names don't get an oversized one).
    """
    pct_width = stock_price * params["max_width_pct"]
    return round(min(params["max_width"], max(params["min_width"], pct_width)), 2)

def build_iron_condors(ticker, stock_price, exp_data, params):
    """
    Build at most one iron condor per ticker/expiration: an OTM put credit spread
    plus an OTM call credit spread, short strikes straddling spot at ~IC delta.

    Why ICs: they collect TWO premiums against ONE width of risk, so the combined
    credit/width clears the 1/3 floor at a LOWER per-side delta (higher PoP) than a
    single vertical can — and they are directionally neutral, the structural cure
    for the Bull-Put monoculture. Returned as one "Iron Condor" candidate carrying
    both verticals; the auto-trader places it as two linked rows.
    """
    dte       = exp_data["dte"]
    IC_MIN_D  = params["ic_min_delta"]
    IC_MAX_D  = params["ic_max_delta"]
    MAX_WIDTH = effective_max_width(stock_price, params)
    MIN_CW    = params["min_credit_to_width"]
    IC_MIN_POP = params["ic_min_pop"]
    IC_MIN_LEG_CW = params["ic_min_leg_credit_pct"]

    strikes = sorted(exp_data["strikes"], key=lambda s: s["strike"])

    def vertical_candidates(is_call):
        """All valid OTM credit verticals with short delta in the IC band."""
        gk           = "call_greeks" if is_call else "put_greeks"
        bid_k, ask_k = ("call_bid", "call_ask") if is_call else ("put_bid", "put_ask")
        out = []
        for idx, ss in enumerate(strikes):
            if gk not in ss:
                continue
            if is_call and ss["strike"] <= stock_price:
                continue
            if (not is_call) and ss["strike"] >= stock_price:
                continue
            sd = abs(ss[gk]["delta"])
            if not (IC_MIN_D <= sd <= IC_MAX_D):
                continue
            if ss.get(bid_k, 0) <= 0:
                continue
            # long leg: widest strike within MAX_WIDTH on the far-OTM side
            long_leg = None
            scan = strikes[idx + 1:] if is_call else list(reversed(strikes[:idx]))
            for ls in scan:
                w = (ls["strike"] - ss["strike"]) if is_call else (ss["strike"] - ls["strike"])
                if w <= 0:
                    continue
                if w > MAX_WIDTH:
                    break
                if ls.get(ask_k, 0) > 0:
                    long_leg = (ls, w)
            if not long_leg:
                continue
            ls, w = long_leg
            credit = ss.get(bid_k, 0) - ls.get(ask_k, 0)
            if credit <= 0:
                continue
            pop = black_scholes_pop(stock_price, ss["strike"], dte,
                                    ss[gk]["iv"], is_call, ss[gk]["delta"])
            out.append({"short": ss, "long": ls, "width": w,
                        "credit": credit, "delta": sd, "pop": pop})
        return out

    puts, calls = vertical_candidates(False), vertical_candidates(True)
    if not puts or not calls:
        return []

    # Search put × call pairs; keep the pair with the HIGHEST PoP (most conservative)
    # that still clears the credit/width EV floor. This naturally selects the
    # lowest-delta IC that is structurally sound, rather than a fixed target delta.
    best = None
    for pv in puts:
        for cv in calls:
            if not (pv["short"]["strike"] < stock_price < cv["short"]["strike"]):
                continue
            width        = max(pv["width"], cv["width"])
            # Each leg must pull its weight — no lopsided (secretly directional) condor
            if (pv["credit"] / pv["width"] < IC_MIN_LEG_CW or
                    cv["credit"] / cv["width"] < IC_MIN_LEG_CW):
                continue
            total_credit = pv["credit"] + cv["credit"]
            credit_pct   = total_credit / width
            if credit_pct < MIN_CW:
                continue
            ic_pop = pv["pop"] + cv["pop"] - 100.0   # P(finish between the shorts)
            if ic_pop < IC_MIN_POP:
                continue
            if best is None or ic_pop > best["ic_pop"]:
                best = {"pv": pv, "cv": cv, "width": width,
                        "total_credit": total_credit, "credit_pct": credit_pct,
                        "ic_pop": ic_pop}
    if best is None:
        return []

    pv, cv       = best["pv"], best["cv"]
    ps, pl, pw, pcredit, pdelta = pv["short"], pv["long"], pv["width"], pv["credit"], pv["delta"]
    cs, cl, cw, ccredit, cdelta = cv["short"], cv["long"], cv["width"], cv["credit"], cv["delta"]
    width        = best["width"]
    total_credit = best["total_credit"]
    credit_pct   = best["credit_pct"]
    ic_pop       = best["ic_pop"]
    max_loss     = width - total_credit
    if max_loss <= 0:
        return []
    roi = (total_credit / max_loss) * 100

    return [{
        "ticker": ticker,
        "type": "Iron Condor",
        "stock_price": round(stock_price, 2),
        # generic fields (put side) so steps 06/07 work without IC awareness
        "short_strike": ps["strike"],
        "long_strike": pl["strike"],
        "width": round(width, 2),
        "net_credit": round(total_credit, 2),
        "credit_pct": round(credit_pct * 100, 1),
        "max_loss": round(max_loss, 2),
        "roi": round(roi, 1),
        "pop": round(ic_pop, 1),
        "short_iv": round(ps["put_greeks"]["iv"] * 100, 1),
        "short_delta": round((pdelta + cdelta) / 2, 2),
        "expiration": {"date": exp_data["expiration_date"], "dte": dte},
        # IC-specific legs (consumed by step 07 + paper_auto_trader)
        "ic": {
            "short_put": ps["strike"],  "long_put": pl["strike"],
            "short_call": cs["strike"], "long_call": cl["strike"],
            "put_credit": round(pcredit, 2), "call_credit": round(ccredit, 2),
            "put_width": round(pw, 2),       "call_width": round(cw, 2),
            "put_delta": round(pdelta, 2),   "call_delta": round(cdelta, 2),
        },
    }]


def calculate_spreads():
    print("="*60)
    print("STEP 5: Calculate Spreads (Black-Scholes)")
    print("="*60)

    params = _load_params()
    MIN_DELTA            = params["min_delta"]
    MAX_DELTA            = params["max_delta"]
    MIN_CREDIT           = params["min_credit"]
    MIN_CREDIT_TO_WIDTH  = params["min_credit_to_width"]
    MIN_POP              = params["min_pop"]
    MIN_DTE              = params["min_dte"]
    MAX_DTE              = params["max_dte"]
    ENABLE_IC            = params["enable_iron_condors"]
    print(f"   Params: delta {MIN_DELTA}–{MAX_DELTA} | min_credit ${MIN_CREDIT:.2f} | "
          f"credit/width ≥ {MIN_CREDIT_TO_WIDTH:.0%} | max_width "
          f"${params['min_width']:.0f}-${params['max_width']:.0f} "
          f"(scaled {params['max_width_pct']:.1%} of price) | PoP ≥ {MIN_POP}%")
    print(f"   Iron condors: {'ON' if ENABLE_IC else 'off'} "
          f"(per-side delta {params['ic_min_delta']}–{params['ic_max_delta']})")

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
        MAX_WIDTH = effective_max_width(stock_price, params)
        print(f"\n{ticker}: ${stock_price:.2f} [{iv_note}] (max_width ${MAX_WIDTH:.2f})")

        for exp_data in expirations:
            dte = exp_data["dte"]
            
            if dte < MIN_DTE or dte > MAX_DTE:
                continue  # runway before the 21-DTE time stop (default 35-45 DTE)
            
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

            # Iron Condors — one neutral candidate per ticker/expiration
            if ENABLE_IC:
                all_spreads.extend(build_iron_condors(ticker, stock_price, exp_data, params))

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
