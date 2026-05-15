# -*- coding: utf-8 -*-
"""
pnl_report.py

On-demand P&L report showing:
  1. Closed trade actuals — realized P&L with multiple breakdowns
  2. Open position pro-forma — unrealized P&L from last known snapshot
  3. Combined portfolio snapshot

Usage:
    python pnl_report.py
"""

import sqlite3
import os
import pytz
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

MARKET_TIMEZONE = "US/Eastern"


def get_db():
    from journal import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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


def parse_dte_bucket(dte):
    if dte is None:
        return "Unknown"
    if dte == 0:
        return "0DTE"
    elif dte <= 2:
        return "1-2 DTE"
    elif dte <= 5:
        return "3-5 DTE"
    elif dte <= 14:
        return "6-14 DTE"
    else:
        return "15+ DTE"


# =============================================================================
# SECTION 1 — CLOSED TRADE ACTUALS
# =============================================================================

def get_closed_summary(cursor):
    cursor.execute("""
        SELECT
            COUNT(*)                                            as total,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)          as wins,
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END)         as losses,
            ROUND(SUM(pnl), 2)                                 as total_pnl,
            ROUND(AVG(pnl), 2)                                 as avg_pnl,
            ROUND(AVG(CASE WHEN pnl > 0 THEN pnl END), 2)     as avg_win,
            ROUND(AVG(CASE WHEN pnl <= 0 THEN pnl END), 2)    as avg_loss,
            ROUND(AVG(hold_days), 1)                           as avg_hold_days,
            MAX(pnl)                                           as best_trade,
            MIN(pnl)                                           as worst_trade
        FROM paper_trades
        WHERE status = 'CLOSED'
    """)
    return dict(cursor.fetchone())


def get_by_exit_reason(cursor):
    cursor.execute("""
        SELECT
            exit_reason,
            COUNT(*)                                        as count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)      as wins,
            ROUND(SUM(pnl), 2)                             as total_pnl,
            ROUND(AVG(pnl_pct), 1)                        as avg_pct
        FROM paper_trades
        WHERE status = 'CLOSED'
        GROUP BY exit_reason
        ORDER BY total_pnl DESC
    """)
    return [dict(r) for r in cursor.fetchall()]


def get_by_ticker(cursor):
    cursor.execute("""
        SELECT
            ticker,
            COUNT(*)                                        as count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)      as wins,
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END)     as losses,
            ROUND(SUM(pnl), 2)                             as total_pnl,
            ROUND(AVG(pnl_pct), 1)                        as avg_pct
        FROM paper_trades
        JOIN signals ON paper_trades.signal_contract = signals.contract
        WHERE paper_trades.status = 'CLOSED'
        GROUP BY ticker
        ORDER BY total_pnl DESC
    """)
    return [dict(r) for r in cursor.fetchall()]


def get_by_contract_type(cursor):
    cursor.execute("""
        SELECT
            CASE WHEN signal_contract LIKE '%C%' THEN 'CALL' ELSE 'PUT' END as contract_type,
            COUNT(*)                                        as count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)      as wins,
            ROUND(SUM(pnl), 2)                             as total_pnl,
            ROUND(AVG(pnl_pct), 1)                        as avg_pct
        FROM paper_trades
        WHERE status = 'CLOSED'
        GROUP BY contract_type
        ORDER BY total_pnl DESC
    """)
    return [dict(r) for r in cursor.fetchall()]


def get_by_dte_bucket(cursor):
    cursor.execute("""
        SELECT
            dte_at_entry,
            COUNT(*)                                        as count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)      as wins,
            ROUND(SUM(pnl), 2)                             as total_pnl,
            ROUND(AVG(pnl_pct), 1)                        as avg_pct
        FROM paper_trades
        WHERE status = 'CLOSED'
        GROUP BY dte_at_entry
        ORDER BY dte_at_entry
    """)
    rows = [dict(r) for r in cursor.fetchall()]

    # Aggregate into buckets
    buckets = {}
    for r in rows:
        bucket = parse_dte_bucket(r["dte_at_entry"])
        if bucket not in buckets:
            buckets[bucket] = {"count": 0, "wins": 0, "total_pnl": 0, "pcts": []}
        buckets[bucket]["count"]     += r["count"]
        buckets[bucket]["wins"]      += r["wins"]
        buckets[bucket]["total_pnl"] += r["total_pnl"] or 0
        if r["avg_pct"]:
            buckets[bucket]["pcts"].append(r["avg_pct"])

    result = []
    order  = ["0DTE", "1-2 DTE", "3-5 DTE", "6-14 DTE", "15+ DTE", "Unknown"]
    for b in order:
        if b in buckets:
            d = buckets[b]
            avg_pct = round(sum(d["pcts"]) / len(d["pcts"]), 1) if d["pcts"] else 0
            result.append({
                "bucket":    b,
                "count":     d["count"],
                "wins":      d["wins"],
                "total_pnl": round(d["total_pnl"], 2),
                "avg_pct":   avg_pct,
            })
    return result


