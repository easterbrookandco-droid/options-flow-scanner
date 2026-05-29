# fix_volatility_analysis.py
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

recovery_drawdowns = []
death_drawdowns = []
false_exit_recovery_amounts = []

TRAILING_STOP = 0.20

for p in positions:
    cursor.execute("""
        SELECT current_price, pnl, snapshot_time, current_dte
        FROM position_snapshots
        WHERE trade_id = ? AND pnl IS NOT NULL AND current_price > 0
        ORDER BY id ASC
    """, (p['id'],))
    snaps = [dict(r) for r in cursor.fetchall()]

    if len(snaps) < 3:
        continue

    # Find local peaks — only count peaks where pnl > 0
    for i in range(1, len(snaps)-1):
        prev = snaps[i-1]['pnl'] or 0
        curr = snaps[i]['pnl'] or 0
        nxt  = snaps[i+1]['pnl'] or 0

        # Must be a genuine peak in positive territory
        if curr <= 0 or curr <= prev or curr <= nxt:
            continue

        # Find minimum after this peak before either recovery or end
        min_after_pnl = curr
        recovered = False

        for j in range(i+1, len(snaps)):
            future = snaps[j]['pnl'] or 0
            if future < min_after_pnl:
                min_after_pnl = future
            # Recovery = got back to 90% of peak
            if future >= curr * 0.90:
                recovered = True
                break

        # Only calculate drawdown within positive territory
        # Clamp min_after to 0 to avoid cross-zero distortion
        min_after_clamped = max(min_after_pnl, 0)
        drawdown_pct = (curr - min_after_clamped) / curr

        if recovered:
            recovery_drawdowns.append(drawdown_pct)
        else:
            death_drawdowns.append(drawdown_pct)

    # Simulate trailing stop and measure false exit recovery
    running_max = 0
    exit_idx = None
    exit_pnl = None

    for i, s in enumerate(snaps):
        pnl = s['pnl'] or 0
        if pnl > running_max:
            running_max = pnl
        if running_max > 0 and exit_pnl is None:
            drawdown = (running_max - pnl) / running_max
            if drawdown > TRAILING_STOP:
                exit_pnl = pnl
                exit_idx = i
                break

    if exit_pnl is not None and exit_idx < len(snaps) - 1:
        post_max = max(s['pnl'] or 0 for s in snaps[exit_idx+1:])
        if post_max > exit_pnl:
            false_exit_recovery_amounts.append(post_max - exit_pnl)

print(f"Recovery drawdown distribution (n={len(recovery_drawdowns)}):")
if recovery_drawdowns:
    print(f"  Median: {statistics.median(recovery_drawdowns)*100:.1f}%")
    print(f"  Mean:   {statistics.mean(recovery_drawdowns)*100:.1f}%")
    # Remove outliers > 100% for stdev
    clean = [x for x in recovery_drawdowns if x <= 1.0]
    if len(clean) > 1:
        print(f"  StdDev (capped at 100%): {statistics.stdev(clean)*100:.1f}%")
    print(f"  Percentiles:")
    sorted_r = sorted(recovery_drawdowns)
    n = len(sorted_r)
    for pct in [25, 50, 75, 90]:
        val = sorted_r[int(n * pct/100)]
        print(f"    {pct}th: {val*100:.1f}%")

print(f"\nDeath drawdown distribution (n={len(death_drawdowns)}):")
if death_drawdowns:
    print(f"  Median: {statistics.median(death_drawdowns)*100:.1f}%")
    clean_d = [x for x in death_drawdowns if x <= 1.0]
    if clean_d:
        print(f"  Mean (capped): {statistics.mean(clean_d)*100:.1f}%")

print(f"\nFalse exit analysis (20% trailing stop):")
if false_exit_recovery_amounts:
    print(f"  Times position recovered after our exit: {len(false_exit_recovery_amounts)}")
    print(f"  Avg recovery amount we missed: ${statistics.mean(false_exit_recovery_amounts):.0f}")
    print(f"  Max recovery missed: ${max(false_exit_recovery_amounts):.0f}")
    print(f"  Total missed upside: ${sum(false_exit_recovery_amounts):.0f}")

conn.close()