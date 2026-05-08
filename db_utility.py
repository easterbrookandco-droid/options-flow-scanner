import sqlite3

DB_PATH = "signals.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Fix entry price AND recalculate P&L on SPY paper trade (ID 5)
# Entry: $4.93 (mid estimate), Exit: $7.42 (intrinsic at expiration)
# P&L: (7.42 - 4.93) * 1 contract * 100 = $249.00
cursor.execute("""
    UPDATE paper_trades 
    SET entry_price = 4.93,
        exit_price = 7.42,
        total_cost = 493.0,
        pnl = 249.0,
        pnl_pct = 50.51
    WHERE id = 5
""")
print(f"Updated {cursor.rowcount} row(s) — SPY paper trade corrected")

conn.commit()
conn.close()
print("Done.")