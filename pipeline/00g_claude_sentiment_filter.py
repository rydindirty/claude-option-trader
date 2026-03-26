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
    prompt = """Analyze these stocks for HIGH RISK indicators that make them bad for credit spreads (15-45 days).

REMOVE stocks with:
- Earnings in next 45 days
- FDA decisions pending
- Merger/acquisition rumors
- Major lawsuits or regulatory action
- Severe negative sentiment spike

KEEP stocks with:
- Normal business news
- Stable or improving sentiment
- No major catalysts upcoming

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
    "TICKER3": "earnings in 12 days",
    "TICKER4": "merger rumors"
  }
}
"""

    # Call Claude API
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
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
