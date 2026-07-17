import sqlite3
conn = sqlite3.connect("signals.db")
conn.row_factory = sqlite3.Row
cursor = conn.cursor()
cursor.execute("""
    SELECT id, signal_contract, exit_price, exit_reason, pnl, pnl_pct
    FROM paper_trades
    WHERE status = 'CLOSED'
    ORDER BY exit_date DESC, exit_time DESC
    LIMIT 5
""")
for row in cursor.fetchall():
    print(f"#{row['id']} {row['signal_contract']}")
    print(f"   Exit: ${row['exit_price']:.2f}  "
          f"Reason: {row['exit_reason']}  "
          f"P&L: ${row['pnl']:.2f} ({row['pnl_pct']:.1f}%)")
conn.close()