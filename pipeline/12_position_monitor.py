"""
Step 12: Position Monitor
Runs every 5 minutes during market hours. Checks all open
positions against three exit rules:
  1. Profit target  — close when spread value drops to 60% of
                      credit received (40% profit locked in)
  2. Stop loss      — close when spread costs 2x the credit to close
  3. Time stop      — hard close when DTE < 21 (past deadline)
                      on DTE = 21: hold through the day; after 3:30 PM ET
                      close only if spread is unfavorable (above credit)
Sends a clear terminal alert and places the closing order
automatically via Tradier.
"""
import os
import sys
import time
import requests
from datetime import datetime, date
from zoneinfo import ZoneInfo

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import TRADIER_TOKEN, TRADIER_ENV, get_tradier_session, TRADIER_BASE_URL, TRADIER_HEADERS, TRADIER_ACCOUNT_ID
import db

# ── Tradier config ─────────────────────────────────────────────
# BASE_URL and HEADERS are now imported from config
_session = get_tradier_session()  # SSL-verified session for Tradier API

# ── Market hours (ET) ──────────────────────────────────────────
MARKET_OPEN_HOUR  = 9
MARKET_OPEN_MIN   = 35   # 5 min after open to avoid chaos
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


def get_spread_value(short_symbol, long_symbol):
    """
    Fetch current bid/ask for both legs and return the cost to close
    the spread (debit needed to buy it back).

    Cost to close = short_ask - long_bid
      (pay ask to buy back the short, receive bid when selling the long)

    Falls back to mid-price if ask or bid is missing.
    Returns None only if no usable price data is available.
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

        if short_ask <= 0 and long_bid <= 0:
            print(f"   ⚠️  Both legs returned $0 — no usable price data")
            return None

        # Cost to close = buy back short (pay ask) - sell long (receive bid)
        return round(short_ask - long_bid, 2)

    except Exception as e:
        print(f"   ⚠️  Quote error: {e}")
        return None


def place_closing_order(position, current_value=None):
    """
    Place a closing multileg order to exit the spread.
    Uses the actual live spread value as the limit price.
    Raises ValueError if no current value is available.
    """
    contracts = position["contracts"]
    ticker    = position["ticker"]

    # Require a real market price — never make up a price
    if not current_value or current_value <= 0:
        raise ValueError(
            f"Cannot place closing order for {ticker} without a valid market price"
        )

    limit_price = round(current_value, 2)

    payload = {
        "class":             "multileg",
        "symbol":            ticker,
        "type":              "debit",
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
    credit    = position["credit_received"]
    contracts = position["contracts"]
    profit    = round((credit - close_value) * contracts * 100, 2)
    profit_pct = round((credit - close_value) / credit * 100, 1)

    db.close_trade(
        trade_id            = position["id"],
        close_reason        = close_reason,
        close_value         = close_value,
        profit_per_contract = round((credit - close_value) * 100, 2),
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
        ticker  = pos["ticker"]
        credit  = pos["credit_received"]
        exp     = date.fromisoformat(pos["expiration"])
        dte     = (exp - today).days

        print(f"\n  {ticker} {pos['type']} "
              f"${pos['short_strike']:.0f}/$"
              f"{pos['long_strike']:.0f} "
              f"| Credit: ${credit:.2f} | DTE: {dte}")

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
                pos["short_symbol"], pos["long_symbol"])
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
            except Exception as e:
                print(f"  ❌ Close failed: {e}")
                remaining.append(pos)
            continue

        # ── Rule 3b: 21 DTE — end-of-day price action check ───
        # Hold through the 21st day. After 3:30 PM ET, close only
        # if spread is unfavorable (above credit received). If
        # favorable, let profit/stop rules below handle it.
        if dte == 21:
            now = datetime.now(_ET)
            is_eod = now.hour > 15 or (now.hour == 15 and now.minute >= 30)
            if is_eod:
                close_val = get_spread_value(
                    pos["short_symbol"], pos["long_symbol"])
                if close_val is None:
                    print(f"  ⚠️  21 DTE EOD: no quote — holding")
                    remaining.append(pos)
                    continue
                eod_pct = (credit - close_val) / credit * 100
                print(f"  ⏰ 21 DTE END-OF-DAY | "
                      f"Spread: ${close_val:.2f} | P&L: {eod_pct:.1f}%")
                if close_val > credit:
                    print(f"  📉 Unfavorable — spread above credit "
                          f"(${credit:.2f}). Closing to limit loss.")
                    try:
                        response = place_closing_order(pos, close_val)
                        profit   = log_closed_trade(
                            pos, "time_stop_eod", close_val, response)
                        print(f"  ✅ Closed at ${close_val:.2f} | "
                              f"P&L: ${profit:.2f}")
                    except Exception as e:
                        print(f"  ❌ Close failed: {e}")
                        remaining.append(pos)
                    continue
                else:
                    print(f"  📈 Favorable — spread at/below credit "
                          f"(${credit:.2f}). Holding for profit target.")
                    # fall through to profit/stop checks below
            else:
                print(f"  📅 21 DTE — holding through end of day "
                      f"(EOD check activates after 3:30 PM)")
                # fall through to profit/stop checks below

        # ── Get current spread value ───────────────────────────
        current_value = get_spread_value(
            pos["short_symbol"], pos["long_symbol"])

        if current_value is None:
            print(f"  ⚠️  Could not get quote — skipping this check")
            remaining.append(pos)
            continue

        profit_pct = (credit - current_value) / credit * 100
        print(f"  Current spread value: ${current_value:.2f} | "
              f"P&L: {profit_pct:.1f}% of max profit")

        trade_id = pos["id"]

        # ── Update peak profit tracker ─────────────────────────
        prev_peak = _peak_profit.get(trade_id, 0.0)
        if profit_pct > prev_peak:
            _peak_profit[trade_id] = profit_pct
        peak = _peak_profit[trade_id]

        # ── Update consecutive-rising counter ──────────────────
        last_val = _last_spread_value.get(trade_id)
        if last_val is not None:
            if current_value > last_val:
                _consecutive_rises[trade_id] = _consecutive_rises.get(trade_id, 0) + 1
            else:
                _consecutive_rises[trade_id] = 0   # flat or improving — reset
        _last_spread_value[trade_id] = current_value
        streak = _consecutive_rises.get(trade_id, 0)

        # ── Fluid Rule A: Trailing profit stop ─────────────────
        # If profit peaked above _TRAIL_TRIGGER_PCT and has since
        # fallen _TRAIL_DROP_PCT points below that peak, close now.
        if peak >= _TRAIL_TRIGGER_PCT:
            trail_threshold = peak - _TRAIL_DROP_PCT
            if profit_pct < trail_threshold:
                print(f"  📉 TRAILING STOP — peaked at {peak:.1f}%, "
                      f"now {profit_pct:.1f}% "
                      f"(dropped {peak - profit_pct:.1f} pts from peak)")
                try:
                    response = place_closing_order(pos, current_value)
                    profit   = log_closed_trade(
                        pos, "trailing_stop", current_value, response)
                    print(f"  ✅ Closed at ${current_value:.2f} | "
                          f"P&L: ${profit:.2f}")
                except Exception as e:
                    print(f"  ❌ Close failed: {e}")
                    remaining.append(pos)
                continue

        # ── Fluid Rule B: Trend-based early stop ───────────────
        # Trigger only when spread has been strictly higher than the
        # prior check on every one of the last _TREND_WINDOW checks
        # (any flat or improving check resets the counter to 0) AND
        # we are already in loss territory (spread > credit).
        if streak >= _TREND_WINDOW and current_value > credit:
            loss_pct = abs(profit_pct)
            print(f"  📈 TREND STOP — spread strictly higher for "
                  f"{streak} consecutive checks while in loss "
                  f"({loss_pct:.1f}%). Closing.")
            try:
                response = place_closing_order(pos, current_value)
                profit   = log_closed_trade(
                    pos, "trend_stop", current_value, response)
                print(f"  ✅ Closed at ${current_value:.2f} | "
                      f"P&L: ${profit:.2f}")
            except Exception as e:
                print(f"  ❌ Close failed: {e}")
                remaining.append(pos)
            continue

        # ── Rule 1: Profit target (40%) ────────────────────────
        target = credit * (1 - pos["profit_target_pct"])
        if current_value <= target:
            print(f"  🎯 PROFIT TARGET hit — "
                  f"{profit_pct:.1f}% profit locked in")
            try:
                response = place_closing_order(pos, current_value)
                profit   = log_closed_trade(
                    pos, "profit_target", current_value, response)
                print(f"  ✅ Closed at ${current_value:.2f} | "
                      f"P&L: ${profit:.2f}")
            except Exception as e:
                print(f"  ❌ Close failed: {e}")
                remaining.append(pos)
            continue

        # ── Rule 2: Stop loss (2x credit) ──────────────────────
        stop = credit * pos["stop_loss_pct"]
        if current_value >= stop:
            print(f"  🛑 STOP LOSS triggered — "
                  f"spread at ${current_value:.2f} vs "
                  f"stop ${stop:.2f}")
            try:
                response = place_closing_order(pos, current_value)
                profit   = log_closed_trade(
                    pos, "stop_loss", current_value, response)
                print(f"  ✅ Closed at ${current_value:.2f} | "
                      f"P&L: ${profit:.2f}")
            except Exception as e:
                print(f"  ❌ Close failed: {e}")
                remaining.append(pos)
            continue

        # ── No trigger — keep position open ───────────────────
        print(f"  ✓  Holding — profit target at "
              f"${target:.2f} | stop at ${stop:.2f}")
        remaining.append(pos)

    # Save updated positions (closed ones removed)
    save_positions(remaining)
    closed_count = len(positions) - len(remaining)
    if closed_count > 0:
        print(f"\n  📊 Closed {closed_count} position(s) this check")


# ── In-memory state for fluid stops (reset each monitor session) ──────────────
# Trailing profit: tracks peak profit % seen so far per trade id
_peak_profit: dict[int, float] = {}
# Trend stop: count of consecutive checks where spread was strictly higher
# than the previous check. Resets to 0 the moment a check is flat or lower.
_consecutive_rises: dict[int, int] = {}
_last_spread_value: dict[int, float] = {}
_TREND_WINDOW = 10         # consecutive strictly-rising checks needed to trigger
_TRAIL_TRIGGER_PCT = 25.0  # profit must have hit this % before trailing
_TRAIL_DROP_PCT   = 10.0   # close if profit drops this many points from peak


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
    print(f"     Profit target: 40% of max credit")
    print(f"     Stop loss:     2x credit received")
    print(f"     Time stop:     hard close at DTE < 21")
    print(f"                    DTE = 21: EOD check after 3:30 PM")
    print("=" * 60)
    print("\nPress Ctrl+C to stop\n")

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
                print("   Retrying in 60 seconds...")
                time.sleep(60)
    finally:
        release_lock()


if __name__ == "__main__":
    # Can also run a single check with:
    # python3 pipeline/12_position_monitor.py --once
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        check_positions()
    else:
        run_monitor(interval_minutes=1)
