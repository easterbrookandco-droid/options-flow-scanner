# check_today_stops.py
import sqlite3
conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# Get the AMD positions stopped today with their snapshot history after exit
ids = [315, 292, 289]
for tid in ids:
    cursor.execute("""
        SELECT exit_price, pnl, pnl_pct, signal_contract, dte_at_entry
        FROM paper_trades WHERE id = ?
    """, (tid,))
    trade = dict(cursor.fetchone())
    
    # Get snapshots after the stop
    cursor.execute("""
        SELECT current_price, pnl, snapshot_time
        FROM position_snapshots
        WHERE trade_id = ?
        ORDER BY id DESC
        LIMIT 5
    """, (tid,))
    snaps = [dict(r) for r in cursor.fetchall()]
    
    print(f"\n#{tid} {trade['signal_contract']}")
    print(f"  Exit: ${trade['exit_price']:.2f}, P&L at exit: ${trade['pnl']:,.0f}")
    print(f"  Most recent snapshots after stop:")
    for s in reversed(snaps):
        print(f"    {s['snapshot_time']} price=${s['current_price']:.2f} pnl=${s['pnl']:,.0f}")

conn.close()