# fix_bad_exits.py
import sqlite3

conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# Get all bad exits
cursor.execute("""
    SELECT id, signal_contract, entry_price, contracts, total_cost
    FROM paper_trades
    WHERE exit_reason = 'EXPIRED'
    AND exit_price = 0.01
    AND entry_price > 5.0
""")
positions = [dict(r) for r in cursor.fetchall()]
print(f"Found {len(positions)} positions to fix")

fixed = 0
skipped = 0

for p in positions:
    trade_id = p['id']
    
    # Get last known snapshot price
    cursor.execute("""
        WITH last_snap AS (
            SELECT trade_id, MAX(id) as max_id
            FROM position_snapshots
            GROUP BY trade_id
        )
        SELECT ps.current_price
        FROM position_snapshots ps
        JOIN last_snap ls ON ps.id = ls.max_id
        WHERE ps.trade_id = ?
    """, (trade_id,))
    snap = cursor.fetchone()
    
    if not snap or not snap['current_price']:
        print(f"  #{trade_id} {p['signal_contract']} — no snapshot, skipping")
        skipped += 1
        continue
    
    exit_price = float(snap['current_price'])
    pnl = round((exit_price - p['entry_price']) * p['contracts'] * 100, 2)
    pnl_pct = round((pnl / p['total_cost']) * 100, 2) if p['total_cost'] else 0
    
    cursor.execute("""
        UPDATE paper_trades SET
            exit_price = ?,
            pnl = ?,
            pnl_pct = ?
        WHERE id = ?
    """, (exit_price, pnl, pnl_pct, trade_id))
    
    print(f"  #{trade_id} {p['signal_contract']} "
          f"entry={p['entry_price']} exit={exit_price:.2f} pnl={pnl:.2f}")
    fixed += 1

conn.commit()
conn.close()
print(f"\nFixed: {fixed}  Skipped: {skipped}")