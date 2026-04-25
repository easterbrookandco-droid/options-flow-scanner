import sqlite3
conn = sqlite3.connect("signals.db")
conn.row_factory = sqlite3.Row
cursor = conn.cursor()
today = "2026-04-24"

cursor.execute("""
    SELECT COUNT(*) as total,
           COUNT(DISTINCT contract) as unique_contracts
    FROM signals
    WHERE expiration = ?
""", (today,))
row = cursor.fetchone()
print(f"Total rows expiring today: {row['total']}")
print(f"Unique contracts expiring today: {row['unique_contracts']}")

cursor.execute("""
    SELECT COUNT(*) as already_expired_pending
    FROM signals  
    WHERE expiration < ?
    AND outcome IS NULL
""", (today,))
row = cursor.fetchone()
print(f"Expired with no outcome: {row['already_expired_pending']}")

conn.close()