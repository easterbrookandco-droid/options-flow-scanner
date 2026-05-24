# analyze_significance_hurdle.py
import sqlite3
import statistics

conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute("""
    SELECT pt.id, pt.signal_contract, pt.entry_price,
           pt.pnl as final_pnl, pt.total_cost, pt.dte_at_entry
    FROM paper_trades pt
    WHERE pt.exit_reason = 'EXPIRED'
    AND pt.pnl < 0
    AND pt.id IN (
        SELECT trade_id FROM position_snapshots WHERE pnl > 0
    )
""")
positions = [dict(r) for r in cursor.fetchall()]

# Test combinations of hurdle % and trailing stop %
hurdles = [0.01, 0.02, 0.03, 0.04, 0.05, 0.075, 0.10, 0.125, 0.15, 0.175, 0.20, 0.225,0.25, 0.50, 0.75, 1.00, 1.50, 2.00]  # % gain from entry
trailing_stops = [0.15, 0.20, 0.25, 0.30]

print(f"TWO-STAGE TRAILING STOP OPTIMIZATION")
print(f"Stage 1: Position must reach hurdle % gain before trailing stop activates")
print(f"Stage 2: Exit if price drops trailing_stop% from peak AFTER hurdle crossed")
print()
print(f"{'Hurdle':>8} {'Trail':>7} {'Saves':>7} {'Extra P&L':>12} {'False Exits':>12} {'Avg Capture%':>13}")
print("-" * 65)

best_combo = None
best_pnl = 0

for hurdle_pct in hurdles:
    for trail_pct in trailing_stops:
        saves = 0
        extra_pnl = 0
        false_exits = 0

        for p in positions:
            cost = p['total_cost'] or 1
            hurdle_pnl = cost * hurdle_pct  # absolute P&L threshold

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

            hurdle_crossed = False
            running_max_after_hurdle = 0
            exit_pnl = None
            exit_idx = None

            for i, s in enumerate(snaps):
                pnl = s['pnl'] or 0

                # Stage 1: check if hurdle crossed
                if not hurdle_crossed and pnl >= hurdle_pnl:
                    hurdle_crossed = True
                    running_max_after_hurdle = pnl

                # Stage 2: trailing stop only active after hurdle
                if hurdle_crossed:
                    if pnl > running_max_after_hurdle:
                        running_max_after_hurdle = pnl
                    drawdown = ((running_max_after_hurdle - pnl) /
                                running_max_after_hurdle)
                    if drawdown > trail_pct:
                        exit_pnl = pnl
                        exit_idx = i
                        break

            if exit_pnl is not None and exit_pnl > p['final_pnl']:
                saves += 1
                extra_pnl += (exit_pnl - p['final_pnl'])

                # Check false exit
                if exit_idx and exit_idx < len(snaps) - 1:
                    post_max = max(s['pnl'] or 0
                                   for s in snaps[exit_idx+1:])
                    if post_max > exit_pnl * 1.10:
                        false_exits += 1

        if saves > 0:
            avg_extra = extra_pnl / saves
            print(f"{hurdle_pct*100:>7.0f}% {trail_pct*100:>6.0f}% "
                  f"{saves:>7} {extra_pnl:>+12,.0f} "
                  f"{false_exits:>12} {avg_extra:>+13,.0f}")

            if extra_pnl > best_pnl:
                best_pnl = extra_pnl
                best_combo = (hurdle_pct, trail_pct, saves, false_exits)

        else:
            print(f"{hurdle_pct*100:>7.0f}% {trail_pct*100:>6.0f}% "
                  f"{'0':>7} {'$0':>12} {'0':>12} {'N/A':>13}")

    print()  # blank line between hurdle groups

if best_combo:
    print(f"\nOptimal combination:")
    print(f"  Hurdle:        {best_combo[0]*100:.0f}% gain before trailing stop activates")
    print(f"  Trailing stop: {best_combo[1]*100:.0f}% from peak after hurdle")
    print(f"  Saves:         {best_combo[2]}")
    print(f"  False exits:   {best_combo[3]}")
    print(f"  Extra P&L:     ${best_pnl:,.0f}")

# Also show what % of positions ever crossed each hurdle
print(f"\nHURDLE CROSSING RATES:")
print(f"(What % of these losing positions ever reached each threshold?)")
for hurdle_pct in hurdles:
    crossed = 0
    for p in positions:
        cost = p['total_cost'] or 1
        hurdle_pnl = cost * hurdle_pct
        cursor.execute("""
            SELECT MAX(pnl) as max_pnl
            FROM position_snapshots
            WHERE trade_id = ?
        """, (p['id'],))
        row = cursor.fetchone()
        if row and row['max_pnl'] and row['max_pnl'] >= hurdle_pnl:
            crossed += 1
    print(f"  {hurdle_pct*100:.0f}% gain hurdle: "
          f"{crossed}/{len(positions)} crossed ({crossed/len(positions)*100:.0f}%)")

conn.close()