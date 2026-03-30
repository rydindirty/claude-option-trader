"""
Rank Spreads: One spread per ticker only.
Applies macro regime score multipliers and adjusted entry thresholds
loaded from data/macro_regime.json (written by step 00H).

Scoring:
  base_score = (ROI × PoP) / 100
  adjusted_score = base_score × regime_multiplier

  Multipliers by regime:
    Goldilocks  — Bull Put ×1.15 | Bear Call ×0.90
    Neutral     — both ×1.00  (no adjustment)
    Slowing     — Bull Put ×0.90 | Bear Call ×1.10
    Contraction — Bull Put ×0.80 | Bear Call ×1.20
    Stagflation — Bull Put ×0.80 | Bear Call ×1.20

Entry thresholds also shift per regime; see 00h_macro_regime.py for details.
Falls back to Neutral (×1.00, standard thresholds) if file is absent.
"""
import json
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Neutral defaults — used when macro_regime.json is absent ───────────────
_NEUTRAL = {
    "regime_label":         "Neutral",
    "preferred_type":       None,
    "bull_put_multiplier":  1.0,
    "bear_call_multiplier": 1.0,
    "enter_pop":            70,
    "enter_roi":            20,
    "watch_pop":            60,
    "watch_roi":            30,
    "regime_note":          ""
}


def load_macro_regime():
    """Load regime config from macro_regime.json; fall back to neutral."""
    try:
        with open("data/macro_regime.json", "r") as f:
            data = json.load(f)
        adj = data.get("scoring_adjustments", {})
        return {
            "regime_label":         data.get("regime_label", "Neutral"),
            "preferred_type":       data.get("preferred_spread_type"),
            "bull_put_multiplier":  adj.get("bull_put_multiplier", 1.0),
            "bear_call_multiplier": adj.get("bear_call_multiplier", 1.0),
            "enter_pop":            adj.get("enter_pop", 70),
            "enter_roi":            adj.get("enter_roi", 20),
            "watch_pop":            adj.get("watch_pop", 60),
            "watch_roi":            adj.get("watch_roi", 30),
            "regime_note":          data.get("regime_note", "")
        }
    except FileNotFoundError:
        print("   ⚠️  macro_regime.json not found — using neutral defaults")
        return _NEUTRAL


def rank_spreads():
    print("=" * 60)
    print("STEP 6: Rank Spreads (1 per ticker)")
    print("=" * 60)

    with open("data/spreads.json", "r") as f:
        data = json.load(f)
    spreads = data["spreads"]

    regime = load_macro_regime()

    print(f"\n🌍 Macro Regime: {regime['regime_label']}")
    if regime["preferred_type"]:
        print(f"   Preferred type: {regime['preferred_type']}")
    else:
        print(f"   Preferred type: No preference")
    print(f"   Entry thresholds: PoP ≥ {regime['enter_pop']}% | ROI ≥ {regime['enter_roi']}%")
    if regime["regime_note"]:
        print(f"   {regime['regime_note']}")

    print(f"\n🏆 Ranking {len(spreads)} spreads...")

    for spread in spreads:
        # Base score
        base_score = (spread["roi"] * spread["pop"]) / 100

        # Regime multiplier by spread type
        if spread["type"] == "Bull Put":
            multiplier = regime["bull_put_multiplier"]
        else:
            multiplier = regime["bear_call_multiplier"]

        spread["score"]             = round(base_score * multiplier, 1)
        spread["regime_multiplier"] = multiplier

        # Regime-adjusted ENTER / WATCH thresholds
        if spread["pop"] >= regime["enter_pop"] and spread["roi"] >= regime["enter_roi"]:
            spread["decision"] = "ENTER"
        elif spread["pop"] >= regime["watch_pop"] and spread["roi"] >= regime["watch_roi"]:
            spread["decision"] = "WATCH"
        else:
            spread["decision"] = "SKIP"

    # Sort by adjusted score descending
    spreads.sort(key=lambda x: x["score"], reverse=True)

    # Keep only the best-scoring spread per ticker
    seen_tickers = set()
    unique_spreads = []
    for spread in spreads:
        if spread["ticker"] not in seen_tickers:
            seen_tickers.add(spread["ticker"])
            unique_spreads.append(spread)

    for i, spread in enumerate(unique_spreads):
        spread["rank"] = i + 1

    enter = [s for s in unique_spreads if s["decision"] == "ENTER"]
    watch = [s for s in unique_spreads if s["decision"] == "WATCH"]
    skip  = [s for s in unique_spreads if s["decision"] == "SKIP"]

    output = {
        "timestamp":    datetime.now().isoformat(),
        "macro_regime": regime["regime_label"],
        "summary": {
            "total": len(unique_spreads),
            "enter": len(enter),
            "watch": len(watch),
            "skip":  len(skip)
        },
        "ranked_spreads": unique_spreads,
        "enter_trades":   enter,
        "watch_list":     watch
    }

    with open("data/ranked_spreads.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n📊 Results (1 per ticker):")
    print(f"   🟢 ENTER: {len(enter)}")
    print(f"   🟡 WATCH: {len(watch)}")
    print(f"   🔴 SKIP:  {len(skip)}")

    print(f"\n🎯 Top 9 Spreads:")
    for spread in unique_spreads[:9]:
        mult = spread.get("regime_multiplier", 1.0)
        mult_note = f" ×{mult:.2f}" if mult != 1.0 else ""
        print(f"   #{spread['rank']}: {spread['ticker']} {spread['type']} "
              f"${spread['short_strike']:.0f}/${spread['long_strike']:.0f}")
        print(f"        Score: {spread['score']}{mult_note} | "
              f"ROI: {spread['roi']}% | PoP: {spread['pop']}%")

    print("\n✅ Step 6 complete: ranked_spreads.json")


if __name__ == "__main__":
    rank_spreads()
