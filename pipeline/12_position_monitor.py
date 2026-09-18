"""
Step 12: Position Monitor
Runs every minute during market hours. Checks all open positions against
exit rules that differ by position type (credit spread vs. debit spread —
see pos["type"] in ("Long Call", "Long Put") throughout this file):

  1. Profit target  — credit spreads: close when spread value drops to
                      pos["profit_target_pct"] of credit received.
                      Debit spreads: close when spread value rises to
                      pos["profit_target_pct"] of max profit potential
                      (width - debit) above the debit paid.
  2. Credit spreads: width hard cap — close if spread value exceeds 80% of
                      max width (sole catastrophic backstop; delta stop
                      removed after backtest showed it closes recoverable
                      trades at peak drawdown — see data/backtest_results.json
                      2026-06-02).
     Debit spreads:  early stop-loss — close if spread value falls to
                      pos["stop_loss_pct"] of debit lost. Loss is already
                      hard-capped at the debit paid by construction; this
                      just frees capital from a clear loser early rather
                      than riding it to the (already-capped) full loss.
  4. Time stop      — hard close when DTE < 21 (past deadline)
                      on DTE = 21: hold through the day; after 3:30 PM ET
                      close only if the position is unfavorable (credit
                      spread: value above credit received; debit spread:
                      value below debit paid)
Sends a clear terminal alert and places the closing order
automatically via Tradier.
"""
import os
import sys
import time
import traceback
import requests
from datetime import datetime, date
from zoneinfo import ZoneInfo

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import TRADIER_TOKEN, TRADIER_ENV, get_tradier_session, TRADIER_BASE_URL, TRADIER_HEADERS, TRADIER_ACCOUNT_ID
import db

# ── Paper trading mode ─────────────────────────────────────────
PAPER_TRADING = os.getenv("PAPER_TRADING", "0").strip().lower() in ("1", "true", "yes")

# ── Twilio SMS ─────────────────────────────────────────────────
_TWILIO_SID  = os.getenv("TWILIO_SID", "")
_TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
_TWILIO_FROM = os.getenv("TWILIO_FROM", "")
_TWILIO_TO   = os.getenv("TWILIO_TO", "")

def send_sms(body: str):
    if not all([_TWILIO_SID, _TWILIO_TOKEN, _TWILIO_FROM, _TWILIO_TO]):
        print(f"[SMS] Twilio not configured — skipping: {body}")
        return
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{_TWILIO_SID}/Messages.json",
            auth=(_TWILIO_SID, _TWILIO_TOKEN),
            data={"From": _TWILIO_FROM, "To": _TWILIO_TO, "Body": body},
            timeout=10,
        )
        r.raise_for_status()
        print(f"[SMS] Sent: {body}")
    except Exception as e:
        print(f"[SMS] Failed: {e}")

# ── Tradier config ─────────────────────────────────────────────
# BASE_URL and HEADERS are now imported from config
_session = get_tradier_session()  # SSL-verified session for Tradier API

# ── Market hours (ET) ──────────────────────────────────────────
MARKET_OPEN_HOUR  = 10
MARKET_OPEN_MIN   = 0    # 30 min after open to avoid wide bid-ask and gap-out stops
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MIN  = 55   # 5 min before close


_ET = ZoneInfo("America/New_York")


def is_market_hours():
    """Return True if current ET time is within market hours."""
    now = datetime.now(_ET)
    open_time  = now.replace(hour=MARKET_OPEN_HOUR,
                              minute=MARKET_OPEN_MIN, second=0, microsecond=0)
    close_time = now.replace(hour=MARKET_CLOSE_HOUR,
                              minute=MARKET_CLOSE_MIN, second=0, microsecond=0)
    return open_time <= now <= close_time


def load_positions():
    """Load open positions from data/trades.db."""
    return db.load_open_positions()


def save_positions(positions):
    """No-op: positions are closed in-place via close_trade(); no bulk save needed."""
    pass


