# simulate_dte_aware_trailing.py
"""
Tests three trailing stop models against all closed trades:

  A. Flat model      — 1% hurdle + 20% trail (same for all DTE)
  B. DTE-aware model — both hurdle AND trail vary by DTE
  C. Hurdle-only     — hurdle varies by DTE, trail stays 20%

All three include the tiered backstop (60/70/80%) and ITM safety exit.
"""
import sqlite3
from datetime import datetime
import pytz

conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()
eastern = pytz.timezone('US/Eastern')

# ── Load data ──────────────────────────────────────────────────
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

print(f"Loading snapshots for {len(all_trades)} trades...")
trade_snaps = {}
for t in all_trades:
    cursor.execute("""
        SELECT pnl, current_price, snapshot_time, current_dte
        FROM position_snapshots
        WHERE trade_id = ? AND pnl IS NOT NULL AND current_price > 0
        ORDER BY id ASC
    """, (t['id'],))
    snaps = [dict(r) for r in cursor.fetchall()]
    if snaps:
        trade_snaps[t['id']] = snaps

print(f"Loaded. Running simulations...\n")


# ── Parameter functions ────────────────────────────────────────

def params_flat(dte):
    """Model A: flat 1% hurdle + 20% trail regardless of DTE."""
    return 0.01, 0.20

def params_dte_aware(dte):
    """Model B: both hurdle AND trail vary by DTE."""
    if dte is None or dte <= 2:
        return 0.01, 0.20
    elif dte <= 5:
        return 0.15, 0.25
    elif dte <= 14:
        return 0.25, 0.30
    else:
        return 0.40, 0.35

def params_hurdle_only(dte):
    """Model C: hurdle varies by DTE, trail stays 20%."""
    if dte is None or dte <= 2:
        return 0.01, 0.20
    elif dte <= 5:
        return 0.10, 0.20
    elif dte <= 14:
        return 0.30, 0.20
    else:
        return 0.50, 0.20

def get_backstop(dte):
    if dte is None or dte <= 2:
        return 0.60
    elif dte <= 14:
        return 0.70
    else:
        return 0.80


# ── Core simulation ────────────────────────────────────────────

def simulate(trades, param_fn, label):
    total_pnl = 0
    exits     = {}
    improved  = []
    hurt      = []

    for t in trades:
        actual_pnl  = t['actual_pnl']
        cost        = t['total_cost']
        exit_reason = t['exit_reason']

        # TARGET and MANUAL unchanged
        if exit_reason in ('TARGET', 'MANUAL'):
            total_pnl += actual_pnl
            exits[exit_reason] = exits.get(exit_reason, 0) + 1
            continue

        snaps = trade_snaps.get(t['id'], [])
        if not snaps:
            total_pnl += actual_pnl
            exits[exit_reason] = exits.get(exit_reason, 0) + 1
            continue

        hurdle_crossed = False
        running_max    = 0
        sim_exit_pnl   = None
        sim_exit_rsn   = None

        for s in snaps:
            pnl   = s['pnl'] or 0
            dte   = s.get('current_dte')
            snap_time_str = s.get('snapshot_time', '')

            hurdle_pct, trailing_pct = param_fn(dte)
            backstop_loss = -(cost * get_backstop(dte))
            hurdle_pnl    = cost * hurdle_pct

            # ITM safety exit (DTE=0, profitable, after 3:45pm ET)
            if dte == 0 and pnl > 0:
                try:
                    snap_dt = datetime.strptime(snap_time_str[:19], '%Y-%m-%d %H:%M:%S')
                    snap_et = pytz.utc.localize(snap_dt).astimezone(eastern)
                    if snap_et.hour > 15 or (snap_et.hour == 15 and snap_et.minute >= 45):
                        sim_exit_pnl = pnl
                        sim_exit_rsn = 'ITM_SAFETY'
                        break
                except:
                    pass

            # Backstop
            if pnl <= backstop_loss:
                sim_exit_pnl = pnl
                sim_exit_rsn = 'BACKSTOP'
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
                        sim_exit_pnl = pnl
                        sim_exit_rsn = 'TRAILING'
                        break

        # Use sim exit only if better than actual
        if sim_exit_pnl is not None and sim_exit_pnl > actual_pnl:
            total_pnl += sim_exit_pnl
            exits[sim_exit_rsn] = exits.get(sim_exit_rsn, 0) + 1
            diff = sim_exit_pnl - actual_pnl
            if diff > 10:
                improved.append({'id': t['id'], 'contract': t['signal_contract'],
                                  'actual': actual_pnl, 'sim': sim_exit_pnl, 'diff': diff,
                                  'dte': t['dte_at_entry']})
        else:
            total_pnl += actual_pnl
            exits[exit_reason] = exits.get(exit_reason, 0) + 1
            if sim_exit_pnl is not None and sim_exit_pnl < actual_pnl - 10:
                hurt.append({'id': t['id'], 'contract': t['signal_contract'],
                              'actual': actual_pnl, 'sim': sim_exit_pnl,
                              'diff': sim_exit_pnl - actual_pnl,
                              'dte': t['dte_at_entry']})

    wins = sum(1 for t in trades
               if simulate_pnl(t, trade_snaps.get(t['id'], []), param_fn) > 0)

    return {
        'label':    label,
        'pnl':      total_pnl,
        'wins':     wins,
        'total':    len(trades),
        'exits':    exits,
        'improved': improved,
        'hurt':     hurt,
    }