def get_top_trades(cursor, n=5):
    cursor.execute("""
        SELECT signal_contract, entry_price, exit_price,
               exit_reason, pnl, pnl_pct, hold_days
        FROM paper_trades
        WHERE status = 'CLOSED' AND pnl IS NOT NULL
        ORDER BY pnl DESC
        LIMIT ?
    """, (n,))
    wins = [dict(r) for r in cursor.fetchall()]

    cursor.execute("""
        SELECT signal_contract, entry_price, exit_price,
               exit_reason, pnl, pnl_pct, hold_days
        FROM paper_trades
        WHERE status = 'CLOSED' AND pnl IS NOT NULL
        ORDER BY pnl ASC
        LIMIT ?
    """, (n,))
    losses = [dict(r) for r in cursor.fetchall()]
    return wins, losses


# =============================================================================
# SECTION 2 — OPEN POSITION PRO-FORMA
# =============================================================================

def get_open_proforma(cursor):
    """
    Get all open and stop-triggered positions with their last
    known price from position_snapshots.
    Uses CTE for fast index-friendly query.
    """
    cursor.execute("""
        WITH last_snapshots AS (
            SELECT trade_id, MAX(id) as max_id
            FROM position_snapshots
            GROUP BY trade_id
        )
        SELECT
            pt.id,
            pt.signal_contract,
            pt.entry_price,
            pt.contracts,
            pt.total_cost,
            pt.status,
            pt.dte_at_entry,
            ps.current_price as last_price,
            ps.pnl           as last_pnl,
            ps.pnl_pct       as last_pnl_pct,
            ps.snapshot_time,
            ps.dynamic_stop,
            ps.current_dte
        FROM paper_trades pt
        JOIN last_snapshots ls ON ls.trade_id = pt.id
        JOIN position_snapshots ps ON ps.id = ls.max_id
        WHERE pt.status IN ('OPEN', 'STOP_TRIGGERED')
        ORDER BY ps.pnl DESC
    """)
    return [dict(r) for r in cursor.fetchall()]


# =============================================================================
# DISPLAY
# =============================================================================

