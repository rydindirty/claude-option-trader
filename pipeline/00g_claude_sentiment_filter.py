"""
Step 0G: Claude Sentiment Pre-Filter
Analyzes news and removes high-risk stocks before spread calculation.
Replaces GPT-4o-mini with Claude.
"""
import json
import sys
import os
from datetime import datetime
import anthropic

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import ANTHROPIC_API_KEY

def analyze_news_sentiment():
    print("=" * 60)
    print("STEP 0G: Claude Sentiment Analysis")
    print("=" * 60)

    # Load news produced by step 00f
    with open("data/finnhub_news.json", "r") as f:
        news_data = json.load(f)

    stocks_with_news = news_data["news_data"]
    print(f"\nAnalyzing {len(stocks_with_news)} stocks for risk...")

    # Build the analysis prompt
    prompt = """You are screening stocks for credit spread suitability. Our spreads have a 21-45 day hold window.

REMOVE only if a headline explicitly confirms a known catalyst WITHIN THE NEXT 21 DAYS:
- Confirmed earnings date within 21 days (e.g. "reports Q1 earnings on May 3")
- FDA binary decision explicitly scheduled within 21 days
- Confirmed merger vote or close date within 21 days

DO NOT REMOVE for:
- General earnings discussion, analyst estimates, or past earnings recaps
- Sector headwinds, macro concerns, or industry trends
- Analyst upgrades/downgrades or price target changes
- AI/tech investment themes or competitive positioning
- Layoffs, restructuring, or cost-cutting (unless tied to a specific catalyst date)
- High volatility or momentum coverage
- Anything you are uncertain about — DEFAULT TO KEEP

When in doubt, KEEP the stock. A false removal hurts us more than a false keep (we have other downstream filters).

STOCKS & NEWS:

"""

    for ticker, data in list(stocks_with_news.items())[:22]:
        prompt += f"\n{ticker} ({data['article_count']} articles):\n"
        for article in data["articles"][:5]:
            headline = article.get("headline", "")
            prompt += f"  - {headline}\n"

    prompt += """

OUTPUT JSON ONLY - no explanation, no markdown fences:
{
  "keep": ["TICKER1", "TICKER2"],
  "remove": {
    "TICKER3": "confirmed earnings May 3 (8 days)",
    "TICKER4": "FDA decision May 5 (10 days)"
  }
}
"""

    # Call Claude API
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="You filter stocks for credit spread safety. Output JSON only. No markdown fences, no explanation.",
        messages=[
            {"role": "user", "content": prompt}
        ]
    )

    content = response.content[0].text

    # Parse Claude's response - strip markdown fences if present
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
        keep_tickers = result.get("keep", [])
        remove_tickers = result.get("remove", {})

        print(f"\n✅ Claude Analysis Complete:")
        print(f"   Keep:   {len(keep_tickers)} stocks")
        print(f"   Remove: {len(remove_tickers)} stocks")

        if remove_tickers:
            print(f"\n   Removed:")
            for ticker, reason in remove_tickers.items():
                print(f"      {ticker}: {reason}")

        # Save filtered stock list for downstream steps
        with open("data/stocks.json", "w") as f:
            json.dump({
                "tickers": keep_tickers,
                "removed": remove_tickers,
                "timestamp": datetime.now().isoformat()
            }, f, indent=2)

        print(f"\n✅ Updated data/stocks.json with {len(keep_tickers)} safe stocks")

    except Exception as e:
        print(f"❌ Parse error: {e}")
        print(f"   Raw response: {content[:200]}")
        print("   Keeping all stocks as fallback")

if __name__ == "__main__":
    analyze_news_sentiment()
