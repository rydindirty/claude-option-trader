# Credit Spread Finder

**Purpose:** Find the 9 best options trades (credit spreads) from 500 stock tickers in minutes

**What to know:** Which exact trades to place today
**What to do:** Execute high-probability credit spreads
**What to feel:** Confident because math backs every trade

> **Original concept and pipeline architecture by [Temple-Stuart](https://github.com/Temple-Stuart/temple-stuart-accounting).**
> This implementation migrates the pipeline to the Tradier brokerage API and replaces GPT analysis with Claude AI.

---

### What's in it for me?
- Save 3 hours of manual searching daily
- Make money 70% of the time (mathematically proven)
- Never miss high-volatility opportunities
- Stop losing on shitty scanner data

---

## Stack

| Layer | Tool |
|---|---|
| Brokerage / market data | [Tradier](https://tradier.com) |
| News data | [Finnhub](https://finnhub.io) |
| AI analysis | [Claude](https://anthropic.com) (Anthropic) |
| Spread math | Black-Scholes (built-in) |

---

## Pipeline

### Phase 1 — Build a Portfolio

**Step 00A:** Download S&P 500 company list from GitHub. Extract ticker symbols. Save 503 tickers to `data/sp500.json`.

```bash
python3 pipeline/00a_get_sp500.py
```

**Step 00B:** Stream live bid/ask quotes from Tradier for all S&P 500 stocks. Filter by price ($30–$400) and spread (<2%). Save liquid stocks to `data/filter1_passed.json`.

```bash
python3 pipeline/00b_filter_price_tradier.py
```

**Step 00C:** Stream options chains from Tradier for output of Step 00B. Filter by expiration (15–45 days) and strike count (20+ strikes). Save stocks with tradeable options to `data/filter2_passed.json`.

```bash
python3 pipeline/00c_filter_options_tradier.py
```

**Step 00D:** Stream ATM option strikes and IV data from Tradier. Filter by IV range 15–80%. Save to `data/filter3_passed.json`.

```bash
python3 pipeline/00d_filter_iv_tradier.py
```

**Step 00E:** Score each stock by IV (40 pts), strike count (30 pts), expirations (20 pts), spread tightness (10 pts). Rank and select top 22 tickers. Save to `data/stocks.json`.

```bash
python3 pipeline/00e_select_22_tradier.py
```

**Step 00F:** Fetch 3 days of news from Finnhub for the top 22. Collect up to 10 articles per stock with headlines. Save to `data/finnhub_news.json`.

```bash
python3 pipeline/00f_get_news_tradier.py
```

**Step 00G:** Send news headlines to Claude AI. Remove any stock with earnings in the next 45 days, FDA decisions, M&A rumors, major lawsuits, or severe negative sentiment. Save filtered list back to `data/stocks.json`.

```bash
python3 pipeline/00g_claude_sentiment_filter.py
```

---

### Phase 2 — Build Credit Spreads

**Step 01:** Stream real-time bid/ask quotes from Tradier for the filtered stock list. Save mid-price, spread, and timestamp to `data/stock_prices.json`.

```bash
python3 pipeline/01_get_prices_tradier.py
```

**Step 02:** Stream full options chains from Tradier. Filter 0–45 DTE, 70–130% of stock price strikes. Save expiration dates, strikes, call/put symbols, and bid/ask to `data/chains.json`.

```bash
python3 pipeline/02_get_chains_tradier.py
```

**Step 03:** Check liquidity for every strike — filter by mid-price ≥ $0.30 and spread <10%. Save liquid strikes per expiration to `data/liquid_chains.json`.

```bash
python3 pipeline/03_check_liquidity_tradier.py
```

**Step 04:** Fetch Greeks (IV/delta/theta/gamma/vega) from Tradier in batches. Embed Greeks into the chain structure at exact strike locations. Save to `data/chains_with_greeks.json`.

```bash
python3 pipeline/04_get_greeks_tradier.py
```

**Step 05:** Pair strikes into Bull Put and Bear Call spreads. Filter short delta 15–35% (OTM probability). Calculate credit, max loss, and ROI. Use Black-Scholes with strike-specific IV to calculate PoP. Filter ROI 5–50%, PoP ≥60%. Save to `data/spreads.json`.

```bash
python3 pipeline/05_calculate_spreads_tradier.py
```

**Step 06:** Score each spread as `(ROI × PoP) / 100`. Keep only the highest-scoring spread per ticker (22 total). Categorize as ENTER (PoP ≥70% + ROI ≥20%), WATCH (PoP ≥60% + ROI ≥30%), or SKIP. Save to `data/ranked_spreads.json`.

```bash
python3 pipeline/06_rank_spreads_tradier.py
```

**Step 07:** Select top 9 by rank. Add sector mapping (XLK/XLF/XLV etc). Format into report table with rank, ticker, type, strikes, DTE, ROI, PoP, credit, max loss. Save to `data/report_table.json`.

```bash
python3 pipeline/07_build_report_tradier.py
```

**Step 08:** Send each trade to Claude AI with 3 news headlines. Claude applies 5W1H analysis (Who/What/When/Where/Why/How), assigns a heat score 1–10 (news/catalyst risk), and recommends **Trade / Wait / Skip**. Save to `data/top9_analysis.json`.

```bash
python3 pipeline/08_claude_analysis.py
```

**Step 09:** Parse Claude's structured output. Extract ticker, type, strikes, DTE, ROI, PoP, heat score, and recommendation. Print formatted table. Save timestamped CSV for Excel.

```bash
python3 pipeline/09_format_trades_tradier.py
```

---

### Phase 3 — Execute & Monitor

**Step 10:** Run the full pipeline end-to-end (Steps 00A → 09), then auto-launch Steps 11 and 12.

```bash
python3 pipeline/10_run_pipeline_tradier.py
```

**Step 11:** Human approval gate. Presents each Claude-recommended **TRADE** with full details (strikes, credit, max loss, ROI, PoP, heat score). Prompts for contract count, previews the order via Tradier, requires explicit `yes` confirmation before placing any live order. Saves placed trades to `data/open_positions.json`.

```bash
python3 pipeline/11_place_trades.py
```

**Step 12:** Position monitor. Runs every 5 minutes during market hours (9:35–15:55 ET). Checks every open position against three exit rules:
- **Profit target** — close when spread value drops to 60% of credit received (40% profit locked in)
- **Stop loss** — close when spread costs 2× the credit to close
- **Time stop** — close when DTE reaches 21 days

Places closing orders automatically via Tradier. Logs closed trades to `data/closed_positions.json`.

```bash
python3 pipeline/12_position_monitor.py
```

---

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/rydindirty/<repo-name>.git
cd <repo-name>

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install requests openai anthropic finnhub-python

# 4. Configure credentials
cp config.py.example config.py
# Edit config.py with your Tradier token, Finnhub key, and Anthropic key

# 5. Run the full pipeline
python3 pipeline/10_run_pipeline_tradier.py
```

---

## Data Flow

```
S&P 500 (503)
  → Price filter       → ~150 liquid stocks
  → Options filter     → ~100 with tradeable chains
  → IV filter          → ~60 in 15–80% IV range
  → Score & select     → Top 22 tickers
  → Sentiment filter   → Risky stocks removed
  → Spread builder     → Thousands of spread candidates
  → Liquidity check    → Only liquid strikes
  → Greeks             → Delta-filtered OTM spreads
  → ROI/PoP filter     → High-probability trades only
  → Rank & top 9       → Best spread per ticker
  → Claude analysis    → Heat score + recommendation
  → Human approval     → Live orders placed
  → Position monitor   → Auto-exit at targets
```

---

## Credit

Original pipeline concept and architecture by **[Temple-Stuart](https://github.com/Temple-Stuart/temple-stuart-accounting)**.
This fork migrates market data and trade execution to the Tradier API and replaces GPT-4 analysis with Claude AI.
