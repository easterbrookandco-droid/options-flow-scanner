import sqlite3
conn = sqlite3.connect("signals.db")
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# What's the outcome breakdown across all signals
cursor.execute("""
    SELECT outcome, COUNT(*) as n
    FROM signals
    GROUP BY outcome
    ORDER BY n DESC
""")
for row in cursor.fetchall():
    outcome = row['outcome'] if row['outcome'] else 'NULL (pending)'
    print(f"  {outcome:<25} {row['n']:>6}")

conn.close()