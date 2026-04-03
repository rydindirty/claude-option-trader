"""
Step 01C: Peer Group Z-Scoring
Classifies each stock's option premium and price momentum relative to
its sector peers, producing a peer_multiplier that step 06 applies
alongside the macro regime and technical multipliers.

Two metrics are z-scored within sector peer groups:

  IV z-score     — Stock's ATM IV relative to sector peers.
                   Elevated IV → richer premium → score boost (both types).
                   Depressed IV → thin premium → score penalty.
                   Drives the peer_multiplier in step 06.

  Return z-score — 20-day price return relative to sector peers.
                   Informational: injected into Claude prompt per trade.
                   Flags sector leaders (caution for Bear Calls) and
                   laggards (caution for Bull Puts).

Peer grouping strategy (in preference order):
  1. Finnhub /stock/profile2 sector — group all 22 stocks by industry
  2. Finnhub /stock/peers overlap   — for isolated tickers (<2 peers in
     universe), check if any Finnhub peers appear in our universe
  3. Neutral z-score (0.0)          — if still isolated after both steps

Peer multiplier thresholds (IV z-score → step 06 multiplier):
  z > 1.5   → ×1.10  (premium notably elevated vs sector)
  z > 0.5   → ×1.05  (slightly elevated)
  z ∈ ±0.5  → ×1.00  (in line with peers)
  z < -0.5  → ×0.95  (slightly depressed)
  z < -1.5  → ×0.90  (notably cheap vs sector)

Inputs:
  data/stocks.json             — ticker list (22 stocks)
  data/stock_prices.json       — current prices (for ATM strike selection)
  data/chains_with_greeks.json — options chains (ATM IV extraction)
  data/technicals.json         — 20-day returns (added in step 01B)

Finnhub calls (rate-limited, ~30/min):
  /stock/profile2 × 22         — sector classification
  /stock/peers    × up to 22   — peer cross-reference for singletons

Output: data/peer_zscores.json
  Consumed by:
    06_rank_spreads_tradier.py  — peer_multiplier on spread scores
    08_claude_analysis.py       — per-trade sector + z-score context

Falls back to neutral multipliers (×1.00) if Finnhub key is missing
or all API calls fail.
"""
import json
import os
import sys
import time
import statistics
from datetime import datetime

import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import FINNHUB_API_KEY

# Finnhub free tier: 60 calls/min. 2.1s gap → ~28/min, safely under limit.
_CALL_DELAY = 2.1
_FINNHUB_BASE = "https://finnhub.io/api/v1"

# ── Sector normalization ─────────────────────────────────────────────────────
# Finnhub returns granular industry names. With only ~22 tickers many end up
# as singletons. Map to broader GICS-style sectors so more tickers share a
# peer group and get meaningful z-scores.
_SECTOR_MAP = {
    # Information Technology
    "Technology":            "Information Technology",
    "Semiconductors":        "Information Technology",
    "Communications":        "Information Technology",   # networking gear (ANET)
    "Electrical Equipment":  "Information Technology",   # fiber/optics (GLW)
    "IT Services":           "Information Technology",
    "Software":              "Information Technology",
    # Communication Services
    "Media":                 "Communication Services",
    "Interactive Media":     "Communication Services",
    "Entertainment":         "Communication Services",
    "Telecommunication":     "Communication Services",
    # Financials
    "Financial Services":    "Financials",
    "Banking":               "Financials",
    "Capital Markets":       "Financials",
    "Insurance":             "Financials",
    "Consumer Finance":      "Financials",
    # Consumer Discretionary
    "Hotels, Restaurants":   "Consumer Discretionary",
    "Retail":                "Consumer Discretionary",
    "Consumer products":     "Consumer Discretionary",   # homebuilders (DHI)
    "Automobiles":           "Consumer Discretionary",
    "Homebuilding":          "Consumer Discretionary",
    "Internet & Direct":     "Consumer Discretionary",
    # Consumer Staples
    "Food":                  "Consumer Staples",
    "Beverages":             "Consumer Staples",
    "Tobacco":               "Consumer Staples",
    "Household Products":    "Consumer Staples",
    # Energy
    "Energy":                "Energy",
    "Oil":                   "Energy",
    # Materials
    "Metals & Mining":       "Materials",
    "Chemicals":             "Materials",
    "Construction Materials":"Materials",
    # Industrials
    "Aerospace":             "Industrials",
    "Machinery":             "Industrials",
    "Air Freight":           "Industrials",
    # Health Care
    "Pharmaceuticals":       "Health Care",
    "Biotechnology":         "Health Care",
    "Health Care":           "Health Care",
    # Utilities
    "Utilities":             "Utilities",
    "Electric":              "Utilities",
    # Real Estate
    "Real Estate":           "Real Estate",
    "REIT":                  "Real Estate",
}


