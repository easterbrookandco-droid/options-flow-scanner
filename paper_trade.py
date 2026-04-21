import sys
import os
import pytz
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from journal import (
    init_paper_trades_table,
    insert_paper_trade,
    close_paper_trade,
    get_open_positions,
    get_closed_trades,
    get_paper_trade_summary,
)

MARKET_TIMEZONE = "US/Eastern"

# =============================================================================
# DISPLAY HELPERS
# =============================================================================

def fmt_pnl(pnl):
    """Format P&L with color indicator."""
    if pnl is None:
        return "—"
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.2f}"


def fmt_pct(pct):
    """Format percentage with sign."""
    if pct is None:
        return "—"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def fmt_price(p):
    """Format option price."""
    if p is None:
        return "—"
    return f"${p:.2f}"


def pnl_indicator(pnl):
    """Green/red indicator for P&L."""
    if pnl is None:
        return "⬜"
    return "🟢" if pnl >= 0 else "🔴"


def print_open_positions(positions, header=True):
    """
    Print a formatted table of open paper trade positions.

    Parameters:
        positions (list): Output of get_open_positions()
        header (bool): Whether to print the section header
    """
    if header:
        print(f"\n  {'='*65}")
        print(f"  📂 OPEN PAPER TRADES ({len(positions)})")
        print(f"  {'='*65}")

    if not positions:
        print(f"  No open positions.")
        return

    print(f"\n  {'ID':<5} {'Contract':<28} {'Entry':<8} "
          f"{'Contracts':<10} {'Cost':<10} {'Target':<8} {'Stop':<8} {'DTE@Entry':<10} {'Days Held'}")
    print(f"  {'-'*105}")

    eastern = pytz.timezone(MARKET_TIMEZONE)
    today   = datetime.now(eastern).date()

    for t in positions:
        try:
            entry_dt  = datetime.strptime(t["entry_date"], "%Y-%m-%d").date()
            days_held = (today - entry_dt).days
        except Exception:
            days_held = 0

        print(f"  {t['id']:<5} {t['signal_contract']:<28} "
              f"{fmt_price(t['entry_price']):<8} "
              f"{t['contracts']:<10} "
              f"${t['total_cost']:>7,.2f}   "
              f"{fmt_price(t['target_price']):<8} "
              f"{fmt_price(t['stop_price']):<8} "
              f"{str(t.get('dte_at_entry', '—')):<10} "
              f"{days_held}d")

        # Thesis on second line, indented
        thesis = t.get("thesis", "")
        if thesis:
            print(f"         💭 {thesis}")

        # Exit rules reminder
        print(f"         🎯 Target: {fmt_price(t['target_price'])}  "
              f"🛑 Stop: {fmt_price(t['stop_price'])}  "
              f"Verdict at entry: {t.get('verdict_at_entry', '—')}")
        print()


def print_closed_trades(trades):
    """
    Print a formatted table of recently closed trades with P&L.
    """
    print(f"\n  {'='*65}")
    print(f"  📋 RECENTLY CLOSED TRADES ({len(trades)})")
    print(f"  {'='*65}")

    if not trades:
        print(f"  No closed trades yet.")
        return

    print(f"\n  {'ID':<5} {'Contract':<28} {'Reason':<10} "
          f"{'Entry':<8} {'Exit':<8} {'P&L':<12} {'Return':<10} {'Held'}")
    print(f"  {'-'*95}")

    for t in trades:
        print(f"  {pnl_indicator(t.get('pnl'))} "
              f"{t['id']:<4} {t['signal_contract']:<28} "
              f"{t.get('exit_reason', '—'):<10} "
              f"{fmt_price(t['entry_price']):<8} "
              f"{fmt_price(t.get('exit_price')):<8} "
              f"{fmt_pnl(t.get('pnl')):<12} "
              f"{fmt_pct(t.get('pnl_pct')):<10} "
              f"{t.get('hold_days', '—')}d")

        if t.get("notes"):
            print(f"         📝 {t['notes']}")
        print()


