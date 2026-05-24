# analyze_peak_sequence.py
import sqlite3
import statistics

conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute("""
    SELECT pt.id, pt.signal_contract, pt.pnl as final_pnl, pt.dte_at_entry
    FROM paper_trades pt
    WHERE pt.exit_reason = 'EXPIRED'
    AND pt.pnl < 0
    AND pt.id IN (
        SELECT trade_id FROM position_snapshots WHERE pnl > 0
    )
""")
positions = [dict(r) for r in cursor.fetchall()]

first_peak_is_largest = 0
later_peak_is_larger = 0
peak_sequences = []

for p in positions:
    cursor.execute("""
        SELECT pnl, snapshot_time, current_dte
        FROM position_snapshots
        WHERE trade_id = ?
        AND pnl IS NOT NULL
        ORDER BY id ASC
    """, (p['id'],))
    snaps = [dict(r) for r in cursor.fetchall()]

    if len(snaps) < 5:
        continue

    # Find all local peaks in positive territory
    peaks = []
    for i in range(1, len(snaps)-1):
        prev = snaps[i-1]['pnl'] or 0
        curr = snaps[i]['pnl'] or 0
        nxt  = snaps[i+1]['pnl'] or 0
        if curr > 0 and curr > prev and curr > nxt:
            peaks.append({
                'pnl': curr,
                'dte': snaps[i].get('current_dte'),
                'time': snaps[i]['snapshot_time'],
                'index': i
            })

    if len(peaks) < 2:
        continue

    # Is the first peak the largest?
    first_pnl = peaks[0]['pnl']
    max_pnl = max(pk['pnl'] for pk in peaks)
    max_peak_idx = next(i for i, pk in enumerate(peaks) if pk['pnl'] == max_pnl)

    peak_sequences.append({
        'id': p['id'],
        'n_peaks': len(peaks),
        'first_peak': first_pnl,
        'max_peak': max_pnl,
        'max_is_first': max_peak_idx == 0,
        'peaks': peaks,
        'final_pnl': p['final_pnl']
    })

    if max_peak_idx == 0:
        first_peak_is_largest += 1
    else:
        later_peak_is_larger += 1

total = len(peak_sequences)
print(f"Positions with 2+ peaks: {total}")
print()
print(f"First peak IS the largest:  {first_peak_is_largest} ({first_peak_is_largest/total*100:.0f}%)")
print(f"Later peak is larger:       {later_peak_is_larger} ({later_peak_is_larger/total*100:.0f}%)")
print()

# When later peak is larger, by how much?
later_larger_ratios = []
for ps in peak_sequences:
    if not ps['max_is_first']:
        ratio = ps['max_peak'] / ps['first_peak']
        later_larger_ratios.append(ratio)

if later_larger_ratios:
    print(f"When later peak is larger:")
    print(f"  Median ratio (later/first): {statistics.median(later_larger_ratios):.1f}x")
    print(f"  Mean ratio:                 {statistics.mean(later_larger_ratios):.1f}x")
    print(f"  Max ratio:                  {max(later_larger_ratios):.1f}x")
    print()

# DTE at first peak vs max peak
first_peak_dtes = [ps['peaks'][0]['dte'] for ps in peak_sequences
                   if ps['peaks'][0]['dte'] is not None]
max_peak_dtes = []
for ps in peak_sequences:
    max_idx = next(i for i, pk in enumerate(ps['peaks'])
                   if pk['pnl'] == ps['max_peak'])
    dte = ps['peaks'][max_idx]['dte']
    if dte is not None:
        max_peak_dtes.append(dte)

if first_peak_dtes:
    print(f"DTE when first peak occurs:")
    print(f"  Median: {statistics.median(first_peak_dtes):.0f}d")
    print(f"  Mean:   {statistics.mean(first_peak_dtes):.1f}d")

if max_peak_dtes:
    print(f"DTE when MAX peak occurs:")
    print(f"  Median: {statistics.median(max_peak_dtes):.0f}d")
    print(f"  Mean:   {statistics.mean(max_peak_dtes):.1f}d")

print()
# Simulate: what if we used a larger trailing stop to avoid exiting on early peaks?
print(f"Simulation: what if we waited for 40% drawdown instead of 20%?")
CONSERVATIVE_STOP = 0.40
saves_conservative = 0
pnl_conservative = 0

for p in positions:
    cursor.execute("""
        SELECT pnl FROM position_snapshots
        WHERE trade_id = ? AND pnl IS NOT NULL
        ORDER BY id ASC
    """, (p['id'],))
    snaps = [r['pnl'] or 0 for r in cursor.fetchall()]

    running_max = 0
    exit_pnl = None
    for pnl in snaps:
        if pnl > running_max:
            running_max = pnl
        if running_max > 0:
            drawdown = (running_max - pnl) / running_max
            if drawdown > CONSERVATIVE_STOP:
                exit_pnl = pnl
                break

    if exit_pnl is not None and exit_pnl > p['final_pnl']:
        saves_conservative += 1
        pnl_conservative += (exit_pnl - p['final_pnl'])

print(f"  Saves: {saves_conservative}, Extra P&L: ${pnl_conservative:,.0f}")
print(f"  vs 20% stop: 91 saves, $60,007 extra P&L")

conn.close()