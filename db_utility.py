import sqlite3

DB_PATH = "signals.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Fix exit price on SPY paper trade (ID 5 in paper_trades table)
cursor.execute("UPDATE paper_trades SET exit_price = 7.42 WHERE id = 5")
print(f"Updated {cursor.rowcount} row(s) — exit price fix")

# Remove deep ITM GOOGL adjusted contracts from signals table
cursor.execute("DELETE FROM signals WHERE ticker = 'GOOGL' AND strike = 150.0")
print(f"Deleted {cursor.rowcount} row(s) — GOOGL adjusted contracts")

conn.commit()
conn.close()
print("Done.")