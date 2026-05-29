# analyze_slippage.py
import sqlite3
import statistics
from datetime import datetime

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
POLL_INTERVAL = 3  # minutes

trigger_pnl_list = []
actual_exit_pnl_list = []
slippage_list = []
slippage_pct_list = []

for p in positions:
    cursor.execute("""
        SELECT pnl, snapshot_time
        FROM position_snapshots
        WHERE trade_id = ?
        AND pnl IS NOT NULL
        ORDER BY id ASC
    """, (p['id'],))
    snaps = [dict(r) for r in cursor.fetchall()]

    if len(snaps) < 3:
        continue

    running_max = 0
    trigger_idx = None
    trigger_pnl = None

    for i, s in enumerate(snaps):
        pnl = s['pnl'] or 0
        if pnl > running_max:
            running_max = pnl
        if running_max > 0 and trigger_idx is None:
            drawdown = (running_max - pnl) / running_max
            if drawdown > TRAILING_STOP:
                trigger_idx = i
                trigger_pnl = pnl
                break

    if trigger_idx is None or trigger_pnl is None:
        continue

    # Simulate: we don't exit at trigger, we exit at next poll
    # Find next snapshot (represents what we'd actually get)
    if trigger_idx + 1 < len(snaps):
        actual_exit_pnl = snaps[trigger_idx + 1]['pnl'] or 0
    else:
        actual_exit_pnl = trigger_pnl

    slippage = trigger_pnl - actual_exit_pnl
    slippage_pct = slippage / abs(trigger_pnl) if trigger_pnl != 0 else 0

    trigger_pnl_list.append(trigger_pnl)
    actual_exit_pnl_list.append(actual_exit_pnl)
    slippage_list.append(slippage)
    slippage_pct_list.append(slippage_pct)

print(f"Slippage Analysis (3-min polling delay)")
print(f"Positions analyzed: {len(slippage_list)}")
print()
print(f"AT TRIGGER POINT:")
print(f"  Avg P&L at trigger:     ${statistics.mean(trigger_pnl_list):,.0f}")
print(f"  Median P&L at trigger:  ${statistics.median(trigger_pnl_list):,.0f}")
print()
print(f"AFTER ONE POLLING DELAY:")
print(f"  Avg actual exit P&L:    ${statistics.mean(actual_exit_pnl_list):,.0f}")
print(f"  Median actual exit P&L: ${statistics.median(actual_exit_pnl_list):,.0f}")
print()
print(f"SLIPPAGE (trigger P&L - actual exit P&L):")
print(f"  Avg slippage:    ${statistics.mean(slippage_list):,.0f}")
print(f"  Median slippage: ${statistics.median(slippage_list):,.0f}")
print(f"  Max slippage:    ${max(slippage_list):,.0f}")
print(f"  Positions where actual exit > trigger: "
      f"{sum(1 for s in slippage_list if s < 0)} "
      f"(price recovered between polls)")
print()

# Net benefit even with slippage
total_trigger_pnl = sum(trigger_pnl_list)
total_actual_pnl = sum(actual_exit_pnl_list)
total_final_pnl = sum(p['final_pnl'] for p in positions
                      if any(True for _ in [1]))

print(f"NET BENEFIT ANALYSIS:")
print(f"  If we exit at trigger:      ${total_trigger_pnl:,.0f}")
print(f"  If we exit one poll later:  ${total_actual_pnl:,.0f}")
print(f"  If we held to expiration:   ${sum(p['final_pnl'] for p in positions):,.0f}")

conn.close()