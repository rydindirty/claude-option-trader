"""
Step 00H: Macro Regime Classification
Fetches key macro indicators from FRED and classifies the current
economic regime as one of five states:

  Goldilocks   — Low VIX, growing GDP, benign inflation, normal yield curve
                 → Bull Puts preferred | relaxed entry thresholds
  Neutral      — Mixed signals, no strong directional bias
                 → Both types equal | standard thresholds
  Slowing      — Growth slowing, yield curve flattening or inverting
                 → Bear Calls preferred | tighter thresholds
  Contraction  — Negative/near-zero GDP, high VIX, inverted yield curve
                 → Bear Calls strongly preferred | require high PoP
  Stagflation  — Slowing/contracting growth AND hot inflation
                 → Bear Calls strongly preferred | tightest thresholds

FRED indicators used:
  VIXCLS            — CBOE Volatility Index (daily, ~1 day lag)
  T10Y2Y            — 10Y minus 2Y Treasury spread (daily, ~1 day lag)
  CPIAUCSL          — CPI, compute YoY from 13 months (monthly, ~1-2 mo lag)
  A191RL1Q225SBEA   — Real GDP % change, annualized (quarterly, ~1 qtr lag)
  UNRATE            — Unemployment rate (informational, not scored)

Output: data/macro_regime.json
  Consumed by:
    06_rank_spreads_tradier.py  — score multipliers + entry thresholds
    08_claude_analysis.py       — macro context injected into Claude prompt

Cache: data/fred_cache.json — last successful fetch per series.
  On FRED API failure (500, timeout, etc.) uses cached values instead of
  dropping to zero indicators. Falls back to Neutral only if both live
  fetch and cache are unavailable.
"""
import os
import sys
import json
import requests
from datetime import datetime, date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import FRED_API_KEY

FRED_BASE       = "https://api.stlouisfed.org/fred/series/observations"
FRED_CACHE_PATH = "data/fred_cache.json"


