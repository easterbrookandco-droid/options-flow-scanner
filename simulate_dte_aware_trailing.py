# simulate_dte_aware_trailing.py
import sqlite3
import statistics
from datetime import datetime
import pytz

conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()
eastern = pytz.timezone('US/Eastern')

cursor.execute("""
    SELECT pt.id, pt.signal_contract, pt.entry_price,
           pt.pnl as actual_pnl, pt.total_cost,
           pt.exit_reason, pt.dte_at_entry
    FROM paper_trades pt
    WHERE pt.status = 'CLOSED'
    AND pt.pnl IS NOT NULL
    AND pt.total_cost > 0
""")
all_trades = [dict(r) for r in cursor.fetchall()]

print(f"Loading snapshots...")
trade_snaps = {}
for t in all_trades:
    cursor.execute("""
        SELECT pnl, pnl_pct, current_price, snapshot_time, current_dte
        FROM position_snapshots
        WHERE trade_id = ? AND pnl IS NOT NULL AND current_price > 0
        ORDER BY id ASC
    """, (t['id'],))
    snaps = [dict(r) for r in cursor.fetchall()]
    if snaps:
        trade_snaps[t['id']] = snaps

def get_dte_params(dte):
    """DTE-aware hurdle and trailing stop percentages."""
    if dte is None or dte <= 2:
        return 0.01, 0.20   # 1% hurdle, 20% trail
    elif dte <= 5:
        return 0.15, 0.25   # 15% hurdle, 25% trail
    elif dte <= 14:
        return 0.25, 0.30   # 25% hurdle, 30% trail
    else:
        return 0.40, 0.35   # 40% hurdle, 35% trail

def get_backstop(dte):
    if dte is None or dte <= 2:
        return 0.60
    elif dte <= 14:
        return 0.70
    else:
        return 0.80

def simulate(trades, use_dte_aware):
    total_pnl = 0
    exits = {}
    improved = []
    hurt = []

    for t in trades:
        actual_pnl  = t['actual_pnl']
        cost        = t['total_cost']
        exit_reason = t['exit_reason']
        dte_entry   = t['dte_at_entry']

        if exit_reason in ('TARGET', 'MANUAL'):
            total_pnl += actual_pnl
            exits[exit_reason] = exits.get(exit_reason, 0) + 1
            continue

        snaps = trade_snaps.get(t['id'], [])
        if not snaps:
            total_pnl += actual_pnl
            exits[exit_reason] = exits.get(exit_reason, 0) + 1
            continue

        sim_exit_pnl    = None
        sim_exit_reason = None
        hurdle_crossed  = False
        running_max     = 0

        for s in snaps:
            pnl = s['pnl'] or 0
            dte = s.get('current_dte')

            # Get DTE-aware params for this snapshot
            if use_dte_aware:
                hurdle_pct, trailing_pct = get_dte_params(dte)
            else:
                hurdle_pct, trailing_pct = 0.01, 0.20

            backstop_pct  = get_backstop(dte)
            backstop_loss = -(cost * backstop_pct)
            hurdle_pnl    = cost * hurdle_pct

            # Backstop
            if pnl <= backstop_loss:
                sim_exit_pnl    = pnl
                sim_exit_reason = 'BACKSTOP'
                break

            # Trailing stop
            if not hurdle_crossed and pnl >= hurdle_pnl:
                hurdle_crossed = True
                running_max    = pnl

            if hurdle_crossed:
                if pnl > running_max:
                    running_max = pnl
                if running_max > 0:
                    drawdown = (running_max - pnl) / running_max
                    if drawdown > trailing_pct:
                        sim_exit_pnl    = pnl
                        sim_exit_reason = 'TRAILING'
                        break

        if sim_exit_pnl is not None and sim_exit_pnl > actual_pnl:
            total_pnl += sim_exit_pnl
            exits[sim_exit_reason] = exits.get(sim_exit_reason, 0) + 1
            diff = sim_exit_pnl - actual_pnl
            if diff > 10:
                improved.append(diff)
        else:
            total_pnl += actual_pnl
            exits[exit_reason] = exits.get(exit_reason, 0) + 1

    wins = sum(1 for t in trades
               if (simulate_single_pnl(t, trade_snaps.get(t['id'], []), use_dte_aware) or 0) > 0)

    return total_pnl, exits, improved

def simulate_single_pnl(trade, snaps, use_dte_aware):
    actual_pnl  = trade['actual_pnl']
    cost        = trade['total_cost']
    exit_reason = trade['exit_reason']

    if exit_reason in ('TARGET', 'MANUAL'):
        return actual_pnl
    if not snaps:
        return actual_pnl

    hurdle_crossed = False
    running_max    = 0

    for s in snaps:
        pnl = s['pnl'] or 0
        dte = s.get('current_dte')

        if use_dte_aware:
            hurdle_pct, trailing_pct = get_dte_params(dte)
        else:
            hurdle_pct, trailing_pct = 0.01, 0.20

        backstop_pct  = get_backstop(dte)
        backstop_loss = -(cost * backstop_pct)
        hurdle_pnl    = cost * hurdle_pct

        if pnl <= backstop_loss:
            return pnl if pnl > actual_pnl else actual_pnl

        if not hurdle_crossed and pnl >= hurdle_pnl:
            hurdle_crossed = True
            running_max    = pnl

        if hurdle_crossed:
            if pnl > running_max:
                running_max = pnl
            if running_max > 0:
                drawdown = (running_max - pnl) / running_max
                if drawdown > trailing_pct:
                    return pnl if pnl > actual_pnl else actual_pnl

    return actual_pnl

current_pnl = sum(t['actual_pnl'] for t in all_trades)

print(f"\nCurrent system P&L:          ${current_pnl:,.0f}")

pnl_flat, exits_flat, _   = simulate(all_trades, use_dte_aware=False)
pnl_dte,  exits_dte,  _   = simulate(all_trades, use_dte_aware=True)

print(f"Flat model (1%/20%):         ${pnl_flat:,.0f}  ({pnl_flat-current_pnl:+,.0f} vs current)")
print(f"DTE-aware model:             ${pnl_dte:,.0f}  ({pnl_dte-current_pnl:+,.0f} vs current)")
print()
print(f"DTE-aware vs flat:           ${pnl_dte-pnl_flat:+,.0f}")
print()
print(f"Flat exits:     {exits_flat}")
print(f"DTE-aware exits: {exits_dte}")

conn.close()