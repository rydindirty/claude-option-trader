"""
Step 00K: Claude Directional Call
Independent bullish/bearish/neutral verdict per ticker from news, used as one
half of the debit-spread direction gate (the other half is Kronos's price
forecast in data/kronos_signals.json). Step 05 only builds a debit spread for a
ticker when this verdict and Kronos's direction agree — see
data/strategy_params.json's "_debit_note".

Unlike 00g (which only screens for dated-catalyst risk), this step makes an
actual directional call, since debit spreads need to know a side (calls vs
puts) before construction — there's no spread type left for Claude to react to
after the fact.
"""
import json
import sys
import os
from datetime import datetime
import anthropic

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import ANTHROPIC_API_KEY


def get_direction_calls():
    print("=" * 60)
    print("STEP 00K: Claude Directional Call")
    print("=" * 60)

    with open("data/finnhub_news.json", "r") as f:
        news_data = json.load(f)

    with open("data/stocks.json", "r") as f:
        stocks_data = json.load(f)

    tickers = stocks_data.get("tickers", [])
    stocks_with_news = news_data.get("news_data", {})
    print(f"\nCalling direction for {len(tickers)} tickers...")

    prompt = """You are making an independent directional call (bullish/bearish/neutral) on
each stock below, based ONLY on the news provided. This call drives which side
(calls or puts) of a debit spread gets built — it is combined with a separate
price-forecast model, and a trade only happens if both agree, so be honest
about "neutral" when the news doesn't support a real directional read.

Call "bullish" only if the news gives a real reason to expect the stock to rise
over the next ~30-45 days (positive catalyst, strong guidance, upgrade with
substance, etc). Call "bearish" only for a real reason to expect a decline
(negative catalyst, guidance cut, downgrade with substance, etc). Otherwise call
"neutral" — mixed, no news, or routine coverage. Do not force a lean.

STOCKS & NEWS:

"""

    for ticker in tickers:
        data = stocks_with_news.get(ticker, {})
        articles = data.get("articles", [])
        prompt += f"\n{ticker} ({len(articles)} articles):\n"
        for article in articles[:5]:
            headline = article.get("headline", "")
            prompt += f"  - {headline}\n"
        if not articles:
            prompt += "  (no recent news)\n"

    prompt += """

OUTPUT JSON ONLY - no explanation, no markdown fences:
{
  "TICKER1": {"direction": "bullish", "confidence": 0.7, "rationale": "..."},
  "TICKER2": {"direction": "neutral", "confidence": 0.0, "rationale": "no directional news"}
}
"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system="You make independent directional calls from news for a trading pipeline. Output JSON only. No markdown fences, no explanation.",
        messages=[
            {"role": "user", "content": prompt}
        ]
    )

    content = response.content[0].text

    try:
        if "```json" in content:
            start = content.find("```json") + 7
            end = content.find("```", start)
            json_str = content[start:end].strip()
        elif "```" in content:
            start = content.find("```") + 3
            end = content.find("```", start)
            json_str = content[start:end].strip()
        else:
            json_str = content.strip()

        result = json.loads(json_str)

        directions = {}
        for ticker in tickers:
            call = result.get(ticker, {"direction": "neutral", "confidence": 0.0, "rationale": "not returned by Claude"})
            direction = call.get("direction", "neutral")
            if direction not in ("bullish", "bearish", "neutral"):
                direction = "neutral"
            directions[ticker] = {
                "direction": direction,
                "confidence": call.get("confidence", 0.0),
                "rationale": call.get("rationale", ""),
            }

        bullish = sum(1 for d in directions.values() if d["direction"] == "bullish")
        bearish = sum(1 for d in directions.values() if d["direction"] == "bearish")
        neutral = sum(1 for d in directions.values() if d["direction"] == "neutral")

        print(f"\n✅ Claude Direction Complete:")
        print(f"   📈 Bullish: {bullish}  📉 Bearish: {bearish}  ➡️ Neutral: {neutral}")
        for ticker, d in directions.items():
            icon = "📈" if d["direction"] == "bullish" else ("📉" if d["direction"] == "bearish" else "➡️")
            print(f"   {icon} {ticker}: {d['direction']} (conf {d['confidence']:.1f}) — {d['rationale']}")

        with open("data/claude_direction.json", "w") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "directions": directions,
            }, f, indent=2)

        print(f"\n✅ Wrote data/claude_direction.json with {len(directions)} tickers")

    except Exception as e:
        print(f"❌ Parse error: {e}")
        print(f"   Raw response: {content[:200]}")
        print("   Writing neutral for all tickers as fallback (no debit spreads will be built this run)")
        directions = {t: {"direction": "neutral", "confidence": 0.0, "rationale": "parse error"} for t in tickers}
        with open("data/claude_direction.json", "w") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "directions": directions,
            }, f, indent=2)


if __name__ == "__main__":
    get_direction_calls()
