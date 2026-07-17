# -*- coding: utf-8 -*-
"""
record_trade.py

Manually record a real capital options trade to live_trades.db.
Use this when you've placed a trade in your Public.com account
and want to start tracking it immediately.

Usage:
    python record_trade.py enter    — record a new trade
    python record_trade.py exit     — record an exit
    python record_trade.py list     — show all open live trades
    python record_trade.py summary  — show P&L summary

The live monitor will automatically pick up any trades recorded here
and start tracking them once live_monitor.py is running.
"""

import sqlite3
import sys
import os
import pytz
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

LIVE_DB_PATH    = LIVE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_trades.db")
MARKET_TIMEZONE = "US/Eastern"


# =============================================================================
# DATABASE SETUP
# =============================================================================

def init_live_db():
    """Create live_trades.db and tables if they don't exist."""
    conn = sqlite3.connect(LIVE_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS live_trades (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_contract     TEXT NOT NULL,
            ticker              TEXT,
            contract_type       TEXT,
            strike              REAL,
            expiration          TEXT,

            entry_date          TEXT NOT NULL,
            entry_time          TEXT NOT NULL,
            exit_date           TEXT,
            exit_time           TEXT,

            status              TEXT DEFAULT 'OPEN',
            exit_reason         TEXT,

            entry_price         REAL NOT NULL,
            exit_price          REAL,
            contracts           INTEGER DEFAULT 1,
            total_cost          REAL,

            pnl                 REAL,
            pnl_pct             REAL,
            hold_days           REAL,

            dte_at_entry        INTEGER,
            target_price        REAL,
            stop_price          REAL,

            -- Live-specific fields
            real_order_id       TEXT,
            fill_price          REAL,
            confirmed_by        TEXT DEFAULT 'manual',
            capital_at_risk     REAL,

            notes               TEXT,
            created_at          TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS live_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id        INTEGER NOT NULL,
            snapshot_time   TEXT NOT NULL,
            current_price   REAL,
            bid             REAL,
            ask             REAL,
            pnl             REAL,
            pnl_pct         REAL,
            current_dte     INTEGER,
            dynamic_stop    REAL,
            stop_triggered  INTEGER DEFAULT 0,
            FOREIGN KEY (trade_id) REFERENCES live_trades(id)
        )
    """)

    conn.commit()
    conn.close()


def get_conn():
    conn = sqlite3.connect(LIVE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# =============================================================================
# HELPERS
# =============================================================================

def parse_contract(symbol):
    """
    Parse OSI contract symbol into components.
    Format: TICKER[YYMMDD][C/P][STRIKE * 1000]
    Example: TSLA260529C00450000
    """
    try:
        # Find where digits start after the ticker
        i = 0
        while i < len(symbol) and not symbol[i].isdigit():
            i += 1

        ticker      = symbol[:i]
        date_str    = symbol[i:i+6]
        cp          = symbol[i+6]
        strike_str  = symbol[i+7:]

        exp_date    = datetime.strptime(date_str, "%y%m%d")
        expiration  = exp_date.strftime("%Y-%m-%d")
        strike      = int(strike_str) / 1000
        contract_type = "CALL" if cp == "C" else "PUT"

        eastern  = pytz.timezone(MARKET_TIMEZONE)
        today    = datetime.now(eastern).date()
        dte      = (exp_date.date() - today).days

        return {
            "ticker":         ticker,
            "expiration":     expiration,
            "contract_type":  contract_type,
            "strike":         strike,
            "dte_at_entry":   max(dte, 0),
        }
    except Exception as e:
        return None


def fmt_pnl(pnl):
    if pnl is None:
        return "N/A"
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.2f}"


def fmt_pct(pct):
    if pct is None:
        return "N/A"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


# =============================================================================
# ENTER TRADE
# =============================================================================

def enter_trade():
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)

    print(f"\n{'='*55}")
    print(f"  📈 RECORD LIVE TRADE ENTRY")
    print(f"  {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"{'='*55}\n")

    # Contract symbol
    symbol = input("  Contract symbol (e.g. TSLA260529C00450000): ").strip().upper()
    if not symbol:
        print("  Aborted.")
        return

    parsed = parse_contract(symbol)
    if not parsed:
        print(f"  ⚠️  Could not parse contract symbol. Continuing with manual entry.")
        parsed = {}

    if parsed:
        print(f"\n  Parsed:")
        print(f"    Ticker:    {parsed['ticker']}")
        print(f"    Type:      {parsed['contract_type']}")
        print(f"    Strike:    ${parsed['strike']:.2f}")
        print(f"    Expires:   {parsed['expiration']}")
        print(f"    DTE:       {parsed['dte_at_entry']} days")

    # Fill price — manual entry, what you actually paid
    print()
    fill_str = input("  Your actual fill price (per contract, e.g. 4.55): ").strip()
    try:
        fill_price = float(fill_str)
    except ValueError:
        print("  Invalid price. Aborted.")
        return

    # Number of contracts
    contracts_str = input("  Number of contracts [1]: ").strip() or "1"
    try:
        contracts = int(contracts_str)
    except ValueError:
        contracts = 1

    total_cost = round(fill_price * contracts * 100, 2)

    # Target (optional)
    target_str = input(f"  Target price (2x = {fill_price*2:.2f}) [enter to use 2x]: ").strip()
    if target_str:
        try:
            target_price = float(target_str)
        except ValueError:
            target_price = round(fill_price * 2, 2)
    else:
        target_price = round(fill_price * 2, 2)

    # Order ID (optional)
    order_id = input("  Public.com order ID (optional, press Enter to skip): ").strip() or None

    # Notes
    notes = input("  Notes (optional): ").strip() or None

    # Confirmation
    pnl_at_target = round((target_price - fill_price) * contracts * 100, 2)
    print(f"\n  {'─'*45}")
    print(f"  Contract:    {symbol}")
    print(f"  Fill price:  ${fill_price:.2f} × {contracts} contract(s)")
    print(f"  Total cost:  ${total_cost:,.2f}")
    print(f"  Target:      ${target_price:.2f} (P&L: +${pnl_at_target:,.2f})")
    if order_id:
        print(f"  Order ID:    {order_id}")
    print(f"  {'─'*45}")

    confirm = input("\n  Record this trade? (y/n): ").strip().lower()
    if confirm != 'y':
        print("  Aborted.")
        return

    # Write to DB
    conn   = get_conn()
    cursor = conn.cursor()

    entry_date = now.strftime("%Y-%m-%d")
    entry_time = now.strftime("%H:%M:%S")

    cursor.execute("""
        INSERT INTO live_trades (
            signal_contract, ticker, contract_type, strike, expiration,
            entry_date, entry_time, status,
            entry_price, fill_price, contracts, total_cost,
            dte_at_entry, target_price,
            real_order_id, confirmed_by, capital_at_risk, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?)
    """, (
        symbol,
        parsed.get('ticker', symbol[:4]),
        parsed.get('contract_type', 'UNKNOWN'),
        parsed.get('strike'),
        parsed.get('expiration'),
        entry_date, entry_time,
        fill_price, fill_price, contracts, total_cost,
        parsed.get('dte_at_entry'),
        target_price,
        order_id,
        total_cost,
        notes,
    ))

    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()

    print(f"\n  ✅ Trade recorded — ID #{trade_id}")
    print(f"  Live monitor will begin tracking this position.")
    print()


# =============================================================================
# EXIT TRADE
# =============================================================================

def exit_trade():
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)

    print(f"\n{'='*55}")
    print(f"  📉 RECORD LIVE TRADE EXIT")
    print(f"  {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"{'='*55}\n")

    # Show open trades first
    conn   = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, signal_contract, entry_price, contracts, total_cost, entry_date
        FROM live_trades
        WHERE status = 'OPEN'
        ORDER BY entry_date DESC
    """)
    open_trades = [dict(r) for r in cursor.fetchall()]

    if not open_trades:
        print("  No open live trades to exit.")
        conn.close()
        return

    print("  Open trades:")
    for t in open_trades:
        print(f"    #{t['id']} {t['signal_contract']} "
              f"entry=${t['entry_price']:.2f} × {t['contracts']} "
              f"(entered {t['entry_date']})")

    print()
    trade_id_str = input("  Trade ID to exit: ").strip()
    try:
        trade_id = int(trade_id_str)
    except ValueError:
        print("  Invalid ID. Aborted.")
        conn.close()
        return

    cursor.execute("SELECT * FROM live_trades WHERE id = ?", (trade_id,))
    trade = cursor.fetchone()
    if not trade:
        print(f"  Trade #{trade_id} not found.")
        conn.close()
        return

    trade = dict(trade)

    # Exit price — what you actually sold for
    exit_str = input(f"  Your actual exit price (per contract): ").strip()
    try:
        exit_price = float(exit_str)
    except ValueError:
        print("  Invalid price. Aborted.")
        conn.close()
        return

    # Exit reason
    print("  Exit reason:")
    print("    1. TARGET  — hit profit target")
    print("    2. STOP    — stopped out")
    print("    3. MANUAL  — discretionary exit")
    print("    4. EXPIRED — expired at this price")
    reason_map = {"1": "TARGET", "2": "STOP", "3": "MANUAL", "4": "EXPIRED"}
    reason_str = input("  Choice [3]: ").strip() or "3"
    exit_reason = reason_map.get(reason_str, "MANUAL")

    notes = input("  Notes (optional): ").strip() or None

    # Calculate P&L
    pnl     = round((exit_price - trade['entry_price']) * trade['contracts'] * 100, 2)
    pnl_pct = round((pnl / trade['total_cost']) * 100, 2) if trade['total_cost'] else 0

    exit_date = now.strftime("%Y-%m-%d")
    exit_time = now.strftime("%H:%M:%S")

    try:
        entry_dt  = datetime.strptime(trade['entry_date'], "%Y-%m-%d")
        exit_dt   = datetime.strptime(exit_date, "%Y-%m-%d")
        hold_days = (exit_dt - entry_dt).days
    except Exception:
        hold_days = 0

    print(f"\n  {'─'*45}")
    print(f"  Contract:   {trade['signal_contract']}")
    print(f"  Entry:      ${trade['entry_price']:.2f}")
    print(f"  Exit:       ${exit_price:.2f}")
    print(f"  P&L:        {fmt_pnl(pnl)} ({fmt_pct(pnl_pct)})")
    print(f"  Reason:     {exit_reason}")
    print(f"  {'─'*45}")

    confirm = input("\n  Record this exit? (y/n): ").strip().lower()
    if confirm != 'y':
        print("  Aborted.")
        conn.close()
        return

    cursor.execute("""
        UPDATE live_trades SET
            status      = 'CLOSED',
            exit_date   = ?,
            exit_time   = ?,
            exit_price  = ?,
            exit_reason = ?,
            pnl         = ?,
            pnl_pct     = ?,
            hold_days   = ?,
            notes       = COALESCE(notes || ' | ', '') || ?
        WHERE id = ?
    """, (
        exit_date, exit_time,
        exit_price, exit_reason,
        pnl, pnl_pct, hold_days,
        notes or "Manual exit recorded",
        trade_id
    ))

    conn.commit()
    conn.close()

    print(f"\n  ✅ Exit recorded for trade #{trade_id}")
    print(f"  P&L: {fmt_pnl(pnl)} ({fmt_pct(pnl_pct)})")
    print()


