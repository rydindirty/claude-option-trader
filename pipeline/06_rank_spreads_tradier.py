"""
Rank Spreads: One spread per ticker only.
Applies independent score multipliers and regime-adjusted entry thresholds.

Final score formula:
  base_score     = pop * (1 + roi / 100)   ← PoP is primary; ROI adds bonus
  adjusted_score = base_score × regime_mult × tech_mult × peer_mult × kronos_mult

This formula ensures that a 80% PoP / 20% ROI spread (score 96) beats a
70% PoP / 35% ROI spread (score 94.5), which is the correct risk ordering
for credit spreads. The old roi*pop/100 formula inverted this.

1. Macro regime multiplier (from data/macro_regime.json, step 00H):
     Goldilocks  — Bull Put ×1.15 | Bear Call ×0.90
     Neutral     — both ×1.00
     Slowing     — Bull Put ×0.90 | Bear Call ×1.10
     Contraction — Bull Put ×0.80 | Bear Call ×1.20
     Stagflation — Bull Put ×0.80 | Bear Call ×1.20

2. Technical indicator multiplier (from data/technicals.json, step 01B):
     strong_bullish — Bull Put ×1.15 | Bear Call ×0.85
     bullish        — Bull Put ×1.08 | Bear Call ×0.93
     neutral        — both ×1.00
     bearish        — Bull Put ×0.92 | Bear Call ×1.08
     strong_bearish — Bull Put ×0.85 | Bear Call ×1.15

3. Peer z-score multiplier (from data/peer_zscores.json, step 01C):
     Derived from IV and return z-scores vs sector peers.

4. Kronos AI multiplier (from data/kronos_signals.json, step 01D):
     Aligned ≥3.0% forecast  — ×1.20
     Aligned ≥1.5% forecast  — ×1.12
     Aligned ≥0.5% forecast  — ×1.06
     Opposing ≥3.0% forecast — ×0.80
     Opposing ≥1.5% forecast — ×0.88
     Opposing ≥0.5% forecast — ×0.94
     Neutral (<0.5% move)    — ×1.00

Entry thresholds shift per regime; see 00h_macro_regime.py for details.
All files fall back gracefully (×1.00, standard thresholds) if absent.
"""
import json
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TECH_MULTIPLIERS = {
    "strong_bullish": {"Bull Put": 1.15, "Bear Call": 0.85},
    "bullish":        {"Bull Put": 1.08, "Bear Call": 0.93},
    "neutral":        {"Bull Put": 1.00, "Bear Call": 1.00},
    "bearish":        {"Bull Put": 0.92, "Bear Call": 1.08},
    "strong_bearish": {"Bull Put": 0.85, "Bear Call": 1.15},
}

_NEUTRAL = {
    "regime_label":         "Neutral",
    "preferred_type":       None,
    "bull_put_multiplier":  1.0,
    "bear_call_multiplier": 1.0,
    "enter_pop":            70,
    "enter_roi":            20,
    "watch_pop":            70,   # PoP floor is 70% — WATCH catches ROI 15–19%, near ENTER
    "watch_roi":            15,
    "regime_note":          ""
}


def load_macro_regime():
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
            "watch_pop":            adj.get("watch_pop", 70),
            "watch_roi":            adj.get("watch_roi", 15),
            "regime_note":          data.get("regime_note", "")
        }
    except FileNotFoundError:
        print("   ⚠️  macro_regime.json not found — using neutral defaults")
        return _NEUTRAL


def load_technicals():
    try:
        with open("data/technicals.json", "r") as f:
            data = json.load(f)
        return {t: v.get("signal", "neutral")
                for t, v in data.get("technicals", {}).items()}
    except FileNotFoundError:
        print("   ⚠️  technicals.json not found — no tech adjustment applied")
        return {}


def load_peer_zscores():
    try:
        with open("data/peer_zscores.json", "r") as f:
            data = json.load(f)
        return {t: v.get("peer_multiplier", 1.0)
                for t, v in data.get("peer_zscores", {}).items()}
    except FileNotFoundError:
        print("   ⚠️  peer_zscores.json not found — no peer adjustment applied")
        return {}


def load_kronos_signals():
    """Load per-ticker Kronos multipliers. Returns {ticker: {bull_put: float, bear_call: float}}."""
    try:
        with open("data/kronos_signals.json", "r") as f:
            data = json.load(f)
        result = {}
        for ticker, sig in data.get("signals", {}).items():
            result[ticker] = {
                "bull_put":   sig.get("kronos_mult_bull_put",  1.0),
                "bear_call":  sig.get("kronos_mult_bear_call", 1.0),
                "direction":  sig.get("direction", "neutral"),
                "forecast_pct": sig.get("forecast_pct", 0.0),
            }
        installed = data.get("kronos_installed", False)
        return result, installed
    except FileNotFoundError:
        print("   ⚠️  kronos_signals.json not found — no Kronos adjustment applied")
        return {}, False


