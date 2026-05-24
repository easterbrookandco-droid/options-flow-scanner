# analyze_profit_patterns.py
import sqlite3
from datetime import datetime
import pytz

conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

eastern = pytz.timezone('US/Eastern')

# Get positions that expired at loss but were profitable
cursor.execute("""
    SELECT pt.id, pt.signal_contract, pt.entry_price,
           pt.pnl as final_pnl, pt.dte_at_entry
    FROM paper_trades pt
    WHERE pt.exit_reason = 'EXPIRED'
    AND pt.pnl < 0
    AND pt.id IN (
        SELECT trade_id FROM position_snapshots
        WHERE pnl > 0
    )
    ORDER BY pt.id
""")
positions = [dict(r) for r in cursor.fetchall()]

print(f"Analyzing {len(positions)} positions that peaked then reversed...\n")

peak_on_0dte = 0
peak_before_0dte = 0
multiple_peaks = 0
trailing_stop_saves = 0
trailing_stop_pnl = 0

for p in positions:
    trade_id = p['id']
    contract = p['signal_contract']

    # Parse expiration
    try:
        date_str = contract[-15:-9]
        exp_date = datetime.strptime(date_str, '%y%m%d').date()
    except:
        continue

    # Get all snapshots ordered by time
    cursor.execute("""
        SELECT current_price, pnl, pnl_pct, snapshot_time, current_dte
        FROM position_snapshots
        WHERE trade_id = ?
        ORDER BY id ASC
    """, (trade_id,))
    snaps = [dict(r) for r in cursor.fetchall()]

    if not snaps:
        continue

    # Find when max pnl occurred
    max_pnl = max(s['pnl'] or 0 for s in snaps)
    max_snap = next(s for s in snaps if (s['pnl'] or 0) == max_pnl)
    max_dte = max_snap.get('current_dte')

    # Count local maxima (peaks before valleys)
    peaks = 0
    in_profit = False
    prev_pnl = 0
    for s in snaps:
        pnl = s['pnl'] or 0
        if pnl > 0 and not in_profit:
            in_profit = True
        elif pnl > prev_pnl and in_profit:
            pass  # still rising
        elif pnl < prev_pnl * 0.8 and in_profit and prev_pnl > 0:
            peaks += 1
            in_profit = False
        prev_pnl = pnl

    if peaks > 1:
        multiple_peaks += 1

    # Check if peak was on 0DTE
    if max_dte == 0:
        peak_on_0dte += 1
    else:
        peak_before_0dte += 1

    # Simulate trailing stop: exit if price drops 25% from peak
    running_max = 0
    trailing_exit_pnl = None
    for s in snaps:
        pnl = s['pnl'] or 0
        price = s['current_price'] or 0
        if pnl > running_max:
            running_max = pnl
        if running_max > 0:
            drawdown = (running_max - pnl) / running_max
            if drawdown > 0.25 and trailing_exit_pnl is None:
                trailing_exit_pnl = pnl
                break

    if trailing_exit_pnl and trailing_exit_pnl > p['final_pnl']:
        trailing_stop_saves += 1
        trailing_stop_pnl += (trailing_exit_pnl - p['final_pnl'])

print(f"Peak occurred on 0DTE:      {peak_on_0dte}")
print(f"Peak occurred before 0DTE:  {peak_before_0dte}")
print(f"Multiple profit peaks:      {multiple_peaks}")
print(f"\nTrailing stop simulation (25% drawdown from peak):")
print(f"  Would have improved exits: {trailing_stop_saves}")
print(f"  Additional P&L captured:   ${trailing_stop_pnl:,.0f}")

conn.close()