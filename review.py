import sqlite3
from datetime import datetime

DB_PATH = "signals.db"

def review_expiring_today():
    """
    Pull all signals expiring today from the journal.
    Use this at end of day to evaluate outcomes.
    """
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT * FROM signals
        WHERE expiration = ?
        AND signal_tier IN ('HIGH', 'INST')
        ORDER BY composite_score DESC
    """, (today,))
    
    signals = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    if not signals:
        print(f"\n  No HIGH or INST signals expiring today ({today})")
        return
    
    print(f"\n{'='*80}")
    print(f"  📋 Expiring Today: {today} — {len(signals)} signals to evaluate")
    print(f"{'='*80}")
    print(f"\n  {'Contract':<30} {'Type':<6} {'Strike':<10} {'Score':<8} {'Premium':<12} Outcome")
    print(f"  {'-'*75}")
    
    for s in signals:
        strike = f"${s['strike']:.2f}"
        premium = s['premium']
        if premium >= 1_000_000:
            premium_display = f"${premium/1_000_000:.1f}M"
        else:
            premium_display = f"${premium/1_000:.0f}K"
        
        outcome = s['outcome'] or "⬜ pending"
        print(f"  {s['contract']:<30} {s['contract_type']:<6} {strike:<10} "
              f"{s['composite_score']:<8} {premium_display:<12} {outcome}")
    
    print(f"\n  To record an outcome:")
    print(f"  from journal import record_outcome")
    print(f"  record_outcome('CONTRACT_SYMBOL', 'WIN', 'brief note')")


def record_batch_outcomes():
    """
    Interactive outcome recorder.
    Walks you through each expiring signal and asks for outcome.
    """
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT * FROM signals
        WHERE expiration = ?
        AND signal_tier IN ('HIGH', 'INST')
        AND outcome IS NULL
        ORDER BY composite_score DESC
    """, (today,))
    
    signals = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    if not signals:
        print(f"\n  No pending outcomes for today.")
        return
    
    print(f"\n{'='*60}")
    print(f"  📝 Recording Outcomes — {len(signals)} signals")
    print(f"  Enter W (win), L (loss), F (flat), or S (skip)")
    print(f"{'='*60}\n")
    
    recorded = 0
    
    for s in signals:
        strike = s['strike']
        print(f"  Contract: {s['contract']}")
        print(f"  Type:     {s['contract_type']} | Strike: ${strike:.2f} | "
              f"Score: {s['composite_score']} | Premium: ${s['premium']/1_000_000:.1f}M")
        
        response = input("  Outcome (W/L/F/S): ").strip().upper()
        
        if response == "S":
            print("  Skipped.\n")
            continue
        
        outcome_map = {"W": "WIN", "L": "LOSS", "F": "FLAT"}
        outcome = outcome_map.get(response)
        
        if not outcome:
            print("  Invalid input — skipped.\n")
            continue
        
        note = input("  Note (optional, press Enter to skip): ").strip()
        
        # Update the database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE signals
            SET outcome = ?, outcome_notes = ?
            WHERE contract = ? AND outcome IS NULL
        """, (outcome, note, s['contract']))
        conn.commit()
        conn.close()
        
        recorded += 1
        print(f"  ✓ Recorded: {outcome}\n")
    
    print(f"  Done — {recorded} outcomes recorded.")


def show_performance():
    """
    Display win rate statistics by tier.
    Run this after recording outcomes to see how the scanner is performing.
    """
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT
            signal_tier,
            contract_type,
            COUNT(*) as total,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN outcome = 'FLAT' THEN 1 ELSE 0 END) as flats,
            AVG(composite_score) as avg_score,
            AVG(premium) as avg_premium
        FROM signals
        WHERE outcome IS NOT NULL
        GROUP BY signal_tier, contract_type
        ORDER BY signal_tier, contract_type
    """)
    
    results = cursor.fetchall()
    conn.close()
    
    if not results:
        print("\n  No completed signals yet. Run after recording outcomes.")
        return
    
    print(f"\n{'='*65}")
    print(f"  📊 Scanner Performance")
    print(f"{'='*65}")
    print(f"\n  {'Tier':<8} {'Type':<6} {'Total':<8} {'Wins':<8} {'Losses':<8} {'Win %'}")
    print(f"  {'-'*50}")
    
    for row in results:
        total = row[2]
        wins = row[3]
        win_pct = round((wins / total) * 100, 1) if total > 0 else 0
        print(f"  {row[0]:<8} {row[1]:<6} {total:<8} {wins:<8} {row[4]:<8} {win_pct}%")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "review":
            review_expiring_today()
        elif command == "record":
            record_batch_outcomes()
        elif command == "performance":
            show_performance()
        else:
            print(f"Unknown command: {command}")
            print("Usage: python review.py [review|record|performance]")
    else:
        # Default: show today's expiring signals
        review_expiring_today()