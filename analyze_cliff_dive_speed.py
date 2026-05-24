# analyze_cliff_dive_speed.py
import sqlite3
import statistics
from datetime import datetime

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

TRAILING_STOP = 0.20

drop_durations_minutes = []
drop_from_peak_to_trigger = []
missed_entirely = 0
caught_within_5min = 0
caught_within_15min = 0
caught_within_30min = 0
caught_within_60min = 0
caught_over_60min = 0

for p in positions:
    cursor.execute("""
        SELECT current_price, pnl, pnl_pct, snapshot_time, current_dte
        FROM position_snapshots
        WHERE trade_id = ?
        AND pnl IS NOT NULL AND current_price > 0
        ORDER BY id ASC
    """, (p['id'],))
    snaps = [dict(r) for r in cursor.fetchall()]

    if len(snaps) < 3:
        continue

    running_max_pnl = 0
    peak_time = None
    trigger_time = None

    for s in snaps:
        pnl = s['pnl'] or 0
        snap_time_str = s['snapshot_time']

        try:
            snap_time = datetime.strptime(snap_time_str[:19], '%Y-%m-%d %H:%M:%S')
        except:
            continue

        if pnl > running_max_pnl:
            running_max_pnl = pnl
            peak_time = snap_time

        if running_max_pnl > 0 and trigger_time is None:
            drawdown = (running_max_pnl - pnl) / running_max_pnl
            if drawdown > TRAILING_STOP:
                trigger_time = snap_time
                break

    if peak_time and trigger_time:
        duration_mins = (trigger_time - peak_time).total_seconds() / 60
        drop_durations_minutes.append(duration_mins)
        drop_from_peak_to_trigger.append(running_max_pnl)

        if duration_mins <= 5:
            caught_within_5min += 1
        elif duration_mins <= 15:
            caught_within_15min += 1
        elif duration_mins <= 30:
            caught_within_30min += 1
        elif duration_mins <= 60:
            caught_within_60min += 1
        else:
            caught_over_60min += 1
    else:
        missed_entirely += 1

print(f"Cliff dive speed analysis (20% trailing stop trigger)")
print(f"Total positions analyzed: {len(positions)}")
print(f"Trailing stop triggered:  {len(drop_durations_minutes)}")
print(f"Never triggered:          {missed_entirely}")
print()
print(f"TIME FROM PEAK TO TRIGGER:")
print(f"  Within 5 min:   {caught_within_5min:>4} ({caught_within_5min/len(drop_durations_minutes)*100:.0f}%)")
print(f"  5-15 min:       {caught_within_15min:>4} ({caught_within_15min/len(drop_durations_minutes)*100:.0f}%)")
print(f"  15-30 min:      {caught_within_30min:>4} ({caught_within_30min/len(drop_durations_minutes)*100:.0f}%)")
print(f"  30-60 min:      {caught_within_60min:>4} ({caught_within_60min/len(drop_durations_minutes)*100:.0f}%)")
print(f"  Over 60 min:    {caught_over_60min:>4} ({caught_over_60min/len(drop_durations_minutes)*100:.0f}%)")
print()
if drop_durations_minutes:
    print(f"DURATION STATISTICS:")
    print(f"  Median: {statistics.median(drop_durations_minutes):.0f} minutes")
    print(f"  Mean:   {statistics.mean(drop_durations_minutes):.0f} minutes")
    print(f"  Min:    {min(drop_durations_minutes):.0f} minutes")
    print(f"  Max:    {max(drop_durations_minutes):.0f} minutes")
    sorted_d = sorted(drop_durations_minutes)
    n = len(sorted_d)
    print(f"  25th percentile: {sorted_d[int(n*0.25)]:.0f} min")
    print(f"  75th percentile: {sorted_d[int(n*0.75)]:.0f} min")

conn.close()