def print_summary(summary):
    """
    Print high-level paper trading P&L summary.
    """
    total     = summary.get("total", 0) or 0
    open_c    = summary.get("open_count", 0) or 0
    closed_c  = summary.get("closed_count", 0) or 0
    wins      = summary.get("wins", 0) or 0
    losses    = summary.get("losses", 0) or 0
    total_pnl = summary.get("total_pnl", 0) or 0
    avg_pct   = summary.get("avg_pnl_pct", 0) or 0

    win_rate = round((wins / closed_c) * 100, 1) if closed_c > 0 else 0

    print(f"\n  {'='*65}")
    print(f"  📊 PAPER TRADING SUMMARY")
    print(f"  {'='*65}")
    print(f"  Total trades:    {total}  ({open_c} open, {closed_c} closed)")
    print(f"  Win rate:        {win_rate}%  ({wins}W / {losses}L)")
    print(f"  Total P&L:       {fmt_pnl(total_pnl)}")
    print(f"  Avg return:      {fmt_pct(avg_pct)}")
    print(f"  {'='*65}")


# =============================================================================
# COMMANDS
# =============================================================================

def cmd_enter(args):
    """
    Record a new paper trade entry.

    Usage:
        python paper_trade.py enter <contract> <entry_price> <contracts> "<thesis>"

    Example:
        python paper_trade.py enter TSLA260429C00392500 5.50 1 "Large call flow, moderate IV, bullish bias"

    Arguments:
        contract     — Full options contract symbol
        entry_price  — Ask price per share (what you'd pay)
        contracts    — Number of contracts (recommend 1-2)
        thesis       — One sentence in quotes explaining the trade

    Optional prompts after entry:
        Underlying stock price at time of entry
    """

    if len(args) < 4:
        print("\n  ✗ Missing arguments.")
        print("  Usage: python paper_trade.py enter <contract> "
              "<entry_price> <contracts> \"<thesis>\"")
        print("  Example: python paper_trade.py enter "
              "TSLA260429C00392500 5.50 1 "
              "\"Large call flow, moderate IV, bullish bias\"")
        return

    contract    = args[0].upper()
    try:
        entry_price = float(args[1])
        contracts   = int(args[2])
    except ValueError:
        print("  ✗ entry_price must be a number, contracts must be an integer")
        return

    thesis = args[3]

    if not thesis.strip():
        print("  ✗ Thesis cannot be empty — one sentence required")
        return

    if contracts < 1 or contracts > 10:
        print("  ✗ Contracts must be between 1 and 10")
        return

    if entry_price <= 0:
        print("  ✗ Entry price must be positive")
        return

    # Prompt for underlying price
    underlying = None
    try:
        raw = input(f"\n  Stock price at entry (press Enter to skip): ").strip()
        if raw:
            underlying = float(raw)
    except (ValueError, EOFError):
        pass

    # Calculate targets
    total_cost   = round(entry_price * contracts * 100, 2)
    target_price = round(entry_price * 2, 2)
    stop_price   = round(entry_price * 0.5, 2)
    target_gain  = round((target_price - entry_price) * contracts * 100, 2)
    stop_loss    = round((entry_price - stop_price) * contracts * 100, 2)

    # Confirm before writing
    print(f"\n  {'─'*55}")
    print(f"  📋 PAPER TRADE ENTRY CONFIRMATION")
    print(f"  {'─'*55}")
    print(f"  Contract:     {contract}")
    print(f"  Entry price:  {fmt_price(entry_price)} per share")
    print(f"  Contracts:    {contracts}")
    print(f"  Total cost:   ${total_cost:,.2f}")
    if underlying:
        print(f"  Stock price:  ${underlying:,.2f}")
    print(f"  Thesis:       {thesis}")
    print(f"  {'─'*55}")
    print(f"  🎯 Target:    {fmt_price(target_price)}  "
          f"(+${target_gain:,.2f} gain if hit)")
    print(f"  🛑 Stop:      {fmt_price(stop_price)}  "
          f"(-${stop_loss:,.2f} loss if hit)")
    print(f"  {'─'*55}")

    confirm = input("  Confirm entry? (y/n): ").strip().lower()
    if confirm != "y":
        print("  Entry cancelled.")
        return

    # Write to DB
    trade_id = insert_paper_trade(
        signal_contract=contract,
        entry_price=entry_price,
        contracts=contracts,
        thesis=thesis,
        entry_underlying_price=underlying,
    )

    print(f"\n  ✅ Paper trade #{trade_id} recorded.")
    print(f"  To exit this trade:")
    print(f"  python paper_trade.py exit {trade_id} <exit_price> "
          f"TARGET|STOP|MANUAL|EXPIRED")
    print()


