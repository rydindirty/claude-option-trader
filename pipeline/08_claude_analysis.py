"""
Claude Risk Analysis - 5W1H News Analysis + Heat Scores
Replaces GPT-4 with Claude for trade risk assessment.
"""
import os
import json
import sys
from datetime import datetime
import anthropic

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import ANTHROPIC_API_KEY

if not ANTHROPIC_API_KEY:
    print("❌ Missing ANTHROPIC_API_KEY")
    sys.exit(1)

def load_comprehensive_data():
    # Load trades, prices, news, and macro regime for analysis
    data = {}

    with open("data/report_table.json", "r") as f:
        data["trades"] = json.load(f)["report_table"]

    with open("data/stock_prices.json", "r") as f:
        data["prices"] = json.load(f)["prices"]

    try:
        with open("data/finnhub_news.json", "r") as f:
            news = json.load(f)
            data["news"] = news["news_data"]
    except:
        data["news"] = {}

    try:
        with open("data/macro_regime.json", "r") as f:
            data["regime"] = json.load(f)
    except FileNotFoundError:
        data["regime"] = {}

    try:
        with open("data/peer_zscores.json", "r") as f:
            data["peers"] = json.load(f).get("peer_zscores", {})
    except FileNotFoundError:
        data["peers"] = {}

    # Enrich each trade with current price and buffer percentage
    for trade in data["trades"]:
        ticker = trade["ticker"]

        if ticker in data["prices"]:
            trade["current_price"] = data["prices"][ticker]["mid"]

        strikes = trade["legs"].replace("$", "").split("/")
        trade["short_strike"] = float(strikes[0])
        trade["long_strike"] = float(strikes[1])

        if "current_price" in trade:
            current = trade["current_price"]
            if "Put" in trade["type"]:
                trade["buffer_pct"] = (current - trade["short_strike"]) / current * 100
            else:
                trade["buffer_pct"] = (trade["short_strike"] - current) / current * 100

    return data


def build_regime_block(regime):
    """Build the MACRO REGIME section for the Claude prompt."""
    if not regime:
        return ""

    label     = regime.get("regime_label", "Neutral")
    note      = regime.get("regime_note", "")
    pref_type = regime.get("preferred_spread_type")
    indicators = regime.get("indicators", {})

    # Build a one-line indicator summary
    parts = []
    if "vix" in indicators:
        v = indicators["vix"]
        parts.append(f"VIX {v['value']:.1f} ({v.get('status', '')})")
    if "yield_curve" in indicators:
        v = indicators["yield_curve"]
        parts.append(f"Yield curve {v['value']:+.2f}% ({v.get('status', '')})")
    if "cpi_yoy" in indicators:
        v = indicators["cpi_yoy"]
        parts.append(f"CPI YoY {v['value']:.1f}% ({v.get('status', '')})")
    if "gdp_growth" in indicators:
        v = indicators["gdp_growth"]
        parts.append(f"GDP {v['value']:.1f}% ({v.get('status', '')})")

    ind_line = " | ".join(parts) if parts else "No macro data available"
    pref_line = (f"Regime prefers {pref_type} spreads." if pref_type
                 else "No directional spread preference this regime.")

    return f"""
MACRO REGIME: {label.upper()}
{note}
Indicators: {ind_line}
{pref_line}
A Bull Put in a Contraction/Stagflation regime carries extra downside tail risk.
A Bear Call in a Goldilocks regime fights upward drift — require extra buffer.
Factor the macro regime into your RECOMMENDATION for each trade.

"""


def create_analysis_prompt(data):
    # Build the structured prompt with trade metrics and news headlines
    regime_block = build_regime_block(data.get("regime", {}))

    prompt = f"""Analyze 9 credit spreads with STRUCTURED NEWS ANALYSIS and HEAT SCORES.

Date: {datetime.now().strftime('%Y-%m-%d')}
{regime_block}
HEAT SCORE (1-10):
1-3 = Low risk (no catalysts, stable news)
4-6 = Medium risk (moderate news activity)
7-10 = High risk (earnings imminent, major events, regulatory)

TRADES WITH NEWS:
"""

    for i, trade in enumerate(data["trades"], 1):
        buffer = trade.get("buffer_pct", 0)
        current = trade.get("current_price", 0)
        roi = float(trade["roi"].rstrip("%"))
        pop = float(trade["pop"].rstrip("%"))
        score = (roi * pop) / 100
        ticker = trade["ticker"]
        dte = trade.get("dte", "N/A")

        # Build peer context line
        peer_data   = data.get("peers", {}).get(ticker, {})
        sector      = peer_data.get("sector", "Unknown")
        iv_z        = peer_data.get("iv_zscore")
        ret_z       = peer_data.get("return_zscore")
        peer_peers  = peer_data.get("peers_in_universe", [])
        iv_z_str    = f"{iv_z:+.2f}" if iv_z is not None else "n/a"
        ret_z_str   = f"{ret_z:+.2f}" if ret_z is not None else "n/a"
        peer_str    = ", ".join(peer_peers) if peer_peers else "none in universe"

        quant_decision = trade.get("decision", "UNKNOWN")
        quant_score    = trade.get("score", score)
        kronos_dir     = trade.get("kronos_direction", "n/a")
        kronos_pct     = trade.get("kronos_forecast_pct", 0.0)
        if kronos_dir not in ("n/a", "neutral"):
            kronos_line = f"Kronos 5-day forecast: {kronos_dir} ({kronos_pct:+.1f}%) — model-predicted price direction"
        else:
            kronos_line = "Kronos 5-day forecast: neutral (no directional signal)"

        dec_context = {
            "ENTER": "✅ Quant engine says ENTER — PoP and ROI clear both thresholds.",
            "WATCH": "🟡 Quant engine says WATCH — borderline; cleared watch threshold only.",
            "SKIP":  "🔴 Quant engine says SKIP — did NOT clear entry/watch thresholds. Presented for completeness only."
        }.get(quant_decision, "")

        prompt += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TRADE #{i}: {ticker} {trade['type']} {trade['legs']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUANT DECISION: {quant_decision}  |  Quant Score: {quant_score:.1f}
{dec_context}

