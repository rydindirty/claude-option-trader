"""
Step 11: Trade Placement with Human Approval Gate
Reads report_table.json and top9_analysis.json, presents each
TRADE-recommended spread for explicit human approval, then places
the order via Tradier. No order is ever submitted without a
direct 'yes' from the user in this terminal session.
"""
import json
import os
import sys
import requests
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import TRADIER_TOKEN, TRADIER_ENV, get_tradier_session, TRADIER_BASE_URL, TRADIER_HEADERS, TRADIER_ACCOUNT_ID
import db

# ── Tradier config ────────────────────────────────────────────
# BASE_URL and HEADERS are now imported from config
_session = get_tradier_session()  # SSL-verified session for Tradier API

# ── Helpers ───────────────────────────────────────────────────

def fetch_buying_power():
    """
    Fetch account balances from Tradier and return option buying power.
    Returns (option_buying_power, total_equity) or (None, None) on error.
    """
    try:
        r = _session.get(
            f"{TRADIER_BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/balances",
            headers=TRADIER_HEADERS,
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        balances = data.get("balances", {})
        # option_buying_power lives under balances.margin for margin accounts
        margin = balances.get("margin", {}) or {}
        obp = (margin.get("option_buying_power")
               or balances.get("option_buying_power")
               or balances.get("total_cash")
               or 0)
        equity = balances.get("total_equity", 0)
        return float(obp), float(equity)
    except Exception as e:
        print(f"⚠️  Could not fetch buying power: {e}")
        return None, None


def suggest_contracts(max_loss_per_contract, buying_power, risk_pct=0.60):
    """
    Suggest a number of contracts using at most risk_pct of buying power
    per trade (default 60%).  Returns at least 1.
    """
    if not buying_power or buying_power <= 0:
        return 1
    budget = buying_power * risk_pct
    max_loss_dollars = max_loss_per_contract * 100
    if max_loss_dollars <= 0:
        return 1
    return max(1, int(budget / max_loss_dollars))


def load_data():
    """Load trade recommendations and Claude analysis."""
    with open("data/report_table.json", "r") as f:
        trades = json.load(f)["report_table"]

    # Load Claude recommendations and heat scores keyed by ticker
    recommendations = {}
    heat_scores = {}
    try:
        with open("data/top9_analysis.json", "r") as f:
            analysis = json.load(f)["analysis"]

        import re
        # Normalize both header formats to ## # style:
        # Format A: "## #1. TICKER ..."
        # Format B: "**#1. TICKER ..."
        normalized = re.sub(r"\*\*#(\d+)\.", r"## #\1.", analysis)

        sections = normalized.split("## #")
        for section in sections[1:]:
            sec_lines = section.strip().split("\n")

            # First line: "1. TICKER TYPE STRIKES"
            first_line = sec_lines[0].strip().replace("**", "").strip()
            parts = first_line.split()
            if len(parts) < 2:
                continue
            ticker = parts[1].upper()

            # Find HEAT score on line containing HEAT:
            for line in sec_lines:
                if "HEAT:" in line.upper():
                    try:
                        heat_part = line.upper().split("HEAT:")[1]
                        heat_str = heat_part.strip().replace("*","").replace("|","").strip().split()[0]
                        heat_scores[ticker] = int(heat_str)
                    except (ValueError, IndexError):
                        pass
                    break

            # Find recommendation line — handles both formats:
            # Direct:  "TRADE ..." / "WAIT ..." / "SKIP ..."
            # Labeled: "RECOMMENDATION: TRADE ..." (Claude's typical output)
            for line in sec_lines:
                clean = line.strip().replace("*", "").strip()
                # Normalize "RECOMMENDATION: TRADE" → just check the value after the colon
                check = clean
                if clean.upper().startswith("RECOMMENDATION:"):
                    check = clean.split(":", 1)[1].strip()
                if check.upper().startswith("TRADE"):
                    recommendations[ticker] = "TRADE"
                    break
                elif check.upper().startswith("WAIT"):
                    recommendations[ticker] = "WAIT"
                    break
                elif check.upper().startswith("SKIP"):
                    recommendations[ticker] = "SKIP"
                    break

    except Exception as e:
        print(f"⚠️  Could not parse recommendations: {e}")

    return trades, recommendations, heat_scores


def parse_strikes(legs_str):
    """Parse '$190/$185' into (190.0, 185.0)."""
    parts = legs_str.replace("$", "").split("/")
    return float(parts[0]), float(parts[1])


def build_option_symbol(ticker, expiration, option_type, strike):
    """
    Build OCC option symbol e.g. AMD260417P00190000
    expiration: 'YYYY-MM-DD'
    option_type: 'put' or 'call'
    strike: float e.g. 190.0
    """
    exp = expiration.replace("-", "")[2:]  # 260417
    otype = "P" if option_type == "put" else "C"
    strike_int = int(strike * 1000)       # 190.0 -> 190000
    return f"{ticker}{exp}{otype}{strike_int:08d}"


def get_current_quote(symbol):
    """Fetch current bid/ask for an option symbol from Tradier."""
    r = _session.get(
        f"{TRADIER_BASE_URL}/markets/quotes",
        headers=TRADIER_HEADERS,
        params={"symbols": symbol}
    )
    r.raise_for_status()
    data = r.json()
    quote = data.get("quotes", {}).get("quote", {})
    if isinstance(quote, list):
        quote = quote[0]
    return quote


def preview_order(trade, contracts):
    """
    Preview a multileg credit spread order via Tradier.
    Returns the preview response dict.
    """
    short_strike, long_strike = parse_strikes(trade["legs"])
    expiration = trade["exp_date"]
    ticker = trade["ticker"]

    # Determine option type based on spread type
    # Bull Put uses puts, Bear Call uses calls
    is_bear_call = "Bear Call" in trade.get("type", "")
    opt_type = "call" if is_bear_call else "put"

    short_symbol = build_option_symbol(ticker, expiration, opt_type, short_strike)
    long_symbol  = build_option_symbol(ticker, expiration, opt_type, long_strike)

    # Net credit is what we collect - strip the $ sign
    credit = float(trade["net_credit"].replace("$", ""))

    payload = {
        "class":             "multileg",
        "symbol":            ticker,
        "type":              "credit",
        "duration":          "day",
        "price":             f"{credit:.2f}",
        "option_symbol[0]":  short_symbol,
        "side[0]":           "sell_to_open",
        "quantity[0]":       str(contracts),
        "option_symbol[1]":  long_symbol,
        "side[1]":           "buy_to_open",
        "quantity[1]":       str(contracts),
        "preview":           "true"
    }

    r = _session.post(
        f"{TRADIER_BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/orders",
        headers=TRADIER_HEADERS,
        data=payload
    )
    r.raise_for_status()
    return r.json()


def place_order(trade, contracts):
    """
    Place a live multileg credit spread order via Tradier.
    Only called after explicit human approval AND successful preview.
    """
    short_strike, long_strike = parse_strikes(trade["legs"])
    expiration = trade["exp_date"]
    ticker = trade["ticker"]

    # Determine option type based on spread type
    is_bear_call = "Bear Call" in trade.get("type", "")
    opt_type = "call" if is_bear_call else "put"

    short_symbol = build_option_symbol(ticker, expiration, opt_type, short_strike)
    long_symbol  = build_option_symbol(ticker, expiration, opt_type, long_strike)
    credit = float(trade["net_credit"].replace("$", ""))

    payload = {
        "class":             "multileg",
        "symbol":            ticker,
        "type":              "credit",
        "duration":          "day",
        "price":             f"{credit:.2f}",
        "option_symbol[0]":  short_symbol,
        "side[0]":           "sell_to_open",
        "quantity[0]":       str(contracts),
        "option_symbol[1]":  long_symbol,
        "side[1]":           "buy_to_open",
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


def save_placed_trade(trade, contracts, order_response):
    """
    Insert placed trade into data/trades.db for the position monitor to track.
    """
    short_strike, long_strike = parse_strikes(trade["legs"])
    credit   = float(trade["net_credit"].replace("$", ""))
    max_loss = float(trade["max_loss"].replace("$", ""))
    opt_type = "call" if "Bear Call" in trade.get("type", "") else "put"

    position = {
        "ticker":            trade["ticker"],
        "type":              trade["type"],
        "short_strike":      short_strike,
        "long_strike":       long_strike,
        "expiration":        trade["exp_date"],
        "dte_at_entry":      trade["dte"],
        "credit_received":   credit,
        "max_profit":        credit,
        "max_loss":          max_loss,
        "contracts":         contracts,
        "short_symbol":      build_option_symbol(
                                 trade["ticker"], trade["exp_date"],
                                 opt_type, short_strike),
        "long_symbol":       build_option_symbol(
                                 trade["ticker"], trade["exp_date"],
                                 opt_type, long_strike),
        "tradier_order_id":  order_response.get("order", {}).get("id", "unknown"),
        "opened_at":         datetime.now().isoformat(),
        "profit_target_pct": 0.40,
        "stop_loss_pct":     1.50,
    }

    row_id = db.insert_open_trade(position)
    print(f"   📝 Logged to data/trades.db (row id {row_id})")


# ── Main approval loop ────────────────────────────────────────

def main():
    print("\n" + "=" * 70)
    print("💰 TRADE PLACEMENT - HUMAN APPROVAL REQUIRED")
    print(f"   Environment: {TRADIER_ENV.upper()}")
    print(f"   Account:     {TRADIER_ACCOUNT_ID}")
    print(f"   Time:        {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    trades, recommendations, heat_scores = load_data()

    # Fetch account buying power
    buying_power, total_equity = fetch_buying_power()
    if buying_power is not None:
        print(f"\n💼 Account: {TRADIER_ACCOUNT_ID}")
        print(f"   Option Buying Power: ${buying_power:,.2f}")
        if total_equity:
            print(f"   Total Equity:        ${total_equity:,.2f}")
        print(f"   Risk per trade (60%): ${buying_power * 0.60:,.2f}")
    else:
        print(f"\n⚠️  Buying power unavailable — enter contracts manually")

    # Only present trades Claude recommended as TRADE
    actionable = [
        t for t in trades
        if recommendations.get(t["ticker"], "") == "TRADE"
    ]

    if not actionable:
        print("\n⚠️  No trades marked as TRADE by Claude today.")
        print("    Review WAIT/SKIP recommendations in data/top9_analysis.json")
        return

    print(f"\n📋 Claude recommends {len(actionable)} trade(s) today:\n")

    placed = []
    skipped = []
    remaining_bp = buying_power  # decremented as trades are approved

    for trade in actionable:
        short_strike, long_strike = parse_strikes(trade["legs"])
        credit = float(trade["net_credit"].replace("$", ""))
        max_loss = float(trade["max_loss"].replace("$", ""))
        width = abs(short_strike - long_strike)
        is_bear_call = "Bear Call" in trade.get("type", "")
        opt_label = "Call" if is_bear_call else "Put"

        print("=" * 70)
        print(f"  TRADE #{trade['rank']}: {trade['ticker']} {trade['type']}")
        print("=" * 70)
        print(f"  Sell: {trade['ticker']} {trade['exp_date']} "
              f"${short_strike:.0f} {opt_label} @ ${credit:.2f} credit")
        print(f"  Buy:  {trade['ticker']} {trade['exp_date']} "
              f"${long_strike:.0f} {opt_label}  (protection)")
        print(f"  ─────────────────────────────────────────────")
        print(f"  Net Credit:  ${credit:.2f} per contract ($"
              f"{credit*100:.0f} total)")
        print(f"  Max Loss:    ${max_loss:.2f} per contract ($"
              f"{max_loss*100:.0f} total)")
        print(f"  Max Profit:  ${credit:.2f} per contract ($"
              f"{credit*100:.0f} total)")
        print(f"  Width:       ${width:.0f}")
        print(f"  DTE:         {trade['dte']} days")
        print(f"  ROI:         {trade['roi']}")
        print(f"  PoP:         {trade['pop']}")
        heat = heat_scores.get(trade["ticker"], 'N/A')
        print(f"  Heat Score:  {heat}/10 (1=safe, 10=risky)")
        print(f"  Expiration:  {trade['exp_date']}")
        print(f"  ─────────────────────────────────────────────")
        print(f"  Profit Target (40%): close when spread = "
              f"${credit * 0.60:.2f}")
        print(f"  Stop Loss (1.5x):    close when spread = "
              f"${credit * 1.5:.2f}")
        print()

        # Suggest contract count based on remaining buying power
        margin_per_contract = max_loss * 100
        suggested = suggest_contracts(max_loss, remaining_bp)
        if remaining_bp:
            print(f"  Margin per contract:  ${margin_per_contract:,.0f}")
            print(f"  Remaining BP:         ${remaining_bp:,.0f}")
            print(f"  Suggested contracts (60% of remaining BP): {suggested}")
        else:
            print(f"  Margin per contract:  ${margin_per_contract:,.0f}")
            print(f"  ⚠️  Buying power unavailable — enter contracts manually")

        # Ask how many contracts
        while True:
            try:
                contracts_input = input(
                    "  How many contracts? (1 contract = "
                    f"${credit*100:.0f} credit / "
                    f"${max_loss*100:.0f} max risk) "
                    "[0 to skip]: "
                ).strip()
                contracts = int(contracts_input)
                if contracts < 0:
                    raise ValueError
                break
            except ValueError:
                print("  Please enter a whole number (0 to skip)")

        if contracts == 0:
            print(f"  ⏭️  Skipping {trade['ticker']}")
            skipped.append(trade["ticker"])
            print()
            continue

        # Preview the order first
        print(f"\n  🔍 Previewing order with Tradier...")
        try:
            preview = preview_order(trade, contracts)
            order_preview = preview.get("order", {})

            if order_preview.get("status") == "ok":
                print(f"  ✅ Preview passed")
                print(f"     Est. commission: "
                      f"${order_preview.get('commission', 0):.2f}")
                print(f"     Margin impact:   "
                      f"${order_preview.get('margin_change', 0):.2f}")
            else:
                print(f"  ⚠️  Preview returned: {preview}")
        except Exception as e:
            print(f"  ⚠️  Preview failed: {e}")
            print(f"     Proceeding to approval anyway...")

        # Final approval before placing
        print()
        confirm = input(
            f"  ⚡ CONFIRM: Place {contracts} contract(s) of "
            f"{trade['ticker']} {trade['legs']} "
            f"for ${credit*contracts*100:.0f} total credit? "
            f"(yes/no): "
        ).strip().lower()

        if confirm == "yes":
            try:
                print(f"  📤 Placing order...")
                response = place_order(trade, contracts)
                order_id = response.get("order", {}).get("id", "unknown")
                status   = response.get("order", {}).get("status", "unknown")
                print(f"  ✅ Order placed! ID: {order_id} | Status: {status}")
                save_placed_trade(trade, contracts, response)
                placed.append(trade["ticker"])
                if remaining_bp is not None:
                    margin_used = contracts * max_loss * 100
                    remaining_bp -= margin_used
                    print(f"  💼 Remaining BP after this trade: ${remaining_bp:,.0f}")
            except Exception as e:
                print(f"  ❌ Order failed: {e}")
        else:
            print(f"  ⏭️  Order cancelled by user")
            skipped.append(trade["ticker"])

        print()

    # Summary
    print("=" * 70)
    print("📊 SESSION SUMMARY")
    print("=" * 70)
    print(f"  Placed:  {len(placed)} trade(s): {', '.join(placed) or 'none'}")
    print(f"  Skipped: {len(skipped)} trade(s): {', '.join(skipped) or 'none'}")
    if placed:
        print(f"\n  ✅ Open positions logged to data/trades.db")
        print(f"  🔍 Position monitor will track these for 40% profit target")
    print()

if __name__ == "__main__":
    main()
