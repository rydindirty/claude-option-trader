#!/usr/bin/env python3
"""
Paper trading strategy optimizer.

Queries all closed paper trades, sends performance data to Claude, and
saves suggested parameter changes to data/strategy_review_pending.json
for human approval via the Strategy tab in the web UI.

Changes are NOT applied automatically — they require explicit approval.

Trigger rules:
  - Fires automatically when >= 10 new trades have closed since last review
  - Enforces a 5-day minimum cooldown between reviews
  - Covers PAPER-AUTO- trades during paper trading; all trades once live

Usage:
  python3 paper_strategy_review.py              # respects trigger rules
  python3 paper_strategy_review.py --force      # bypass triggers
  python3 paper_strategy_review.py --dry-run    # show what would be saved, don't write

Review log: data/strategy_review_log.json
Pending review: data/strategy_review_pending.json
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
REVIEW_LOG  = DATA_DIR / "strategy_review_log.json"
PENDING     = DATA_DIR / "strategy_review_pending.json"
PARAMS_FILE = DATA_DIR / "strategy_params.json"

MIN_CLOSED_TRADES  = 10    # minimum new closed trades to trigger a review
COOLDOWN_DAYS      = 5     # minimum days between reviews

sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "pipeline"))

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

try:
    import anthropic
except ImportError:
    print("anthropic package not installed — run: pip install anthropic")
    sys.exit(1)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    print("ANTHROPIC_API_KEY not set in .env")
    sys.exit(1)

_TWILIO_SID   = os.getenv("TWILIO_SID", "")
_TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
_TWILIO_FROM  = os.getenv("TWILIO_FROM", "")
_TWILIO_TO    = os.getenv("TWILIO_TO", "")


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def _send_sms(body: str):
    if not all([_TWILIO_SID, _TWILIO_TOKEN, _TWILIO_FROM, _TWILIO_TO]):
        _log(f"[SMS] Twilio not configured — skipping: {body}")
        return
    try:
        import urllib.request, urllib.parse, base64
        data = urllib.parse.urlencode({"From": _TWILIO_FROM, "To": _TWILIO_TO, "Body": body}).encode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{_TWILIO_SID}/Messages.json", data=data
        )
        creds = base64.b64encode(f"{_TWILIO_SID}:{_TWILIO_TOKEN}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")
        urllib.request.urlopen(req, timeout=10)
        _log(f"[SMS] Sent: {body}")
    except Exception as e:
        _log(f"[SMS] Failed: {e}")


# ─── trigger checks ───────────────────────────────────────────────────────────
def _last_review_info() -> dict:
    """Return timestamp and trade count of the last completed review, or None."""
    if not REVIEW_LOG.exists():
        return {"timestamp": None, "count": 0}
    try:
        with open(REVIEW_LOG) as f:
            log = json.load(f)
        if not log:
            return {"timestamp": None, "count": 0}
        last = log[-1]
        return {"timestamp": last.get("timestamp"), "count": last.get("trades_analyzed", 0)}
    except Exception:
        return {"timestamp": None, "count": 0}


def _new_closed_since(since_ts: str) -> int:
    """Count closed paper trades after a given timestamp."""
    _db.init_db()
    conn = sqlite3.connect(_db.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT COUNT(*) as n FROM trades WHERE status='closed' "
        "AND tradier_order_id LIKE 'PAPER-AUTO-%' AND closed_at > ?",
        (since_ts,),
    ).fetchone()
    conn.close()
    return rows["n"] if rows else 0


def _should_run(force: bool) -> tuple:
    """Return (should_run: bool, reason: str)."""
    if force:
        return True, "forced"

    last = _last_review_info()

    # Count all closed paper trades for first-run check
    _db.init_db()
    conn = sqlite3.connect(_db.DB_PATH)
    conn.row_factory = sqlite3.Row
    total = conn.execute(
        "SELECT COUNT(*) as n FROM trades WHERE status='closed' AND tradier_order_id LIKE 'PAPER-AUTO-%'"
    ).fetchone()["n"]
    conn.close()

    if total < MIN_CLOSED_TRADES:
        return False, f"only {total} closed trades total (need {MIN_CLOSED_TRADES})"

    if last["timestamp"] is None:
        # First-ever review — check total count
        if total >= MIN_CLOSED_TRADES:
            return True, f"first review: {total} closed trades"
        return False, f"only {total} closed trades (need {MIN_CLOSED_TRADES})"

    # Cooldown check
    try:
        last_dt = datetime.fromisoformat(last["timestamp"])
        days_since = (datetime.now() - last_dt).days
        if days_since < COOLDOWN_DAYS:
            return False, f"cooldown: last review was {days_since}d ago (need {COOLDOWN_DAYS}d)"
    except Exception:
        pass

    # New-trade threshold check
    new_count = _new_closed_since(last["timestamp"])
    if new_count < MIN_CLOSED_TRADES:
        return False, f"only {new_count} new closed trades since last review (need {MIN_CLOSED_TRADES})"

    return True, f"{new_count} new trades since last review"


# ─── data loading ─────────────────────────────────────────────────────────────
def _load_all_closed_trades() -> list:
    """Return closed PAPER-AUTO trades (all time)."""
    _db.init_db()
    conn = sqlite3.connect(_db.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trades WHERE status='closed' AND tradier_order_id LIKE 'PAPER-AUTO-%' "
        "ORDER BY closed_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_current_params() -> dict:
    try:
        with open(PARAMS_FILE) as f:
            p = json.load(f)
        p.pop("_note", None)
        return p
    except Exception:
        return {"min_delta": 0.12, "max_delta": 0.22, "min_credit": 1.00,
                "enter_pop": 72, "enter_roi": 8, "watch_pop": 72, "watch_roi": 5}


# ─── performance analysis ─────────────────────────────────────────────────────
def _build_summary(trades: list) -> dict:
    if not trades:
        return {}

    def _avg(seq, key):
        vals = [t[key] for t in seq if t.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else 0

    wins   = [t for t in trades if (t["total_profit"] or 0) > 0]
    losses = [t for t in trades if (t["total_profit"] or 0) <= 0]

    close_reasons = {}
    for t in trades:
        r = t.get("close_reason") or "unknown"
        close_reasons[r] = close_reasons.get(r, 0) + 1

    by_type = {}
    for t in trades:
        tp = t.get("type", "unknown")
        if tp not in by_type:
            by_type[tp] = {"count": 0, "wins": 0, "total_profit": 0.0}
        by_type[tp]["count"] += 1
        if (t["total_profit"] or 0) > 0:
            by_type[tp]["wins"] += 1
        by_type[tp]["total_profit"] = round(by_type[tp]["total_profit"] + (t["total_profit"] or 0), 2)

    by_regime = {}
    for t in trades:
        reg = t.get("regime") or "unknown"
        if reg not in by_regime:
            by_regime[reg] = {"count": 0, "wins": 0}
        by_regime[reg]["count"] += 1
        if (t["total_profit"] or 0) > 0:
            by_regime[reg]["wins"] += 1

    return {
        "total_trades":   len(trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate_pct":   round(len(wins) / len(trades) * 100, 1),
        "total_pnl":      round(sum(t["total_profit"] or 0 for t in trades), 2),
        "avg_win":        _avg(wins, "total_profit"),
        "avg_loss":       _avg(losses, "total_profit"),
        "avg_profit_pct": _avg(trades, "profit_pct"),
        "avg_dte_entry":  _avg(trades, "dte_at_entry"),
        "close_reasons":  close_reasons,
        "by_type":        by_type,
        "by_regime":      by_regime,
    }


# ─── Claude analysis ──────────────────────────────────────────────────────────
BOUNDS = {
    "min_delta":  (0.08, 0.18),
    "max_delta":  (0.18, 0.30),
    "min_credit": (0.60, 1.50),
    "enter_pop":  (68,   80),
    "enter_roi":  (5,    15),
    "watch_pop":  (65,   78),
    "watch_roi":  (3,    10),
}


def _ask_claude(summary: dict, params: dict) -> dict:
    prompt = f"""You are a quantitative options trading strategy analyst optimizing a paper-trading credit spread system.