METRICS:
- Current: ${current:.2f} | Short Strike: ${trade['short_strike']:.0f} | Buffer: {buffer:.1f}%
- DTE: {dte} | ROI: {roi:.1f}% | PoP: {pop:.1f}%
- Sector: {sector} | IV vs peers: z={iv_z_str} | 20d return vs peers: z={ret_z_str}
- Sector peers in today's universe: {peer_str}
- {kronos_line}

NEWS (last 3 days):
"""

        if ticker in data.get("news", {}):
            articles = data["news"][ticker]["articles"][:3]
            for idx, article in enumerate(articles, 1):
                headline = article.get("headline", "")
                summary = article.get("summary", "No summary")
                prompt += f"{idx}. {headline}\n   → {summary}\n\n"
        else:
            prompt += "No significant news\n"

    prompt += """

REQUIRED OUTPUT FORMAT — follow this exactly, no deviations:

CRITICAL FORMAT RULES:
- Start each trade with "#N. TICKER TYPE STRIKES" — number, period, space, then ticker
- No markdown headers (no ##), no "TRADE" prefix, no bold (**) around the header
- Put the recommendation keyword (Trade / Wait / Skip) on its own line after "RECOMMENDATION:"
- Analyze only the trades provided; do not mention any missing trades

Example of correct header:
#1. GOOGL Bull Put $320/$315
   DTE: 23 | ROI: 37.0% | PoP: 70.5% | HEAT: 4

For each trade use this exact structure:

#N. [TICKER] [TYPE] [STRIKES]
   DTE: [X] | ROI: [X%] | PoP: [X%] | HEAT: [1-10]

   5W1H ANALYSIS:
   • WHO: Key entities/players
   • WHAT: Main events/developments
   • WHEN: Specific dates/timing
   • WHERE: Geographic/market context
   • WHY: Underlying reasons/causes
   • HOW: Impact on price/volatility

   CATALYST RISK:
   [Only confirmed upcoming events with a specific date within DTE — e.g. earnings date,
   FDA decision date, confirmed merger vote. Write "None identified" if nothing specific.]

   RECOMMENDATION:
   Trade
   [Reason]

   (or Wait / Skip — see rules below)

RECOMMENDATION RULES — apply these strictly:
- DEFAULT IS TRADE. Recommend Trade unless a specific rule below forces Wait or Skip.
- SKIP only when: (a) confirmed earnings date falls within DTE, OR (b) confirmed FDA/regulatory
  binary decision within DTE, OR (c) quant decision is SKIP with no overriding news catalyst.
- WAIT only when: a specific dated event within DTE creates meaningful uncertainty (e.g.
  "earnings confirmed for May 3") but the quant signal is still ENTER or WATCH.
- DO NOT Skip or Wait for: ongoing sector competition, analyst ratings, general market themes,
  leadership changes, past earnings, macro uncertainty, or any risk without a specific date.
- A HEAT score of 7-10 alone does not justify Wait or Skip — it is informational only.
- If quant says ENTER and no dated catalyst exists within DTE, the answer is Trade.
"""
    return prompt


def main():
    print("=" * 60)
    print("STEP 08: Claude News Analysis")
    print("=" * 60)

    print("\n📊 Loading data...")
    data = load_comprehensive_data()

    tickers = [t["ticker"] for t in data["trades"]]
    news_count = sum(1 for t in tickers if t in data.get("news", {}))

    print(f"   ✓ {len(data['trades'])} trades")
    print(f"   ✓ {news_count}/{len(tickers)} tickers with news")
    print(f"   Tickers: {', '.join(tickers)}")

    prompt = create_analysis_prompt(data)

    # Call Claude API for 5W1H analysis
    print("\n🤖 Calling Claude for 5W1H analysis...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=(
                "You analyze credit spreads with structured 5W1H news analysis. "
                "Each trade shows a QUANT DECISION (ENTER/WATCH/SKIP) computed by a quantitative engine "
                "based on probability of profit, ROI thresholds, macro regime, technicals, peer z-scores, "
                "and Kronos AI price forecasts. "
                "Your job is news/catalyst risk assessment — not to override a SKIP without a compelling reason. "
                "If quant says SKIP, your default should also be Skip unless there is a strong newsflow reason to reconsider. "
                "If quant says ENTER and news is clean, confirm Trade. "
                "Extract specific dates, events, and entities from headlines. Assign risk heat scores 1-10. "
                "Be specific with dates and events."
            ),
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        # Extract text from Claude's response
        analysis = response.content[0].text
        print("✅ Analysis complete\n")

        print("=" * 60)
        print("CLAUDE ANALYSIS:")
        print("=" * 60)
        print(analysis)

        # Save analysis for step 09 to parse
        with open("data/top9_analysis.json", "w") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "analysis": analysis,
                "tickers": tickers
            }, f, indent=2)

        print("\n✅ Saved to data/top9_analysis.json")

    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    main()
