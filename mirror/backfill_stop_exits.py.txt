# -*- coding: utf-8 -*-
"""
backfill_stop_exits.py

One-time backfill script to write exit_price and pnl for all
STOP_TRIGGERED positions that are missing exit data.

Uses the position_snapshots table to find:
  1. The snapshot where stop_triggered=1 (exact stop moment)
  2. Falls back to the snapshot nearest to when status changed
  3. Falls back to the last known snapshot price

After this runs, all STOP_TRIGGERED positions will have:
  - exit_price: price at moment stop fired
  - pnl: actual loss (not total loss — recovered the exit price)
  - exit_reason: 'STOP'
  - Status remains STOP_TRIGGERED so monitoring continues

Usage:
    python backfill_stop_exits.py
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
    now_str = datetime.now(eastern).strftime("%Y-%m-%d %H:%M:%S %Z")

    print(f"\n{'='*65}")
    print(f"  🔧 BACKFILL STOP EXIT PRICES")
    print(f"  {now_str}")
    print(f"{'='*65}\n")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Find all STOP_TRIGGERED positions missing exit data
    cursor.execute("""
        SELECT id, signal_contract, entry_price, contracts, total_cost,
               entry_date, status
        FROM paper_trades
        WHERE status = 'STOP_TRIGGERED'
        AND (exit_price IS NULL OR pnl IS NULL)
        ORDER BY id
    """)
    positions = [dict(r) for r in cursor.fetchall()]

    print(f"  Found {len(positions)} STOP_TRIGGERED positions needing backfill\n")

    if not positions:
        print(f"  Nothing to backfill — all stop-triggered positions have exit data.")
        conn.close()
        return

    print(f"  {'ID':<6} {'Contract':<28} {'Entry':>7} {'Stop Price':>11} "
          f"{'P&L':>9} {'Source'}")
    print(f"  {'─'*75}")

    updated = 0
    failed  = 0
    total_pnl = 0

    for p in positions:
        trade_id    = p["id"]
        entry_price = p["entry_price"]
        contracts   = p["contracts"]
        total_cost  = p["total_cost"]
        contract    = p["signal_contract"]

        # ── Strategy 1: Find snapshot where stop_triggered=1 ──────────
        cursor.execute("""
            SELECT current_price, snapshot_time
            FROM position_snapshots
            WHERE trade_id = ?
            AND stop_triggered = 1
            ORDER BY id ASC
            LIMIT 1
        """, (trade_id,))
        snap = cursor.fetchone()
        source = "stop_triggered flag"

        # ── Strategy 2: Snapshot closest to notes timestamp ───────────
        if not snap:
            cursor.execute("""
                SELECT current_price, snapshot_time
                FROM position_snapshots
                WHERE trade_id = ?
                AND current_price > 0
                ORDER BY id ASC
                LIMIT 1
            """, (trade_id,))
            snap = cursor.fetchone()
            source = "first snapshot after entry"

        # ── Strategy 3: Last known price ───────────────────────────────
        if not snap:
            cursor.execute("""
                SELECT current_price, snapshot_time
                FROM position_snapshots
                WHERE trade_id = ?
                AND current_price IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
            """, (trade_id,))
            snap = cursor.fetchone()
            source = "last known snapshot"

        if not snap or not snap["current_price"]:
            print(f"  {trade_id:<6} {contract:<28} ${entry_price:>6.2f}  "
                  f"{'NO SNAPSHOTS':>11}  — skipping")
            failed += 1
            continue

        exit_price = float(snap["current_price"])
        snap_time  = snap["snapshot_time"]

        # Parse exit date from snapshot time
        try:
            exit_date = snap_time[:10]
            exit_time = snap_time[11:19] if len(snap_time) > 10 else "16:00:00"
        except Exception:
            exit_date = p["entry_date"]
            exit_time = "16:00:00"

        # Calculate P&L
        pnl     = round((exit_price - entry_price) * contracts * 100, 2)
        pnl_pct = round((pnl / total_cost) * 100, 2) if total_cost else 0

        # Calculate hold days
        try:
            entry_dt = datetime.strptime(p["entry_date"], "%Y-%m-%d")
            exit_dt  = datetime.strptime(exit_date, "%Y-%m-%d")
            hold_days = (exit_dt - entry_dt).days
        except Exception:
            hold_days = 0

        # Write the exit data
        cursor.execute("""
            UPDATE paper_trades SET
                exit_date   = ?,
                exit_time   = ?,
                exit_price  = ?,
                exit_reason = 'STOP',
                pnl         = ?,
                pnl_pct     = ?,
                hold_days   = ?,
                notes       = COALESCE(notes || ' | ', '') ||
                              'Exit backfilled from snapshot (' || ? || ')'
            WHERE id = ?
            AND status = 'STOP_TRIGGERED'
            AND exit_price IS NULL
        """, (
            exit_date, exit_time,
            exit_price,
            pnl, pnl_pct,
            hold_days,
            source,
            trade_id
        ))

        if cursor.rowcount > 0:
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            print(f"  {trade_id:<6} {contract:<28} ${entry_price:>6.2f} "
                  f"${exit_price:>10.2f} {pnl_str:>9}  [{source}]")
            updated   += 1
            total_pnl += pnl
        else:
            failed += 1

    conn.commit()
    conn.close()

    print(f"\n{'='*65}")
    print(f"  📊 BACKFILL COMPLETE")
    print(f"  Updated : {updated}")
    print(f"  Failed  : {failed}")
    print(f"  Total P&L recorded for stop exits: ${total_pnl:,.2f}")
    print(f"\n  Run pnl_report.py to see updated portfolio numbers.")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()