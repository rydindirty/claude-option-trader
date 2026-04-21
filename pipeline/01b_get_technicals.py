"""
Step 01B: Technical Indicators
Fetches 300 days of daily OHLCV history from Tradier for each ticker
and computes four technical indicators:

  RSI(14)         — Momentum oscillator (overbought/oversold)
  SMA50 / SMA200  — 50 and 200-day simple moving averages (trend)
  BB(20, 2σ)      — Bollinger Bands upper/middle/lower (volatility envelope)
  %B              — Price position within Bollinger Bands (0=lower, 1=upper)

Each ticker receives a technical_signal derived from a 5-point scoring rubric:

  SMA200 alignment (+2 / -2) — primary trend, highest weight
  SMA50  alignment (+1 / -1) — medium-term trend
  RSI    level     (+1 / -1) — momentum health (45–70 → +1; >75 or <30 → -1)
  %B     position  (+1 / -1) — position in volatility band

  Score ≥  3 → strong_bullish  (Bull Put ×1.15 | Bear Call ×0.85 in step 06)
  Score  1-2 → bullish         (Bull Put ×1.08 | Bear Call ×0.93)
  Score    0 → neutral         (no adjustment)
  Score -1,-2→ bearish         (Bull Put ×0.92 | Bear Call ×1.08)
  Score ≤ -3 → strong_bearish  (Bull Put ×0.85 | Bear Call ×1.15)

Falls back gracefully: tickers with insufficient history (e.g., under 200
trading days) receive a neutral signal and don't block the pipeline.

Input:  data/stocks.json (ticker list from step 00E)
Output: data/technicals.json
"""
import json
import os
import sys
import time
from datetime import datetime, date, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import TRADIER_TOKEN, TRADIER_ENV, get_tradier_session, TRADIER_BASE_URL

_session = get_tradier_session()

# Number of calendar days of history to request.
# 400 calendar days ≈ 275 trading days — gives a 75-day buffer above SMA200.
HISTORY_DAYS = 400

# Technical multipliers — consumed by step 06
TECH_MULTIPLIERS = {
    "strong_bullish": {"Bull Put": 1.15, "Bear Call": 0.85},
    "bullish":        {"Bull Put": 1.08, "Bear Call": 0.93},
    "neutral":        {"Bull Put": 1.00, "Bear Call": 1.00},
    "bearish":        {"Bull Put": 0.92, "Bear Call": 1.08},
    "strong_bearish": {"Bull Put": 0.85, "Bear Call": 1.15},
}


# ── Indicator math ──────────────────────────────────────────────────────────

