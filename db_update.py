import sqlite3
conn = sqlite3.connect("signals.db")
cursor = conn.cursor()

cursor.execute("""
    UPDATE paper_trades SET thesis = ? WHERE id = ?
""", ("$5.2M premium in 4 DTE calls contradicts the 58% put bias, signaling potential gamma squeeze on a reversal.", 3))

cursor.execute("""
    UPDATE paper_trades SET thesis = ? WHERE id = ?
""", ("$2.9M in put premium with -0.41 delta at 3 DTE contradicts the 77% bullish call bias, signaling smart money hedging into SPY weakness.", 4))

conn.commit()
conn.close()
print("✓ Thesis updated")