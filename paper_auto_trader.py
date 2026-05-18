#!/usr/bin/env python3
"""
Autonomous paper trading agent.

Runs after the daily pipeline (step 09). Reads report_table.json and
top9_analysis.json, selects the best ENTER trades that Claude confirms,
and records them as paper trades in the DB without human approval.

Starts with a simulated $1000 account. Tracks balance across sessions
by querying the DB for all PAPER-AUTO- prefixed trades.

Usage:
  python3 paper_auto_trader.py              # normal run (requires PAPER_TRADING=1)
  python3 paper_auto_trader.py --force      # bypass PAPER_TRADING check (testing)

Cron (EC2): runs 15 min after the 9:25 AM pipeline
  40 13 * * 1-5 cd /home/ubuntu/News_Spread_Engine && venv/bin/python3 paper_auto_trader.py >> data/auto_trader.log 2>&1
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
PAPER_ACCOUNT_FILE = DATA_DIR / "paper_account.json"
LOG_FILE = DATA_DIR / "auto_trader.log"

sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "pipeline"))

# Load .env manually so this script has no extra dependencies
def _load_env():
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env()

import db as _db

# ─── agent config ─────────────────────────────────────────────────────────────
STARTING_BALANCE = 1000.00   # simulated account funded at launch
MAX_RISK_PCT     = 0.25      # max fraction of available capital at risk per trade
MAX_OPEN_TRADES  = 4         # max concurrent paper positions
MAX_HEAT_SCORE   = 7         # skip trades with HEAT > this (too much catalyst risk)
NOTE_PREFIX      = "[PAPER AUTO]"


# ─── logging ──────────────────────────────────────────────────────────────────
def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ─── pipeline output helpers ──────────────────────────────────────────────────
def _is_fresh(timestamp_str: str, max_hours: float = 3.0) -> bool:
    try:
        ts = datetime.fromisoformat(timestamp_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        return age <= max_hours
    except Exception:
        return False


def _parse_recommendations(analysis_text: str, tickers: list) -> dict:
    """Extract TRADE/WAIT/SKIP for each ticker from Claude analysis text."""
    ticker_set = {t.upper() for t in tickers}
    recs = {}
    current_ticker = None
    pending_rec = False

    for line in analysis_text.split("\n"):
        stripped = line.strip()
        if pending_rec and current_ticker and current_ticker not in recs:
            for kw in ("TRADE", "WAIT", "SKIP"):
                if stripped.upper().startswith(kw):
                    recs[current_ticker] = kw
                    break
            pending_rec = False

        plain_tokens = re.sub(r"[#*`_]+", " ", stripped).split()
        has_digit = any(re.search(r"\d", t) for t in plain_tokens[:3])
        if has_digit:
            for tok in plain_tokens[:5]:
                candidate = re.sub(r"[^A-Z]", "", tok.upper())
                if candidate in ticker_set:
                    current_ticker = candidate
                    break

        if current_ticker is None:
            continue

        upper = stripped.upper()
        if "RECOMMENDATION:" in upper and current_ticker not in recs:
            after = upper.split("RECOMMENDATION:", 1)[1].strip().lstrip("* ")
            if after:
                for kw in ("TRADE", "WAIT", "SKIP"):
                    if after.startswith(kw):
                        recs[current_ticker] = kw
                        break
            else:
                pending_rec = True

    return recs


def _parse_heat_scores(analysis_text: str, tickers: list) -> dict:
    ticker_set = {t.upper() for t in tickers}
    scores = {}
    current_ticker = None

    for line in analysis_text.split("\n"):
        stripped = line.strip()
        plain_tokens = re.sub(r"[#*`_]+", " ", stripped).split()
        has_digit = any(re.search(r"\d", t) for t in plain_tokens[:3])
        if has_digit:
            for tok in plain_tokens[:5]:
                candidate = re.sub(r"[^A-Z]", "", tok.upper())
                if candidate in ticker_set:
                    current_ticker = candidate
                    break
        if current_ticker is None:
            continue
        upper = stripped.upper()
        if "HEAT:" in upper and current_ticker not in scores:
            try:
                after = upper.split("HEAT:", 1)[1].strip().lstrip("* |")
                heat_str = re.sub(r"[^0-9].*", "", after.split()[0])
                scores[current_ticker] = int(heat_str)
            except (ValueError, IndexError):
                pass

    return scores


# ─── paper account ────────────────────────────────────────────────────────────
def _init_account() -> dict:
    if not PAPER_ACCOUNT_FILE.exists():
        state = {
            "starting_balance": STARTING_BALANCE,
            "initialized_at": datetime.now().isoformat(),
        }
        with open(PAPER_ACCOUNT_FILE, "w") as f:
            json.dump(state, f, indent=2)
        _log(f"Paper account initialized: ${STARTING_BALANCE:.2f}")
    with open(PAPER_ACCOUNT_FILE) as f:
        return json.load(f)


def _paper_balance() -> dict:
    """Derive current paper account state from DB trades."""
    account = _init_account()
    starting = account["starting_balance"]

    _db.init_db()
    conn = sqlite3.connect(_db.DB_PATH)
    conn.row_factory = sqlite3.Row

    # Realized P&L: all closed auto-trader paper trades
    closed = conn.execute(
        "SELECT total_profit FROM trades WHERE status='closed' AND tradier_order_id LIKE 'PAPER-AUTO-%'"
    ).fetchall()
    realized = sum(r["total_profit"] or 0.0 for r in closed)

    # Reserved capital: max loss locked in open paper trades
    open_rows = conn.execute(
        "SELECT max_loss, contracts FROM trades "
        "WHERE status IN ('open','pending','closing') AND tradier_order_id LIKE 'PAPER-AUTO-%'"
    ).fetchall()
    reserved = sum(r["max_loss"] * r["contracts"] for r in open_rows)
    open_count = len(open_rows)
    conn.close()

    equity = starting + realized
    available = equity - reserved
    return {
        "starting": round(starting, 2),
        "realized_pnl": round(realized, 2),
        "reserved": round(reserved, 2),
        "equity": round(equity, 2),
        "available": round(available, 2),
        "open_count": open_count,
    }


# ─── trade helpers ────────────────────────────────────────────────────────────
def _parse_strikes(legs_str: str) -> tuple:
    parts = legs_str.replace("$", "").split("/")
    return float(parts[0]), float(parts[1])


def _build_option_symbol(ticker: str, expiration: str, opt_type: str, strike: float) -> str:
    exp = expiration.replace("-", "")[2:]
    otype = "P" if opt_type == "put" else "C"
    return f"{ticker}{exp}{otype}{int(strike * 1000):08d}"


def _already_open(ticker: str) -> bool:
    """True if we already have an active auto-paper position in this ticker."""
    _db.init_db()
    conn = sqlite3.connect(_db.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id FROM trades WHERE ticker=? AND status IN ('open','pending','closing') "
        "AND tradier_order_id LIKE 'PAPER-AUTO-%'",
        (ticker,),
    ).fetchall()
    conn.close()
    return len(rows) > 0


def _open_position_sectors(report_trades: list) -> set:
    """
    Return the set of sector ETF labels (e.g. 'XLK', 'XLU') that are
    already occupied by at least one active auto-paper position.
    Uses today's report_table as the sector lookup; tickers not in
    the report fall back to 'Unknown' and don't block anything.
    """
    sector_lookup = {t["ticker"]: t.get("sector", "Unknown") for t in report_trades}
    _db.init_db()
    conn = sqlite3.connect(_db.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ticker FROM trades WHERE status IN ('open','pending','closing') "
        "AND tradier_order_id LIKE 'PAPER-AUTO-%'"
    ).fetchall()
    conn.close()
    sectors: set = set()
    for row in rows:
        s = sector_lookup.get(row["ticker"], "Unknown")
        if s and s != "Unknown":
            sectors.add(s)
    return sectors


def _place_paper_trade(trade: dict, contracts: int, reason: str) -> int:
    """Insert a paper auto trade into the DB and return the row id."""
    short_strike, long_strike = _parse_strikes(trade["legs"])
    credit = float(trade["net_credit"].replace("$", ""))
    max_loss = float(trade["max_loss"].replace("$", ""))
    opt_type = "call" if "Bear Call" in trade.get("type", "") else "put"
    ticker, expiration = trade["ticker"], trade["exp_date"]
    order_id = f"PAPER-AUTO-{ticker}-{int(datetime.now().timestamp())}"

    regime = None
    try:
        with open(DATA_DIR / "macro_regime.json") as f:
            regime = json.load(f).get("regime_label")
    except Exception:
        pass

    row_id = _db.insert_open_trade({
        "ticker": ticker,
        "type": trade["type"],
        "short_strike": short_strike,
        "long_strike": long_strike,
        "expiration": expiration,
        "dte_at_entry": trade["dte"],
        "credit_received": credit,
        "max_profit": credit,
        "max_loss": max_loss,
        "contracts": contracts,
        "short_symbol": _build_option_symbol(ticker, expiration, opt_type, short_strike),
        "long_symbol": _build_option_symbol(ticker, expiration, opt_type, long_strike),
        "tradier_order_id": order_id,
        "opened_at": datetime.now().isoformat(),
        "profit_target_pct": 0.50,
        "stop_loss_pct": 1.50,
        "regime": regime,
    }, status="open")

    _db.save_trade_notes(row_id, f"{NOTE_PREFIX} {reason}")
    return row_id


# ─── main ─────────────────────────────────────────────────────────────────────
def main():
    force = "--force" in sys.argv

    _log("=" * 60)
    _log("PAPER AUTO TRADER — starting")

    # 1. Require paper trading mode (safety gate)
    paper_mode = os.getenv("PAPER_TRADING", "0").strip().lower() in ("1", "true", "yes")
    if not paper_mode and not force:
        _log("PAPER_TRADING is not set to 1 — aborting (use --force to override)")
        sys.exit(1)

    # 2. Load and validate pipeline output
    report_path = DATA_DIR / "report_table.json"
    analysis_path = DATA_DIR / "top9_analysis.json"

    if not report_path.exists() or not analysis_path.exists():
        _log("Pipeline output files missing — run the pipeline first")
        sys.exit(1)

    with open(report_path) as f:
        report = json.load(f)
    with open(analysis_path) as f:
        analysis = json.load(f)

    if not _is_fresh(analysis.get("timestamp", ""), max_hours=3):
        _log(f"Pipeline data is stale (ts={analysis.get('timestamp', 'missing')}) — skipping")
        sys.exit(0)

    trades = report.get("report_table", [])
    if not trades:
        _log("No trades in report_table.json — nothing to do")
        sys.exit(0)

    tickers = [t["ticker"] for t in trades]
    recs = _parse_recommendations(analysis["analysis"], tickers)
    heat = _parse_heat_scores(analysis["analysis"], tickers)

    _log(f"Pipeline: {len(trades)} trades | Claude recs: {recs}")

    # 3. Paper account state
    bal = _paper_balance()
    _log(
        f"Account: equity=${bal['equity']:.2f}  available=${bal['available']:.2f}  "
        f"open={bal['open_count']}  realized_pnl=${bal['realized_pnl']:+.2f}"
    )

    if bal["open_count"] >= MAX_OPEN_TRADES:
        _log(f"Max open positions ({MAX_OPEN_TRADES}) already active — no new entries")
        _log("=" * 60)
        return

    if bal["available"] < 100:
        _log(f"Available capital ${bal['available']:.2f} below $100 floor — skipping today")
        _log("=" * 60)
        return

    slots = MAX_OPEN_TRADES - bal["open_count"]

    # 4. Filter and rank candidates
    # Sort by pipeline score (descending) — best risk-adjusted PoP×ROI×multipliers first
    sorted_trades = sorted(trades, key=lambda x: float(x.get("score", 0)), reverse=True)

    # Sectors already occupied by open positions — block one trade per sector
    blocked_sectors = _open_position_sectors(trades)
    _log(f"Blocked sectors (open positions): {blocked_sectors or 'none'}")

    candidates = []
    for t in sorted_trades:
        ticker = t["ticker"]
        quant = t.get("decision", "")
        claude = recs.get(ticker, "")
        heat_score = heat.get(ticker, 0)
        max_loss_per_contract = float(t["max_loss"].replace("$", ""))
        risk_limit = bal["available"] * MAX_RISK_PCT
        trade_sector = t.get("sector", "Unknown")

        reason = None
        if quant != "ENTER":
            reason = f"quant={quant} (not ENTER)"
        elif claude != "TRADE":
            reason = f"claude={claude or 'no_rec'} (not TRADE)"
        elif heat_score > MAX_HEAT_SCORE:
            reason = f"heat={heat_score} > {MAX_HEAT_SCORE} (catalyst risk too high)"
        elif max_loss_per_contract > risk_limit:
            reason = (
                f"max_loss=${max_loss_per_contract:.0f} > "
                f"${risk_limit:.0f} ({MAX_RISK_PCT*100:.0f}% of ${bal['available']:.0f})"
            )
        elif _already_open(ticker):
            reason = "already have open position in this ticker"
        elif trade_sector != "Unknown" and trade_sector in blocked_sectors:
            reason = f"sector {trade_sector} already occupied by open position"

        if reason:
            _log(f"  SKIP {ticker}: {reason}")
        else:
            candidates.append(t)

    if not candidates:
        _log("No eligible candidates today after all filters")
        _log("=" * 60)
        return

    _log(f"{len(candidates)} candidate(s) for {slots} slot(s)")

    # 5. Place paper trades (up to available slots)
    placed = 0
    for trade in candidates[:slots]:
        ticker = trade["ticker"]
        max_loss = float(trade["max_loss"].replace("$", ""))
        score = trade.get("score", 0)
        reason = (
            f"score={score:.1f} pop={trade['pop']} roi={trade['roi']} "
            f"dte={trade['dte']} delta={trade.get('delta', '?')} "
            f"heat={heat.get(ticker, '?')} regime={trade.get('kronos_direction', 'n/a')}"
        )

        row_id = _place_paper_trade(trade, contracts=1, reason=reason)
        _log(
            f"  ENTER {ticker} {trade['type']} {trade['legs']} "
            f"x1 credit={trade['net_credit']} max_loss=${max_loss:.2f} "
            f"sector={trade.get('sector', '?')} | DB#{row_id}"
        )
        placed += 1

        # Block this sector for any further placements this run
        sector = trade.get("sector", "Unknown")
        if sector != "Unknown":
            blocked_sectors.add(sector)

        # Recheck balance after each placement to stay within limits
        bal = _paper_balance()
        if bal["open_count"] >= MAX_OPEN_TRADES or bal["available"] < 100:
            break

    _log(f"Done — {placed} paper trade(s) placed today")
    _log("=" * 60)


if __name__ == "__main__":
    main()
