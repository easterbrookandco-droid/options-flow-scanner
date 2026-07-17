# -*- coding: utf-8 -*-
"""
close_stuck_positions.py

One-shot cleanup script to properly close all STOP_TRIGGERED positions
that have already expired, using their last known P&L from snapshots.

Run once to clean up accounting, then the fixed position_monitor.py
will handle this correctly going forward.
"""

import sqlite3
import pytz
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

MARKET_TIMEZONE = "US/Eastern"


def main():
    from journal import DB_PATH

    eastern = pytz.timezone(MARKET_TIMEZONE)
    today   = datetime.now(eastern).date()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Find all STOP_TRIGGERED positions with their last snapshot P&L
    cursor.execute("""
        SELECT 
            pt.id,
            pt.signal_contract,
            pt.entry_price,
            pt.contracts,
            pt.total_cost,
            ps.current_price as last_price,
            ps.pnl          as last_pnl,
            ps.snapshot_time
        FROM paper_trades pt
        JOIN position_snapshots ps ON ps.trade_id = pt.id
        WHERE pt.status = 'STOP_TRIGGERED'
        AND ps.id = (
            SELECT MAX(id) FROM position_snapshots 
            WHERE trade_id = pt.id
        )
        ORDER BY pt.id
    """)

    positions = [dict(row) for row in cursor.fetchall()]

    if not positions:
        print("No STOP_TRIGGERED positions found.")
        conn.close()
        return

    print(f"\n{'='*65}")
    print(f"  🔧 STOP_TRIGGERED POSITION CLEANUP")
    print(f"  Found {len(positions)} positions to close")
    print(f"{'='*65}")
    print(f"\n  {'ID':<5} {'Contract':<28} {'Entry':>7} {'Last':>7} {'P&L':>9}  Expiry")
    print(f"  {'─'*65}")

    expired     = []
    not_expired = []

    for p in positions:
        contract = p["signal_contract"]

        # Parse expiration from contract symbol
        try:
            date_str = contract[-15:-9]
            exp_date = datetime.strptime(date_str, "%y%m%d").date()
        except Exception:
            exp_date = None

        pnl     = p["last_pnl"] or 0
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        exp_str = str(exp_date) if exp_date else "unknown"

        print(f"  {p['id']:<5} {contract:<28} "
              f"${p['entry_price']:>6.2f} "
              f"${p['last_price']:>6.2f} "
              f"{pnl_str:>9}  {exp_str}")

        if exp_date and exp_date <= today:
            expired.append(p)
        else:
            not_expired.append(p)

    print(f"  {'─'*65}")
    print(f"\n  Already expired (will close): {len(expired)}")
    print(f"  Not yet expired (will skip):  {len(not_expired)}")

    total_pnl = sum(p["last_pnl"] or 0 for p in expired)
    print(f"  Total P&L to record:          ${total_pnl:.2f}")

    if not expired:
        print(f"\n  No expired positions to close.")
        conn.close()
        return

    confirm = input(f"\n  Close {len(expired)} expired positions? (y/n): ").strip().lower()
    if confirm != "y":
        print("  Aborted.")
        conn.close()
        return

    # Close each expired position
    closed  = 0
    failed  = 0
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)

    for p in expired:
        try:
            contract    = p["signal_contract"]
            exit_price  = p["last_price"] or 0
            pnl         = p["last_pnl"] or 0
            total_cost  = p["total_cost"] or 1
            pnl_pct     = round((pnl / total_cost) * 100, 2)

            # Parse expiration date for exit_date
            try:
                date_str = contract[-15:-9]
                exp_date = datetime.strptime(date_str, "%y%m%d")
                exit_date = exp_date.strftime("%Y-%m-%d")
            except Exception:
                exit_date = now.strftime("%Y-%m-%d")

            cursor.execute("""
                UPDATE paper_trades SET
                    status              = 'CLOSED',
                    exit_date           = ?,
                    exit_time           = '16:00:00',
                    exit_price          = ?,
                    exit_reason         = 'EXPIRED',
                    pnl                 = ?,
                    pnl_pct             = ?,
                    hold_days           = julianday(?) - julianday(entry_date),
                    notes               = COALESCE(notes || ' | ', '') || 
                                         'Closed by cleanup script — stop triggered, tracked to expiration'
                WHERE id = ?
                AND status = 'STOP_TRIGGERED'
            """, (
                exit_date, exit_price, pnl, pnl_pct,
                exit_date, p["id"]
            ))

            if cursor.rowcount > 0:
                print(f"  ✅ #{p['id']} {contract} — "
                      f"closed at ${exit_price:.2f}  "
                      f"P&L: ${pnl:.2f}")
                closed += 1
            else:
                print(f"  ✗ #{p['id']} — no rows updated")
                failed += 1

        except Exception as e:
            print(f"  ✗ #{p['id']} error: {e}")
            failed += 1

    conn.commit()
    conn.close()

    print(f"\n{'='*65}")
    print(f"  📊 CLEANUP COMPLETE")
    print(f"  Closed:  {closed}")
    print(f"  Failed:  {failed}")
    print(f"  Skipped: {len(not_expired)} (not yet expired)")
    print(f"  Total P&L recorded: ${total_pnl:.2f}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()