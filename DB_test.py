import sqlite3
conn = sqlite3.connect("signals.db")
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# Verify backlog is cleared
cursor.execute("""
    SELECT COUNT(*) as n FROM signals
    WHERE expiration < date('now')
    AND outcome IS NULL
""")
print(f"Unresolved expired contracts: {cursor.fetchone()['n']}")

# Verify EXPIRED count looks right
cursor.execute("""
    SELECT COUNT(*) as n FROM signals
    WHERE outcome = 'EXPIRED'
""")
print(f"Marked as EXPIRED: {cursor.fetchone()['n']}")

# Check today's expiring list size
from datetime import datetime
today = datetime.now().strftime("%Y-%m-%d")
cursor.execute("""
    SELECT COUNT(DISTINCT contract) as n FROM signals
    WHERE expiration = ?
    AND outcome IS NULL
""", (today,))
print(f"Unique contracts expiring today (pending): {cursor.fetchone()['n']}")

conn.close()