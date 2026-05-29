# -*- coding: utf-8 -*-
"""
entry_analyzer.py

Analyzes closed paper trades to find which signal characteristics
at entry time predict winning vs losing trades.

Answers:
  - What score/premium/DTE thresholds separate winners from losers?
  - Which score buckets produce the best win rates?
  - Which premium tiers are worth trading?
  - How does win rate vary by DTE at entry?
  - What does the average winner look like vs the average loser?

Usage:
    python entry_analyzer.py
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


def fmt_pct(pct, width=7):
    if pct is None:
        return "N/A".rjust(width)
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%".rjust(width)


def fmt_pnl(pnl, width=10):
    if pnl is None:
        return "N/A".rjust(width)
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.0f}".rjust(width)


def win_rate(wins, total):
    if total == 0:
        return 0.0
    return round((wins / total) * 100, 1)


def bar(pct, width=20):
    """Simple ASCII progress bar for win rate."""
    filled = int((pct / 100) * width)
    return "█" * filled + "░" * (width - filled)


# =============================================================================
# WINNER vs LOSER PROFILE
# =============================================================================

def winner_loser_profile(cursor):
    cursor.execute("""
        SELECT
            CASE WHEN pnl > 0 THEN 'WINNER' ELSE 'LOSER' END as outcome,
            COUNT(*)                            as count,
            ROUND(AVG(score_at_entry), 2)       as avg_score,
            ROUND(MIN(score_at_entry), 2)       as min_score,
            ROUND(MAX(score_at_entry), 2)       as max_score,
            ROUND(AVG(dte_at_entry), 1)         as avg_dte,
            ROUND(AVG(ABS(delta_at_entry)), 3)  as avg_delta,
            ROUND(AVG(iv_at_entry), 3)          as avg_iv,
            ROUND(AVG(pnl), 2)                  as avg_pnl,
            ROUND(AVG(pnl_pct), 1)              as avg_pnl_pct,
            ROUND(AVG(hold_days), 1)            as avg_hold
        FROM paper_trades
        WHERE status = 'CLOSED'
        AND pnl IS NOT NULL
        GROUP BY outcome
        ORDER BY outcome DESC
    """)
    return [dict(r) for r in cursor.fetchall()]


# =============================================================================
# SCORE BUCKETS
# =============================================================================

def by_score_bucket(cursor):
    cursor.execute("""
        SELECT
            CASE
                WHEN score_at_entry >= 8.0 THEN '8.0+'
                WHEN score_at_entry >= 7.0 THEN '7.0-7.9'
                WHEN score_at_entry >= 6.0 THEN '6.0-6.9'
                WHEN score_at_entry >= 5.0 THEN '5.0-5.9'
                WHEN score_at_entry >= 4.0 THEN '4.0-4.9'
                ELSE 'Under 4.0'
            END as score_bucket,
            COUNT(*) as total,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(AVG(pnl), 2) as avg_pnl,
            ROUND(AVG(pnl_pct), 1) as avg_pct,
            ROUND(SUM(pnl), 2) as total_pnl
        FROM paper_trades
        WHERE status = 'CLOSED'
        AND pnl IS NOT NULL
        AND score_at_entry IS NOT NULL
        GROUP BY score_bucket
        ORDER BY MIN(score_at_entry) DESC
    """)
    return [dict(r) for r in cursor.fetchall()]


# =============================================================================
# PREMIUM TIERS
# =============================================================================

def by_premium_tier(cursor):
    # Need to join signals to get premium at entry
    cursor.execute("""
        SELECT
            CASE
                WHEN s.premium >= 5000000  THEN '$5M+'
                WHEN s.premium >= 2000000  THEN '$2M-5M'
                WHEN s.premium >= 1000000  THEN '$1M-2M'
                WHEN s.premium >= 500000   THEN '$500K-1M'
                WHEN s.premium >= 100000   THEN '$100K-500K'
                ELSE 'Under $100K'
            END as premium_tier,
            COUNT(*) as total,
            SUM(CASE WHEN pt.pnl > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(AVG(pt.pnl), 2) as avg_pnl,
            ROUND(AVG(pt.pnl_pct), 1) as avg_pct,
            ROUND(SUM(pt.pnl), 2) as total_pnl
        FROM paper_trades pt
        JOIN signals s ON pt.signal_contract = s.contract
        WHERE pt.status = 'CLOSED'
        AND pt.pnl IS NOT NULL
        GROUP BY premium_tier
        ORDER BY MIN(s.premium) DESC
    """)
    return [dict(r) for r in cursor.fetchall()]


# =============================================================================
# DTE BUCKETS
# =============================================================================

def by_dte_bucket(cursor):
    cursor.execute("""
        SELECT
            CASE
                WHEN dte_at_entry = 0  THEN '0DTE'
                WHEN dte_at_entry <= 2 THEN '1-2d'
                WHEN dte_at_entry <= 5 THEN '3-5d'
                WHEN dte_at_entry <= 14 THEN '6-14d'
                WHEN dte_at_entry <= 30 THEN '15-30d'
                ELSE 'Unknown'
            END as dte_bucket,
            COUNT(*) as total,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(AVG(pnl), 2) as avg_pnl,
            ROUND(AVG(pnl_pct), 1) as avg_pct,
            ROUND(SUM(pnl), 2) as total_pnl,
            ROUND(AVG(score_at_entry), 2) as avg_score
        FROM paper_trades
        WHERE status = 'CLOSED'
        AND pnl IS NOT NULL
        GROUP BY dte_bucket
        ORDER BY MIN(dte_at_entry)
    """)
    return [dict(r) for r in cursor.fetchall()]


# =============================================================================
# SCORE x DTE CROSS TABLE
# =============================================================================

def score_x_dte(cursor):
    """Cross-tab of score bucket vs DTE bucket showing win rate."""
    cursor.execute("""
        SELECT
            CASE
                WHEN score_at_entry >= 7.0 THEN 'Score 7+'
                WHEN score_at_entry >= 5.0 THEN 'Score 5-7'
                ELSE 'Score <5'
            END as score_group,
            CASE
                WHEN dte_at_entry <= 2  THEN '0-2d'
                WHEN dte_at_entry <= 5  THEN '3-5d'
                WHEN dte_at_entry <= 14 THEN '6-14d'
                ELSE '15d+'
            END as dte_group,
            COUNT(*) as total,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(AVG(pnl_pct), 1) as avg_pct,
            ROUND(SUM(pnl), 2) as total_pnl
        FROM paper_trades
        WHERE status = 'CLOSED'
        AND pnl IS NOT NULL
        AND score_at_entry IS NOT NULL
        AND dte_at_entry IS NOT NULL
        GROUP BY score_group, dte_group
        ORDER BY score_group DESC, MIN(dte_at_entry)
    """)
    return [dict(r) for r in cursor.fetchall()]


# =============================================================================
# DIRECTION x MARKET CONTEXT
# =============================================================================

def direction_x_market(cursor):
    cursor.execute("""
        SELECT
            CASE 
                WHEN substr(signal_contract, length(signal_contract)-8, 1) = 'C'
                THEN 'CALL'
                ELSE 'PUT'
            END as contract_type,
            market_bias_at_entry,
            COUNT(*) as total,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(AVG(pnl), 2) as avg_pnl,
            ROUND(AVG(pnl_pct), 1) as avg_pct,
            ROUND(SUM(pnl), 2) as total_pnl
        FROM paper_trades
        WHERE status = 'CLOSED'
        AND pnl IS NOT NULL
        GROUP BY contract_type, market_bias_at_entry
        ORDER BY contract_type, market_bias_at_entry
    """)
    return [dict(r) for r in cursor.fetchall()]


# =============================================================================
# EXIT REASON BREAKDOWN BY SCORE
# =============================================================================

def exit_by_score(cursor):
    """
    For each score bucket, what % hit target vs expired vs stopped?
    Shows whether higher scores actually lead to better exits.
    """
    cursor.execute("""
        SELECT
            CASE
                WHEN score_at_entry >= 8.0 THEN '8.0+'
                WHEN score_at_entry >= 6.0 THEN '6.0-7.9'
                WHEN score_at_entry >= 4.0 THEN '4.0-5.9'
                ELSE 'Under 4.0'
            END as score_bucket,
            exit_reason,
            COUNT(*) as count,
            ROUND(AVG(pnl_pct), 1) as avg_pct
        FROM paper_trades
        WHERE status = 'CLOSED'
        AND pnl IS NOT NULL
        AND score_at_entry IS NOT NULL
        GROUP BY score_bucket, exit_reason
        ORDER BY MIN(score_at_entry) DESC, exit_reason
    """)
    return [dict(r) for r in cursor.fetchall()]


# =============================================================================
# MINIMUM VIABLE SIGNAL THRESHOLD FINDER
# =============================================================================

def threshold_analysis(cursor):
    """
    Simulate what win rate and total P&L would have been if we had
    used different minimum score thresholds for entry.
    """
    thresholds = [3.0, 4.0, 5.0, 6.0, 7.0, 7.5, 8.0]
    results = []

    for thresh in thresholds:
        cursor.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                ROUND(SUM(pnl), 2) as total_pnl,
                ROUND(AVG(pnl_pct), 1) as avg_pct
            FROM paper_trades
            WHERE status = 'CLOSED'
            AND pnl IS NOT NULL
            AND score_at_entry >= ?
        """, (thresh,))
        row = dict(cursor.fetchone())
        row["threshold"] = thresh
        results.append(row)

    return results