# =============================================================================
# LIST OPEN TRADES
# =============================================================================

def list_trades():
    conn   = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, signal_contract, entry_date, entry_price,
               contracts, total_cost, target_price, status, dte_at_entry
        FROM live_trades
        WHERE status IN ('OPEN', 'STOP_TRIGGERED')
        ORDER BY entry_date DESC, id DESC
    """)
    trades = [dict(r) for r in cursor.fetchall()]
    conn.close()

    if not trades:
        print("\n  No open live trades.\n")
        return

    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)

    print(f"\n{'='*70}")
    print(f"  📋 OPEN LIVE TRADES — {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"{'='*70}")
    print(f"  {'ID':<5} {'Contract':<28} {'Entry':>7} {'Contracts':>10} "
          f"{'Cost':>9} {'Target':>8} {'DTE':>5}")
    print(f"  {'─'*70}")

    for t in trades:
        print(f"  #{t['id']:<4} {t['signal_contract']:<28} "
              f"${t['entry_price']:>6.2f} {t['contracts']:>10} "
              f"${t['total_cost']:>8,.0f} ${t['target_price']:>7.2f} "
              f"{t['dte_at_entry']:>5}d")

    print(f"\n  Total open: {len(trades)}\n")


# =============================================================================
# SUMMARY
# =============================================================================

def summary():
    conn   = get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) as open_count,
            SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) as closed_count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
            ROUND(SUM(CASE WHEN status='CLOSED' THEN pnl ELSE 0 END), 2) as realized_pnl,
            ROUND(SUM(total_cost), 2) as total_invested
        FROM live_trades
    """)
    s = dict(cursor.fetchone())
    conn.close()

    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)

    closed = s['closed_count'] or 0
    wins   = s['wins'] or 0
    wr     = round((wins / closed * 100), 1) if closed > 0 else 0

    print(f"\n{'='*50}")
    print(f"  💼 LIVE TRADES SUMMARY")
    print(f"  {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"{'='*50}")
    print(f"  Total trades:    {s['total']}")
    print(f"  Open:            {s['open_count']}")
    print(f"  Closed:          {closed}  ({wins}W / {s['losses'] or 0}L — {wr}% win rate)")
    print(f"  Realized P&L:    {fmt_pnl(s['realized_pnl'])}")
    print(f"  Total invested:  ${s['total_invested']:,.2f}")
    print(f"{'='*50}\n")


# =============================================================================
# MAIN
# =============================================================================

def main():
    # Initialize DB on every run
    init_live_db()

    if len(sys.argv) < 2:
        print("\nUsage:")
        print("  python record_trade.py enter    — record a new trade")
        print("  python record_trade.py exit     — record an exit")
        print("  python record_trade.py list     — show open live trades")
        print("  python record_trade.py summary  — show P&L summary")
        print()
        return

    command = sys.argv[1].lower()

    if command == "enter":
        enter_trade()
    elif command == "exit":
        exit_trade()
    elif command == "list":
        list_trades()
    elif command == "summary":
        summary()
    else:
        print(f"  Unknown command: {command}")
        print("  Use: enter, exit, list, summary")


if __name__ == "__main__":
    main()