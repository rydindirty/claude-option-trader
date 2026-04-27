# News Spread Engine

**Purpose:** Find and execute the best options credit spreads from 500 stock tickers — fully automated, cloud-hosted, with a web UI for trade approval and position management.

**What to know:** Which exact trades to place today
**What to do:** Approve high-probability credit spreads via the web UI
**What to feel:** Confident because math, AI, and macro regime data back every trade

> **Original concept and pipeline architecture by [Temple-Stuart](https://github.com/Temple-Stuart/temple-stuart-accounting).**
> This implementation migrates the pipeline to the Tradier brokerage API, replaces GPT analysis with Claude AI, and adds a full cloud deployment with web UI.

---

## Stack

| Layer | Tool |
|---|---|
| Brokerage / market data | [Tradier](https://tradier.com) |
| News data | [Finnhub](https://finnhub.io) |
| AI analysis | [Claude](https://anthropic.com) (Anthropic) |
| Macro regime | [FRED API](https://fred.stlouisfed.org/docs/api/) (cached) |
| Volatility forecast | [KronosAI](https://github.com/kronos-ai) (×0.80–1.20 score multiplier) |
| Spread math | Black-Scholes (built-in) |
| Web UI | [FastAPI](https://fastapi.tiangolo.com) + vanilla JS |
| Analytics | SQLite (`data/trades.db`) |
| Cloud | AWS EC2 t3.micro, Ubuntu 24.04, US East (Ohio) |
| SSL / reverse proxy | nginx + Let's Encrypt |

---

## Web UI

The web UI has three tabs:

### Portfolio
- Total equity, option buying power, total P&L, and open position count
- Chart.js cumulative P&L chart with 1W / 1M / ALL filter
- Win rate, average P&L, and total trades
- Closed trade history table with color-coded close-reason badges (profit target / stop loss / time stop / manual)

### Approval
- Claude-recommended TRADE spreads shown as cards with full stats (credit, max loss, ROI, PoP, strikes, expiry, profit target, stop loss)
- Contract quantity input with +/− stepper and suggested count based on buying power
- Approve / Skip buttons — Approve calls Tradier preview then places a live order and logs to SQLite
- WATCH and SKIP candidates listed below for visibility

### Positions
- Open positions with live P&L pulled from Tradier
- P&L progress bar toward profit target
- Auto-refresh every 60 seconds
- Manual close button with confirmation modal

---

## Pipeline

### Phase 1 — Build a Universe

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

**Step 00H:** Pull macro regime signals from FRED (VIX, yield curve, credit spreads). Classify market as Bull / Bear / Neutral. Cache results to `data/fred_cache.json` to avoid redundant API calls.

```bash
python3 pipeline/00h_macro_regime.py
```

---

### Phase 2 — Build Credit Spreads

**Step 01A:** Stream real-time bid/ask quotes from Tradier for the filtered stock list. Save mid-price, spread, and timestamp to `data/stock_prices.json`.

```bash
python3 pipeline/01a_get_prices_tradier.py
```

**Step 01B:** Fetch RSI, MACD, and Bollinger Bands for each ticker. Score technicals (positive range RSI 45–70; extremes >75 or <30 penalized). Uses 400-day history buffer. Save to `data/technicals.json`.

```bash
python3 pipeline/01b_get_technicals.py
```

**Step 01C:** Calculate peer z-scores for IV within GICS sectors. Normalize each ticker's IV against sector peers to identify relative richness/cheapness. Save to `data/peer_zscores.json`.

```bash
python3 pipeline/01c_peer_zscores.py
```

**Step 01D:** Call KronosAI volatility forecast API. Apply a ×0.80–1.20 multiplier to each spread's composite score based on the forecast. Falls back to neutral (×1.0) on API failure. Save adjusted scores to `data/kronos_scores.json`.

```bash
python3 pipeline/01d_kronos_forecast.py
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

**Step 05:** Pair strikes into Bull Put and Bear Call spreads. Filter short delta 15–35% (OTM probability). Calculate credit, max loss, and ROI. Use Black-Scholes with strike-specific IV to calculate PoP. Filter: ROI 5–50%, PoP ≥68%, credit ≥$0.60, max width $25. Save to `data/spreads.json`.

```bash
python3 pipeline/05_calculate_spreads_tradier.py
```

**Step 06:** Score each spread as `PoP × (1 + ROI/100)`, adjusted by Kronos multiplier. Keep highest-scoring spread per ticker. Categorize as ENTER (PoP ≥68% + ROI ≥15%), WATCH (PoP ≥68% + ROI ≥10%), or SKIP. Save to `data/ranked_spreads.json`.

```bash
python3 pipeline/06_rank_spreads_tradier.py
```

**Step 07:** Select top candidates — ENTER/WATCH fills first, SKIP fills remaining slots up to 9. Add sector mapping. Format into report table with rank, ticker, type, strikes, DTE, ROI, PoP, credit, max loss. Save to `data/report_table.json`.

```bash
python3 pipeline/07_build_report_tradier.py
```

**Step 08:** Send each trade to Claude AI with 3 news headlines and the macro regime signal. Claude applies 5W1H analysis, assigns a heat score 1–10, and recommends **TRADE / WATCH / SKIP**. Default is TRADE — SKIP only for confirmed dated binary catalysts within DTE. Save to `data/top9_analysis.json`.

```bash
python3 pipeline/08_claude_analysis.py
```

**Step 09:** Parse Claude's structured output. Extract ticker, type, strikes, DTE, ROI, PoP, heat score, and recommendation. Print formatted table. Save timestamped CSV for records.

```bash
python3 pipeline/09_format_trades_tradier.py
```

---

### Phase 3 — Execute & Monitor

**Step 10:** Run the full pipeline end-to-end (Steps 00A → 09), then serve results to the web UI for approval.

```bash
python3 pipeline/10_run_pipeline_tradier.py
```

**Step 11:** Parse Claude's final recommendations and prepare TRADE entries for the web UI approval queue. Format-agnostic parser handles any Claude output structure.

```bash
python3 pipeline/11_place_trades.py
```

**Step 12:** Position monitor. Runs every 5 minutes during market hours (9:35–15:55 ET). Checks every open position against three exit rules:
- **Profit target** — close when spread value drops to 60% of credit received (40% profit locked in)
- **Stop loss** — close when spread costs 2× the credit to close
- **Time stop** — close when DTE reaches 21 days

Places closing orders automatically via Tradier. Logs closed trades to SQLite and `data/closed_positions.json`.

```bash
python3 pipeline/12_position_monitor.py
```

---

## Deployment

The pipeline and web UI run on **AWS EC2** (t3.micro, Ubuntu 24.04, US East Ohio).

| Component | Detail |
|---|---|
| Web UI | Custom domain, nginx + Let's Encrypt SSL |
| Pipeline schedule | 9:25 AM ET, Monday–Friday (cron) |
| Service | `spreads-ui.service` (systemd, auto-starts on reboot) |
| Pipeline logs | `data/pipeline.log` |
| Web app logs | `journalctl -u spreads-ui` |

> **Note:** In November when EDT→EST, update the cron from `13:25 UTC` to `14:25 UTC`.

---

## Current Trading Parameters

| Parameter | Value |
|---|---|
| PoP floor | 68% |
| Min credit | $0.60 absolute |
| Max spread width | $25 |
| ENTER threshold | ROI ≥15%, PoP ≥68% |
| WATCH threshold | ROI ≥10%, PoP ≥68% |

---

## Quick Start (local development)

```bash
# 1. Clone the repo
git clone https://github.com/rydindirty/claude-option-trader.git
cd claude-option-trader

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure credentials
cp .env.example .env
# Edit .env with your Tradier token, Finnhub key, Anthropic key,
# and web UI credentials (WEB_USERNAME, WEB_PASSWORD, SESSION_SECRET)

# 4. Run the full pipeline
python3 pipeline/10_run_pipeline_tradier.py

# 5. Start the web UI
uvicorn web_app:app --host 0.0.0.0 --port 8000 --reload
```

---

## Data Flow

```
S&P 500 (503)
  → Price filter         → ~150 liquid stocks
  → Options filter       → ~100 with tradeable chains
  → IV filter            → ~60 in 15–80% IV range
  → Score & select       → Top 22 tickers
  → Sentiment filter     → Risky stocks removed
  → Macro regime (FRED)  → Bull / Bear / Neutral classification
  → Technicals           → RSI / MACD / Bollinger scored
  → Peer z-scores        → IV richness vs sector
  → Kronos AI            → Volatility forecast multiplier
  → Spread builder       → Thousands of spread candidates
  → Liquidity check      → Only liquid strikes
  → Greeks               → Delta-filtered OTM spreads
  → ROI/PoP filter       → High-probability trades only
  → Rank & top 9         → Best spread per ticker
  → Claude analysis      → Heat score + recommendation
  → Web UI approval      → Human approves contract count
  → Live order           → Tradier executes multi-leg spread
  → Position monitor     → Auto-exit at profit target / stop / DTE
  → SQLite analytics     → Full trade history and P&L tracking
```

---

## Credit

Original pipeline concept and architecture by **[Temple-Stuart](https://github.com/Temple-Stuart/temple-stuart-accounting)**.
This fork migrates market data and trade execution to the Tradier API, replaces GPT-4 analysis with Claude AI, and adds macro regime filtering, KronosAI volatility forecasting, a FastAPI web UI, and full cloud deployment.
