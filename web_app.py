"""
FastAPI web UI for News Spread Engine.
Run from project root: uvicorn web_app:app --host 0.0.0.0 --port 8000 --reload
"""
import asyncio
import json
import os
import re
import smtplib
import subprocess
import sys
from datetime import datetime, date
from email.mime.text import MIMEText

from fastapi import FastAPI, HTTPException, Request, Form, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)                                 # config.py
sys.path.insert(0, os.path.join(BASE_DIR, "pipeline"))       # db.py

from config import (TRADIER_TOKEN, TRADIER_ENV, get_tradier_session,
                    TRADIER_BASE_URL, TRADIER_HEADERS, TRADIER_ACCOUNT_ID,
                    WEB_USERNAME, WEB_PASSWORD, SESSION_SECRET)

ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")
SMTP_HOST   = os.getenv("SMTP_HOST", "")
SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER   = os.getenv("SMTP_USER", "")
SMTP_PASS   = os.getenv("SMTP_PASS", "")
import db

_session = get_tradier_session()

_pipeline_proc: "subprocess.Popen | None" = None
_pipeline_log = os.path.join(BASE_DIR, "data", "pipeline.log")

app = FastAPI(title="News Spread Engine", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)


@app.on_event("startup")
async def _start_monitors():
    asyncio.create_task(_alert_monitor_loop())


# ── Auth helpers ───────────────────────────────────────────────────────

def _is_authenticated(request: Request) -> bool:
    return request.session.get("authenticated") is True

def _require_auth(request: Request):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

_LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>News Spread Engine — Login</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0d0d0d; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont,
      'Segoe UI', sans-serif; display: flex; align-items: center;
      justify-content: center; min-height: 100vh;
    }
    .card {
      background: #1a1a2e; border: 1px solid #2a2a4a; border-radius: 12px;
      padding: 2.5rem; width: 100%; max-width: 380px;
    }
    .logo { font-size: 1.5rem; font-weight: 700; margin-bottom: 1.8rem; text-align: center; }
    .logo span { color: #00c853; }
    label { display: block; font-size: 0.75rem; color: #888; text-transform: uppercase;
            letter-spacing: .06em; margin-bottom: .4rem; }
    input {
      width: 100%; background: #111; border: 1px solid #333; border-radius: 8px;
      color: #e0e0e0; font-size: 1rem; padding: .7rem 1rem; margin-bottom: 1.2rem;
      outline: none;
    }
    input:focus { border-color: #00c853; }
    button {
      width: 100%; background: #00c853; color: #000; font-weight: 700;
      font-size: 1rem; padding: .8rem; border: none; border-radius: 8px; cursor: pointer;
    }
    button:hover { background: #00e676; }
    .error { color: #ff5252; font-size: .875rem; margin-bottom: 1rem; text-align: center; }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo"><span>News</span>Spread</div>
    {error}
    <form method="post" action="/login">
      <label>Username</label>
      <input type="text" name="username" autocomplete="username" autofocus/>
      <label>Password</label>
      <input type="password" name="password" autocomplete="current-password"/>
      <button type="submit">Sign In</button>
    </form>
  </div>
</body>
</html>
"""


# ── Data helpers ───────────────────────────────────────────────────────

def _data(filename: str) -> str:
    return os.path.join(BASE_DIR, "data", filename)


def fetch_buying_power():
    try:
        r = _session.get(
            f"{TRADIER_BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/balances",
            headers=TRADIER_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        balances = data.get("balances", {})
        margin = balances.get("margin", {}) or {}
        obp = (margin.get("option_buying_power")
               or balances.get("option_buying_power")
               or balances.get("total_cash") or 0)
        equity = balances.get("total_equity", 0)
        return float(obp), float(equity)
    except Exception:
        return None, None


def suggest_contracts(max_loss_per_contract, buying_power, risk_pct=0.60):
    if not buying_power or buying_power <= 0:
        return 1
    budget = buying_power * risk_pct
    max_loss_dollars = max_loss_per_contract * 100
    if max_loss_dollars <= 0:
        return 1
    return max(1, int(budget / max_loss_dollars))


def _parse_analysis(analysis_text, tickers):
    ticker_set = {t.upper() for t in tickers}
    recommendations, heat_scores = {}, {}
    current_ticker = None
    pending_rec = False

    for line in analysis_text.split("\n"):
        stripped = line.strip()
        if pending_rec and current_ticker and current_ticker not in recommendations:
            for kw in ("TRADE", "WAIT", "SKIP"):
                if stripped.upper().startswith(kw):
                    recommendations[current_ticker] = kw
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
        if "RECOMMENDATION:" in upper and current_ticker not in recommendations:
            after = upper.split("RECOMMENDATION:", 1)[1].strip().lstrip("* ")
            if after:
                for kw in ("TRADE", "WAIT", "SKIP"):
                    if after.startswith(kw):
                        recommendations[current_ticker] = kw
                        break
            else:
                pending_rec = True

        if "HEAT:" in upper and current_ticker not in heat_scores:
            try:
                after = upper.split("HEAT:", 1)[1].strip().lstrip("* |")
                heat_str = re.sub(r"[^0-9].*", "", after.split()[0])
                heat_scores[current_ticker] = int(heat_str)
            except (ValueError, IndexError):
                pass

    return recommendations, heat_scores


def _parse_rationale(analysis_text: str, tickers: list) -> dict:
    ticker_set = {t.upper() for t in tickers}
    rationale = {}
    blocks = re.split(r'\n(?=#\d+\.)', "\n" + analysis_text)
    for block in blocks:
        m = re.match(r'#\d+\.\s+([A-Z]+)', block.strip())
        if not m:
            continue
        ticker = m.group(1).upper()
        if ticker not in ticker_set:
            continue
        info = {}
        for bullet in re.finditer(r'[•·]\s+(WHAT|HOW|WHY):\s*(.+)', block):
            info[bullet.group(1).lower()] = bullet.group(2).strip()
        cat = re.search(r'CATALYST RISK[:\s]*\n\s*(.+?)(?=\n\s*RECOMMENDATION|\Z)', block, re.DOTALL | re.IGNORECASE)
        info['catalyst'] = cat.group(1).strip() if cat else ''
        reason = re.search(
            r'RECOMMENDATION[:\s]*\n\s*(?:Trade|Wait|Skip)[^\n]*\n(.+?)(?=\n\n|\Z)',
            block, re.DOTALL | re.IGNORECASE
        )
        info['reason'] = reason.group(1).strip() if reason else ''
        rationale[ticker] = info
    return rationale


def load_trades():
    with open(_data("report_table.json"), "r") as f:
        trades = json.load(f)["report_table"]
    recommendations, heat_scores, rationale = {}, {}, {}
    try:
        with open(_data("top9_analysis.json"), "r") as f:
            analysis = json.load(f)["analysis"]
        tickers = [t["ticker"].upper() for t in trades]
        recommendations, heat_scores = _parse_analysis(analysis, tickers)
        rationale = _parse_rationale(analysis, tickers)
    except Exception:
        pass
    return trades, recommendations, heat_scores, rationale


def parse_strikes(legs_str):
    parts = legs_str.replace("$", "").split("/")
    return float(parts[0]), float(parts[1])


def build_option_symbol(ticker, expiration, option_type, strike):
    exp = expiration.replace("-", "")[2:]
    otype = "P" if option_type == "put" else "C"
    return f"{ticker}{exp}{otype}{int(strike * 1000):08d}"


def _read_regime():
    try:
        with open(_data("macro_regime.json")) as f:
            return json.load(f).get("regime_label")
    except Exception:
        return None


def _last_run_info() -> dict:
    ts, regime = None, None
    try:
        with open(_data("top9_analysis.json")) as f:
            ts = json.load(f).get("timestamp")
    except Exception:
        pass
    try:
        with open(_data("macro_regime.json")) as f:
            regime = json.load(f).get("regime_label", "Unknown")
    except Exception:
        pass
    if not ts:
        return {"ran": False, "label": "Never run", "stale": True, "regime": regime or "Unknown"}
    try:
        run_dt = datetime.fromisoformat(ts)
        delta_days = (datetime.now().date() - run_dt.date()).days
        if delta_days == 0:
            label = f"Today {run_dt.strftime('%-I:%M %p')} ✓"
            stale = False
        elif delta_days == 1:
            label = "Stale — last run yesterday"
            stale = True
        else:
            label = f"Stale — last run {delta_days} days ago"
            stale = True
        return {"ran": True, "label": label, "stale": stale, "regime": regime or "Unknown", "ts": ts}
    except Exception:
        return {"ran": False, "label": "Unknown", "stale": True, "regime": regime or "Unknown"}


def _send_alert_email(ticker: str, alert_type: str, profit_pct: float):
    sign = "+" if profit_pct >= 0 else ""
    msg_line = f"{ticker} {alert_type} ({sign}{profit_pct:.1f}%)"
    if not ALERT_EMAIL or not SMTP_HOST or not SMTP_USER:
        print(f"[ALERT] {msg_line} — SMTP not configured, skipping email")
        return
    try:
        body = (f"Position: {ticker}\nAlert: {alert_type}\nP&L: {sign}{profit_pct:.1f}%\n\n"
                f"Log in to https://richjo.co to review.")
        msg = MIMEText(body)
        msg["Subject"] = f"NSE Alert: {msg_line}"
        msg["From"] = SMTP_USER
        msg["To"] = ALERT_EMAIL
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.sendmail(SMTP_USER, ALERT_EMAIL, msg.as_string())
        print(f"[ALERT] Email sent: {msg_line}")
    except Exception as e:
        print(f"[ALERT] Email failed: {e}")


async def _alert_monitor_loop():
    await asyncio.sleep(30)
    while True:
        try:
            positions = db.load_open_positions()
            for pos in positions:
                if pos.get("alert_sent"):
                    continue
                current_value = get_spread_value(pos["short_symbol"], pos["long_symbol"])
                if current_value is None:
                    continue
                credit = pos["credit_received"]
                profit_pct = round((credit - current_value) / credit * 100, 1)
                if profit_pct >= 40.0:
                    _send_alert_email(pos["ticker"], "Profit Target Hit", profit_pct)
                    db.mark_alert_sent(pos["id"])
                elif profit_pct <= -50.0:
                    _send_alert_email(pos["ticker"], "Stop Loss Hit", profit_pct)
                    db.mark_alert_sent(pos["id"])
        except Exception as e:
            print(f"[ALERT MONITOR] {e}")
        await asyncio.sleep(60)


def _order_payload(trade, contracts, preview=False):
    short_strike, long_strike = parse_strikes(trade["legs"])
    is_bear_call = "Bear Call" in trade.get("type", "")
    opt_type = "call" if is_bear_call else "put"
    credit = float(trade["net_credit"].replace("$", ""))
    ticker = trade["ticker"]
    expiration = trade["exp_date"]
    return {
        "class": "multileg",
        "symbol": ticker,
        "type": "credit",
        "duration": "day",
        "price": f"{credit:.2f}",
        "option_symbol[0]": build_option_symbol(ticker, expiration, opt_type, short_strike),
        "side[0]": "sell_to_open",
        "quantity[0]": str(contracts),
        "option_symbol[1]": build_option_symbol(ticker, expiration, opt_type, long_strike),
        "side[1]": "buy_to_open",
        "quantity[1]": str(contracts),
        "preview": "true" if preview else "false",
    }


def preview_order(trade, contracts):
    r = _session.post(
        f"{TRADIER_BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/orders",
        headers=TRADIER_HEADERS,
        data=_order_payload(trade, contracts, preview=True))
    r.raise_for_status()
    return r.json()


def place_order(trade, contracts):
    r = _session.post(
        f"{TRADIER_BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/orders",
        headers=TRADIER_HEADERS,
        data=_order_payload(trade, contracts, preview=False))
    r.raise_for_status()
    return r.json()


def save_placed_trade(trade, contracts, order_response):
    short_strike, long_strike = parse_strikes(trade["legs"])
    credit = float(trade["net_credit"].replace("$", ""))
    max_loss = float(trade["max_loss"].replace("$", ""))
    opt_type = "call" if "Bear Call" in trade.get("type", "") else "put"
    ticker, expiration = trade["ticker"], trade["exp_date"]
    return db.insert_open_trade({
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
        "short_symbol": build_option_symbol(ticker, expiration, opt_type, short_strike),
        "long_symbol": build_option_symbol(ticker, expiration, opt_type, long_strike),
        "tradier_order_id": order_response.get("order", {}).get("id", "unknown"),
        "opened_at": datetime.now().isoformat(),
        "profit_target_pct": 0.40,
        "stop_loss_pct": 1.50,
        "regime": _read_regime(),
    })


def get_spread_value(short_symbol, long_symbol):
    try:
        r = _session.get(
            f"{TRADIER_BASE_URL}/markets/quotes",
            headers={"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"},
            params={"symbols": f"{short_symbol},{long_symbol}"},
            timeout=10)
        r.raise_for_status()
        quotes = r.json().get("quotes", {}).get("quote", [])
        if isinstance(quotes, dict):
            quotes = [quotes]
        qmap = {q["symbol"]: q for q in quotes if "symbol" in q}

        def best(q, side):
            v = float(q.get(side) or 0)
            if v > 0:
                return v
            bid = float(q.get("bid") or 0)
            ask = float(q.get("ask") or 0)
            return round((bid + ask) / 2, 2) if (bid + ask) > 0 else 0

        short_ask = best(qmap.get(short_symbol, {}), "ask")
        long_bid  = best(qmap.get(long_symbol, {}), "bid")
        if short_ask <= 0 and long_bid <= 0:
            return None
        return round(short_ask - long_bid, 2)
    except Exception:
        return None


def close_position(position, current_value):
    if not current_value or current_value <= 0:
        raise ValueError("Cannot place closing order without a valid market price")
    credit, contracts = position["credit_received"], position["contracts"]
    payload = {
        "class": "multileg",
        "symbol": position["ticker"],
        "type": "debit",
        "duration": "day",
        "price": f"{round(current_value, 2):.2f}",
        "option_symbol[0]": position["short_symbol"],
        "side[0]": "buy_to_close",
        "quantity[0]": str(contracts),
        "option_symbol[1]": position["long_symbol"],
        "side[1]": "sell_to_close",
        "quantity[1]": str(contracts),
        "preview": "false",
    }
    r = _session.post(
        f"{TRADIER_BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/orders",
        headers=TRADIER_HEADERS, data=payload)
    r.raise_for_status()
    response = r.json()
    profit = round((credit - current_value) * contracts * 100, 2)
    profit_pct = round((credit - current_value) / credit * 100, 1)
    db.close_trade(
        trade_id=position["id"],
        close_reason="manual_close",
        close_value=current_value,
        profit_per_contract=round((credit - current_value) * 100, 2),
        total_profit=profit,
        profit_pct=profit_pct,
        close_order_id=response.get("order", {}).get("id", "unknown"),
    )
    return profit, response.get("order", {}).get("id", "unknown")


# ── API routes ─────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _is_authenticated(request):
        return RedirectResponse("/portfolio")
    return HTMLResponse(_LOGIN_PAGE.replace("{error}", ""))


@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == WEB_USERNAME and password == WEB_PASSWORD:
        request.session["authenticated"] = True
        return RedirectResponse("/portfolio", status_code=303)
    return HTMLResponse(_LOGIN_PAGE.replace(
        "{error}", '<p class="error">Invalid username or password.</p>'
    ))


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


@app.get("/", response_class=RedirectResponse)
async def root(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/login")
    return "/portfolio"


@app.get("/api/account")
async def api_account(request: Request):
    _require_auth(request)
    obp, equity = fetch_buying_power()
    return {"buying_power": obp, "equity": equity, "account_id": TRADIER_ACCOUNT_ID}


@app.get("/api/trades")
async def api_trades(request: Request):
    _require_auth(request)
    try:
        trades, recommendations, heat_scores, rationale = load_trades()
    except FileNotFoundError:
        return {"trades": [], "error": "Trade data not found — run the pipeline first.", "last_run": _last_run_info()}

    obp, _ = fetch_buying_power()
    result = []
    for t in trades:
        ticker = t["ticker"].upper()
        rec = recommendations.get(ticker, "UNKNOWN")
        heat = heat_scores.get(ticker)
        short_strike, long_strike = parse_strikes(t["legs"])
        credit = float(t["net_credit"].replace("$", ""))
        max_loss = float(t["max_loss"].replace("$", ""))
        r = rationale.get(ticker, {})
        result.append({
            "rank": t["rank"],
            "ticker": ticker,
            "type": t["type"],
            "legs": t["legs"],
            "short_strike": short_strike,
            "long_strike": long_strike,
            "exp_date": t["exp_date"],
            "dte": t["dte"],
            "net_credit": credit,
            "max_loss": max_loss,
            "roi": t["roi"],
            "pop": t["pop"],
            "recommendation": rec,
            "heat": heat,
            "suggested_contracts": suggest_contracts(max_loss, obp) if obp else 1,
            "profit_target": round(credit * 0.60, 2),
            "stop_loss": round(credit * 1.5, 2),
            "rationale": {
                "what": r.get("what", ""),
                "how": r.get("how", ""),
                "why": r.get("why", ""),
                "catalyst": r.get("catalyst", ""),
                "reason": r.get("reason", ""),
            },
        })
    return {"trades": result, "buying_power": obp, "last_run": _last_run_info()}


class ApproveRequest(BaseModel):
    contracts: int
    notes: str = ""


@app.post("/api/trades/{ticker}/approve")
async def api_approve(request: Request, ticker: str, req: ApproveRequest):
    _require_auth(request)
    if req.contracts < 1:
        raise HTTPException(400, "contracts must be >= 1")
    try:
        trades, _, _, _ = load_trades()
    except FileNotFoundError:
        raise HTTPException(404, "Trade data not found")

    trade = next((t for t in trades if t["ticker"].upper() == ticker.upper()), None)
    if not trade:
        raise HTTPException(404, f"No trade found for {ticker}")

    preview_info = None
    try:
        prev = preview_order(trade, req.contracts)
        o = prev.get("order", {})
        preview_info = {"status": o.get("status"), "commission": o.get("commission", 0)}
    except Exception as e:
        preview_info = {"status": "error", "error": str(e)}

    try:
        response = place_order(trade, req.contracts)
        order_id = response.get("order", {}).get("id", "unknown")
        status = response.get("order", {}).get("status", "unknown")
        row_id = save_placed_trade(trade, req.contracts, response)
        if req.notes.strip():
            db.save_trade_notes(row_id, req.notes.strip())
        return {"success": True, "order_id": order_id, "status": status,
                "db_row": row_id, "preview": preview_info}
    except Exception as e:
        raise HTTPException(500, f"Order failed: {e}")


@app.get("/api/positions")
async def api_positions(request: Request):
    _require_auth(request)
    positions = db.load_open_positions()
    today = date.today()
    result = []
    for pos in positions:
        dte = (date.fromisoformat(pos["expiration"]) - today).days
        current_value = get_spread_value(pos["short_symbol"], pos["long_symbol"])
        credit = pos["credit_received"]
        profit_pct = None
        total_profit = None
        if current_value is not None:
            profit_pct = round((credit - current_value) / credit * 100, 1)
            total_profit = round((credit - current_value) * pos["contracts"] * 100, 2)
        result.append({
            "id": pos["id"],
            "ticker": pos["ticker"],
            "type": pos["type"],
            "short_strike": pos["short_strike"],
            "long_strike": pos["long_strike"],
            "expiration": pos["expiration"],
            "dte": dte,
            "credit_received": credit,
            "contracts": pos["contracts"],
            "current_value": current_value,
            "profit_pct": profit_pct,
            "total_profit": total_profit,
            "profit_target": round(credit * 0.60, 2),
            "stop_loss": round(credit * 1.5, 2),
            "opened_at": pos.get("opened_at"),
            "regime": pos.get("regime"),
        })
    return {"positions": result}


@app.post("/api/positions/{position_id}/close")
async def api_close(request: Request, position_id: int):
    _require_auth(request)
    positions = db.load_open_positions()
    pos = next((p for p in positions if p["id"] == position_id), None)
    if not pos:
        raise HTTPException(404, f"Position {position_id} not found")

    current_value = get_spread_value(pos["short_symbol"], pos["long_symbol"])
    if current_value is None:
        raise HTTPException(503, "Could not fetch live price — try again")

    try:
        profit, order_id = close_position(pos, current_value)
        return {"success": True, "order_id": order_id,
                "close_value": current_value, "total_profit": profit}
    except Exception as e:
        raise HTTPException(500, f"Close failed: {e}")


def load_closed_trades() -> list[dict]:
    import sqlite3
    db.init_db()
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trades WHERE status = 'closed' ORDER BY closed_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/portfolio")
async def api_portfolio(request: Request):
    _require_auth(request)
    obp, equity = fetch_buying_power()
    open_positions = db.load_open_positions()
    closed_trades = load_closed_trades()

    total_pnl = sum(t.get("total_profit") or 0 for t in closed_trades)
    wins = [t for t in closed_trades if (t.get("total_profit") or 0) > 0]
    win_rate = round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else 0
    avg_pnl = round(total_pnl / len(closed_trades), 2) if closed_trades else 0

    # Build cumulative P&L series for chart
    chart_points = []
    cumulative = 0.0
    for t in closed_trades:
        cumulative += t.get("total_profit") or 0
        chart_points.append({
            "date": (t.get("closed_at") or "")[:10],
            "cumulative": round(cumulative, 2),
            "pnl": round(t.get("total_profit") or 0, 2),
        })

    history = []
    for t in reversed(closed_trades[-50:]):
        history.append({
            "id": t["id"],
            "ticker": t["ticker"],
            "type": t["type"],
            "short_strike": t["short_strike"],
            "long_strike": t["long_strike"],
            "expiration": t["expiration"],
            "contracts": t["contracts"],
            "credit_received": t["credit_received"],
            "close_value": t.get("close_value"),
            "total_profit": t.get("total_profit"),
            "profit_pct": t.get("profit_pct"),
            "close_reason": t.get("close_reason"),
            "closed_at": (t.get("closed_at") or "")[:10],
            "regime": t.get("regime"),
            "notes": t.get("notes") or "",
        })

    ticker_map: dict[str, float] = {}
    for t in closed_trades:
        tk = t["ticker"]
        ticker_map[tk] = round(ticker_map.get(tk, 0.0) + (t.get("total_profit") or 0.0), 2)
    by_ticker = sorted(
        [{"ticker": k, "total_profit": v} for k, v in ticker_map.items()],
        key=lambda x: x["total_profit"], reverse=True
    )

    return {
        "buying_power": obp,
        "equity": equity,
        "open_count": len(open_positions),
        "total_pnl": round(total_pnl, 2),
        "total_trades": len(closed_trades),
        "win_rate": win_rate,
        "avg_pnl": avg_pnl,
        "chart": chart_points,
        "history": history,
        "by_ticker": by_ticker,
    }


def _pipeline_is_running() -> bool:
    global _pipeline_proc
    return _pipeline_proc is not None and _pipeline_proc.poll() is None


@app.post("/api/pipeline/run")
async def api_pipeline_run(request: Request):
    _require_auth(request)
    global _pipeline_proc
    if _pipeline_is_running():
        return {"started": False, "reason": "Pipeline already running"}
    script = os.path.join(BASE_DIR, "run_full_pipeline.py")
    with open(_pipeline_log, "w") as log_f:
        _pipeline_proc = subprocess.Popen(
            [sys.executable, script],
            stdout=log_f, stderr=subprocess.STDOUT,
            cwd=BASE_DIR,
        )
    return {"started": True}


@app.get("/api/pipeline/logs")
async def api_pipeline_logs(request: Request):
    _require_auth(request)
    lines: list[str] = []
    try:
        with open(_pipeline_log) as f:
            lines = f.read().splitlines()[-150:]
    except FileNotFoundError:
        pass
    return {"running": _pipeline_is_running(), "lines": lines}


# ── HTML ───────────────────────────────────────────────────────────────

_CSS = """
:root {
  --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
  --text: #e8eaf0; --muted: #8b8fa8;
  --green: #22c55e; --red: #ef4444; --yellow: #f59e0b; --blue: #3b82f6;
  --radius: 12px; --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: var(--font); min-height: 100vh; }

nav { background: var(--surface); border-bottom: 1px solid var(--border);
      display: flex; align-items: center; padding: 0 16px; height: 52px;
      position: sticky; top: 0; z-index: 100; }
.logo { font-weight: 700; font-size: 16px; }
.logo span { color: var(--green); }
.tabs { display: flex; gap: 4px; margin-left: auto; }
.tab { padding: 6px 14px; border-radius: 8px; font-size: 14px; font-weight: 500;
       text-decoration: none; color: var(--muted); transition: all 0.15s; }
.tab.active { background: var(--bg); color: var(--text); }

.acct-bar { background: var(--surface); border-bottom: 1px solid var(--border);
             padding: 10px 16px; display: flex; gap: 20px; flex-wrap: wrap; align-items: center; }
.acct-stat .lbl { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
.acct-stat .val { font-size: 15px; font-weight: 600; margin-top: 1px; }

.content { padding: 16px; max-width: 600px; margin: 0 auto; }

.card { background: var(--surface); border: 1px solid var(--border);
        border-radius: var(--radius); margin-bottom: 16px; overflow: hidden; }
.card-hdr { padding: 14px 16px; display: flex; align-items: center; gap: 8px;
             border-bottom: 1px solid var(--border); }
.ticker { font-size: 20px; font-weight: 700; }
.badge { font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 6px; }
.badge-bull { background: rgba(34,197,94,.15); color: var(--green); }
.badge-bear { background: rgba(239,68,68,.15); color: var(--red); }
.badge-TRADE { background: rgba(34,197,94,.15); color: var(--green); }
.badge-WAIT  { background: rgba(245,158,11,.15); color: var(--yellow); }
.badge-SKIP  { background: rgba(239,68,68,.15); color: var(--red); }
.heat { margin-left: auto; font-size: 12px; }

.card-body { padding: 14px 16px; }

.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; }
.stat { background: var(--bg); border-radius: 8px; padding: 10px 12px; }
.stat .lbl { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; }
.stat .val { font-size: 16px; font-weight: 600; margin-top: 3px; }
.green { color: var(--green); } .red { color: var(--red); } .yellow { color: var(--yellow); }

.exit-row { display: flex; gap: 8px; margin-bottom: 12px; }
.exit-item { flex: 1; background: var(--bg); border-radius: 8px; padding: 8px 10px; }
.exit-item .lbl { font-size: 10px; color: var(--muted); text-transform: uppercase; }
.exit-item .val { font-size: 13px; font-weight: 600; margin-top: 2px; }

.qty-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.qty-lbl { font-size: 13px; color: var(--muted); white-space: nowrap; }
.qty-input { background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
              color: var(--text); font-size: 18px; font-weight: 600; width: 72px;
              text-align: center; padding: 8px; -webkit-appearance: none; }
.qty-input:focus { outline: none; border-color: var(--blue); }
.qty-btn { background: var(--border); border: none; color: var(--text);
            width: 36px; height: 36px; border-radius: 8px; font-size: 20px;
            cursor: pointer; display: flex; align-items: center; justify-content: center;
            -webkit-tap-highlight-color: transparent; }
.sugg { font-size: 12px; color: var(--muted); }

.btn-row { display: flex; gap: 10px; }
.btn { flex: 1; padding: 14px; border-radius: 10px; font-size: 16px; font-weight: 600;
        border: none; cursor: pointer; -webkit-tap-highlight-color: transparent; transition: opacity 0.15s; }
.btn:active { opacity: 0.7; }
.btn:disabled { opacity: 0.5; }
.btn-approve { background: var(--green); color: #000; }
.btn-skip { background: var(--border); color: var(--muted); }
.btn-close { background: var(--red); color: #fff; }

.result { padding: 12px 16px; border-top: 1px solid var(--border); font-size: 13px; }
.result-ok  { background: rgba(34,197,94,.08); color: var(--green); }
.result-err { background: rgba(239,68,68,.08); color: var(--red); }
.result-skip{ background: rgba(139,143,168,.08); color: var(--muted); }

.pbar { background: var(--border); border-radius: 4px; height: 4px; margin-top: 6px; }
.pbar-fill { height: 4px; border-radius: 4px; }

.section-hdr { font-size: 12px; color: var(--muted); font-weight: 600;
                text-transform: uppercase; letter-spacing: 0.5px; margin: 20px 0 10px; }
.section-hdr:first-child { margin-top: 0; }

.empty { text-align: center; padding: 48px 24px; color: var(--muted); }
.empty .icon { font-size: 40px; margin-bottom: 12px; }
.empty h3 { font-size: 16px; margin-bottom: 6px; color: var(--text); }

/* Portfolio */
.port-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 16px; }
.port-stat { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
              padding: 14px 16px; }
.port-stat .lbl { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
.port-stat .val { font-size: 22px; font-weight: 700; margin-top: 4px; }
.chart-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
               margin-bottom: 16px; overflow: hidden; }
.chart-hdr { padding: 14px 16px; display: flex; align-items: center; border-bottom: 1px solid var(--border); }
.chart-hdr .title { font-weight: 600; font-size: 14px; }
.chart-filter { display: flex; gap: 4px; margin-left: auto; }
.cf-btn { background: none; border: 1px solid var(--border); color: var(--muted); padding: 4px 10px;
           border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; transition: all 0.15s; }
.cf-btn.active { background: var(--blue); border-color: var(--blue); color: #fff; }
.chart-wrap { padding: 12px 16px 16px; height: 220px; position: relative; }
.chart-empty { display: flex; align-items: center; justify-content: center; height: 200px;
                color: var(--muted); font-size: 14px; }
.mini-stats { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-bottom: 16px; }
.mini-stat { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
              padding: 12px 14px; text-align: center; }
.mini-stat .lbl { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; }
.mini-stat .val { font-size: 18px; font-weight: 700; margin-top: 4px; }
.history-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.history-table th { text-align: left; padding: 8px 10px; color: var(--muted); font-size: 11px;
                     text-transform: uppercase; letter-spacing: 0.4px; border-bottom: 1px solid var(--border); }
.history-table td { padding: 10px 10px; border-bottom: 1px solid var(--border); }
.history-table tr:last-child td { border-bottom: none; }
.reason-badge { font-size: 10px; font-weight: 600; padding: 2px 6px; border-radius: 4px;
                 background: rgba(139,143,168,.15); color: var(--muted); }
.reason-profit_target { background: rgba(34,197,94,.12); color: var(--green); }
.reason-stop_loss, .reason-trailing_stop { background: rgba(239,68,68,.12); color: var(--red); }
.reason-time_stop, .reason-time_stop_eod { background: rgba(245,158,11,.12); color: var(--yellow); }
.reason-manual_close { background: rgba(59,130,246,.12); color: var(--blue); }

.analyst-note { background: rgba(59,130,246,.06); border: 1px solid rgba(59,130,246,.2);
                border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; font-size: 13px; }
.analyst-note .an-lbl { font-size: 10px; color: var(--blue); text-transform: uppercase;
                         letter-spacing: 0.5px; font-weight: 700; margin-bottom: 6px; }
.analyst-note .an-row { color: var(--muted); line-height: 1.5; margin-bottom: 4px; }
.analyst-note .an-row strong { color: var(--text); }
.analyst-note .an-reason { color: var(--text); margin-top: 6px; padding-top: 6px;
                             border-top: 1px solid rgba(59,130,246,.15); font-style: italic; }
.catalyst-warn { background: rgba(245,158,11,.08); border: 1px solid rgba(245,158,11,.25);
                  border-radius: 8px; padding: 8px 12px; margin-bottom: 12px;
                  font-size: 12px; color: var(--yellow); }

.spin { display: inline-block; width: 16px; height: 16px; border: 2px solid var(--border);
         border-top-color: var(--blue); border-radius: 50%; animation: spin 0.7s linear infinite;
         vertical-align: middle; }
@keyframes spin { to { transform: rotate(360deg); } }

#toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
          background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
          padding: 12px 20px; font-size: 14px; opacity: 0; transition: opacity 0.2s;
          pointer-events: none; white-space: nowrap; z-index: 999; }
#toast.show { opacity: 1; }

.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.7); display: none;
            align-items: flex-end; justify-content: center; z-index: 200; padding: 16px; }
.overlay.open { display: flex; }
.modal { background: var(--surface); border: 1px solid var(--border);
          border-radius: var(--radius); padding: 20px; width: 100%; max-width: 400px; }
.modal h3 { font-size: 18px; margin-bottom: 16px; }
.drow { display: flex; justify-content: space-between; padding: 6px 0;
         border-bottom: 1px solid var(--border); font-size: 14px; }
.drow:last-of-type { border-bottom: none; }
.drow .v { font-weight: 600; }
.modal-btns { display: flex; gap: 10px; margin-top: 16px; }

/* Run banner */
.run-banner { display: flex; align-items: center; justify-content: space-between;
              gap: 10px; margin-bottom: 14px; padding: 10px 14px;
              background: var(--surface); border: 1px solid var(--border);
              border-radius: var(--radius); font-size: 13px; }
.run-banner.stale { border-color: rgba(245,158,11,.35); background: rgba(245,158,11,.05); }
.run-status-text { color: var(--muted); }
.run-status-text strong { color: var(--text); }
.run-regime { font-size: 11px; color: var(--muted); margin-top: 2px; }
.btn-run { background: var(--blue); color: #fff; border: none; border-radius: 8px;
            padding: 7px 14px; font-size: 13px; font-weight: 600; cursor: pointer;
            white-space: nowrap; -webkit-tap-highlight-color: transparent; }
.btn-run:disabled { opacity: 0.6; cursor: default; }

/* Pipeline log modal */
.log-modal { background: var(--surface); border: 1px solid var(--border);
              border-radius: var(--radius); padding: 16px; width: 100%;
              max-width: 600px; max-height: 70vh; display: flex; flex-direction: column; }
.log-modal h3 { font-size: 16px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
.log-box { flex: 1; overflow-y: auto; background: var(--bg); border: 1px solid var(--border);
            border-radius: 8px; padding: 10px 12px; font-family: monospace; font-size: 12px;
            line-height: 1.6; color: #a8b0c8; white-space: pre-wrap; min-height: 200px; }

/* Notes */
.notes-area { width: 100%; background: var(--bg); border: 1px solid var(--border);
               border-radius: 8px; color: var(--text); font-size: 13px; padding: 8px 10px;
               resize: vertical; min-height: 60px; margin-bottom: 10px;
               font-family: var(--font); }
.notes-area:focus { outline: none; border-color: var(--blue); }
.notes-lbl { font-size: 11px; color: var(--muted); text-transform: uppercase;
              letter-spacing: 0.4px; margin-bottom: 5px; }
.history-note { font-size: 12px; color: var(--muted); font-style: italic;
                 padding: 2px 10px 8px; }
"""

_JS_COMMON = """
const fmt = n => n == null ? '—' : '$' + parseFloat(n).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
const fmtPct = n => n == null ? '—' : (n >= 0 ? '+' : '') + n.toFixed(1) + '%';

async function loadAccount() {
  try {
    const d = await fetch('/api/account').then(r => r.json());
    document.getElementById('bp').textContent = fmt(d.buying_power);
    document.getElementById('eq').textContent = fmt(d.equity);
  } catch(e) {}
}
loadAccount();

function toast(msg, err=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.color = err ? 'var(--red)' : 'var(--green)';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}

let _closeId = null, _closeData = null;
function openCloseModal(id, data) {
  _closeId = id; _closeData = data;
  const pc = data.profit_pct >= 0 ? 'green' : 'red';
  document.getElementById('modal-body').innerHTML = `
    <div class="drow"><span>${data.ticker} ${data.type}</span><span class="v">$${data.short_strike}/$${data.long_strike}</span></div>
    <div class="drow"><span>Credit Received</span><span class="v">${fmt(data.credit_received)}</span></div>
    <div class="drow"><span>Current Value</span><span class="v">${fmt(data.current_value)}</span></div>
    <div class="drow"><span>P&L</span><span class="v ${pc}">${fmt(data.total_profit)} (${fmtPct(data.profit_pct)})</span></div>
    <div class="drow"><span>Contracts</span><span class="v">${data.contracts}</span></div>`;
  document.getElementById('overlay').classList.add('open');
}
function closeModal() { document.getElementById('overlay').classList.remove('open'); _closeId = null; }
async function confirmClose() {
  if (!_closeId) return;
  const btn = document.getElementById('modal-confirm');
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>';
  try {
    const d = await fetch('/api/positions/' + _closeId + '/close', {method:'POST'}).then(r => r.json());
    if (d.success) { toast('Closed · P&L: ' + fmt(d.total_profit)); closeModal(); if (typeof loadPositions==='function') loadPositions(); }
    else toast('Close failed', true);
  } catch(e) { toast('Error: ' + e.message, true); }
  finally { btn.disabled = false; btn.textContent = 'Close Position'; }
}
"""

def _page(active_tab: str, page_content: str) -> str:
    portfolio_cls = "active" if active_tab == "portfolio" else ""
    approval_cls  = "active" if active_tab == "approval"  else ""
    positions_cls = "active" if active_tab == "positions" else ""
    show_acct_bar = active_tab != "portfolio"
    acct_bar = """
<div class="acct-bar">
  <div class="acct-stat"><div class="lbl">Buying Power</div><div class="val" id="bp">—</div></div>
  <div class="acct-stat"><div class="lbl">Total Equity</div><div class="val" id="eq">—</div></div>
</div>""" if show_acct_bar else ""
    # Split HTML from JS so the <script> block is never nested inside the content div
    _parts = page_content.split("<!-- JS -->", 1)
    html_part = _parts[0]
    js_part   = _parts[1] if len(_parts) > 1 else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>News Spread Engine</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
<style>{_CSS}</style>
</head>
<body>
<nav>
  <div class="logo">News<span>Spread</span></div>
  <div class="tabs">
    <a href="/portfolio" class="tab {portfolio_cls}">Portfolio</a>
    <a href="/approval"  class="tab {approval_cls}">Approval</a>
    <a href="/positions" class="tab {positions_cls}">Positions</a>
  </div>
</nav>
{acct_bar}
<div class="content">{html_part}</div>
<div id="toast"></div>
<div class="overlay" id="overlay">
  <div class="modal">
    <h3>Confirm Close</h3>
    <div id="modal-body"></div>
    <div class="modal-btns">
      <button class="btn btn-skip" onclick="closeModal()">Cancel</button>
      <button class="btn btn-close" id="modal-confirm" onclick="confirmClose()">Close Position</button>
    </div>
  </div>
</div>
<script>{_JS_COMMON}</script>
{js_part}
</body>
</html>"""


_PORTFOLIO_CONTENT = """
<div id="port-stats" class="port-stats">
  <div class="port-stat"><div class="lbl">Total Equity</div><div class="val" id="p-equity">—</div></div>
  <div class="port-stat"><div class="lbl">Option Buying Power</div><div class="val" id="p-bp">—</div></div>
  <div class="port-stat"><div class="lbl">Total P&amp;L</div><div class="val" id="p-pnl">—</div></div>
  <div class="port-stat"><div class="lbl">Open Positions</div><div class="val" id="p-open">—</div></div>
</div>

<div class="chart-card">
  <div class="chart-hdr">
    <span class="title">Cumulative P&amp;L</span>
    <div class="chart-filter">
      <button class="cf-btn" onclick="setFilter('1W',this)">1W</button>
      <button class="cf-btn" onclick="setFilter('1M',this)">1M</button>
      <button class="cf-btn active" onclick="setFilter('ALL',this)">ALL</button>
    </div>
  </div>
  <div class="chart-wrap">
    <div class="chart-empty" id="chart-empty" style="display:none">No closed trades yet</div>
    <canvas id="pnl-chart"></canvas>
  </div>
</div>

<div class="mini-stats">
  <div class="mini-stat"><div class="lbl">Win Rate</div><div class="val" id="p-wr">—</div></div>
  <div class="mini-stat"><div class="lbl">Avg P&amp;L</div><div class="val" id="p-avg">—</div></div>
  <div class="mini-stat"><div class="lbl">Total Trades</div><div class="val" id="p-total">—</div></div>
</div>

<div class="section-hdr">P&amp;L by Ticker</div>
<div class="chart-card" id="ticker-chart-card" style="display:none">
  <div class="chart-wrap" style="height:auto;padding-bottom:16px">
    <canvas id="ticker-chart"></canvas>
  </div>
</div>
<div id="ticker-empty" style="display:none" class="empty" style="padding:20px">
  <h3 style="font-size:14px;color:var(--muted)">No closed trades yet</h3>
</div>

<div class="section-hdr">Trade History</div>
<div class="card">
  <div id="history-body" style="overflow-x:auto">
    <div class="empty"><span class="spin"></span></div>
  </div>
</div>
<!-- JS -->
<script>
let _chartData = [], _chartInstance = null, _tickerChartInstance = null, _activeFilter = 'ALL';

function filterPoints(points, filter) {
  if (filter === 'ALL' || !points.length) return points;
  const now = new Date();
  const cutoff = new Date(now);
  if (filter === '1W') cutoff.setDate(now.getDate() - 7);
  if (filter === '1M') cutoff.setMonth(now.getMonth() - 1);
  return points.filter(p => new Date(p.date) >= cutoff);
}

function buildChart(points) {
  const canvas = document.getElementById('pnl-chart');
  const empty  = document.getElementById('chart-empty');
  if (!points.length) {
    canvas.style.display = 'none';
    empty.style.display = 'flex';
    return;
  }
  canvas.style.display = 'block';
  empty.style.display = 'none';

  const labels = points.map(p => p.date);
  const values = points.map(p => p.cumulative);
  const finalVal = values[values.length - 1] || 0;
  const lineColor = finalVal >= 0 ? '#22c55e' : '#ef4444';
  const fillColor = finalVal >= 0 ? 'rgba(34,197,94,0.1)' : 'rgba(239,68,68,0.1)';

  if (_chartInstance) _chartInstance.destroy();
  _chartInstance = new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data: values,
        borderColor: lineColor,
        backgroundColor: fillColor,
        borderWidth: 2,
        pointRadius: points.length > 30 ? 0 : 4,
        pointHoverRadius: 6,
        pointBackgroundColor: lineColor,
        fill: true,
        tension: 0.3,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1a1d27',
          borderColor: '#2a2d3a',
          borderWidth: 1,
          titleColor: '#8b8fa8',
          bodyColor: '#e8eaf0',
          callbacks: {
            label: ctx => ' $' + ctx.parsed.y.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})
          }
        }
      },
      scales: {
        x: {
          grid: { color: '#2a2d3a' },
          ticks: { color: '#8b8fa8', maxTicksLimit: 6, font: {size:11} }
        },
        y: {
          grid: { color: '#2a2d3a' },
          ticks: {
            color: '#8b8fa8', font: {size:11},
            callback: v => '$' + v.toLocaleString()
          }
        }
      }
    }
  });
}

function setFilter(f, btn) {
  _activeFilter = f;
  document.querySelectorAll('.cf-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  buildChart(filterPoints(_chartData, _activeFilter));
}

const reasonLabel = {
  profit_target: 'Profit Target', stop_loss: 'Stop Loss', trailing_stop: 'Trailing Stop',
  time_stop: 'Time Stop', time_stop_eod: 'Time Stop EOD', manual_close: 'Manual Close'
};

function renderHistory(history) {
  const el = document.getElementById('history-body');
  if (!history.length) {
    el.innerHTML = '<div class="empty" style="padding:32px"><div class="icon">📋</div><h3>No closed trades yet</h3></div>';
    return;
  }
  let rows = '';
  for (const t of history) {
    const pnlCls = (t.total_profit || 0) >= 0 ? 'green' : 'red';
    const reason = t.close_reason || '';
    rows += `<tr>
      <td><strong>${t.ticker}</strong></td>
      <td>$${t.short_strike}/$${t.long_strike}</td>
      <td>${t.closed_at}</td>
      <td class="${pnlCls}">${fmt(t.total_profit)}</td>
      <td>${t.profit_pct != null ? (t.profit_pct >= 0 ? '+' : '') + t.profit_pct.toFixed(1) + '%' : '—'}</td>
      <td><span class="reason-badge reason-${reason}">${reasonLabel[reason] || reason}</span></td>
    </tr>`;
    if (t.notes) rows += `<tr><td colspan="6" class="history-note">${t.notes}</td></tr>`;
  }
  el.innerHTML = `<table class="history-table">
    <thead><tr><th>Ticker</th><th>Strikes</th><th>Closed</th><th>P&L</th><th>%</th><th>Reason</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

async function loadPortfolio() {
  try {
    const d = await fetch('/api/portfolio').then(r => r.json());
    document.getElementById('p-equity').textContent = fmt(d.equity);
    document.getElementById('p-bp').textContent     = fmt(d.buying_power);

    const pnlEl = document.getElementById('p-pnl');
    pnlEl.textContent = fmt(d.total_pnl);
    pnlEl.className = 'val ' + ((d.total_pnl || 0) >= 0 ? 'green' : 'red');

    document.getElementById('p-open').textContent  = d.open_count;
    document.getElementById('p-wr').textContent    = d.win_rate + '%';
    document.getElementById('p-avg').textContent   = fmt(d.avg_pnl);
    document.getElementById('p-total').textContent = d.total_trades;

    _chartData = d.chart;
    buildChart(filterPoints(_chartData, _activeFilter));
    buildTickerChart(d.by_ticker || []);
    renderHistory(d.history);
  } catch(e) {
    document.getElementById('history-body').innerHTML =
      `<div class="empty"><div class="icon">⚠️</div><h3>Error</h3><p>${e.message}</p></div>`;
  }
}

function buildTickerChart(byTicker) {
  const card = document.getElementById('ticker-chart-card');
  const empty = document.getElementById('ticker-empty');
  if (!byTicker.length) {
    card.style.display = 'none';
    empty.style.display = 'block';
    return;
  }
  card.style.display = 'block';
  empty.style.display = 'none';
  const canvas = document.getElementById('ticker-chart');
  const labels = byTicker.map(r => r.ticker);
  const values = byTicker.map(r => r.total_profit);
  const colors = values.map(v => v >= 0 ? 'rgba(34,197,94,0.75)' : 'rgba(239,68,68,0.75)');
  const borders = values.map(v => v >= 0 ? '#22c55e' : '#ef4444');
  const barH = Math.max(28, labels.length * 34);
  canvas.style.height = barH + 'px';
  canvas.height = barH;
  if (_tickerChartInstance) _tickerChartInstance.destroy();
  _tickerChartInstance = new Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        data: values,
        backgroundColor: colors,
        borderColor: borders,
        borderWidth: 1,
        borderRadius: 4,
      }]
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1a1d27',
          borderColor: '#2a2d3a',
          borderWidth: 1,
          titleColor: '#8b8fa8',
          bodyColor: '#e8eaf0',
          callbacks: {
            label: ctx => ' ' + (ctx.parsed.x >= 0 ? '+' : '') + '$' + ctx.parsed.x.toLocaleString('en-US', {minimumFractionDigits:2,maximumFractionDigits:2})
          }
        }
      },
      scales: {
        x: {
          grid: { color: '#2a2d3a' },
          ticks: { color: '#8b8fa8', font: {size:11},
                   callback: v => (v >= 0 ? '+' : '') + '$' + v.toLocaleString() }
        },
        y: { grid: { display: false }, ticks: { color: '#e8eaf0', font: {size:12,weight:'600'} } }
      }
    }
  });
}

loadPortfolio();
</script>
"""

_APPROVAL_CONTENT = """
<div id="run-banner" class="run-banner" style="display:none"></div>
<div id="trades"></div>

<div class="overlay" id="pipeline-overlay">
  <div class="log-modal">
    <h3>Pipeline Run <span class="spin" id="pipe-spin" style="display:none"></span></h3>
    <div class="log-box" id="pipe-log">Starting…</div>
    <div class="modal-btns" style="margin-top:12px">
      <button class="btn btn-skip" onclick="closePipelineModal()">Close</button>
    </div>
  </div>
</div>

<!-- JS -->
<script>
let _trades = [], _results = {}, _pipePoller = null;

function renderAnalystNote(r) {
  if (!r || (!r.what && !r.reason)) return '';
  const hasCatalyst = r.catalyst && !r.catalyst.toLowerCase().startsWith('none');
  let html = '<div class="analyst-note"><div class="an-lbl">Analyst Note</div>';
  if (r.what) html += `<div class="an-row"><strong>Event:</strong> ${r.what}</div>`;
  if (r.how)  html += `<div class="an-row"><strong>Price impact:</strong> ${r.how}</div>`;
  if (r.reason) html += `<div class="an-reason">${r.reason}</div>`;
  html += '</div>';
  if (hasCatalyst) {
    html = `<div class="catalyst-warn">⚠ Catalyst: ${r.catalyst}</div>` + html;
  }
  return html;
}

function heatColor(h) {
  if (h == null) return 'var(--muted)';
  if (h <= 3) return 'var(--green)';
  if (h <= 6) return 'var(--yellow)';
  return 'var(--red)';
}

function renderResult(r) {
  if (!r) return '';
  if (r.type === 'placed') return `<div class="result result-ok">✓ Order placed · ID: ${r.order_id} · ${r.contracts} contract(s)</div>`;
  if (r.type === 'skipped') return `<div class="result result-skip">⏭ Skipped</div>`;
  if (r.type === 'error')   return `<div class="result result-err">✗ ${r.msg}</div>`;
  return '';
}

function renderTrades() {
  const el = document.getElementById('trades');
  if (!_trades.length) {
    el.innerHTML = '<div class="empty"><div class="icon">📭</div><h3>No trade data</h3><p>Run the pipeline first.</p></div>';
    return;
  }
  const tradeable = _trades.filter(t => t.recommendation === 'TRADE');
  const others    = _trades.filter(t => t.recommendation !== 'TRADE');
  let html = '';

  if (tradeable.length) {
    html += `<div class="section-hdr">Claude Recommendations — ${tradeable.length} Trade(s)</div>`;
    for (const t of tradeable) {
      const res = _results[t.ticker];
      const tc = t.type.includes('Bull') ? 'badge-bull' : 'badge-bear';
      const done = !!res;
      html += `
      <div class="card" id="card-${t.ticker}">
        <div class="card-hdr">
          <span class="ticker">${t.ticker}</span>
          <span class="badge ${tc}">${t.type}</span>
          <span class="badge badge-TRADE">TRADE</span>
          ${t.heat != null ? `<span class="heat" style="color:${heatColor(t.heat)}">Heat ${t.heat}/10</span>` : ''}
        </div>
        <div class="card-body">
          <div class="grid2">
            <div class="stat"><div class="lbl">Net Credit / Contract</div><div class="val green">${fmt(t.net_credit * 100)}</div></div>
            <div class="stat"><div class="lbl">Max Loss / Contract</div><div class="val red">${fmt(t.max_loss * 100)}</div></div>
            <div class="stat"><div class="lbl">ROI</div><div class="val">${t.roi}</div></div>
            <div class="stat"><div class="lbl">PoP</div><div class="val">${t.pop}</div></div>
          </div>
          <div class="grid2">
            <div class="stat"><div class="lbl">Strikes</div><div class="val">$${t.short_strike}/$${t.long_strike}</div></div>
            <div class="stat"><div class="lbl">Expiry</div><div class="val">${t.exp_date} (${t.dte}d)</div></div>
          </div>
          <div class="exit-row">
            <div class="exit-item"><div class="lbl">Profit Target (40%)</div><div class="val green">${fmt(t.profit_target * 100)}</div></div>
            <div class="exit-item"><div class="lbl">Stop Loss (1.5x)</div><div class="val red">${fmt(t.stop_loss * 100)}</div></div>
          </div>
          ${renderAnalystNote(t.rationale)}
          ${done ? '' : `
          <div class="notes-lbl">Trade Note (optional)</div>
          <textarea class="notes-area" id="note-${t.ticker}" placeholder="Why you're taking or skipping this trade…"></textarea>
          <div class="qty-row">
            <button class="qty-btn" onclick="adj('${t.ticker}',-1)">−</button>
            <input class="qty-input" type="number" id="qty-${t.ticker}" value="${t.suggested_contracts}" min="1" max="99">
            <button class="qty-btn" onclick="adj('${t.ticker}',1)">+</button>
            <span class="sugg">Suggested: ${t.suggested_contracts}</span>
          </div>
          <div class="btn-row">
            <button class="btn btn-approve" id="approve-${t.ticker}" onclick="approve('${t.ticker}')">Approve</button>
            <button class="btn btn-skip" id="skip-${t.ticker}" onclick="skip('${t.ticker}')">Skip</button>
          </div>`}
        </div>
        ${renderResult(res)}
      </div>`;
    }
  }

  if (others.length) {
    html += `<div class="section-hdr">Other Candidates</div>`;
    for (const t of others) {
      const tc = t.type.includes('Bull') ? 'badge-bull' : 'badge-bear';
      const rc = `badge-${t.recommendation}`;
      html += `
      <div class="card">
        <div class="card-hdr">
          <span class="ticker">${t.ticker}</span>
          <span class="badge ${tc}">${t.type}</span>
          <span class="badge ${rc}">${t.recommendation}</span>
        </div>
        <div class="card-body">
          <div class="grid2">
            <div class="stat"><div class="lbl">Net Credit / Contract</div><div class="val">${fmt(t.net_credit * 100)}</div></div>
            <div class="stat"><div class="lbl">Max Loss / Contract</div><div class="val">${fmt(t.max_loss * 100)}</div></div>
            <div class="stat"><div class="lbl">ROI</div><div class="val">${t.roi}</div></div>
            <div class="stat"><div class="lbl">PoP</div><div class="val">${t.pop}</div></div>
          </div>
        </div>
      </div>`;
    }
  }
  el.innerHTML = html;
}

function adj(ticker, d) {
  const el = document.getElementById('qty-' + ticker);
  el.value = Math.max(1, parseInt(el.value || 1) + d);
}

async function approve(ticker) {
  const contracts = parseInt(document.getElementById('qty-' + ticker).value || 1);
  if (contracts < 1) { toast('Enter at least 1 contract', true); return; }
  const notes = (document.getElementById('note-' + ticker) || {}).value || '';
  const ab = document.getElementById('approve-' + ticker);
  const sb = document.getElementById('skip-' + ticker);
  ab.disabled = sb.disabled = true;
  ab.innerHTML = '<span class="spin"></span> Placing…';
  try {
    const d = await fetch('/api/trades/' + ticker + '/approve', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({contracts, notes})
    }).then(r => r.json());
    if (d.success) {
      _results[ticker] = {type:'placed', order_id: d.order_id, contracts};
      toast(ticker + ' order placed · ID: ' + d.order_id);
      loadAccount();
    } else {
      _results[ticker] = {type:'error', msg: d.detail || 'Unknown error'};
      toast('Order failed', true);
    }
  } catch(e) {
    _results[ticker] = {type:'error', msg: e.message};
    toast('Error: ' + e.message, true);
  }
  renderTrades();
}

function skip(ticker) {
  _results[ticker] = {type:'skipped'};
  toast(ticker + ' skipped');
  renderTrades();
}

function renderRunBanner(lr) {
  const el = document.getElementById('run-banner');
  if (!lr) { el.style.display = 'none'; return; }
  el.style.display = 'flex';
  el.className = 'run-banner' + (lr.stale ? ' stale' : '');
  const color = lr.stale ? 'var(--yellow)' : 'var(--green)';
  el.innerHTML = `
    <div>
      <div class="run-status-text" style="color:${color}"><strong>${lr.label}</strong></div>
      <div class="run-regime">Regime: ${lr.regime}</div>
    </div>
    <button class="btn-run" onclick="runPipeline()">Re-run Pipeline</button>`;
}

async function runPipeline() {
  document.getElementById('pipeline-overlay').classList.add('open');
  document.getElementById('pipe-log').textContent = 'Starting pipeline…';
  document.getElementById('pipe-spin').style.display = 'inline-block';
  try {
    const d = await fetch('/api/pipeline/run', {method:'POST'}).then(r => r.json());
    if (!d.started) {
      document.getElementById('pipe-log').textContent = d.reason || 'Could not start.';
      document.getElementById('pipe-spin').style.display = 'none';
      return;
    }
  } catch(e) {
    document.getElementById('pipe-log').textContent = 'Error: ' + e.message;
    document.getElementById('pipe-spin').style.display = 'none';
    return;
  }
  _pipePoller = setInterval(pollPipelineLogs, 2000);
}

async function pollPipelineLogs() {
  try {
    const d = await fetch('/api/pipeline/logs').then(r => r.json());
    const box = document.getElementById('pipe-log');
    box.textContent = d.lines.join('\\n') || '(no output yet)';
    box.scrollTop = box.scrollHeight;
    if (!d.running) {
      clearInterval(_pipePoller); _pipePoller = null;
      document.getElementById('pipe-spin').style.display = 'none';
      loadTrades();
    }
  } catch(e) {}
}

function closePipelineModal() {
  document.getElementById('pipeline-overlay').classList.remove('open');
  if (_pipePoller) { clearInterval(_pipePoller); _pipePoller = null; }
}

async function loadTrades() {
  const el = document.getElementById('trades');
  el.innerHTML = '<div class="empty"><span class="spin"></span></div>';
  try {
    const d = await fetch('/api/trades').then(r => r.json());
    if (d.error) {
      renderRunBanner(d.last_run || null);
      el.innerHTML = `<div class="empty"><div class="icon">⚠️</div><h3>No Data</h3><p>${d.error}</p></div>`;
      return;
    }
    renderRunBanner(d.last_run || null);
    _trades = d.trades;
    renderTrades();
  } catch(e) {
    renderRunBanner({ran: false, label: 'Pipeline not run', stale: true, regime: '—'});
    el.innerHTML = `<div class="empty"><div class="icon">⚠️</div><h3>Error</h3><p>${e.message}</p></div>`;
  }
}
loadTrades();
</script>
"""

_POSITIONS_CONTENT = """
<div id="positions"></div>
<!-- JS -->
<script>
function pnlClass(pct) { return pct == null ? '' : pct >= 0 ? 'green' : 'red'; }

function renderPositions(list) {
  const el = document.getElementById('positions');
  if (!list.length) {
    el.innerHTML = '<div class="empty"><div class="icon">📊</div><h3>No Open Positions</h3><p>Approved trades will appear here.</p></div>';
    return;
  }
  let html = `<div class="section-hdr">Open Positions (${list.length})</div>`;
  for (const p of list) {
    const tc = p.type.includes('Bull') ? 'badge-bull' : 'badge-bear';
    const pct = p.profit_pct;
    const barColor = pct == null ? 'var(--muted)' : pct >= 0 ? 'var(--green)' : 'var(--red)';
    const barWidth = pct == null ? 0 : Math.min(100, Math.abs(pct));
    html += `
    <div class="card">
      <div class="card-hdr">
        <span class="ticker">${p.ticker}</span>
        <span class="badge ${tc}">${p.type}</span>
        <span style="margin-left:auto;font-size:12px;color:var(--muted)">DTE ${p.dte}</span>
      </div>
      <div class="card-body">
        <div class="grid2">
          <div class="stat"><div class="lbl">Credit</div><div class="val green">${fmt(p.credit_received)}</div></div>
          <div class="stat"><div class="lbl">Current Value</div><div class="val">${p.current_value != null ? fmt(p.current_value) : '<span class="spin"></span>'}</div></div>
          <div class="stat">
            <div class="lbl">P&L</div>
            <div class="val ${pnlClass(pct)}">${fmt(p.total_profit)}</div>
            <div class="pbar"><div class="pbar-fill" style="background:${barColor};width:${barWidth}%"></div></div>
          </div>
          <div class="stat"><div class="lbl">P&L %</div><div class="val ${pnlClass(pct)}">${fmtPct(pct)}</div></div>
        </div>
        <div class="grid2">
          <div class="stat"><div class="lbl">Strikes</div><div class="val">$${p.short_strike}/$${p.long_strike}</div></div>
          <div class="stat"><div class="lbl">Expiry</div><div class="val">${p.expiration}</div></div>
        </div>
        <div class="exit-row">
          <div class="exit-item"><div class="lbl">Profit Target</div><div class="val green">${fmt(p.profit_target)}</div></div>
          <div class="exit-item"><div class="lbl">Stop Loss</div><div class="val red">${fmt(p.stop_loss)}</div></div>
          ${p.regime ? `<div class="exit-item"><div class="lbl">Regime</div><div class="val">${p.regime}</div></div>` : ''}
        </div>
        <div class="btn-row">
          <button class="btn btn-close" onclick='openCloseModal(${p.id}, ${JSON.stringify(p)})'>Close Position</button>
        </div>
      </div>
    </div>`;
  }
  el.innerHTML = html;
}

async function loadPositions() {
  try {
    const d = await fetch('/api/positions').then(r => r.json());
    renderPositions(d.positions);
  } catch(e) {
    document.getElementById('positions').innerHTML =
      `<div class="empty"><div class="icon">⚠️</div><h3>Error</h3><p>${e.message}</p></div>`;
  }
}
loadPositions();
setInterval(loadPositions, 60000);
</script>
"""


@app.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/login")
    return HTMLResponse(_page("portfolio", _PORTFOLIO_CONTENT))


@app.get("/approval", response_class=HTMLResponse)
async def approval_page(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/login")
    return HTMLResponse(_page("approval", _APPROVAL_CONTENT))


@app.get("/positions", response_class=HTMLResponse)
async def positions_page(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/login")
    return HTMLResponse(_page("positions", _POSITIONS_CONTENT))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web_app:app", host="0.0.0.0", port=8000, reload=True)