def print_report():
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now_str = datetime.now(eastern).strftime("%Y-%m-%d %H:%M:%S %Z")

    conn   = get_db()
    cursor = conn.cursor()

    print(f"\n{'='*70}")
    print(f"  📊 P&L REPORT — {now_str}")
    print(f"{'='*70}")

    # ── Section 1: Closed actuals ──────────────────────────────────────────
    print(f"\n  {'─'*70}")
    print(f"  💰 CLOSED TRADE ACTUALS")
    print(f"  {'─'*70}")

    summary = get_closed_summary(cursor)
    total   = summary["total"] or 0
    wins    = summary["wins"] or 0
    losses  = summary["losses"] or 0
    win_rate = round((wins / total) * 100, 1) if total > 0 else 0

    print(f"\n  Total closed trades : {total}")
    print(f"  Wins / Losses       : {wins}W / {losses}L  ({win_rate}% win rate)")
    print(f"  Total realized P&L  : {fmt_pnl(summary['total_pnl'])}")
    print(f"  Avg P&L per trade   : {fmt_pnl(summary['avg_pnl'])}")
    print(f"  Avg winner          : {fmt_pnl(summary['avg_win'])}")
    print(f"  Avg loser           : {fmt_pnl(summary['avg_loss'])}")
    print(f"  Avg hold time       : {summary['avg_hold_days']} days")
    print(f"  Best trade          : {fmt_pnl(summary['best_trade'])}")
    print(f"  Worst trade         : {fmt_pnl(summary['worst_trade'])}")

    # By exit reason
    print(f"\n  BY EXIT REASON")
    print(f"  {'─'*50}")
    print(f"  {'Reason':<12} {'Count':>6} {'Wins':>6} {'Total P&L':>12} {'Avg %':>8}")
    print(f"  {'─'*50}")
    for r in get_by_exit_reason(cursor):
        win_str = f"{r['wins']}/{r['count']}"
        print(f"  {r['exit_reason']:<12} {r['count']:>6} {win_str:>6} "
              f"{fmt_pnl(r['total_pnl']):>12} {fmt_pct(r['avg_pct']):>8}")

    # By contract type
    print(f"\n  BY CONTRACT TYPE")
    print(f"  {'─'*50}")
    print(f"  {'Type':<8} {'Count':>6} {'Wins':>6} {'Total P&L':>12} {'Avg %':>8}")
    print(f"  {'─'*50}")
    for r in get_by_contract_type(cursor):
        win_str = f"{r['wins']}/{r['count']}"
        print(f"  {r['contract_type']:<8} {r['count']:>6} {win_str:>6} "
              f"{fmt_pnl(r['total_pnl']):>12} {fmt_pct(r['avg_pct']):>8}")

    # By DTE bucket
    print(f"\n  BY DTE AT ENTRY")
    print(f"  {'─'*50}")
    print(f"  {'DTE':<10} {'Count':>6} {'Wins':>6} {'Total P&L':>12} {'Avg %':>8}")
    print(f"  {'─'*50}")
    for r in get_by_dte_bucket(cursor):
        win_str = f"{r['wins']}/{r['count']}"
        print(f"  {r['bucket']:<10} {r['count']:>6} {win_str:>6} "
              f"{fmt_pnl(r['total_pnl']):>12} {fmt_pct(r['avg_pct']):>8}")

    # By ticker
    print(f"\n  BY TICKER (sorted by P&L)")
    print(f"  {'─'*55}")
    print(f"  {'Ticker':<8} {'Count':>6} {'W/L':>6} {'Total P&L':>12} {'Avg %':>8}")
    print(f"  {'─'*55}")
    for r in get_by_ticker(cursor):
        wl_str = f"{r['wins']}W/{r['losses']}L"
        print(f"  {r['ticker']:<8} {r['count']:>6} {wl_str:>6} "
              f"{fmt_pnl(r['total_pnl']):>12} {fmt_pct(r['avg_pct']):>8}")

    # Top wins and losses
    top_wins, top_losses = get_top_trades(cursor, n=5)

    print(f"\n  TOP 5 WINNERS")
    print(f"  {'─'*65}")
    print(f"  {'Contract':<28} {'Entry':>7} {'Exit':>7} {'P&L':>9} {'%':>8}  Reason")
    print(f"  {'─'*65}")
    for t in top_wins:
        print(f"  {t['signal_contract']:<28} "
              f"${t['entry_price']:>6.2f} "
              f"${t['exit_price']:>6.2f} "
              f"{fmt_pnl(t['pnl']):>9} "
              f"{fmt_pct(t['pnl_pct']):>8}  {t['exit_reason']}")

    print(f"\n  TOP 5 LOSERS")
    print(f"  {'─'*65}")
    print(f"  {'Contract':<28} {'Entry':>7} {'Exit':>7} {'P&L':>9} {'%':>8}  Reason")
    print(f"  {'─'*65}")
    for t in top_losses:
        print(f"  {t['signal_contract']:<28} "
              f"${t['entry_price']:>6.2f} "
              f"${t['exit_price']:>6.2f} "
              f"{fmt_pnl(t['pnl']):>9} "
              f"{fmt_pct(t['pnl_pct']):>8}  {t['exit_reason']}")

    # ── Section 2: Open pro-forma ──────────────────────────────────────────
    print(f"\n  {'─'*70}")
    print(f"  📈 OPEN POSITION PRO-FORMA (last known prices)")
    print(f"  {'─'*70}")

    positions = get_open_proforma(cursor)

    if not positions:
        print(f"\n  No open positions.")
    else:
        open_pos   = [p for p in positions if p["status"] == "OPEN"]
        tracked    = [p for p in positions if p["status"] == "STOP_TRIGGERED"]

        unrealized_open    = sum(p["last_pnl"] or 0 for p in open_pos)
        unrealized_tracked = sum(p["last_pnl"] or 0 for p in tracked)
        unrealized_total   = unrealized_open + unrealized_tracked

        profitable = [p for p in positions if (p["last_pnl"] or 0) > 0]
        losing     = [p for p in positions if (p["last_pnl"] or 0) <= 0]

        print(f"\n  Open positions      : {len(open_pos)}")
        print(f"  Tracking (stopped)  : {len(tracked)}")
        print(f"  Profitable          : {len(profitable)}")
        print(f"  Losing              : {len(losing)}")
        print(f"\n  Unrealized P&L (OPEN)      : {fmt_pnl(unrealized_open)}")
        print(f"  Unrealized P&L (TRACKING)  : {fmt_pnl(unrealized_tracked)}")
        print(f"  Total unrealized           : {fmt_pnl(unrealized_total)}")

        # Show profitable open positions
        if profitable:
            print(f"\n  PROFITABLE POSITIONS ({len(profitable)})")
            print(f"  {'─'*70}")
            print(f"  {'ID':<5} {'Contract':<28} {'Entry':>7} {'Last':>7} "
                  f"{'P&L':>9} {'%':>8}  DTE  Status")
            print(f"  {'─'*70}")
            for p in sorted(profitable, key=lambda x: x["last_pnl"] or 0, reverse=True):
                dte_str  = f"{p['current_dte']}d" if p["current_dte"] is not None else "?"
                status_badge = "⏸" if p["status"] == "STOP_TRIGGERED" else "▶"
                print(f"  {p['id']:<5} {p['signal_contract']:<28} "
                      f"${p['entry_price']:>6.2f} "
                      f"${p['last_price']:>6.2f} "
                      f"{fmt_pnl(p['last_pnl']):>9} "
                      f"{fmt_pct(p['last_pnl_pct']):>8}  "
                      f"{dte_str:<5}{status_badge}")

        # Show losing positions summary (not full list to keep report readable)
        if losing:
            total_loss = sum(p["last_pnl"] or 0 for p in losing)
            print(f"\n  LOSING POSITIONS ({len(losing)}) — Total: {fmt_pnl(total_loss)}")
            print(f"  {'─'*70}")
            print(f"  {'ID':<5} {'Contract':<28} {'Entry':>7} {'Last':>7} "
                  f"{'P&L':>9} {'%':>8}  DTE  Status")
            print(f"  {'─'*70}")
            for p in sorted(losing, key=lambda x: x["last_pnl"] or 0):
                dte_str  = f"{p['current_dte']}d" if p["current_dte"] is not None else "?"
                status_badge = "⏸" if p["status"] == "STOP_TRIGGERED" else "▶"
                print(f"  {p['id']:<5} {p['signal_contract']:<28} "
                      f"${p['entry_price']:>6.2f} "
                      f"${p['last_price']:>6.2f} "
                      f"{fmt_pnl(p['last_pnl']):>9} "
                      f"{fmt_pct(p['last_pnl_pct']):>8}  "
                      f"{dte_str:<5}{status_badge}")

    # ── Combined bottom line ───────────────────────────────────────────────
    print(f"\n  {'═'*70}")
    print(f"  💼 PORTFOLIO SNAPSHOT")
    print(f"  {'═'*70}")

    realized   = summary["total_pnl"] or 0
    unrealized = unrealized_total if positions else 0
    combined   = realized + unrealized

    print(f"\n  Realized P&L (closed trades)    : {fmt_pnl(realized)}")
    print(f"  Unrealized P&L (open positions) : {fmt_pnl(unrealized)}")
    print(f"  {'─'*45}")
    print(f"  Combined portfolio value        : {fmt_pnl(combined)}")
    print(f"\n  Starting bankroll               : $10,000.00")
    print(f"  Current value (realized only)   : {fmt_pnl(10000 + realized)}")
    print(f"  Current value (mark-to-market)  : {fmt_pnl(10000 + combined)}")
    print(f"\n{'='*70}\n")

    conn.close()


if __name__ == "__main__":
    print_report()
