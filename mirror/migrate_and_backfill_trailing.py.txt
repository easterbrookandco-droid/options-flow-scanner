"""
migrate_and_backfill_trailing.py — one-time migration + backfill for the
price-based trailing-stop observability fields. Safe to run repeatedly.

Adds (idempotent):
    paper_trades.hurdle_price
    position_snapshots.hurdle_crossed
    position_snapshots.running_max_price
    position_snapshots.running_max_pnl

Backfills:
    - hurdle_price for every paper_trade           = entry_price * (1 + HURDLE_PCT)
    - hurdle_crossed / running_max_price / running_max_pnl for every historical
      snapshot, replayed in chronological order per trade using the SAME
      price-peak logic the live monitor now uses.

Usage:
    python migrate_and_backfill_trailing.py            # migrate + backfill
    python migrate_and_backfill_trailing.py --dry-run  # report only, no writes
"""

import sqlite3
import sys

import strategy_config as strat
from journal import DB_PATH


def add_column(cursor, table, column, decl):
    """Add a column if it doesn't already exist. Returns True if added."""
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        print(f"  [+] added {table}.{column}")
        return True
    except sqlite3.OperationalError:
        print(f"  [=] {table}.{column} already exists")
        return False


def migrate(cursor):
    print("Migrating schema...")
    add_column(cursor, "paper_trades", "hurdle_price", "REAL")
    add_column(cursor, "position_snapshots", "hurdle_crossed", "INTEGER")
    add_column(cursor, "position_snapshots", "running_max_price", "REAL")
    add_column(cursor, "position_snapshots", "running_max_pnl", "REAL")


def backfill(conn, dry_run=False):
    cursor = conn.cursor()

    # ── 1. hurdle_price on every trade ──────────────────────────────────
    cursor.execute("SELECT id, entry_price FROM paper_trades")
    trades = cursor.fetchall()
    hp_updates = 0
    for trade_id, entry_price in trades:
        if entry_price is None:
            continue
        hp = strat.hurdle_price(entry_price)
        if not dry_run:
            cursor.execute(
                "UPDATE paper_trades SET hurdle_price = ? WHERE id = ?",
                (hp, trade_id),
            )
        hp_updates += 1
    print(f"  hurdle_price set on {hp_updates} trades")

    # ── 2. Replay snapshots per trade ───────────────────────────────────
    # Need entry_price + contracts to convert peak price → peak P&L.
    cursor.execute("SELECT id, entry_price, contracts FROM paper_trades")
    trade_meta = {tid: (ep, ctr) for tid, ep, ctr in cursor.fetchall()}

    snap_updates = 0
    crossed_trades = 0
    for trade_id, (entry_price, contracts) in trade_meta.items():
        if entry_price is None:
            continue
        contracts = contracts or 1
        hurdle = strat.hurdle_price(entry_price)

        cursor.execute(
            """SELECT id, current_price FROM position_snapshots
               WHERE trade_id = ? ORDER BY id ASC""",
            (trade_id,),
        )
        snaps = cursor.fetchall()

        hurdle_crossed    = False
        running_max_price = 0.0
        ever_crossed      = False

        for snap_id, price in snaps:
            if price is not None and hurdle is not None:
                if not hurdle_crossed and price >= hurdle:
                    hurdle_crossed    = True
                    running_max_price = price
                    ever_crossed      = True
                elif hurdle_crossed and price > running_max_price:
                    running_max_price = price

            if hurdle_crossed:
                rmp = round(running_max_price, 4)
                rm_pnl = round((running_max_price - entry_price) * contracts * 100, 2)
                hc = 1
            else:
                rmp = None
                rm_pnl = None
                hc = 0

            if not dry_run:
                cursor.execute(
                    """UPDATE position_snapshots
                       SET hurdle_crossed = ?, running_max_price = ?, running_max_pnl = ?
                       WHERE id = ?""",
                    (hc, rmp, rm_pnl, snap_id),
                )
            snap_updates += 1

        if ever_crossed:
            crossed_trades += 1

    print(f"  snapshots updated: {snap_updates}")
    print(f"  trades that ever crossed the hurdle: {crossed_trades} / {len(trade_meta)}")

    if not dry_run:
        conn.commit()


def main():
    dry_run = "--dry-run" in sys.argv
    print(f"DB: {DB_PATH}   HURDLE_PCT: {strat.HURDLE_PCT}"
          f"{'   (DRY RUN)' if dry_run else ''}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if dry_run:
        print("(dry run — skipping schema migration and all writes)")
    else:
        migrate(cursor)
        conn.commit()
    backfill(conn, dry_run=dry_run)
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
