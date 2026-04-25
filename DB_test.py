import sqlite3
conn = sqlite3.connect("signals.db")
conn.row_factory = sqlite3.Row
cursor = conn.cursor()
cursor.execute("SELECT id, signal_contract, thesis FROM paper_trades ORDER BY id")
for row in cursor.fetchall():
    print(f"#{row['id']} {row['signal_contract']}")
    print(f"   {row['thesis']}")
conn.close()