def _load_fred_cache():
    try:
        with open(FRED_CACHE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_fred_cache_entry(series_id, observations):
    cache = _load_fred_cache()
    cache[series_id] = {
        "cached_at":    datetime.now().isoformat(),
        "observations": observations,
    }
    with open(FRED_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)

# ── Regime configuration ────────────────────────────────────────────────────
# Each entry defines score multipliers applied to Bull Put / Bear Call scores
# in step 06, plus the adjusted ENTER / WATCH decision thresholds.
REGIME_CONFIG = {
    "goldilocks": {
        "label":                "Goldilocks",
        "preferred_type":       "Bull Put",
        "bull_put_multiplier":  1.15,
        "bear_call_multiplier": 0.90,
        "enter_pop":            68,
        "enter_roi":            18,
        "watch_pop":            68,   # PoP floor is 68%; WATCH catches ROI 13–17%
        "watch_roi":            13,
        "note": (
            "Goldilocks regime — low volatility, steady growth, benign inflation. "
            "Theta and upward drift favor Bull Puts."
        )
    },
    "neutral": {
        "label":                "Neutral",
        "preferred_type":       None,
        "bull_put_multiplier":  1.0,
        "bear_call_multiplier": 1.0,
        "enter_pop":            68,
        "enter_roi":            20,
        "watch_pop":            68,   # PoP floor is 68%; WATCH catches ROI 15–19%
        "watch_roi":            15,
        "note": (
            "Neutral regime — balanced macro signals. "
            "No spread-type preference; standard thresholds apply."
        )
    },
    "slowing": {
        "label":                "Slowing",
        "preferred_type":       "Bear Call",
        "bull_put_multiplier":  0.90,
        "bear_call_multiplier": 1.10,
        "enter_pop":            72,
        "enter_roi":            22,
        "watch_pop":            68,   # PoP floor is 68%; WATCH catches ROI 17–21%
        "watch_roi":            17,
        "note": (
            "Slowing growth regime — rising downside risk. "
            "Favor Bear Calls; tighten entry thresholds."
        )
    },
    "contraction": {
        "label":                "Contraction",
        "preferred_type":       "Bear Call",
        "bull_put_multiplier":  0.80,
        "bear_call_multiplier": 1.20,
        "enter_pop":            75,
        "enter_roi":            25,
        "watch_pop":            68,   # PoP floor is 68%; WATCH catches 68–74% PoP or ROI 20–24%
        "watch_roi":            20,
        "note": (
            "Contraction regime — elevated downside risk, high VIX. "
            "Strongly prefer Bear Calls; require high PoP."
        )
    },
    "stagflation": {
        "label":                "Stagflation",
        "preferred_type":       "Bear Call",
        "bull_put_multiplier":  0.80,
        "bear_call_multiplier": 1.20,
        "enter_pop":            75,
        "enter_roi":            25,
        "watch_pop":            68,   # PoP floor is 68%; WATCH catches 68–74% PoP or ROI 20–24%
        "watch_roi":            20,
        "note": (
            "Stagflation regime — slowing/contracting growth AND hot inflation. "
            "Most adverse for equities; strongly prefer Bear Calls."
        )
    }
}


def fetch_fred(series_id, limit=13):
    """
    Fetch the N most recent observations for a FRED series.
    On success, writes results to fred_cache.json.
    On failure, returns last-known cached data rather than None.
    Returns a list of {date, value} dicts (most recent first), or None if
    both the API and cache are unavailable.
    """
    try:
        r = requests.get(
            FRED_BASE,
            params={
                "series_id":       series_id,
                "api_key":         FRED_API_KEY,
                "file_type":       "json",
                "sort_order":      "desc",
                "limit":           limit,
                "observation_end": date.today().isoformat()
            },
            timeout=10
        )
        r.raise_for_status()
        obs = [o for o in r.json().get("observations", [])
               if o.get("value") not in (".", "", None)]
        _save_fred_cache_entry(series_id, obs)
        return obs
    except Exception as e:
        print(f"   ⚠️  FRED fetch failed ({series_id}): {e}")
        cache = _load_fred_cache()
        if series_id in cache:
            cached_at = cache[series_id].get("cached_at", "unknown")
            print(f"   ↩️  Using cached {series_id} data from {cached_at}")
            return cache[series_id]["observations"]
        return None


def latest_value(obs):
    """Return (float_value, date_string) from the most recent valid observation."""
    if obs:
        return float(obs[0]["value"]), obs[0]["date"]
    return None, None


def classify_regime(indicators):
    """
    Score each available indicator and derive the regime label.

    Scoring rubric:
      VIX:         < 15 → +2 | 15-20 → +1 | 20-25 → 0 | > 25 → -2
      Yield curve: > 1.0 → +2 | 0.5-1.0 → +1 | 0 to 0.5 → 0 | < 0 → -2
      CPI YoY:     < 2.5 → +1 | 2.5-4.0 → 0 | > 4.0 → -1
      GDP growth:  > 2.5 → +2 | 1.0-2.5 → +1 | 0-1.0 → 0 | < 0 → -2

    Stagflation override: slowing/contraction regime + CPI YoY ≥ 4.0
    """
    score = 0
    breakdown = {}

    vix = indicators.get("vix", {}).get("value")
    if vix is not None:
        if vix < 15:
            pts, status = 2, "low"
        elif vix < 20:
            pts, status = 1, "moderate"
        elif vix < 25:
            pts, status = 0, "elevated"
        else:
            pts, status = -2, "high"
        score += pts
        breakdown["vix"] = pts
        indicators["vix"]["status"] = status

    t10y2y = indicators.get("yield_curve", {}).get("value")
    if t10y2y is not None:
        if t10y2y > 1.0:
            pts, status = 2, "steep"
        elif t10y2y > 0.5:
            pts, status = 1, "normal"
        elif t10y2y > 0:
            pts, status = 0, "flat"
        else:
            pts, status = -2, "inverted"
        score += pts
        breakdown["yield_curve"] = pts
        indicators["yield_curve"]["status"] = status

    cpi_yoy = indicators.get("cpi_yoy", {}).get("value")
    if cpi_yoy is not None:
        if cpi_yoy < 2.5:
            pts, status = 1, "benign"
        elif cpi_yoy < 4.0:
            pts, status = 0, "moderate"
        else:
            pts, status = -1, "hot"
        score += pts
        breakdown["cpi_yoy"] = pts
        indicators["cpi_yoy"]["status"] = status

    gdp = indicators.get("gdp_growth", {}).get("value")
    if gdp is not None:
        if gdp > 2.5:
            pts, status = 2, "strong"
        elif gdp > 1.0:
            pts, status = 1, "moderate"
        elif gdp > 0:
            pts, status = 0, "slow"
        else:
            pts, status = -2, "contraction"
        score += pts
        breakdown["gdp_growth"] = pts
        indicators["gdp_growth"]["status"] = status

    # Base regime from score
    if score >= 4:
        regime = "goldilocks"
    elif score >= 1:
        regime = "neutral"
    elif score >= -1:
        regime = "slowing"
    else:
        regime = "contraction"

    # Stagflation override: slowing or contraction + hot inflation
    if regime in ("slowing", "contraction") and cpi_yoy is not None and cpi_yoy >= 4.0:
        regime = "stagflation"

    return regime, score, breakdown


def build_neutral_output(note="FRED key missing — defaulted to Neutral"):
    """Return a valid macro_regime output using neutral defaults."""
    cfg = REGIME_CONFIG["neutral"]
    return {
        "timestamp":            datetime.now().isoformat(),
        "regime":               "neutral",
        "regime_label":         cfg["label"],
        "score":                0,
        "score_breakdown":      {},
        "preferred_spread_type": cfg["preferred_type"],
        "regime_note":          cfg["note"],
        "indicators":           {},
        "scoring_adjustments": {
            "bull_put_multiplier":  cfg["bull_put_multiplier"],
            "bear_call_multiplier": cfg["bear_call_multiplier"],
            "enter_pop":            cfg["enter_pop"],
            "enter_roi":            cfg["enter_roi"],
            "watch_pop":            cfg["watch_pop"],
            "watch_roi":            cfg["watch_roi"]
        },
        "fallback_note": note
    }


def main():
    print("=" * 60)
    print("STEP 00H: Macro Regime Classification")
    print("=" * 60)

    if not FRED_API_KEY:
        print("\n⚠️  FRED_API_KEY not set — defaulting to Neutral regime")
        print("   Add FRED_API_KEY=<key> to .env to enable macro classification")
        print("   Free key: https://fred.stlouisfed.org/docs/api/api_key.html")
        output = build_neutral_output()
        with open("data/macro_regime.json", "w") as f:
            json.dump(output, f, indent=2)
        print("\n✅ Step 00H complete: macro_regime.json (Neutral fallback)")
        return

    print("\n📡 Fetching FRED indicators...")
    indicators = {}
    any_data = False

    # ── VIX — CBOE Volatility Index (daily) ────────────────────────
    vix_obs = fetch_fred("VIXCLS", limit=5)
    vix_val, vix_date = latest_value(vix_obs)
    if vix_val is not None:
        indicators["vix"] = {"value": round(vix_val, 2), "date": vix_date}
        print(f"   VIX:          {vix_val:>6.2f}  ({vix_date})")
        any_data = True
    else:
        print("   VIX:          unavailable")

    # ── VIX shock detection ─────────────────────────────────────────
    # Hard threshold >30 OR single-day spike >20% → override to Contraction
    vix_shock = False
    vix_shock_reason = None
    if vix_val is not None:
        if vix_val > 30:
            vix_shock = True
            vix_shock_reason = f"VIX crossed hard threshold ({vix_val:.2f} > 30)"
        elif vix_obs and len(vix_obs) >= 2:
            prev_vix = float(vix_obs[1]["value"])
            daily_pct = (vix_val - prev_vix) / prev_vix * 100
            if daily_pct > 20:
                vix_shock = True
                vix_shock_reason = (
                    f"VIX spiked {daily_pct:.1f}% in one day "
                    f"({prev_vix:.2f} → {vix_val:.2f})"
                )
    if vix_shock:
        print(f"\n🚨 VIX SHOCK: {vix_shock_reason}")
        print(f"   → Overriding regime to CONTRACTION; Bull Put entries blocked")

    # ── Yield curve — 10Y minus 2Y Treasury spread (daily) ─────────
    t10y2y_obs = fetch_fred("T10Y2Y", limit=5)
    t10y2y_val, t10y2y_date = latest_value(t10y2y_obs)
    if t10y2y_val is not None:
        indicators["yield_curve"] = {"value": round(t10y2y_val, 3), "date": t10y2y_date}
        print(f"   Yield curve:  {t10y2y_val:>+6.3f}  ({t10y2y_date})")
        any_data = True
    else:
        print("   Yield curve:  unavailable")

    # ── CPI YoY — compute from last 13 monthly readings ────────────
    cpi_obs = fetch_fred("CPIAUCSL", limit=13)
    if cpi_obs and len(cpi_obs) >= 13:
        cpi_latest   = float(cpi_obs[0]["value"])
        cpi_year_ago = float(cpi_obs[12]["value"])
        cpi_yoy      = round((cpi_latest / cpi_year_ago - 1) * 100, 2)
        indicators["cpi_yoy"] = {"value": cpi_yoy, "date": cpi_obs[0]["date"]}
        print(f"   CPI YoY:      {cpi_yoy:>6.2f}%  ({cpi_obs[0]['date']})")
        any_data = True
    else:
        print("   CPI YoY:      unavailable")

    # ── GDP growth — Real GDP % change, annualized SAAR (quarterly) ─
    gdp_obs = fetch_fred("A191RL1Q225SBEA", limit=3)
    gdp_val, gdp_date = latest_value(gdp_obs)
    if gdp_val is not None:
        indicators["gdp_growth"] = {"value": round(gdp_val, 2), "date": gdp_date}
        print(f"   GDP growth:   {gdp_val:>+6.2f}%  ({gdp_date})")
        any_data = True
    else:
        print("   GDP growth:   unavailable")

    # ── Unemployment — informational only, not scored ───────────────
    unrate_obs = fetch_fred("UNRATE", limit=2)
    unrate_val, unrate_date = latest_value(unrate_obs)
    if unrate_val is not None:
        indicators["unemployment"] = {"value": round(unrate_val, 2), "date": unrate_date}
        print(f"   Unemployment: {unrate_val:>6.1f}%  ({unrate_date})")

    # ── Classify regime ─────────────────────────────────────────────
    if not any_data:
        print("\n⚠️  No FRED data retrieved — defaulting to Neutral")
        output = build_neutral_output(note="All FRED fetches failed — defaulted to Neutral")
        with open("data/macro_regime.json", "w") as f:
            json.dump(output, f, indent=2)
        print("\n✅ Step 00H complete: macro_regime.json (Neutral fallback)")
        return

    regime, score, breakdown = classify_regime(indicators)

    # VIX shock overrides regime to Contraction (unless already worse)
    if vix_shock and regime not in ("contraction", "stagflation"):
        print(f"   ⚠️  VIX shock override: {regime} → contraction")
        regime = "contraction"

    cfg = REGIME_CONFIG[regime]

    print(f"\n📊 Score breakdown: {breakdown}  →  total: {score}")
    print(f"\n🏷️  Regime: {cfg['label'].upper()}")
    print(f"   Preferred type: {cfg['preferred_type'] or 'No preference'}")
    print(f"   Entry thresholds: PoP ≥ {cfg['enter_pop']}% | ROI ≥ {cfg['enter_roi']}%")
    print(f"   {cfg['note']}")

    output = {
        "timestamp":             datetime.now().isoformat(),
        "regime":                regime,
        "regime_label":          cfg["label"],
        "score":                 score,
        "score_breakdown":       breakdown,
        "preferred_spread_type": cfg["preferred_type"],
        "regime_note":           cfg["note"],
        "indicators":            indicators,
        "scoring_adjustments": {
            "bull_put_multiplier":  cfg["bull_put_multiplier"],
            "bear_call_multiplier": cfg["bear_call_multiplier"],
            "enter_pop":            cfg["enter_pop"],
            "enter_roi":            cfg["enter_roi"],
            "watch_pop":            cfg["watch_pop"],
            "watch_roi":            cfg["watch_roi"]
        },
        "vix_shock_override": vix_shock,
        "vix_shock_reason":   vix_shock_reason,
        "block_bull_puts":    vix_shock,
    }

    with open("data/macro_regime.json", "w") as f:
        json.dump(output, f, indent=2)

    print("\n✅ Step 00H complete: macro_regime.json")


if __name__ == "__main__":
    main()