def cmd_exit(args):
    """
    Close an open paper trade and record P&L.

    Usage:
        python paper_trade.py exit <trade_id> <exit_price> <reason>

    Example:
        python paper_trade.py exit 1 11.00 TARGET
        python paper_trade.py exit 1 2.75 STOP
        python paper_trade.py exit 1 7.50 MANUAL

    Reasons:
        TARGET   — hit your 2x price target
        STOP     — hit your 50% stop loss
        MANUAL   — closed for another reason
        EXPIRED  — held to expiration
    """

    if len(args) < 3:
        print("\n  ✗ Missing arguments.")
        print("  Usage: python paper_trade.py exit "
              "<trade_id> <exit_price> TARGET|STOP|MANUAL|EXPIRED")
        return

    try:
        trade_id   = int(args[0])
        exit_price = float(args[1])
    except ValueError:
        print("  ✗ trade_id must be an integer, exit_price must be a number")
        return

    reason = args[2].upper()
    if reason not in ("TARGET", "STOP", "MANUAL", "EXPIRED"):
        print("  ✗ Reason must be TARGET, STOP, MANUAL, or EXPIRED")
        return

    # Fetch trade to show confirmation
    from journal import DB_PATH
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM paper_trades WHERE id = ? AND status = 'OPEN'",
                   (trade_id,))
    trade = cursor.fetchone()
    conn.close()

    if not trade:
        print(f"  ✗ No open paper trade found with ID {trade_id}")
        return

    trade = dict(trade)

    # Prompt for underlying price at exit
    underlying = None
    try:
        raw = input(f"\n  Stock price at exit (press Enter to skip): ").strip()
        if raw:
            underlying = float(raw)
    except (ValueError, EOFError):
        pass

    # Optional notes
    notes = None
    try:
        raw = input("  Notes (press Enter to skip): ").strip()
        if raw:
            notes = raw
    except EOFError:
        pass

    # Calculate P&L preview
    contracts  = trade["contracts"]
    entry_price = trade["entry_price"]
    total_cost  = trade["total_cost"]
    pnl         = round((exit_price - entry_price) * contracts * 100, 2)
    pnl_pct     = round((pnl / total_cost) * 100, 2) if total_cost else 0

    print(f"\n  {'─'*55}")
    print(f"  📋 PAPER TRADE EXIT CONFIRMATION")
    print(f"  {'─'*55}")
    print(f"  Trade ID:     #{trade_id}")
    print(f"  Contract:     {trade['signal_contract']}")
    print(f"  Entry price:  {fmt_price(entry_price)}")
    print(f"  Exit price:   {fmt_price(exit_price)}")
    print(f"  Reason:       {reason}")
    print(f"  P&L:          {fmt_pnl(pnl)}  ({fmt_pct(pnl_pct)})")
    if underlying:
        print(f"  Stock @ exit: ${underlying:,.2f}")
    if notes:
        print(f"  Notes:        {notes}")
    print(f"  {'─'*55}")

    confirm = input("  Confirm exit? (y/n): ").strip().lower()
    if confirm != "y":
        print("  Exit cancelled.")
        return

    result = close_paper_trade(
        trade_id=trade_id,
        exit_price=exit_price,
        exit_reason=reason,
        exit_underlying_price=underlying,
        notes=notes,
    )

    if result:
        icon = "🟢" if pnl >= 0 else "🔴"
        print(f"\n  {icon} Trade #{trade_id} closed.")
        print(f"  P&L: {fmt_pnl(pnl)}  ({fmt_pct(pnl_pct)})")
        print(f"  Held: {result['hold_days']} days")
        print()
    else:
        print("  ✗ Something went wrong closing the trade")


