# optimize_full_dataset.py
"""
Comprehensive trailing stop optimization across ALL closed trades.
Tests every combination of hurdle + trailing stop and measures
total P&L impact vs current system.
Includes ITM safety exit at 3:45pm on 0DTE.
"""
import sqlite3
import statistics
from datetime import datetime
import pytz

conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

eastern = pytz.timezone('US/Eastern')

# Get ALL closed trades with snapshots
cursor.execute("""
    SELECT pt.id, pt.signal_contract, pt.entry_price,
           pt.pnl as actual_pnl, pt.total_cost,
           pt.exit_reason, pt.exit_price,
           pt.dte_at_entry, pt.entry_date
    FROM paper_trades pt
    WHERE pt.status = 'CLOSED'
    AND pt.pnl IS NOT NULL
    AND pt.total_cost > 0
    ORDER BY pt.id
""")
all_trades = [dict(r) for r in cursor.fetchall()]

print(f"Total closed trades: {len(all_trades)}")
print(f"Current system P&L: ${sum(t['actual_pnl'] for t in all_trades):,.0f}")
print()

# Load snapshots for all trades upfront for efficiency
print("Loading snapshots...")
trade_snaps = {}
for t in all_trades:
    cursor.execute("""
        SELECT pnl, current_price, snapshot_time, current_dte
        FROM position_snapshots
        WHERE trade_id = ?
        AND pnl IS NOT NULL
        AND current_price > 0
        ORDER BY id ASC
    """, (t['id'],))
    snaps = [dict(r) for r in cursor.fetchall()]
    if snaps:
        trade_snaps[t['id']] = snaps

print(f"Loaded snapshots for {len(trade_snaps)} trades")
print()

def simulate_strategy(trades, hurdle_pct, trail_pct, itm_safety=True):
    """
    Simulate two-stage trailing stop + ITM safety exit across all trades.
    Returns total P&L under the new strategy.
    """
    total_pnl = 0
    exits = {'TARGET': 0, 'STOP': 0, 'TRAILING': 0,
             'ITM_SAFETY': 0, 'EXPIRED': 0, 'MANUAL': 0}

    for t in trades:
        trade_id = t['id']
        cost = t['total_cost']
        entry_price = t['entry_price']
        actual_pnl = t['actual_pnl']
        exit_reason = t['exit_reason']

        # TARGET and MANUAL exits are already optimal — don't change them
        if exit_reason in ('TARGET', 'MANUAL'):
            total_pnl += actual_pnl
            exits[exit_reason] += 1
            continue

        snaps = trade_snaps.get(trade_id, [])
        if not snaps:
            total_pnl += actual_pnl
            exits[exit_reason] += 1
            continue

        hurdle_pnl = cost * hurdle_pct
        hurdle_crossed = False
        running_max_after_hurdle = 0
        sim_exit_pnl = None
        sim_exit_reason = None

        for i, s in enumerate(snaps):
            pnl = s['pnl'] or 0
            price = s['current_price'] or 0
            dte = s.get('current_dte')
            snap_time_str = s.get('snapshot_time', '')

            # ITM safety exit: DTE=0, profitable, after 3:45pm
            if itm_safety and dte == 0 and pnl > 0:
                try:
                    snap_time = datetime.strptime(
                        snap_time_str[:19], '%Y-%m-%d %H:%M:%S'
                    )
                    snap_time_et = pytz.utc.localize(snap_time).astimezone(eastern)
                    if snap_time_et.hour >= 15 and snap_time_et.minute >= 45:
                        sim_exit_pnl = pnl
                        sim_exit_reason = 'ITM_SAFETY'
                        break
                except:
                    pass

            # Stage 1: check hurdle
            if not hurdle_crossed and pnl >= hurdle_pnl:
                hurdle_crossed = True
                running_max_after_hurdle = pnl

            # Stage 2: trailing stop after hurdle
            if hurdle_crossed:
                if pnl > running_max_after_hurdle:
                    running_max_after_hurdle = pnl
                if running_max_after_hurdle > 0:
                    drawdown = ((running_max_after_hurdle - pnl) /
                                running_max_after_hurdle)
                    if drawdown > trail_pct:
                        sim_exit_pnl = pnl
                        sim_exit_reason = 'TRAILING'
                        break

        # Use simulated exit if better than actual
        if sim_exit_pnl is not None:
            if sim_exit_pnl > actual_pnl:
                total_pnl += sim_exit_pnl
                exits[sim_exit_reason] += 1
            else:
                total_pnl += actual_pnl
                exits[exit_reason] += 1
        else:
            total_pnl += actual_pnl
            exits[exit_reason] += 1

    return total_pnl, exits


# Current system baseline
current_pnl = sum(t['actual_pnl'] for t in all_trades)

# Test all combinations
hurdles = [0.01, 0.05, 0.10, 0.15, 0.18, 0.20, 0.25, 0.30, 0.40, 0.50]
trails  = [0.15, 0.20, 0.25, 0.30]

print(f"Current system P&L: ${current_pnl:,.0f}")
print()
print(f"{'Hurdle':>8} {'Trail':>7} {'New P&L':>12} {'Improvement':>13} {'Trailing Exits':>15}")
print("-" * 65)

best_pnl = current_pnl
best_combo = None
results = []

for hurdle in hurdles:
    for trail in trails:
        new_pnl, exits = simulate_strategy(all_trades, hurdle, trail)
        improvement = new_pnl - current_pnl
        trailing_exits = exits.get('TRAILING', 0) + exits.get('ITM_SAFETY', 0)

        results.append({
            'hurdle': hurdle,
            'trail': trail,
            'pnl': new_pnl,
            'improvement': improvement,
            'exits': exits,
            'trailing_exits': trailing_exits
        })

        marker = " ◄ BEST" if new_pnl > best_pnl else ""
        if new_pnl > best_pnl:
            best_pnl = new_pnl
            best_combo = (hurdle, trail, exits)

        print(f"{hurdle*100:>7.0f}% {trail*100:>6.0f}% "
              f"${new_pnl:>11,.0f} "
              f"${improvement:>+12,.0f} "
              f"{trailing_exits:>15}{marker}")

    print()

if best_combo:
    print(f"\n{'='*65}")
    print(f"OPTIMAL COMBINATION:")
    print(f"  Hurdle:        {best_combo[0]*100:.0f}% gain")
    print(f"  Trailing stop: {best_combo[1]*100:.0f}% from peak")
    print(f"  New total P&L: ${best_pnl:,.0f}")
    print(f"  Improvement:   ${best_pnl - current_pnl:+,.0f}")
    print(f"  Exit breakdown: {dict(best_combo[2])}")
    print()
    print(f"  vs current system: ${current_pnl:,.0f}")
    print(f"  Improvement %: {(best_pnl-current_pnl)/abs(current_pnl)*100:+.1f}%")

# Also show ITM safety exit alone (no trailing stop)
print(f"\n{'='*65}")
print(f"ITM SAFETY EXIT ONLY (no trailing stop):")
itm_only_pnl, itm_exits = simulate_strategy(
    all_trades, hurdle_pct=999, trail_pct=999, itm_safety=True
)
print(f"  P&L: ${itm_only_pnl:,.0f} (improvement: ${itm_only_pnl-current_pnl:+,.0f})")
print(f"  ITM safety exits fired: {itm_exits.get('ITM_SAFETY', 0)}")

conn.close()