CURRENT STRATEGY PARAMETERS:
{json.dumps(params, indent=2)}

CLOSED TRADE PERFORMANCE ({summary.get('total_trades', 0)} trades):
{json.dumps(summary, indent=2)}

PARAMETER BOUNDS (never suggest outside these):
{json.dumps({k: {"min": v[0], "max": v[1]} for k, v in BOUNDS.items()}, indent=2)}

ANALYSIS RULES:
- Adjust only parameters with clear statistical support from the data
- Make conservative changes (max ±2 per numeric param per review)
- If win_rate_pct < 50%: prioritize raising enter_pop, lowering max_delta
- If stop_loss is the most common close_reason: raise min_credit or lower max_delta
- If profit_target is the most common close_reason: strategy is working, reinforce current params
- If win_rate_pct > 65% and avg_win is low relative to avg_loss: consider relaxing enter_roi slightly to capture more trades

Respond with ONLY valid JSON — no markdown, no explanation outside the JSON:
{{
  "rationale": "2-3 sentence explanation of what the data shows and why you recommend these specific changes",
  "changes": {{
    "min_delta": <float or null>,
    "max_delta": <float or null>,
    "min_credit": <float or null>,
    "enter_pop": <int or null>,
    "enter_roi": <int or null>,
    "watch_pop": <int or null>,
    "watch_roi": <int or null>
  }}
}}