def get_spread_value(short_symbol, long_symbol, max_value=None, is_debit=False):
    """
    Fetch current bid/ask for both legs and return the spread's current value.

    "short"/"long" always mean sold/bought, the same for every position type,
    so the closing actions are identical either way: buy_to_close the short
    leg (pay its ask), sell_to_close the long leg (receive its bid). What
    differs is which number is useful to callers:

    - Credit spread (is_debit=False): value = cost to close = short_ask - long_bid
      (what you'd pay to buy it back). Decaying toward 0 is favorable.
    - Debit spread (is_debit=True):   value = proceeds from closing = long_bid - short_ask
      (what you'd receive selling it). Rising toward the width is favorable.

    These are exact negatives of each other computed from the same quotes.

    Falls back to mid-price if ask or bid is missing, but ONLY when the
    leg has *some* real quote data. If a leg has no bid AND no ask at
    all, that leg's true value is unknown — treating it as $0 silently
    would bias the computed spread value (understating the credit-spread
    hedge, or crediting a naked short with a bogus windfall), so we
    refuse to price the spread instead of guessing.

    max_value, if given, hard-clamps the result: a vertical spread can
    never be worth more than its own strike width, so any quote that
    implies otherwise (stale/illiquid marks) is capped there. The result
    is also floored at 0 for the same reason.

    Returns None if no reliable price data is available.
    """
    symbols = f"{short_symbol},{long_symbol}"
    try:
        r = _session.get(
            f"{TRADIER_BASE_URL}/markets/quotes",
            headers={"Authorization": f"Bearer {TRADIER_TOKEN}",
                     "Accept": "application/json"},
            params={"symbols": symbols},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        quotes = data.get("quotes", {}).get("quote", [])
        if isinstance(quotes, dict):
            quotes = [quotes]

        quote_map = {q["symbol"]: q for q in quotes if "symbol" in q}

        # Warn about any symbol missing from the response entirely
        for sym in (short_symbol, long_symbol):
            if sym not in quote_map:
                print(f"   ⚠️  Symbol not in quote response: {sym}")

        short_q = quote_map.get(short_symbol, {})
        long_q  = quote_map.get(long_symbol, {})

        # A leg with no bid AND no ask has no usable market at all —
        # don't let it silently become $0 in the formula below.
        for label, q, sym in (("short", short_q, short_symbol),
                               ("long",  long_q,  long_symbol)):
            bid = float(q.get("bid") or 0)
            ask = float(q.get("ask") or 0)
            if bid <= 0 and ask <= 0:
                print(f"   ⚠️  {sym} ({label} leg): no bid AND no ask — "
                      f"true value unknown, refusing to price this spread")
                return None

        def best_price(q, prefer):
            """Return preferred side; fall back to mid if zero/missing."""
            val = float(q.get(prefer) or 0)
            if val > 0:
                return val, False
            bid = float(q.get("bid") or 0)
            ask = float(q.get("ask") or 0)
            mid = round((bid + ask) / 2, 2) if (bid + ask) > 0 else 0
            return mid, True

        short_ask, short_fb = best_price(short_q, prefer="ask")
        long_bid,  long_fb  = best_price(long_q,  prefer="bid")

        if short_fb:
            print(f"   ℹ️  {short_symbol}: no ask — using mid ${short_ask:.2f}")
        if long_fb:
            print(f"   ℹ️  {long_symbol}: no bid — using mid ${long_bid:.2f}")

        if is_debit:
            # Proceeds from selling to close a long debit spread.
            value = round(long_bid - short_ask, 2)
        else:
            # Cost to close a short credit spread: buy back short (pay ask),
            # sell long (receive bid).
            value = round(short_ask - long_bid, 2)

        # Sanity bound: a vertical spread's value can never exceed its
        # own width, and can't be negative. Clamp instead of trusting a
        # bad/stale quote outright (this is what let a $5-wide spread
        # get "closed" at $8.60 on 2026-07-24 — see MSFT trades 51/52).
        if max_value is not None and value > max_value:
            print(f"   ⚠️  Computed value ${value:.2f} exceeds "
                  f"spread width ${max_value:.2f} — clamping (bad/stale quote)")
            value = max_value
        if value < 0:
            print(f"   ⚠️  Computed value ${value:.2f} is negative — clamping to $0")
            value = 0.0

        return value

    except Exception as e:
        print(f"   ⚠️  Quote error: {e}")
        return None


def get_short_delta(short_symbol: str) -> float | None:
    """
    Fetch current delta for the short leg via Tradier greeks.
    Returns the raw signed delta (negative for puts, positive for calls),
    or None if the data is unavailable.
    """
    try:
        r = _session.get(
            f"{TRADIER_BASE_URL}/markets/quotes",
            headers={"Authorization": f"Bearer {TRADIER_TOKEN}",
                     "Accept": "application/json"},
            params={"symbols": short_symbol, "greeks": "true"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        quote = data.get("quotes", {}).get("quote", {})
        if isinstance(quote, list):
            quote = quote[0] if quote else {}
        greeks = quote.get("greeks") or {}
        delta = greeks.get("delta")
        return float(delta) if delta is not None else None
    except Exception as e:
        print(f"   ⚠️  Delta fetch error for {short_symbol}: {e}")
        return None


def place_closing_order(position, current_value=None):
    """
    Place a closing multileg order to exit the spread.
    Uses the actual live spread value as the limit price.
    In PAPER_TRADING mode, skips the Tradier API and returns a mock response.
    Raises ValueError if no current value is available.
    """
    contracts = position["contracts"]
    ticker    = position["ticker"]

    # Require a real market price — never make up a price
    if not current_value or current_value <= 0:
        raise ValueError(
            f"Cannot place closing order for {ticker} without a valid market price"
        )

    # Paper trading — log the close but don't touch Tradier
    if PAPER_TRADING:
        print(f"  📄 [PAPER] Would close {ticker} at ${current_value:.2f}")
        return {"order": {"id": f"PAPER-CLOSE-{ticker}-{int(time.time())}", "status": "filled"}}

    limit_price = round(current_value, 2)
    is_debit = position["type"] in ("Long Call", "Long Put")

    # Closing actions are the same either way (buy_to_close the short leg,
    # sell_to_close the long leg) — what differs is the order's net type:
    # closing a short credit spread costs a debit; closing a long debit
    # spread nets a credit.
    payload = {
        "class":             "multileg",
        "symbol":            ticker,
        "type":              "credit" if is_debit else "debit",
        "duration":          "day",
        "price":             f"{limit_price:.2f}",
        "option_symbol[0]":  position["short_symbol"],
        "side[0]":           "buy_to_close",
        "quantity[0]":       str(contracts),
        "option_symbol[1]":  position["long_symbol"],
        "side[1]":           "sell_to_close",
        "quantity[1]":       str(contracts),
        "preview":           "false"
    }

    r = _session.post(
        f"{TRADIER_BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/orders",
        headers=TRADIER_HEADERS,
        data=payload
    )
    r.raise_for_status()
    return r.json()

def log_closed_trade(position, close_reason, close_value, order_response):
    """Mark position closed in data/trades.db and return total P&L."""
    contracts = position["contracts"]
    is_debit  = position["type"] in ("Long Call", "Long Put")

    if is_debit:
        # Long debit spread: profit = what you receive closing - what you paid.
        cost_basis = position["debit_paid"]
        profit     = round((close_value - cost_basis) * contracts * 100, 2)
        profit_pct = round((close_value - cost_basis) / cost_basis * 100, 1)
    else:
        # Short credit spread: profit = what you collected - what it costs to close.
        cost_basis = position["credit_received"]
        profit     = round((cost_basis - close_value) * contracts * 100, 2)
        profit_pct = round((cost_basis - close_value) / cost_basis * 100, 1)

    profit_per_contract = round(
        ((close_value - cost_basis) if is_debit else (cost_basis - close_value)) * 100, 2
    )
    db.close_trade(
        trade_id            = position["id"],
        close_reason        = close_reason,
        close_value         = close_value,
        profit_per_contract = profit_per_contract,
        total_profit        = profit,
        profit_pct          = profit_pct,
        close_order_id      = order_response.get("order", {}).get("id", "unknown"),
    )

    return profit


def check_positions():
    """
    Main check loop — evaluate every open position against
    the three exit rules and close if triggered.
    """
    positions = load_positions()

    if not positions:
        print(f"   [{datetime.now().strftime('%H:%M:%S')}] "
              f"No open positions to monitor")
        return

    print(f"\n{'='*60}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] "
          f"Checking {len(positions)} position(s)")
    print(f"{'='*60}")

    remaining = []
    today = date.today()

    for pos in positions:
        ticker       = pos["ticker"]
        is_debit     = pos["type"] in ("Long Call", "Long Put")
        cost_basis   = pos["debit_paid"] if is_debit else pos["credit_received"]
        cost_label   = "Debit" if is_debit else "Credit"
        exp          = date.fromisoformat(pos["expiration"])
        dte          = (exp - today).days
        spread_width = abs(pos["short_strike"] - pos["long_strike"])

        print(f"\n  {ticker} {pos['type']} "
              f"${pos['short_strike']:.0f}/$"
              f"{pos['long_strike']:.0f} "
              f"| {cost_label}: ${cost_basis:.2f} | DTE: {dte}")

        # ── Minimum hold time — don't exit within 10 min of entry ──
        opened_at = datetime.fromisoformat(pos.get("opened_at", "2000-01-01"))
        minutes_held = (datetime.now() - opened_at).total_seconds() / 60
        if minutes_held < 10:
            print(f"  ⏳ Hold period — position is only {minutes_held:.0f} min old (min 10)")
            remaining.append(pos)
            continue

        # ── Rule 3: Hard time stop (DTE < 21) ─────────────────
        if dte < 21:
            print(f"  ⏰ TIME STOP triggered — {dte} DTE (past deadline)")
            close_val = get_spread_value(
                pos["short_symbol"], pos["long_symbol"],
                max_value=spread_width, is_debit=is_debit)
            if close_val is None:
                print(f"  ⚠️  Could not get quote for time stop — skipping")
                remaining.append(pos)
                continue
            try:
                response = place_closing_order(pos, close_val)
                profit   = log_closed_trade(
                    pos, "time_stop", close_val, response)
                print(f"  ✅ Closed at ${close_val:.2f} | "
                      f"P&L: ${profit:.2f}")
                send_sms(f"DickTrades CLOSED {ticker} | Time Stop | "
                         f"${close_val:.2f} | P&L: ${profit:+.2f}")
            except Exception as e:
                print(f"  ❌ Close failed: {e}")
                remaining.append(pos)
            continue

        # ── Rule 3b: 21 DTE — end-of-day price action check ───
        # Hold through the 21st day. After 3:30 PM ET, close only if the
        # position is unfavorable (for a credit spread: value above credit
        # received; for a debit spread: value below what was paid). If
        # favorable, let profit/stop rules below handle it.
        if dte == 21:
            now = datetime.now(_ET)
            is_eod = now.hour > 15 or (now.hour == 15 and now.minute >= 30)
            if is_eod:
                close_val = get_spread_value(
                    pos["short_symbol"], pos["long_symbol"],
                    max_value=spread_width, is_debit=is_debit)
                if close_val is None:
                    print(f"  ⚠️  21 DTE EOD: no quote — holding")
                    remaining.append(pos)
                    continue
                if is_debit:
                    eod_pct    = (close_val - cost_basis) / cost_basis * 100
                    unfavorable = close_val < cost_basis
                else:
                    eod_pct    = (cost_basis - close_val) / cost_basis * 100
                    unfavorable = close_val > cost_basis
                print(f"  ⏰ 21 DTE END-OF-DAY | "
                      f"Spread: ${close_val:.2f} | P&L: {eod_pct:.1f}%")
                if unfavorable:
                    print(f"  📉 Unfavorable — spread {'below' if is_debit else 'above'} "
                          f"{cost_label.lower()} (${cost_basis:.2f}). Closing to limit loss.")
                    try:
                        response = place_closing_order(pos, close_val)
                        profit   = log_closed_trade(
                            pos, "time_stop_eod", close_val, response)
                        print(f"  ✅ Closed at ${close_val:.2f} | "
                              f"P&L: ${profit:.2f}")
                        send_sms(f"DickTrades CLOSED {ticker} | 21 DTE EOD | "
                                 f"${close_val:.2f} | P&L: ${profit:+.2f}")
                    except Exception as e:
                        print(f"  ❌ Close failed: {e}")
                        remaining.append(pos)
                    continue
                else:
                    print(f"  📈 Favorable — spread {'at/above' if is_debit else 'at/below'} "
                          f"{cost_label.lower()} (${cost_basis:.2f}). Holding for profit target.")
                    # fall through to profit/stop checks below
            else:
                print(f"  📅 21 DTE — holding through end of day "
                      f"(EOD check activates after 3:30 PM)")
                # fall through to profit/stop checks below

        # ── Get current spread value ───────────────────────────
        current_value = get_spread_value(
            pos["short_symbol"], pos["long_symbol"],
            max_value=spread_width, is_debit=is_debit)

        if current_value is None:
            print(f"  ⚠️  Could not get quote — skipping this check")
            remaining.append(pos)
            continue

        if is_debit:
            profit_pct = (current_value - cost_basis) / cost_basis * 100
        else:
            profit_pct = (cost_basis - current_value) / cost_basis * 100
        print(f"  Current spread value: ${current_value:.2f} | "
              f"P&L: {profit_pct:.1f}% of max profit")

        # ── Rule 1: Profit target ────────────────────────────────
        # Credit spread: target = credit * (1 - pct), close when value <= target
        #   (value decaying toward zero is favorable).
        # Debit spread:  target = debit * (1 + pct * (width/debit - 1))... expressed
        #   directly as debit + pct * max_profit_potential, close when value >= target
        #   (value rising toward the width is favorable).
        # Trailing stop removed 2026-07-09 — it triggered at 25% and capped winners
        # short of the 50% target (e.g. closed a leg at 21% for +$12 vs a $28 target).
        # Winners now close at the target; the DTE entry fix gives them the runway.
        if is_debit:
            max_profit_potential = spread_width - cost_basis
            target = cost_basis + pos["profit_target_pct"] * max_profit_potential
            target_hit = current_value >= target
        else:
            target = cost_basis * (1 - pos["profit_target_pct"])
            target_hit = current_value <= target

        if target_hit:
            print(f"  🎯 PROFIT TARGET hit — "
                  f"{profit_pct:.1f}% profit locked in")
            try:
                response = place_closing_order(pos, current_value)
                profit   = log_closed_trade(
                    pos, "profit_target", current_value, response)
                print(f"  ✅ Closed at ${current_value:.2f} | "
                      f"P&L: ${profit:.2f}")
                send_sms(f"DickTrades CLOSED {ticker} | Profit Target | "
                         f"${current_value:.2f} | P&L: ${profit:+.2f}")
            except Exception as e:
                print(f"  ❌ Close failed: {e}")
                remaining.append(pos)
            continue

        # ── Rule 2: REMOVED for credit spreads (was: delta stop ≥ 0.50) ──
        # Backtest of 32 closed paper trades (2026-06-02) showed the delta
        # stop closes trades at peak drawdown that often recover by expiry.
        # No-stop (width cap + profit target + time stop only) simulated
        # -$457 vs delta-stop -$2,816 on the same 32 trades.
        # Delta is now reported for telemetry but does NOT trigger a close.
        short_delta = get_short_delta(pos["short_symbol"])

        if is_debit:
            # ── Rule 3b (debit): early stop-loss ──────────────────────
            # A debit spread's loss is already hard-capped at the debit paid
            # by construction (there's no width-blowup risk like a short
            # credit spread has) — but cutting a clear loser early frees up
            # capital for the next trade instead of waiting out a position
            # already trending toward its full, already-capped loss. Revives
            # the stop_loss_pct column, which was vestigial for credit spreads.
            stop_value = cost_basis * (1 - pos["stop_loss_pct"])
            if current_value <= stop_value:
                print(f"  🛑 STOP LOSS triggered — "
                      f"spread at ${current_value:.2f} ≤ "
                      f"${stop_value:.2f} ({pos['stop_loss_pct']:.0%} of debit lost)")
                try:
                    response = place_closing_order(pos, current_value)
                    profit   = log_closed_trade(
                        pos, "stop_loss", current_value, response)
                    print(f"  ✅ Closed at ${current_value:.2f} | "
                          f"P&L: ${profit:.2f}")
                    send_sms(f"DickTrades CLOSED {ticker} | Stop Loss | "
                             f"${current_value:.2f} | P&L: ${profit:+.2f}")
                except Exception as e:
                    print(f"  ❌ Close failed: {e}")
                    remaining.append(pos)
                continue

            delta_str = f"|Δ|={abs(short_delta):.2f}" if short_delta is not None else "|Δ|=?"
            print(f"  ✓  Holding — profit target at "
                  f"${target:.2f} | {delta_str} (info only) | "
                  f"stop loss at ${stop_value:.2f}")
            remaining.append(pos)
            continue

        # ── Rule 3b (credit): Spread-width hard cap (gap-through protection) ──
        width_cap = round(spread_width * MAX_WIDTH_PCT, 2)
        if current_value >= width_cap:
            print(f"  🚨 WIDTH CAP triggered — "
                  f"spread at ${current_value:.2f} ≥ "
                  f"80% of width ${width_cap:.2f} "
                  f"(width ${spread_width:.0f})")
            try:
                response = place_closing_order(pos, current_value)
                profit   = log_closed_trade(
                    pos, "width_cap", current_value, response)
                print(f"  ✅ Closed at ${current_value:.2f} | "
                      f"P&L: ${profit:.2f}")
                send_sms(f"DickTrades CLOSED {ticker} | Width Cap | "
                         f"${current_value:.2f} | P&L: ${profit:+.2f}")
            except Exception as e:
                print(f"  ❌ Close failed: {e}")
                remaining.append(pos)
            continue

        # ── No trigger — keep position open ───────────────────
        delta_str = f"|Δ|={abs(short_delta):.2f}" if short_delta is not None else "|Δ|=?"
        print(f"  ✓  Holding — profit target at "
              f"${target:.2f} | {delta_str} (info only) | "
              f"width cap at ${width_cap:.2f}")
        remaining.append(pos)

    # Save updated positions (closed ones removed)
    save_positions(remaining)
    closed_count = len(positions) - len(remaining)
    if closed_count > 0:
        print(f"\n  📊 Closed {closed_count} position(s) this check")


# ── Exit rule constants ────────────────────────────────────────────────────────
# DELTA_STOP removed 2026-06-02 — see backtest_results.json. Delta is now
# fetched for telemetry only via get_short_delta(); does not trigger a close.
MAX_WIDTH_PCT = 0.80   # hard cap: close if spread value > 80% of max width


LOCK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "monitor.pid")


def acquire_lock():
    """
    Write current PID to lock file. If a lock file exists and the
    recorded PID is still running, exit immediately to prevent
    duplicate monitors.
    """
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                existing_pid = int(f.read().strip())
            # Check if that process is still alive
            os.kill(existing_pid, 0)
            # If we get here the process exists — abort
            print(f"❌ Monitor already running (PID {existing_pid}). Exiting.")
            print(f"   If that process is dead, delete {LOCK_FILE} and retry.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            # Stale lock — previous process died without cleanup
            print(f"   Stale lock file found (PID gone). Overwriting.")

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))


def release_lock():
    """Remove the PID lock file on clean exit."""
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


def run_monitor(interval_minutes=1):
    """
    Run the monitor loop continuously during market hours.
    Checks every interval_minutes minutes.
    """
    acquire_lock()

    now_et = datetime.now(_ET)
    open_positions = load_positions()
    print("=" * 60)
    print("🔍 POSITION MONITOR STARTED")
    print(f"   PID: {os.getpid()}")
    print(f"   Current ET time: {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"   Checking every {interval_minutes} minute(s)")
    print(f"   Market hours: "
          f"{MARKET_OPEN_HOUR}:{MARKET_OPEN_MIN:02d} - "
          f"{MARKET_CLOSE_HOUR}:{MARKET_CLOSE_MIN:02d} ET")
    print(f"   Open positions in DB: {len(open_positions)}")
    print(f"   Exit rules:")
    print(f"     Profit target: per-position pct of max profit (credit or debit)")
    print(f"     Credit spreads: width cap {int(MAX_WIDTH_PCT*100)}% (hard backstop)")
    print(f"     Debit spreads:  early stop-loss at pct of debit lost (per-position)")
    print(f"     Delta stop:    REMOVED (telemetry only)")
    print(f"     Time stop:     hard close at DTE < 21")
    print(f"                    DTE = 21: EOD check after 3:30 PM")
    print("=" * 60)
    print("\nPress Ctrl+C to stop")
    print("To manually close a position: open a new terminal and run:")
    print("  python3 pipeline/12_position_monitor.py --close\n")

    try:
        while True:
            try:
                if is_market_hours():
                    check_positions()
                else:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                          f"Outside market hours — waiting...")

                time.sleep(interval_minutes * 60)

            except KeyboardInterrupt:
                print("\n\n🛑 Monitor stopped by user")
                break
            except Exception as e:
                print(f"❌ Monitor error: {e}")
                traceback.print_exc()
                print("   Retrying in 60 seconds...")
                time.sleep(60)
    finally:
        release_lock()


def manual_close():
    """
    Interactive manual close — run in a separate terminal while the monitor
    is active. Lists open positions, prompts for selection, fetches live
    price, requires confirmation, then places a closing order.
    """
    positions = load_positions()
    if not positions:
        print("No open positions found.")
        return

    today = date.today()
    print("\n" + "=" * 60)
    print("MANUAL POSITION CLOSE")
    print("=" * 60)
    for i, pos in enumerate(positions, start=1):
        dte = (date.fromisoformat(pos["expiration"]) - today).days
        is_debit_row = pos["type"] in ("Long Call", "Long Put")
        cost_row = pos["debit_paid"] if is_debit_row else pos["credit_received"]
        cost_row_label = "Debit" if is_debit_row else "Credit"
        print(f"  [{i}] {pos['ticker']} {pos['type']}  "
              f"${pos['short_strike']:.0f}/${pos['long_strike']:.0f}  "
              f"Exp {pos['expiration']}  DTE {dte}  "
              f"{cost_row_label} ${cost_row:.2f}  "
              f"Contracts {pos['contracts']}")
    print(f"  [0] Cancel")
    print()

    try:
        choice = int(input("Select position to close: ").strip())
    except (ValueError, EOFError):
        print("Invalid input — aborting.")
        return

    if choice == 0:
        print("Cancelled.")
        return
    if choice < 1 or choice > len(positions):
        print("Invalid selection — aborting.")
        return

    pos = positions[choice - 1]
    ticker = pos["ticker"]
    is_debit = pos["type"] in ("Long Call", "Long Put")
    cost_basis = pos["debit_paid"] if is_debit else pos["credit_received"]
    cost_label = "Debit" if is_debit else "Credit"

    print(f"\nFetching live price for {ticker} spread...")
    current_value = get_spread_value(
        pos["short_symbol"], pos["long_symbol"], is_debit=is_debit)

    if current_value is None:
        print("❌ Could not fetch live price — aborting.")
        return

    if is_debit:
        profit_pct = (current_value - cost_basis) / cost_basis * 100
        est_profit = round((current_value - cost_basis) * pos["contracts"] * 100, 2)
    else:
        profit_pct = (cost_basis - current_value) / cost_basis * 100
        est_profit = round((cost_basis - current_value) * pos["contracts"] * 100, 2)

    print(f"\n  Position : {ticker} {pos['type']} "
          f"${pos['short_strike']:.0f}/${pos['long_strike']:.0f}")
    print(f"  {cost_label}   : ${cost_basis:.2f}")
    print(f"  Close at : ${current_value:.2f}  ({profit_pct:+.1f}% P&L)")
    print(f"  Contracts: {pos['contracts']}")
    print(f"  Est. P&L : ${est_profit:+.2f}")
    print()

    confirm = input("Confirm manual close? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        return

    try:
        response = place_closing_order(pos, current_value)
        profit = log_closed_trade(pos, "manual_close", current_value, response)
        order_id = response.get("order", {}).get("id", "unknown")
        print(f"\n✅ Closing order placed — Order ID: {order_id}")
        print(f"   Closed at ${current_value:.2f} | P&L: ${profit:.2f}")
    except Exception as e:
        print(f"\n❌ Close failed: {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        check_positions()
    elif len(sys.argv) > 1 and sys.argv[1] == "--close":
        manual_close()
    else:
        run_monitor(interval_minutes=1)