def rank_spreads():
    print("=" * 60)
    print("STEP 6: Rank Spreads (1 per ticker)")
    print("=" * 60)

    with open("data/spreads.json", "r") as f:
        data = json.load(f)
    spreads = data["spreads"]

    regime      = load_macro_regime()
    tech_map    = load_technicals()
    peer_map    = load_peer_zscores()
    kronos_map, kronos_installed = load_kronos_signals()

    print(f"\n🌍 Macro Regime: {regime['regime_label']}")
    if regime["preferred_type"]:
        print(f"   Preferred type: {regime['preferred_type']}")
    else:
        print(f"   Preferred type: No preference")
    print(f"   Entry thresholds: PoP ≥ {regime['enter_pop']}% | ROI ≥ {regime['enter_roi']}%")
    if regime["regime_note"]:
        print(f"   {regime['regime_note']}")
    print(f"\n📊 Technicals: {len(tech_map)} tickers | Peer z-scores: {len(peer_map)} tickers")
    kronos_status = "✅ active" if kronos_installed else "⚠️  neutral fallback"
    print(f"   Kronos: {kronos_status} ({len(kronos_map)} tickers)")

    print(f"\n🏆 Ranking {len(spreads)} spreads...")

    for spread in spreads:
        pop = spread["pop"]
        roi = spread["roi"]
        spread_type = spread["type"]
        ticker = spread["ticker"]

        # PoP-primary base score: higher PoP always wins vs lower PoP at same ROI
        base_score = pop * (1 + roi / 100)

        # Regime multiplier
        regime_mult = (regime["bull_put_multiplier"] if spread_type == "Bull Put"
                       else regime["bear_call_multiplier"])

        # Technical multiplier
        signal    = tech_map.get(ticker, "neutral")
        tech_mult = _TECH_MULTIPLIERS.get(signal, _TECH_MULTIPLIERS["neutral"])[spread_type]

        # Peer z-score multiplier
        peer_mult = peer_map.get(ticker, 1.0)

        # Kronos AI multiplier
        kronos_ticker = kronos_map.get(ticker, {})
        if spread_type == "Bull Put":
            kronos_mult = kronos_ticker.get("bull_put", 1.0)
        else:
            kronos_mult = kronos_ticker.get("bear_call", 1.0)

        spread["score"]             = round(base_score * regime_mult * tech_mult * peer_mult * kronos_mult, 1)
        spread["regime_multiplier"] = regime_mult
        spread["tech_signal"]       = signal
        spread["tech_multiplier"]   = tech_mult
        spread["peer_multiplier"]   = peer_mult
        spread["kronos_multiplier"] = kronos_mult
        spread["kronos_direction"]  = kronos_ticker.get("direction", "n/a")
        spread["kronos_forecast_pct"] = kronos_ticker.get("forecast_pct", 0.0)

        # Regime-adjusted ENTER / WATCH / SKIP thresholds
        if spread["pop"] >= regime["enter_pop"] and spread["roi"] >= regime["enter_roi"]:
            spread["decision"] = "ENTER"
        elif spread["pop"] >= regime["watch_pop"] and spread["roi"] >= regime["watch_roi"]:
            spread["decision"] = "WATCH"
        else:
            spread["decision"] = "SKIP"

    spreads.sort(key=lambda x: x["score"], reverse=True)

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
        r_mult  = spread.get("regime_multiplier", 1.0)
        t_mult  = spread.get("tech_multiplier",   1.0)
        p_mult  = spread.get("peer_multiplier",   1.0)
        k_mult  = spread.get("kronos_multiplier", 1.0)
        signal  = spread.get("tech_signal", "neutral")
        k_dir   = spread.get("kronos_direction", "n/a")
        k_pct   = spread.get("kronos_forecast_pct", 0.0)
        parts   = []
        if r_mult != 1.0: parts.append(f"regime×{r_mult:.2f}")
        if t_mult != 1.0: parts.append(f"tech×{t_mult:.2f}")
        if p_mult != 1.0: parts.append(f"peer×{p_mult:.2f}")
        if k_mult != 1.0: parts.append(f"kronos×{k_mult:.2f}")
        adj_note = f" ({' × '.join(parts)})" if parts else ""
        dec_icon = {"ENTER": "🟢", "WATCH": "🟡", "SKIP": "🔴"}.get(spread["decision"], "")
        print(f"   #{spread['rank']}: {spread['ticker']} {spread['type']} "
              f"${spread['short_strike']:.0f}/${spread['long_strike']:.0f}  "
              f"{dec_icon} {spread['decision']}  [{signal}]  kronos:{k_dir}({k_pct:+.1f}%)")
        print(f"        Score: {spread['score']}{adj_note} | "
              f"ROI: {spread['roi']}% | PoP: {spread['pop']}%")

    print("\n✅ Step 6 complete: ranked_spreads.json")


if __name__ == "__main__":
    rank_spreads()
