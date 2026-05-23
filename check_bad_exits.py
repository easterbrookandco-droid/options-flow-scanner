# check_bad_exits.py
import sqlite3
conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()
cursor.execute("""
    SELECT id, signal_contract, entry_price, exit_price, pnl
    FROM paper_trades
    WHERE exit_reason = 'EXPIRED'
    AND exit_price = 0.01
    AND entry_price > 5.0
    ORDER BY pnl ASC
""")
rows = cursor.fetchall()
for r in rows:
    print(f"#{r['id']} {r['signal_contract']} entry={r['entry_price']} pnl={r['pnl']}")
print(f"Total: {len(rows)}")
conn.close()