# =============================================================================
# DISPLAY
# =============================================================================

def print_analysis():
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now_str = datetime.now(eastern).strftime("%Y-%m-%d %H:%M:%S %Z")

    conn   = get_db()
    cursor = conn.cursor()

    print(f"\n{'='*70}")
    print(f"  🔬 ENTRY QUALITY ANALYZER — {now_str}")
    print(f"  Based on {67} closed trades (week 1 data)")
    print(f"{'='*70}")

    # ── Winner vs Loser Profile ────────────────────────────────────────────
    print(f"\n  {'─'*70}")
    print(f"  📊 WINNER vs LOSER PROFILE AT ENTRY")
    print(f"  {'─'*70}")

    profiles = winner_loser_profile(cursor)
    for p in profiles:
        icon = "🟢" if p["outcome"] == "WINNER" else "🔴"
        print(f"\n  {icon} {p['outcome']}S ({p['count']} trades)")
        print(f"     Avg score    : {p['avg_score']}  "
              f"(range: {p['min_score']} - {p['max_score']})")
        print(f"     Avg DTE      : {p['avg_dte']} days")
        print(f"     Avg |delta|  : {p['avg_delta']}")
        print(f"     Avg IV       : {p['avg_iv']}")
        print(f"     Avg P&L      : {fmt_pnl(p['avg_pnl'])}  "
              f"({fmt_pct(p['avg_pnl_pct'])})")
        print(f"     Avg hold     : {p['avg_hold']} days")

    # ── Score Buckets ──────────────────────────────────────────────────────
    print(f"\n  {'─'*70}")
    print(f"  📊 WIN RATE BY SCORE BUCKET")
    print(f"  {'─'*70}")
    print(f"  {'Score':<12} {'Count':>6} {'W/L':>8} {'Win%':>7} "
          f"{'Avg P&L':>10} {'Total P&L':>12}  Chart")
    print(f"  {'─'*70}")

    for r in by_score_bucket(cursor):
        wl    = f"{r['wins']}/{r['total']-r['wins']}"
        wr    = win_rate(r["wins"], r["total"])
        chart = bar(wr, width=15)
        print(f"  {r['score_bucket']:<12} {r['total']:>6} {wl:>8} "
              f"{wr:>6.1f}% "
              f"{fmt_pnl(r['avg_pnl']):>10} "
              f"{fmt_pnl(r['total_pnl']):>12}  {chart}")

    # ── Premium Tiers ──────────────────────────────────────────────────────
    print(f"\n  {'─'*70}")
    print(f"  📊 WIN RATE BY PREMIUM TIER AT SIGNAL TIME")
    print(f"  {'─'*70}")
    print(f"  {'Premium':<14} {'Count':>6} {'W/L':>8} {'Win%':>7} "
          f"{'Avg P&L':>10} {'Total P&L':>12}  Chart")
    print(f"  {'─'*70}")

    for r in by_premium_tier(cursor):
        wl    = f"{r['wins']}/{r['total']-r['wins']}"
        wr    = win_rate(r["wins"], r["total"])
        chart = bar(wr, width=15)
        print(f"  {r['premium_tier']:<14} {r['total']:>6} {wl:>8} "
              f"{wr:>6.1f}% "
              f"{fmt_pnl(r['avg_pnl']):>10} "
              f"{fmt_pnl(r['total_pnl']):>12}  {chart}")

    # ── DTE Buckets ────────────────────────────────────────────────────────
    print(f"\n  {'─'*70}")
    print(f"  📊 WIN RATE BY DTE AT ENTRY")
    print(f"  {'─'*70}")
    print(f"  {'DTE':<10} {'Count':>6} {'W/L':>8} {'Win%':>7} "
          f"{'Avg Score':>10} {'Avg P&L':>10} {'Total P&L':>12}  Chart")
    print(f"  {'─'*70}")

    for r in by_dte_bucket(cursor):
        wl    = f"{r['wins']}/{r['total']-r['wins']}"
        wr    = win_rate(r["wins"], r["total"])
        chart = bar(wr, width=15)
        avg_score_str = f"{r['avg_score']:.2f}" if r['avg_score'] is not None else "N/A"
        print(f"  {r['dte_bucket']:<10} {r['total']:>6} {wl:>8} "
              f"{wr:>6.1f}% "
              f"{avg_score_str:>10} "
              f"{fmt_pnl(r['avg_pnl']):>10} "
              f"{fmt_pnl(r['total_pnl']):>12}  {chart}")

    # ── Score x DTE Cross Table ────────────────────────────────────────────
    print(f"\n  {'─'*70}")
    print(f"  📊 SCORE x DTE CROSS ANALYSIS")
    print(f"  (Key question: does high score + short DTE = best combo?)")
    print(f"  {'─'*70}")
    print(f"  {'Score':<12} {'DTE':<8} {'Count':>6} {'W/L':>8} "
          f"{'Win%':>7} {'Avg%':>8} {'Total P&L':>12}")
    print(f"  {'─'*70}")

    for r in score_x_dte(cursor):
        wl = f"{r['wins']}/{r['total']-r['wins']}"
        wr = win_rate(r["wins"], r["total"])
        print(f"  {r['score_group']:<12} {r['dte_group']:<8} "
              f"{r['total']:>6} {wl:>8} "
              f"{wr:>6.1f}% "
              f"{fmt_pct(r['avg_pct']):>8} "
              f"{fmt_pnl(r['total_pnl']):>12}")

    # ── Direction x Market Context ─────────────────────────────────────────
    print(f"\n  {'─'*70}")
    print(f"  📊 CALLS vs PUTS BY MARKET BIAS AT ENTRY")
    print(f"  (The key table for market context analysis)")
    print(f"  {'─'*70}")
    print(f"  {'Type':<6} {'Market Bias':<12} {'Count':>6} {'W/L':>8} "
          f"{'Win%':>7} {'Avg%':>8} {'Total P&L':>12}")
    print(f"  {'─'*70}")

    for r in direction_x_market(cursor):
        wl   = f"{r['wins']}/{r['total']-r['wins']}"
        wr   = win_rate(r["wins"], r["total"])
        bias = r["market_bias_at_entry"] or "UNKNOWN"
        print(f"  {r['contract_type']:<6} {bias:<12} "
              f"{r['total']:>6} {wl:>8} "
              f"{wr:>6.1f}% "
              f"{fmt_pct(r['avg_pct']):>8} "
              f"{fmt_pnl(r['total_pnl']):>12}")

    # ── Exit Reason by Score ───────────────────────────────────────────────
    print(f"\n  {'─'*70}")
    print(f"  📊 EXIT REASON BY SCORE BUCKET")
    print(f"  (Do higher scores actually hit targets more often?)")
    print(f"  {'─'*70}")
    print(f"  {'Score':<12} {'Exit':<10} {'Count':>6} {'Avg%':>8}")
    print(f"  {'─'*70}")

    last_bucket = None
    for r in exit_by_score(cursor):
        if r["score_bucket"] != last_bucket:
            if last_bucket is not None:
                print()
            last_bucket = r["score_bucket"]
        print(f"  {r['score_bucket']:<12} {r['exit_reason']:<10} "
              f"{r['count']:>6} {fmt_pct(r['avg_pct']):>8}")

    # ── Threshold Analysis ─────────────────────────────────────────────────
    print(f"\n  {'─'*70}")
    print(f"  📊 MINIMUM SCORE THRESHOLD SIMULATION")
    print(f"  (If we had only entered signals above each threshold...)")
    print(f"  {'─'*70}")
    print(f"  {'Min Score':>10} {'Trades':>8} {'Win%':>8} "
          f"{'Avg%':>8} {'Total P&L':>12}  Chart")
    print(f"  {'─'*70}")

    for r in threshold_analysis(cursor):
        wr    = win_rate(r["wins"], r["total"])
        chart = bar(wr, width=15)
        print(f"  {r['threshold']:>10} {r['total']:>8} "
              f"{wr:>7.1f}% "
              f"{fmt_pct(r['avg_pct']):>8} "
              f"{fmt_pnl(r['total_pnl']):>12}  {chart}")

    # ── Key Findings ───────────────────────────────────────────────────────
    print(f"\n  {'═'*70}")
    print(f"  💡 KEY QUESTIONS TO ANSWER WITH MORE DATA")
    print(f"  {'═'*70}")
    print(f"""
  1. Does the score threshold simulation show a clear jump in win rate
     at any specific score level? If yes, that's your first model change.

  2. Do calls perform better or worse than puts across ALL market biases,
     or only when market is bearish? Need 3-4 weeks to know.

  3. Is the 1-2 DTE dominance real edge, or just this week's market move?
     Compare DTE breakdown week over week.

  4. Does premium tier predict outcomes independent of score?
     If $2M+ premium = 70%+ win rate regardless of score, that's a filter.

  5. Check the Score x DTE cross table — is Score 7+ / 0-2d DTE your
     sweet spot, or does another combination dominate?

  Run this report again in 2 weeks. The patterns that persist across
  different market conditions are your real edge.
""")

    conn.close()


if __name__ == "__main__":
    print_analysis()
