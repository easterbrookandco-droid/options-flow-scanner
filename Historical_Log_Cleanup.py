import sqlite3
from datetime import datetime

conn = sqlite3.connect("signals.db")
cursor = conn.cursor()

today = datetime.now().strftime("%Y-%m-%d")

# Mark all contracts that have already expired with no outcome
cursor.execute("""
    UPDATE signals
    SET outcome = 'EXPIRED',
        outcome_notes = 'Auto-marked EXPIRED — predates automated outcome tracking'
    WHERE expiration < ?
    AND outcome IS NULL
""", (today,))

updated = cursor.rowcount
conn.commit()
conn.close()

print(f"✓ Marked {updated} expired contracts as EXPIRED")