def compute_rsi(closes, period=14):
    """Wilder's RSI. Requires at least period+1 closes. Returns None if insufficient."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_sma(closes, period):
    """Simple moving average of the last `period` closes. Returns None if insufficient."""
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)


def compute_bollinger(closes, period=20, num_std=2):
    """
    Bollinger Bands using the last `period` closes.
    Returns (upper, middle, lower) or (None, None, None) if insufficient.
    """
    if len(closes) < period:
        return None, None, None
    recent  = closes[-period:]
    middle  = sum(recent) / period
    std     = (sum((x - middle) ** 2 for x in recent) / period) ** 0.5
    return (
        round(middle + num_std * std, 2),
        round(middle, 2),
        round(middle - num_std * std, 2)
    )


def compute_bb_pct(price, upper, lower):
    """
    %B: position of price within Bollinger Bands.
    0 = at lower band, 1 = at upper band, >1 or <0 means outside.
    """
    if upper is None or lower is None or upper == lower:
        return None
    return round((price - lower) / (upper - lower), 3)


# ── Signal scoring ──────────────────────────────────────────────────────────

def score_signal(price, rsi, sma50, sma200, bb_pct):
    """
    Score the technical setup on a -5 to +5 scale.

    Rules:
      SMA200 alignment: price > sma200 → +2 | price < sma200 → -2
      SMA50  alignment: price > sma50  → +1 | price < sma50  → -1
      RSI    level:     50-65 → +1 | >70 or <35 → -1
      %B     position:  0.40-0.75 → +1 | >0.85 or <0.15 → -1
    """
    score = 0
    breakdown = {}

    if sma200 is not None:
        pts = 2 if price > sma200 else -2
        score += pts
        breakdown["sma200"] = pts

    if sma50 is not None:
        pts = 1 if price > sma50 else -1
        score += pts
        breakdown["sma50"] = pts

    if rsi is not None:
        if 45 <= rsi <= 70:   # healthy momentum — not oversold, not overbought
            pts = 1
        elif rsi > 75 or rsi < 30:   # extreme overbought or deeply oversold
            pts = -1
        else:
            pts = 0
        score += pts
        breakdown["rsi"] = pts

    if bb_pct is not None:
        if 0.40 <= bb_pct <= 0.75:
            pts = 1
        elif bb_pct > 0.85 or bb_pct < 0.15:
            pts = -1
        else:
            pts = 0
        score += pts
        breakdown["bb_pct"] = pts

    if score >= 3:
        signal = "strong_bullish"
    elif score >= 1:
        signal = "bullish"
    elif score == 0:
        signal = "neutral"
    elif score >= -2:
        signal = "bearish"
    else:
        signal = "strong_bearish"

    return signal, score, breakdown


# ── Tradier history fetch ───────────────────────────────────────────────────

def fetch_history(ticker):
    """
    Fetch daily OHLCV history for one ticker from Tradier.
    Returns a list of closing prices (oldest first), or None on failure.
    """
    end_date   = date.today()
    start_date = end_date - timedelta(days=HISTORY_DAYS)
    try:
        r = _session.get(
            f"{TRADIER_BASE_URL}/markets/history",
            headers={
                "Authorization": f"Bearer {TRADIER_TOKEN}",
                "Accept": "application/json"
            },
            params={
                "symbol":   ticker,
                "interval": "daily",
                "start":    start_date.isoformat(),
                "end":      end_date.isoformat()
            },
            timeout=15
        )
        r.raise_for_status()
        history = r.json().get("history")
        if not history or history == "null":
            return None
        days = history.get("day", [])
        if isinstance(days, dict):
            days = [days]
        closes = [float(d["close"]) for d in days if "close" in d]
        return closes if len(closes) >= 20 else None
    except Exception as e:
        print(f"   ⚠️  History fetch failed ({ticker}): {e}")
        return None


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("STEP 01B: Technical Indicators")
    print("=" * 60)

    # Load ticker list from step 00E
    try:
        with open("data/stocks.json", "r") as f:
            data = json.load(f)
        tickers = data.get("tickers", data) if isinstance(data, dict) else data
    except FileNotFoundError:
        print("❌ data/stocks.json not found — run step 00E first")
        sys.exit(1)

    # Load current prices for %B and signal scoring
    try:
        with open("data/stock_prices.json", "r") as f:
            prices_data = json.load(f)
        prices = prices_data.get("prices", {})
    except FileNotFoundError:
        print("⚠️  data/stock_prices.json not found — using close price as current price")
        prices = {}

    print(f"\n📈 Computing technicals for {len(tickers)} tickers "
          f"({HISTORY_DAYS}-day history window)...\n")

    results = {}
    neutral_count = 0

    for ticker in tickers:
        closes = fetch_history(ticker)

        if closes is None or len(closes) < 21:
            # Not enough history — neutral fallback
            print(f"   {ticker:<6} ⚠️  Insufficient history — neutral fallback")
            results[ticker] = {
                "signal":           "neutral",
                "signal_score":     0,
                "score_breakdown":  {},
                "current_price":    None,
                "price_return_20d": None,
                "indicators":       {},
                "fallback":         True
            }
            neutral_count += 1
            time.sleep(0.1)
            continue

        # Use current market price if available, else last close
        current_price = prices.get(ticker, {}).get("mid") or closes[-1]

        rsi     = compute_rsi(closes)
        sma50   = compute_sma(closes, 50)
        sma200  = compute_sma(closes, 200)
        bb_u, bb_m, bb_l = compute_bollinger(closes, 20, 2)
        bb_pct  = compute_bb_pct(current_price, bb_u, bb_l)

        signal, score, breakdown = score_signal(
            current_price, rsi, sma50, sma200, bb_pct)

        # Signal label for display
        signal_icons = {
            "strong_bullish": "📈📈",
            "bullish":        "📈 ",
            "neutral":        "➡️  ",
            "bearish":        "📉 ",
            "strong_bearish": "📉📉",
        }
        icon = signal_icons.get(signal, "   ")

        sma200_str = f"{sma200:.2f}" if sma200 else "n/a"
        rsi_str    = f"{rsi:.1f}"   if rsi    else "n/a"
        bb_str     = f"{bb_pct:.2f}" if bb_pct is not None else "n/a"

        print(f"   {ticker:<6} {icon}  {signal:<15}  "
              f"score:{score:+d}  price:{current_price:.2f}  "
              f"SMA200:{sma200_str}  RSI:{rsi_str}  %B:{bb_str}")

        indicators = {}
        if rsi    is not None: indicators["rsi"]    = {"value": rsi}
        if sma50  is not None: indicators["sma50"]  = {"value": sma50}
        if sma200 is not None: indicators["sma200"] = {"value": sma200}
        if bb_u   is not None:
            indicators["bollinger"] = {
                "upper": bb_u, "middle": bb_m, "lower": bb_l, "pct_b": bb_pct
            }

        ret_20d = (round((closes[-1] / closes[-21] - 1) * 100, 2)
                   if len(closes) >= 21 else None)

        results[ticker] = {
            "signal":            signal,
            "signal_score":      score,
            "score_breakdown":   breakdown,
            "current_price":     current_price,
            "price_return_20d":  ret_20d,
            "history_days":      len(closes),
            "indicators":        indicators,
            "fallback":          False
        }

        time.sleep(0.1)   # light rate-limit buffer

    computed = len(tickers) - neutral_count
    print(f"\n📊 Summary:")
    print(f"   Computed:  {computed}/{len(tickers)}")
    print(f"   Fallbacks: {neutral_count} (neutral)")

    # Signal distribution
    dist = {}
    for v in results.values():
        s = v["signal"]
        dist[s] = dist.get(s, 0) + 1
    for sig, cnt in sorted(dist.items()):
        icon = {"strong_bullish": "📈📈", "bullish": "📈", "neutral": "➡️",
                "bearish": "📉", "strong_bearish": "📉📉"}.get(sig, "")
        print(f"   {icon} {sig}: {cnt}")

    output = {
        "timestamp":          datetime.now().isoformat(),
        "ticker_count":       len(tickers),
        "history_days":       HISTORY_DAYS,
        "tech_multipliers":   TECH_MULTIPLIERS,
        "technicals":         results
    }

    with open("data/technicals.json", "w") as f:
        json.dump(output, f, indent=2)

    print("\n✅ Step 01B complete: technicals.json")


if __name__ == "__main__":
    main()
