# simulate_combined_model.py
"""
Simulates the complete current model against all closed trades:
  1. Two-stage trailing stop (1% hurdle + 20% trail)
  2. Tiered DTE backstop (60% / 70% / 80%)
  3. ITM safety exit (DTE=0, profitable, after 3:45pm)

Compares against:
  A. Current system (as-is)
  B. Trailing stop only (no backstop)
  C. Backstop only (no trailing stop)
  D. Combined model (trailing stop + backstop)

Also shows which specific trades each model improves or hurts.
"""
import sqlite3
import statistics
from datetime import datetime
import pytz

conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

eastern = pytz.timezone('US/Eastern')

# Load all closed trades
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

print(f"Loading snapshots for {len(all_trades)} trades...")

# Load all snapshots upfront
trade_snaps = {}
for t in all_trades:
    cursor.execute("""
        SELECT pnl, pnl_pct, current_price, snapshot_time, current_dte
        FROM position_snapshots
        WHERE trade_id = ?
        AND pnl IS NOT NULL
        AND current_price > 0
        ORDER BY id ASC
    """, (t['id'],))
    snaps = [dict(r) for r in cursor.fetchall()]
    if snaps:
        trade_snaps[t['id']] = snaps

print(f"Snapshots loaded for {len(trade_snaps)} trades")
print()

# ── Model parameters ──────────────────────────────────────────
HURDLE_PCT       = 0.01   # 1% gain to activate trailing stop
TRAILING_PCT     = 0.20   # 20% drawdown from post-hurdle peak

# Tiered backstop by DTE
def get_backstop(dte):
    if dte is None:
        return 0.70
    if dte <= 2:
        return 0.60   # 60% for 1-2 DTE
    elif dte <= 14:
        return 0.70   # 70% for 3-14 DTE
    else:
        return 0.80   # 80% for 15+ DTE


def simulate_trade(trade, snaps, use_trailing, use_backstop, use_itm_safety):
    """
    Simulate a single trade under the given model.
    Returns (exit_pnl, exit_reason_sim)
    """
    actual_pnl   = trade['actual_pnl']
    cost         = trade['total_cost']
    entry_price  = trade['entry_price']
    exit_reason  = trade['exit_reason']
    dte_at_entry = trade['dte_at_entry']

    # TARGET and MANUAL — already optimal, don't touch
    if exit_reason in ('TARGET', 'MANUAL'):
        return actual_pnl, exit_reason

    if not snaps:
        return actual_pnl, exit_reason

    hurdle_pnl      = cost * HURDLE_PCT
    backstop_pct    = get_backstop(dte_at_entry)
    backstop_loss   = -(cost * backstop_pct)

    hurdle_crossed           = False
    running_max_after_hurdle = 0
    sim_exit_pnl             = None
    sim_exit_reason          = None

    for i, s in enumerate(snaps):
        pnl   = s['pnl'] or 0
        dte   = s.get('current_dte')
        price = s.get('current_price', 0)
        snap_time_str = s.get('snapshot_time', '')

        # ITM safety exit
        if use_itm_safety and dte == 0 and pnl > 0:
            try:
                snap_dt = datetime.strptime(snap_time_str[:19], '%Y-%m-%d %H:%M:%S')
                snap_et = pytz.utc.localize(snap_dt).astimezone(eastern)
                if snap_et.hour > 15 or (snap_et.hour == 15 and snap_et.minute >= 45):
                    sim_exit_pnl    = pnl
                    sim_exit_reason = 'ITM_SAFETY'
                    break
            except:
                pass

        # Backstop check
        if use_backstop and pnl <= backstop_loss:
            sim_exit_pnl    = pnl
            sim_exit_reason = f'BACKSTOP_{int(backstop_pct*100)}pct'
            break

        # Trailing stop
        if use_trailing:
            if not hurdle_crossed and pnl >= hurdle_pnl:
                hurdle_crossed           = True
                running_max_after_hurdle = pnl

            if hurdle_crossed:
                if pnl > running_max_after_hurdle:
                    running_max_after_hurdle = pnl
                if running_max_after_hurdle > 0:
                    drawdown = (running_max_after_hurdle - pnl) / running_max_after_hurdle
                    if drawdown > TRAILING_PCT:
                        sim_exit_pnl    = pnl
                        sim_exit_reason = 'TRAILING'
                        break

    # Use simulated exit only if it's better than actual
    if sim_exit_pnl is not None and sim_exit_pnl > actual_pnl:
        return sim_exit_pnl, sim_exit_reason
    return actual_pnl, exit_reason


