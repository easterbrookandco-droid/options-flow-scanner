# optimize_trailing_stop.py
import sqlite3
from datetime import datetime

conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# Get positions that expired at loss but were profitable
cursor.execute("""
    SELECT pt.id, pt.signal_contract, pt.entry_price,
           pt.pnl as final_pnl, pt.dte_at_entry
    FROM paper_trades pt
    WHERE pt.exit_reason = 'EXPIRED'
    AND pt.pnl < 0
    AND pt.id IN (
        SELECT trade_id FROM position_snapshots WHERE pnl > 0
    )
""")
positions = [dict(r) for r in cursor.fetchall()]

# For each position get full snapshot history
position_data = []
for p in positions:
    cursor.execute("""
        SELECT current_price, pnl, pnl_pct, snapshot_time, current_dte
        FROM position_snapshots
        WHERE trade_id = ?
        AND pnl IS NOT NULL
        ORDER BY id ASC
    """, (p['id'],))
    snaps = [dict(r) for r in cursor.fetchall()]
    if len(snaps) > 3:
        position_data.append({'trade': p, 'snaps': snaps})

print(f"Positions with sufficient snapshot history: {len(position_data)}")
print()

# Test different trailing stop thresholds
thresholds = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50]

print(f"{'Threshold':>10} {'Saves':>7} {'Extra P&L':>12} {'Avg Extra':>10} {'False Exits':>12}")
print("-" * 60)

best_threshold = None
best_pnl = 0

for thresh in thresholds:
    saves = 0
    extra_pnl = 0
    false_exits = 0  # times we'd exit but position later recovered higher

    for pd in position_data:
        snaps = pd['snaps']
        final_pnl = pd['trade']['final_pnl']

        running_max_pnl = 0
        trailing_exit_pnl = None
        trailing_exit_idx = None

        for i, s in enumerate(snaps):
            pnl = s['pnl'] or 0
            if pnl > running_max_pnl:
                running_max_pnl = pnl
            if running_max_pnl > 0 and trailing_exit_pnl is None:
                drawdown = (running_max_pnl - pnl) / running_max_pnl
                if drawdown > thresh:
                    trailing_exit_pnl = pnl
                    trailing_exit_idx = i
                    break

        if trailing_exit_pnl is not None:
            if trailing_exit_pnl > final_pnl:
                saves += 1
                extra_pnl += (trailing_exit_pnl - final_pnl)

                # Check if position recovered higher after our exit
                if trailing_exit_idx < len(snaps) - 1:
                    post_exit_max = max(
                        (s['pnl'] or 0) for s in snaps[trailing_exit_idx+1:]
                    )
                    if post_exit_max > trailing_exit_pnl * 1.1:
                        false_exits += 1

    avg_extra = extra_pnl / saves if saves > 0 else 0
    print(f"{thresh*100:>9.0f}% {saves:>7} {extra_pnl:>+12,.0f} "
          f"{avg_extra:>+10,.0f} {false_exits:>12}")

    if extra_pnl > best_pnl:
        best_pnl = extra_pnl
        best_threshold = thresh

print(f"\nOptimal threshold: {best_threshold*100:.0f}%")
print(f"Best additional P&L: ${best_pnl:,.0f}")

# Now analyze volatility characteristics at exit points
print("\n--- VOLATILITY ANALYSIS ---")
print("Looking at price behavior around peaks...")

all_drawdowns_before_recovery = []
all_drawdowns_before_death = []

for pd in position_data:
    snaps = pd['snaps']

    # Find all local peaks and what happened after
    for i in range(1, len(snaps)-1):
        prev_pnl = snaps[i-1]['pnl'] or 0
        curr_pnl = snaps[i]['pnl'] or 0
        next_pnl = snaps[i+1]['pnl'] or 0

        # Local peak
        if curr_pnl > prev_pnl and curr_pnl > next_pnl and curr_pnl > 0:
            # How far did it fall before either recovering or dying?
            min_after = curr_pnl
            recovered = False
            for j in range(i+1, len(snaps)):
                future_pnl = snaps[j]['pnl'] or 0
                min_after = min(min_after, future_pnl)
                if future_pnl > curr_pnl * 0.9:  # recovered to 90% of peak
                    recovered = True
                    break

            drawdown_pct = (curr_pnl - min_after) / curr_pnl if curr_pnl > 0 else 0

            if recovered:
                all_drawdowns_before_recovery.append(drawdown_pct)
            else:
                all_drawdowns_before_death.append(drawdown_pct)

if all_drawdowns_before_recovery:
    import statistics
    rec_mean = statistics.mean(all_drawdowns_before_recovery)
    rec_stdev = statistics.stdev(all_drawdowns_before_recovery) if len(all_drawdowns_before_recovery) > 1 else 0
    rec_median = statistics.median(all_drawdowns_before_recovery)

    death_mean = statistics.mean(all_drawdowns_before_death) if all_drawdowns_before_death else 0
    death_stdev = statistics.stdev(all_drawdowns_before_death) if len(all_drawdowns_before_death) > 1 else 0
    death_median = statistics.median(all_drawdowns_before_death) if all_drawdowns_before_death else 0

    print(f"\nDrawdowns before RECOVERY (n={len(all_drawdowns_before_recovery)}):")
    print(f"  Mean:   {rec_mean*100:.1f}%")
    print(f"  Median: {rec_median*100:.1f}%")
    print(f"  StdDev: {rec_stdev*100:.1f}%")
    print(f"  → Stop at mean+1stdev = {(rec_mean+rec_stdev)*100:.1f}% to allow recovery")

    print(f"\nDrawdowns before DEATH (n={len(all_drawdowns_before_death)}):")
    print(f"  Mean:   {death_mean*100:.1f}%")
    print(f"  Median: {death_median*100:.1f}%")
    print(f"  StdDev: {death_stdev*100:.1f}%")

    separation = death_mean - rec_mean
    optimal_stat = rec_mean + rec_stdev
    print(f"\nSeparation between recovery and death drawdowns: {separation*100:.1f}%")
    print(f"Statistical optimal stop (mean + 1 stdev of recovery drawdowns): {optimal_stat*100:.1f}%")

conn.close()