def normalize_sector(raw: str) -> str:
    """Map a Finnhub industry string to a broader GICS-style sector name."""
    if not raw or raw == "Unknown":
        return "Unknown"
    for keyword, gics in _SECTOR_MAP.items():
        if keyword.lower() in raw.lower():
            return gics
    return raw   # keep original if no mapping matches


# ── Finnhub helpers ─────────────────────────────────────────────────────────

def _fh_get(endpoint, params=None):
    """Single Finnhub REST call. Returns parsed JSON or None on error."""
    p = {"token": FINNHUB_API_KEY}
    if params:
        p.update(params)
    try:
        r = requests.get(f"{_FINNHUB_BASE}{endpoint}", params=p, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"   ⚠️  Finnhub {endpoint} error: {e}")
        return None


def fetch_sector(ticker):
    """Return a normalized GICS-style sector for a ticker, or 'Unknown'."""
    time.sleep(_CALL_DELAY)
    data = _fh_get("/stock/profile2", {"symbol": ticker})
    raw = (data or {}).get("finnhubIndustry") or "Unknown"
    return normalize_sector(raw)


def fetch_peers(ticker):
    """Return list of peer tickers from Finnhub, or []."""
    time.sleep(_CALL_DELAY)
    data = _fh_get("/stock/peers", {"symbol": ticker})
    if isinstance(data, list):
        return [p for p in data if isinstance(p, str) and p != ticker]
    return []


# ── ATM IV extraction ────────────────────────────────────────────────────────

def get_atm_iv(ticker, chains, current_price):
    """
    Extract ATM implied volatility (as %) from chains_with_greeks.
    Prefers expirations 21-45 DTE, then nearest to 30 DTE.
    Uses the strike closest to current price; averages put and call IV.
    Returns IV as a percentage (e.g. 32.5), or None if unavailable.
    """
    exps = chains.get(ticker, [])
    if not exps:
        return None

    valid = [e for e in exps if 21 <= e.get("dte", 0) <= 45]
    pool  = valid if valid else exps
    exp   = min(pool, key=lambda e: abs(e.get("dte", 0) - 30))

    strikes = exp.get("strikes", [])
    if not strikes:
        return None

    best = min(strikes, key=lambda s: abs(s.get("strike", 0) - current_price))

    ivs = []
    pg = best.get("put_greeks",  {})
    cg = best.get("call_greeks", {})
    if pg.get("iv", 0) > 0:
        ivs.append(pg["iv"] * 100)   # stored as decimal, convert to %
    if cg.get("iv", 0) > 0:
        ivs.append(cg["iv"] * 100)

    return round(sum(ivs) / len(ivs), 2) if ivs else None


# ── Z-score math ─────────────────────────────────────────────────────────────

def zscore_group(ticker_values):
    """
    {ticker: value_or_None} → {ticker: z_score}.
    Tickers with None, or groups with < 2 valid values, receive z = 0.0.
    """
    valid = {t: v for t, v in ticker_values.items() if v is not None}
    result = {t: 0.0 for t in ticker_values}

    if len(valid) < 2:
        return result

    vals = list(valid.values())
    mean = statistics.mean(vals)
    std  = statistics.stdev(vals)

    if std == 0:
        return result

    for t, v in valid.items():
        result[t] = round((v - mean) / std, 3)

    return result


def iv_z_to_multiplier(z):
    """Convert IV z-score to a peer_multiplier for step 06 scoring."""
    if   z >  1.5: return 1.10
    elif z >  0.5: return 1.05
    elif z < -1.5: return 0.90
    elif z < -0.5: return 0.95
    else:          return 1.00


# ── Neutral output ────────────────────────────────────────────────────────────

