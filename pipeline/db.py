"""
Shared SQLite helper for trade persistence.

Schema: single 'trades' table.  Open positions have status='open';
closed positions are updated in-place to status='closed'.

DB lives at: data/trades.db  (relative to project root)
"""
import sqlite3
import os
from datetime import datetime

# Resolve to <project_root>/data/trades.db regardless of cwd
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_PROJECT_ROOT, "data", "trades.db")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Entry
    ticker              TEXT    NOT NULL,
    type                TEXT    NOT NULL,
    short_strike        REAL    NOT NULL,
    long_strike         REAL    NOT NULL,
    expiration          TEXT    NOT NULL,
    dte_at_entry        INTEGER NOT NULL,
    credit_received     REAL    NOT NULL,
    max_profit          REAL    NOT NULL,
    max_loss            REAL    NOT NULL,
    contracts           INTEGER NOT NULL,
    short_symbol        TEXT    NOT NULL,
    long_symbol         TEXT    NOT NULL,
    tradier_order_id    TEXT,
    opened_at           TEXT    NOT NULL,
    profit_target_pct   REAL    NOT NULL DEFAULT 0.40,
    stop_loss_pct       REAL    NOT NULL DEFAULT 1.50,
    regime              TEXT,
    -- Lifecycle
    status              TEXT    NOT NULL DEFAULT 'open',
    -- Close (populated when status = 'closed')
    closed_at           TEXT,
    close_reason        TEXT,
    close_value         REAL,
    profit_per_contract REAL,
    total_profit        REAL,
    profit_pct          REAL,
    close_order_id      TEXT
)
"""

_MIGRATIONS = [
    "ALTER TABLE trades ADD COLUMN regime TEXT",
    "ALTER TABLE trades ADD COLUMN notes TEXT",
    "ALTER TABLE trades ADD COLUMN alert_sent INTEGER NOT NULL DEFAULT 0",
]


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the trades table if it doesn't exist yet, then run migrations."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = _get_conn()
    conn.execute(_CREATE_TABLE)
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
        except Exception:
            pass  # column already exists
    conn.commit()
    conn.close()


def insert_open_trade(position: dict, status: str = "open") -> int:
    """
    Insert a new trade record.  status should be 'open' or 'pending'.
    Returns the new row id.
    """
    init_db()
    conn = _get_conn()
    cur = conn.execute(
        """
        INSERT INTO trades (
            ticker, type, short_strike, long_strike, expiration,
            dte_at_entry, credit_received, max_profit, max_loss,
            contracts, short_symbol, long_symbol, tradier_order_id,
            opened_at, profit_target_pct, stop_loss_pct, regime, status
        ) VALUES (
            :ticker, :type, :short_strike, :long_strike, :expiration,
            :dte_at_entry, :credit_received, :max_profit, :max_loss,
            :contracts, :short_symbol, :long_symbol, :tradier_order_id,
            :opened_at, :profit_target_pct, :stop_loss_pct, :regime, :status
        )
        """,
        {**position, "regime": position.get("regime"), "status": status},
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def update_trade_status(trade_id: int, status: str):
    """Update lifecycle status (e.g. pending → open, closing → closed)."""
    init_db()
    conn = _get_conn()
    conn.execute("UPDATE trades SET status = ? WHERE id = ?", (status, trade_id))
    conn.commit()
    conn.close()


def delete_trade(trade_id: int):
    """Hard-delete a trade row (used when a pending order is rejected/expired)."""
    init_db()
    conn = _get_conn()
    conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()


def mark_closing(trade_id: int, close_order_id: str, close_value: float):
    """Record a submitted close order without finalising the trade yet."""
    init_db()
    conn = _get_conn()
    conn.execute(
        "UPDATE trades SET status = 'closing', close_order_id = ?, close_value = ? WHERE id = ?",
        (close_order_id, close_value, trade_id),
    )
    conn.commit()
    conn.close()


def load_active_positions() -> list[dict]:
    """Return open + pending + closing trades for the UI."""
    init_db()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM trades WHERE status IN ('open','pending','closing') ORDER BY opened_at"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def load_open_positions() -> list[dict]:
    """Return all open trades as plain dicts (includes 'id' key)."""
    init_db()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def save_trade_notes(trade_id: int, notes: str):
    """Update the notes field on a trade record."""
    init_db()
    conn = _get_conn()
    conn.execute("UPDATE trades SET notes = ? WHERE id = ?", (notes, trade_id))
    conn.commit()
    conn.close()


def mark_alert_sent(trade_id: int):
    """Mark that a P&L alert email has been sent for this position."""
    init_db()
    conn = _get_conn()
    conn.execute("UPDATE trades SET alert_sent = 1 WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()


def close_trade(
    trade_id: int,
    close_reason: str,
    close_value: float,
    profit_per_contract: float,
    total_profit: float,
    profit_pct: float,
    close_order_id: str,
):
    """Mark a trade as closed and record the exit details."""
    init_db()
    conn = _get_conn()
    conn.execute(
        """
        UPDATE trades SET
            status              = 'closed',
            closed_at           = ?,
            close_reason        = ?,
            close_value         = ?,
            profit_per_contract = ?,
            total_profit        = ?,
            profit_pct          = ?,
            close_order_id      = ?
        WHERE id = ?
        """,
        (
            datetime.now().isoformat(),
            close_reason,
            close_value,
            profit_per_contract,
            total_profit,
            profit_pct,
            close_order_id,
            trade_id,
        ),
    )
    conn.commit()
    conn.close()