def run_model(trades, use_trailing, use_backstop, use_itm_safety, label):
    total_pnl  = 0
    exits      = {}
    improved   = []
    hurt       = []

    for t in trades:
        snaps     = trade_snaps.get(t['id'], [])
        sim_pnl, sim_reason = simulate_trade(
            t, snaps, use_trailing, use_backstop, use_itm_safety
        )
        total_pnl += sim_pnl
        exits[sim_reason] = exits.get(sim_reason, 0) + 1

        diff = sim_pnl - t['actual_pnl']
        if diff > 10:
            improved.append({'id': t['id'], 'contract': t['signal_contract'],
                            'actual': t['actual_pnl'], 'sim': sim_pnl, 'diff': diff})
        elif diff < -10:
            hurt.append({'id': t['id'], 'contract': t['signal_contract'],
                        'actual': t['actual_pnl'], 'sim': sim_pnl, 'diff': diff})

    wins   = sum(1 for t in trades
                 if simulate_trade(t, trade_snaps.get(t['id'], []),
                                   use_trailing, use_backstop, use_itm_safety)[0] > 0)
    total  = len(trades)
    wr     = wins / total * 100 if total else 0

    return {
        'label':    label,
        'pnl':      total_pnl,
        'win_rate': wr,
        'wins':     wins,
        'total':    total,
        'exits':    exits,
        'improved': improved,
        'hurt':     hurt
    }


# Run all four models
current_pnl = sum(t['actual_pnl'] for t in all_trades)

print(f"Running simulations...")
print()

models = [
    (False, False, False, "A. Current system (no trailing, no backstop)"),
    (True,  False, True,  "B. Trailing stop only (+ ITM safety)"),
    (False, True,  True,  "C. Backstop only (+ ITM safety)"),
    (True,  True,  True,  "D. Combined model (trailing + backstop + ITM safety)"),
]

results = []
for use_t, use_b, use_i, label in models:
    r = run_model(all_trades, use_t, use_b, use_i, label)
    results.append(r)

# ── Summary table ──────────────────────────────────────────────
print(f"{'='*70}")
print(f"MODEL COMPARISON SUMMARY")
print(f"{'='*70}")
print(f"{'Model':<45} {'Total P&L':>12} {'vs Current':>12} {'Win Rate':>10}")
print(f"{'-'*70}")
for r in results:
    improvement = r['pnl'] - current_pnl
    marker = " ◄ BEST" if r['pnl'] == max(x['pnl'] for x in results) else ""
    print(f"  {r['label']:<43} ${r['pnl']:>10,.0f} "
          f"${improvement:>+10,.0f} "
          f"{r['win_rate']:>9.1f}%{marker}")

print()

# ── Detailed exit breakdown for combined model ─────────────────
combined = results[3]
print(f"{'='*70}")
print(f"COMBINED MODEL — EXIT BREAKDOWN")
print(f"{'='*70}")
for reason, count in sorted(combined['exits'].items(),
                             key=lambda x: x[1], reverse=True):
    print(f"  {reason:<30} {count:>5} exits")

print()

# ── Trades improved by combined model ─────────────────────────
improved = sorted(combined['improved'], key=lambda x: x['diff'], reverse=True)
print(f"TOP 10 TRADES IMPROVED BY COMBINED MODEL")
print(f"{'ID':<6} {'Contract':<28} {'Actual':>9} {'Simulated':>10} {'Improvement':>12}")
print(f"{'-'*68}")
for t in improved[:10]:
    print(f"  #{t['id']:<4} {t['contract']:<28} "
          f"${t['actual']:>8,.0f} ${t['sim']:>9,.0f} ${t['diff']:>+11,.0f}")

print()

# ── Trades hurt by combined model ─────────────────────────────
hurt = sorted(combined['hurt'], key=lambda x: x['diff'])
print(f"TRADES HURT BY COMBINED MODEL (exited too early)")
print(f"{'ID':<6} {'Contract':<28} {'Actual':>9} {'Simulated':>10} {'Cost':>12}")
print(f"{'-'*68}")
for t in hurt[:10]:
    print(f"  #{t['id']:<4} {t['contract']:<28} "
          f"${t['actual']:>8,.0f} ${t['sim']:>9,.0f} ${t['diff']:>+11,.0f}")

print()

# ── Backstop analysis ─────────────────────────────────────────
backstop_only = results[2]
backstop_fires = {k: v for k, v in backstop_only['exits'].items()
                  if 'BACKSTOP' in k}
print(f"BACKSTOP ANALYSIS")
print(f"How often does each backstop tier fire?")
for tier, count in sorted(backstop_fires.items()):
    print(f"  {tier:<30} {count:>5} fires")

print()

# ── Winners killed by backstop ─────────────────────────────────
print(f"WINNERS KILLED BY BACKSTOP")
print(f"(Positions that would have been profitable but backstop fired first)")
killed = [t for t in backstop_only['hurt'] if t['actual'] > 0]
print(f"  Count: {len(killed)}")
if killed:
    avg_loss = statistics.mean(t['diff'] for t in killed)
    print(f"  Avg cost per killed winner: ${avg_loss:,.0f}")
    for t in sorted(killed, key=lambda x: x['diff'])[:5]:
        print(f"    #{t['id']} {t['contract']} "
              f"actual=${t['actual']:,.0f} sim={t['sim']:,.0f}")

print()
print(f"{'='*70}")
print(f"RECOMMENDATION")
print(f"{'='*70}")
best = max(results, key=lambda x: x['pnl'])
print(f"  Best model: {best['label']}")
print(f"  Total P&L:  ${best['pnl']:,.0f}")
print(f"  vs current: ${best['pnl'] - current_pnl:+,.0f}")
print()

conn.close()