def write_neutral(tickers, note=""):
    output = {
        "timestamp":    datetime.now().isoformat(),
        "ticker_count": len(tickers),
        "sector_groups": {},
        "peer_zscores": {
            t: {
                "sector":            "Unknown",
                "peers_in_universe": [],
                "atm_iv":            None,
                "iv_zscore":         0.0,
                "price_return_20d":  None,
                "return_zscore":     0.0,
                "peer_multiplier":   1.0
            }
            for t in tickers
        },
        "fallback_note": note
    }
    with open("data/peer_zscores.json", "w") as f:
        json.dump(output, f, indent=2)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("STEP 01C: Peer Group Z-Scoring")
    print("=" * 60)

    # Load ticker list
    try:
        with open("data/stocks.json", "r") as f:
            raw = json.load(f)
        tickers = raw.get("tickers", raw) if isinstance(raw, dict) else raw
    except FileNotFoundError:
        print("❌ data/stocks.json not found — run step 00E first")
        sys.exit(1)

    if not FINNHUB_API_KEY:
        print("\n⚠️  FINNHUB_API_KEY not set — defaulting to neutral peer scores")
        write_neutral(tickers, "FINNHUB_API_KEY missing")
        print("\n✅ Step 01C complete: peer_zscores.json (neutral fallback)")
        return

    # Load prices (for ATM strike selection)
    try:
        with open("data/stock_prices.json", "r") as f:
            prices = json.load(f).get("prices", {})
    except FileNotFoundError:
        prices = {}

    # Load chains (for ATM IV extraction)
    try:
        with open("data/chains_with_greeks.json", "r") as f:
            chains = json.load(f).get("chains_with_greeks", {})
    except FileNotFoundError:
        print("⚠️  chains_with_greeks.json not found — IV z-scores unavailable")
        chains = {}

    # Load technicals (for 20-day returns)
    try:
        with open("data/technicals.json", "r") as f:
            tech = json.load(f).get("technicals", {})
    except FileNotFoundError:
        print("⚠️  technicals.json not found — return z-scores unavailable")
        tech = {}

    # ── Extract per-ticker raw metrics ──────────────────────────────────────
    atm_ivs = {}
    ret_20d = {}
    for ticker in tickers:
        price = prices.get(ticker, {}).get("mid")
        atm_ivs[ticker] = get_atm_iv(ticker, chains, price) if price else None
        ret_20d[ticker] = tech.get(ticker, {}).get("price_return_20d")

    # ── Fetch sectors from Finnhub ───────────────────────────────────────────
    print(f"\n🏢 Fetching Finnhub sectors ({len(tickers)} calls)...")
    sectors = {}
    for ticker in tickers:
        sector = fetch_sector(ticker)
        sectors[ticker] = sector
        iv_str = f"IV {atm_ivs[ticker]:.1f}%" if atm_ivs[ticker] else "IV n/a"
        print(f"   {ticker:<6} [{sector}]  {iv_str}")

    # ── Build sector peer groups ─────────────────────────────────────────────
    sector_groups: dict[str, list] = {}
    for ticker, sector in sectors.items():
        sector_groups.setdefault(sector, []).append(ticker)

    # For singletons, try Finnhub peers to find universe overlap
    universe_set = set(tickers)
    peers_fetched: dict[str, list] = {}

    singletons = [t for t in tickers if len(sector_groups.get(sectors[t], [])) < 2]
    if singletons:
        print(f"\n🔍 Fetching peers for {len(singletons)} isolated ticker(s)...")
        for ticker in singletons:
            peer_list = fetch_peers(ticker)
            peers_fetched[ticker] = peer_list
            overlap = [p for p in peer_list if p in universe_set]
            if overlap:
                sector = sectors[ticker]
                for p in overlap:
                    if p not in sector_groups[sector]:
                        sector_groups[sector].append(p)
                print(f"   {ticker}: found universe peers {overlap}")
            else:
                print(f"   {ticker}: no universe overlap in peers — neutral z-score")

    print(f"\n📊 Sector groups:")
    for sector, group in sorted(sector_groups.items()):
        print(f"   {sector:<35} {group}")

    # ── Z-score within each sector group ────────────────────────────────────
    print(f"\n📐 Computing z-scores...")
    iv_zscores  = {t: 0.0 for t in tickers}
    ret_zscores = {t: 0.0 for t in tickers}

    for sector, group in sector_groups.items():
        if len(group) < 2:
            continue

        z_iv  = zscore_group({t: atm_ivs.get(t)  for t in group})
        z_ret = zscore_group({t: ret_20d.get(t)   for t in group})

        for t in group:
            if t in tickers:      # only update our 22 stocks
                iv_zscores[t]  = z_iv.get(t, 0.0)
                ret_zscores[t] = z_ret.get(t, 0.0)

    # ── Build output ─────────────────────────────────────────────────────────
    print(f"\n🔢 Peer multipliers:")
    results = {}
    for ticker in tickers:
        iv_z  = iv_zscores[ticker]
        ret_z = ret_zscores[ticker]
        mult  = iv_z_to_multiplier(iv_z)
        sector = sectors.get(ticker, "Unknown")
        peers  = [t for t in sector_groups.get(sector, []) if t != ticker]

        iv_str  = f"{atm_ivs[ticker]:.1f}%" if atm_ivs[ticker] else "n/a"
        ret_str = (f"{ret_20d[ticker]:+.1f}%" if ret_20d[ticker] is not None
                   else "n/a")
        print(f"   {ticker:<6}  IV:{iv_str} z={iv_z:+.2f}  "
              f"ret:{ret_str} z={ret_z:+.2f}  mult=×{mult:.2f}  "
              f"peers={peers}")

        results[ticker] = {
            "sector":            sector,
            "peers_in_universe": peers,
            "atm_iv":            atm_ivs[ticker],
            "iv_zscore":         iv_z,
            "price_return_20d":  ret_20d[ticker],
            "return_zscore":     ret_z,
            "peer_multiplier":   mult
        }

    output = {
        "timestamp":    datetime.now().isoformat(),
        "ticker_count": len(tickers),
        "sector_groups": sector_groups,
        "peer_zscores": results
    }

    with open("data/peer_zscores.json", "w") as f:
        json.dump(output, f, indent=2)

    print("\n✅ Step 01C complete: peer_zscores.json")


if __name__ == "__main__":
    main()