def simulate_pnl(trade, snaps, param_fn):
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
        pnl  = s['pnl'] or 0
        dte  = s.get('current_dte')
        hurdle_pct, trailing_pct = param_fn(dte)
        backstop_loss = -(cost * get_backstop(dte))
        hurdle_pnl    = cost * hurdle_pct
        if pnl <= backstop_loss:
            return pnl if pnl > actual_pnl else actual_pnl
        if not hurdle_crossed and pnl >= hurdle_pnl:
            hurdle_crossed = True
            running_max    = pnl
        if hurdle_crossed:
            if pnl > running_max:
                running_max = pnl
            if running_max > 0 and (running_max - pnl) / running_max > trailing_pct:
                return pnl if pnl > actual_pnl else actual_pnl
    return actual_pnl


# ── Run all three models ───────────────────────────────────────

current_pnl = sum(t['actual_pnl'] for t in all_trades)

results = [
    simulate(all_trades, params_flat,        "A. Flat (1% hurdle / 20% trail)"),
    simulate(all_trades, params_dte_aware,   "B. DTE-aware (hurdle + trail vary)"),
    simulate(all_trades, params_hurdle_only, "C. Hurdle-only (hurdle varies / 20% trail)"),
]

# ── Summary ────────────────────────────────────────────────────
print(f"Current system P&L: ${current_pnl:,.0f}")
print()
print(f"{'='*72}")
print(f"MODEL COMPARISON")
print(f"{'='*72}")
print(f"{'Model':<45} {'P&L':>10} {'vs Current':>12} {'Win%':>8}")
print(f"{'-'*72}")
best_pnl = max(r['pnl'] for r in results)
for r in results:
    wr     = r['wins'] / r['total'] * 100
    marker = " ◄ BEST" if r['pnl'] == best_pnl else ""
    print(f"  {r['label']:<43} ${r['pnl']:>8,.0f} "
          f"${r['pnl']-current_pnl:>+10,.0f} "
          f"{wr:>7.1f}%{marker}")

print()

# ── Exit breakdown comparison ──────────────────────────────────
print(f"EXIT BREAKDOWN")
print(f"{'Exit Reason':<20} {'Model A':>10} {'Model B':>10} {'Model C':>10}")
print(f"{'-'*52}")
all_reasons = set()
for r in results:
    all_reasons.update(r['exits'].keys())
for reason in sorted(all_reasons):
    counts = [r['exits'].get(reason, 0) for r in results]
    print(f"  {reason:<18} {counts[0]:>10} {counts[1]:>10} {counts[2]:>10}")

print()

# ── Trades hurt by each model ──────────────────────────────────
for r in results:
    if r['hurt']:
        print(f"TRADES HURT BY {r['label']}:")
        for t in sorted(r['hurt'], key=lambda x: x['diff'])[:5]:
            print(f"  #{t['id']} {t['contract']} "
                  f"actual=${t['actual']:,.0f} sim=${t['sim']:,.0f} "
                  f"diff=${t['diff']:+,.0f} dte={t['dte']}")
        print()

# ── Top improvements for best model ───────────────────────────
best = max(results, key=lambda x: x['pnl'])
print(f"TOP 10 IMPROVEMENTS — {best['label']}")
print(f"{'ID':<6} {'Contract':<28} {'DTE':>4} {'Actual':>9} {'Sim':>9} {'Gain':>9}")
print(f"{'-'*68}")
for t in sorted(best['improved'], key=lambda x: x['diff'], reverse=True)[:10]:
    print(f"  #{t['id']:<4} {t['contract']:<28} {t['dte']:>4} "
          f"${t['actual']:>8,.0f} ${t['sim']:>8,.0f} ${t['diff']:>+8,.0f}")

print()
print(f"{'='*72}")
print(f"RECOMMENDATION: {best['label']}")
print(f"Total P&L: ${best['pnl']:,.0f} (vs current ${current_pnl:,.0f})")
print(f"Improvement: ${best['pnl']-current_pnl:+,.0f}")
print(f"{'='*72}")

conn.close()