Use null for any parameter you are NOT recommending a change to.
"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="You are a quantitative trading strategy optimizer. Respond only with valid JSON.",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ─── save / log ───────────────────────────────────────────────────────────────
def _save_pending(summary: dict, params: dict, suggestion: dict):
    """Write the pending review for web UI approval."""
    changes = {k: v for k, v in suggestion.get("changes", {}).items() if v is not None}

    # Build before/after table for the UI
    proposed = {}
    for param, new_val in changes.items():
        lo, hi = BOUNDS.get(param, (None, None))
        if lo is not None:
            if isinstance(new_val, float):
                new_val = round(max(lo, min(hi, new_val)), 2)
            else:
                new_val = int(max(lo, min(hi, new_val)))
        proposed[param] = {"before": params.get(param), "after": new_val}

    pending = {
        "review_id":       datetime.now().isoformat(),
        "status":          "pending",
        "trades_analyzed": summary["total_trades"],
        "win_rate_pct":    summary["win_rate_pct"],
        "total_pnl":       summary["total_pnl"],
        "close_reasons":   summary["close_reasons"],
        "rationale":       suggestion.get("rationale", ""),
        "proposed":        proposed,
        "current_params":  params,
    }
    with open(PENDING, "w") as f:
        json.dump(pending, f, indent=2)
    return pending


def _append_log(summary: dict, params: dict, suggestion: dict, status: str = "pending"):
    log = []
    if REVIEW_LOG.exists():
        try:
            with open(REVIEW_LOG) as f:
                log = json.load(f)
        except Exception:
            log = []
    log.append({
        "timestamp":       datetime.now().isoformat(),
        "status":          status,
        "trades_analyzed": summary.get("total_trades"),
        "win_rate_pct":    summary.get("win_rate_pct"),
        "total_pnl":       summary.get("total_pnl"),
        "params_before":   params,
        "rationale":       suggestion.get("rationale", ""),
        "suggested":       suggestion.get("changes", {}),
    })
    with open(REVIEW_LOG, "w") as f:
        json.dump(log, f, indent=2)


# ─── main ─────────────────────────────────────────────────────────────────────
def main():
    force   = "--force"   in sys.argv
    dry_run = "--dry-run" in sys.argv

    _log("=" * 60)
    _log("PAPER STRATEGY REVIEW — starting")

    should, reason = _should_run(force)
    if not should:
        _log(f"Skipping: {reason}")
        _log("=" * 60)
        return

    _log(f"Trigger: {reason}")

    trades = _load_all_closed_trades()
    _log(f"Closed PAPER-AUTO trades: {len(trades)}")

    summary = _build_summary(trades)
    _log(
        f"Performance: {summary['win_rate_pct']}% win rate | "
        f"P&L ${summary['total_pnl']:+.2f} | "
        f"avg win ${summary['avg_win']:.2f} | avg loss ${summary['avg_loss']:.2f}"
    )
    _log(f"Close reasons: {summary['close_reasons']}")

    params = _load_current_params()
    _log(f"Current params: {params}")

    _log("Calling Claude for analysis...")
    try:
        suggestion = _ask_claude(summary, params)
    except Exception as e:
        _log(f"Claude API error: {e}")
        sys.exit(1)

    _log(f"Rationale: {suggestion.get('rationale', '')}")
    changes = {k: v for k, v in suggestion.get("changes", {}).items() if v is not None}
    _log(f"Suggested changes: {changes}")

    if dry_run:
        _log("[DRY RUN] Would save pending review — not writing")
        _log("=" * 60)
        return

    if not changes:
        _log("No parameter changes recommended — strategy on track")
        _append_log(summary, params, suggestion, status="no_changes")
        _log("=" * 60)
        return

    pending = _save_pending(summary, params, suggestion)
    _append_log(summary, params, suggestion, status="pending")

    n = len(changes)
    msg = (
        f"DickTrades: Strategy review ready — {summary['total_trades']} trades analyzed, "
        f"{summary['win_rate_pct']}% win rate. {n} param change{'s' if n > 1 else ''} "
        f"pending your approval in the Strategy tab."
    )
    _send_sms(msg)
    _log(f"Pending review saved to {PENDING}")
    _log("=" * 60)


if __name__ == "__main__":
    main()
