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

        prompt += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TRADE #{i}: {ticker} {trade['type']} {trade['legs']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
METRICS:
- Current: ${current:.2f} | Short Strike: ${trade['short_strike']:.0f} | Buffer: {buffer:.1f}%
- DTE: {dte} | ROI: {roi:.1f}% | PoP: {pop:.1f}% | Score: {score:.1f}

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

REQUIRED OUTPUT:

For each trade:

#1. [TICKER] [TYPE] [STRIKES]
   DTE: [X] | ROI: [X%] | PoP: [X%] | HEAT: [1-10]
   
   5W1H ANALYSIS:
   • WHO: Key entities/players
   • WHAT: Main events/developments
   • WHEN: Specific dates/timing
   • WHERE: Geographic/market context
   • WHY: Underlying reasons/causes
   • HOW: Impact on price/volatility
   
   CATALYST RISK:
   [Specific upcoming events within DTE]
   
   RECOMMENDATION:
   [Trade / Wait / Skip - with reason]

Continue through all 9 trades. Be specific with dates and events.
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
            system="You analyze credit spreads with structured 5W1H news analysis. Extract specific dates, events, and entities from headlines and summaries. Assign risk heat scores 1-10. Be specific with dates and events.",
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
