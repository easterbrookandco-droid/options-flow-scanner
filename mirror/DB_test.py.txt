import sqlite3
conn = sqlite3.connect("signals.db")
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute("""
    SELECT DISTINCT
        ticker, contract, contract_type, strike,
        expiration, premium, composite_score,
        signal_tier, share_price, scan_time
    FROM signals
    WHERE scan_time LIKE '2026-04-27%'
    AND signal_tier = 'HIGH'
    ORDER BY composite_score DESC, premium DESC
    LIMIT 30
""")

for row in cursor.fetchall():
    prem = row['premium']
    prem_display = (f"${prem/1_000_000:.1f}M" if prem >= 1_000_000
                    else f"${prem/1_000:.0f}K")
    price = f"${row['share_price']:,.2f}" if row['share_price'] else "—"
    print(f"{row['ticker']:<6} {row['contract']:<28} "
          f"{row['contract_type']:<5} "
          f"Score:{row['composite_score']:<6} "
          f"Premium:{prem_display:<10} "
          f"Stock:{price:<10} "
          f"Exp:{row['expiration']}")

conn.close()