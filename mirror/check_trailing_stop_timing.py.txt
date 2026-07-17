# check_trailing_stop_timing.py
import sqlite3
import statistics

conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute("""
    SELECT pt.id, pt.signal_contract, pt.pnl as final_pnl
    FROM paper_trades pt
    WHERE pt.exit_reason = 'EXPIRED'
    AND pt.pnl < 0
    AND pt.id IN (
        SELECT trade_id FROM position_snapshots WHERE pnl > 0
    )
""")
positions = [dict(r) for r in cursor.fetchall()]

TRAILING_STOP = 0.20

profitable_at_trigger = 0
loss_at_trigger = 0
peak_pnl_list = []
trigger_pnl_list = []

for p in positions:
    cursor.execute("""
        SELECT pnl, current_price
        FROM position_snapshots
        WHERE trade_id = ?
        AND pnl IS NOT NULL
        ORDER BY id ASC
    """, (p['id'],))
    snaps = [dict(r) for r in cursor.fetchall()]

    if len(snaps) < 3:
        continue

    running_max = 0
    for s in snaps:
        pnl = s['pnl'] or 0
        if pnl > running_max:
            running_max = pnl
        if running_max > 0:
            drawdown = (running_max - pnl) / running_max
            if drawdown > TRAILING_STOP:
                trigger_pnl = pnl
                peak_pnl_list.append(running_max)
                trigger_pnl_list.append(trigger_pnl)
                if trigger_pnl > 0:
                    profitable_at_trigger += 1
                else:
                    loss_at_trigger += 1
                break

print(f"At the moment trailing stop triggers:")
print(f"  Still profitable: {profitable_at_trigger} ({profitable_at_trigger/len(positions)*100:.0f}%)")
print(f"  Already at loss:  {loss_at_trigger} ({loss_at_trigger/len(positions)*100:.0f}%)")
print()
print(f"Peak P&L stats:")
print(f"  Median peak: ${statistics.median(peak_pnl_list):,.0f}")
print(f"  Avg peak:    ${statistics.mean(peak_pnl_list):,.0f}")
print(f"  Min peak:    ${min(peak_pnl_list):,.0f}")
print()
print(f"Trigger P&L stats:")
print(f"  Median trigger: ${statistics.median(trigger_pnl_list):,.0f}")
print(f"  Avg trigger:    ${statistics.mean(trigger_pnl_list):,.0f}")
print()

# The real question: what % of peak is captured at trigger
capture_rates = []
for peak, trigger in zip(peak_pnl_list, trigger_pnl_list):
    if peak > 0:
        capture_rates.append(trigger / peak)

print(f"Capture rate (trigger P&L / peak P&L):")
print(f"  Median: {statistics.median(capture_rates)*100:.1f}%")
print(f"  Mean:   {statistics.mean(capture_rates)*100:.1f}%")
print(f"  Positions capturing positive P&L at exit: "
      f"{sum(1 for r in capture_rates if r > 0)} of {len(capture_rates)}")

conn.close()