def cmd_clear_test(args):
    """
    Delete paper trades marked as test entries, or all trades if confirmed.
    Use before going live to start with a clean slate.

    Usage:
        python paper_trade.py clear-test        — deletes trades with 'test'
                                                  anywhere in the thesis
        python paper_trade.py clear-test --all  — deletes ALL paper trades
                                                  (nuclear option, requires
                                                  double confirmation)
    """

    import sqlite3
    from journal import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    nuke = "--all" in args

    if nuke:
        cursor.execute("SELECT COUNT(*) as n FROM paper_trades")
        count = cursor.fetchone()["n"]

        print(f"\n  ⚠️  WARNING: This will permanently delete ALL {count} "
              f"paper trade records.")
        print(f"  This cannot be undone.")
        confirm1 = input("  Type DELETE to confirm: ").strip()
        if confirm1 != "DELETE":
            print("  Cancelled.")
            conn.close()
            return

        confirm2 = input("  Type DELETE again to double-confirm: ").strip()
        if confirm2 != "DELETE":
            print("  Cancelled.")
            conn.close()
            return

        cursor.execute("DELETE FROM paper_trades")
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        print(f"\n  ✅ Deleted all {deleted} paper trade records.")
        print(f"  Clean slate — ready for live paper trading.")

    else:
        # Delete only trades where thesis contains 'test' (case-insensitive)
        cursor.execute("""
            SELECT id, signal_contract, thesis, status
            FROM paper_trades
            WHERE LOWER(thesis) LIKE '%test%'
            ORDER BY id ASC
        """)
        candidates = [dict(row) for row in cursor.fetchall()]

        if not candidates:
            print("\n  No test entries found.")
            print("  Tip: To delete all trades use: "
                  "python paper_trade.py clear-test --all")
            conn.close()
            return

        print(f"\n  Found {len(candidates)} test trade(s) to delete:\n")
        for t in candidates:
            print(f"  #{t['id']}  {t['signal_contract']:<28} "
                  f"[{t['status']}]  \"{t['thesis']}\"")

        print()
        confirm = input("  Delete these entries? (y/n): ").strip().lower()
        if confirm != "y":
            print("  Cancelled.")
            conn.close()
            return

        ids = [t["id"] for t in candidates]
        cursor.execute(
            f"DELETE FROM paper_trades WHERE id IN "
            f"({','.join('?' for _ in ids)})",
            ids
        )
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        print(f"\n  ✅ Deleted {deleted} test trade record(s).")


def cmd_review(args):
    """
    Display open positions, recent closed trades, and summary stats.

    Usage:
        python paper_trade.py review
    """

    summary   = get_paper_trade_summary()
    open_pos  = get_open_positions()
    closed    = get_closed_trades(limit=10)

    print_summary(summary)
    print_open_positions(open_pos)
    print_closed_trades(closed)


def cmd_help():
    """Print usage instructions."""

    print(f"""
  {'='*65}
  📘 PAPER TRADE — USAGE GUIDE
  {'='*65}

  ENTER A TRADE:
    python paper_trade.py enter <contract> <price> <contracts> "<thesis>"

    Example:
    python paper_trade.py enter TSLA260429C00392500 5.50 1 \\
      "Large call flow, moderate IV, bullish market context"

  EXIT A TRADE:
    python paper_trade.py exit <trade_id> <exit_price> <reason>

    Reasons: TARGET  — hit 2x price target
             STOP    — hit 50% stop loss
             MANUAL  — closed manually
             EXPIRED — held to expiration

    Examples:
    python paper_trade.py exit 1 11.00 TARGET
    python paper_trade.py exit 1 2.75 STOP
    python paper_trade.py exit 1 7.50 MANUAL

  REVIEW POSITIONS:
    python paper_trade.py review

  CLEAR TEST ENTRIES:
    python paper_trade.py clear-test        — removes trades with 'test'
                                              in the thesis
    python paper_trade.py clear-test --all  — removes ALL trades
                                              (requires double confirmation)

  {'='*65}
    """)


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    init_paper_trades_table()

    if len(sys.argv) < 2:
        cmd_help()
        return

    command = sys.argv[1].lower()
    args    = sys.argv[2:]

    if command == "enter":
        cmd_enter(args)
    elif command == "exit":
        cmd_exit(args)
    elif command == "review":
        cmd_review(args)
    elif command == "help":
        cmd_help()
    elif command == "clear-test":
        cmd_clear_test(args)
    else:
        print(f"\n  ✗ Unknown command: {command}")
        cmd_help()


if __name__ == "__main__":
    main()