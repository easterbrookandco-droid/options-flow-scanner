# simulate_lifecycle.py
"""
Simulates every closed contract's full lifecycle through DTE tranches.

Each contract moves through tranches as it ages:
  Entry at 9 DTE → 6-14 DTE params → 3-5 DTE params → 1-2 DTE params → 0 DTE params

Compares:
  A. Current live parameters (in position_monitor.py right now)
  B. Optimizer recommended parameters
  C. Proposed hybrid parameters

Shows exactly which contracts each model handles differently.
"""
import sqlite3
from collections import defaultdict

conn = sqlite3.connect('signals_vm.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# ── Load all closed trades ─────────────────────────────────────
cursor.execute("""
    SELECT pt.id, pt.signal_contract, pt.entry_price,
           pt.pnl as actual_pnl, pt.total_cost,
           pt.exit_reason, pt.dte_at_entry,
           pt.entry_date
    FROM paper_trades pt
    WHERE pt.status = 'CLOSED'
    AND pt.pnl IS NOT NULL
    AND pt.total_cost > 0
    ORDER BY pt.entry_date ASC, pt.id ASC
""")
all_trades = [dict(r) for r in cursor.fetchall()]

# Load snapshots with current_dte
trade_snaps = {}
for t in all_trades:
    cursor.execute("""
        SELECT pnl, current_dte, snapshot_time
        FROM position_snapshots
        WHERE trade_id = ?
        AND pnl IS NOT NULL
        ORDER BY id ASC
    """, (t['id'],))
    snaps = [dict(r) for r in cursor.fetchall()]
    if snaps:
        trade_snaps[t['id']] = snaps

print(f"Loaded {len(all_trades)} trades with snapshots for {len(trade_snaps)}")
print()

# ── Parameter sets ─────────────────────────────────────────────
MODELS = {
    "A. Current live": {
        "0 DTE":    (0.01, 0.20),
        "1-2 DTE":  (0.01, 0.20),
        "3-5 DTE":  (0.10, 0.20),
        "6-14 DTE": (0.30, 0.20),
        "15+ DTE":  (0.50, 0.20),
    },
    "B. Optimizer": {
        "0 DTE":    (0.01, 0.10),
        "1-2 DTE":  (0.01, 0.10),
        "3-5 DTE":  (0.01, 0.20),
        "6-14 DTE": (0.01, 0.10),
        "15+ DTE":  (0.01, 0.10),
    },
    "C. Hybrid proposed": {
        "0 DTE":    (0.01, 0.10),
        "1-2 DTE":  (0.01, 0.15),
        "3-5 DTE":  (0.01, 0.20),
        "6-14 DTE": (0.01, 0.25),
        "15+ DTE":  (0.01, 0.25),
    },
}

def get_tranche(dte):
    if dte is None:
        return "3-5 DTE"
    if dte <= 0:
        return "0 DTE"
    elif dte <= 2:
        return "1-2 DTE"
    elif dte <= 5:
        return "3-5 DTE"
    elif dte <= 14:
        return "6-14 DTE"
    else:
        return "15+ DTE"

def get_backstop(dte):
    if dte is None or dte <= 2:
        return 0.60
    elif dte <= 14:
        return 0.70
    else:
        return 0.80

def simulate_lifecycle(trade, snaps, params):
    """
    Simulate a single contract through its full lifecycle.
    Parameters shift dynamically as current_dte changes each snapshot.
    Returns (simulated_pnl, exit_reason, exit_dte, tranche_at_exit)
    """
    actual_pnl  = trade['actual_pnl']
    cost        = trade['total_cost']
    exit_reason = trade['exit_reason']

    # TARGET and MANUAL — keep as-is
    if exit_reason in ('TARGET', 'MANUAL'):
        return actual_pnl, exit_reason, None, None

    if not snaps:
        return actual_pnl, exit_reason, None, None

    hurdle_crossed = False
    running_max    = 0

    for s in snaps:
        pnl = s['pnl'] or 0
        dte = s.get('current_dte')

        tranche = get_tranche(dte)
        hurdle_pct, trailing_pct = params.get(tranche, (0.01, 0.20))

        hurdle_pnl    = cost * hurdle_pct
        backstop_loss = -(cost * get_backstop(dte))

        # Backstop
        if pnl <= backstop_loss:
            if pnl > actual_pnl:
                return pnl, 'BACKSTOP', dte, tranche
            return actual_pnl, exit_reason, None, None

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
                    if pnl > actual_pnl:
                        return pnl, 'TRAILING', dte, tranche
                    return actual_pnl, exit_reason, None, None

    return actual_pnl, exit_reason, None, None


# ── Run all three models ───────────────────────────────────────
actual_total = sum(t['actual_pnl'] for t in all_trades)

results = {}
for model_name, params in MODELS.items():
    model_pnl   = 0
    exit_counts = defaultdict(int)
    improvements = []
    hurts        = []
    tranche_pnl  = defaultdict(float)

    for t in all_trades:
        snaps    = trade_snaps.get(t['id'], [])
        sim_pnl, sim_reason, exit_dte, exit_tranche = simulate_lifecycle(
            t, snaps, params
        )
        model_pnl += sim_pnl
        exit_counts[sim_reason] += 1

        # Track by tranche at entry
        dte_bucket = get_tranche(t.get('dte_at_entry'))
        tranche_pnl[dte_bucket] += sim_pnl

        diff = sim_pnl - t['actual_pnl']
        if diff > 10:
            improvements.append({
                'id':       t['id'],
                'contract': t['signal_contract'],
                'actual':   t['actual_pnl'],
                'sim':      sim_pnl,
                'diff':     diff,
                'dte':      t['dte_at_entry'],
                'reason':   sim_reason,
                'exit_dte': exit_dte,
                'exit_tranche': exit_tranche,
            })
        elif diff < -10:
            hurts.append({
                'id':       t['id'],
                'contract': t['signal_contract'],
                'actual':   t['actual_pnl'],
                'sim':      sim_pnl,
                'diff':     diff,
                'dte':      t['dte_at_entry'],
                'reason':   sim_reason,
            })

    wins     = sum(1 for t in all_trades
                   if simulate_lifecycle(
                       t, trade_snaps.get(t['id'], []), params
                   )[0] > 0)
    win_rate = wins / len(all_trades) * 100

    results[model_name] = {
        'pnl':          model_pnl,
        'win_rate':     win_rate,
        'exits':        dict(exit_counts),
        'improvements': improvements,
        'hurts':        hurts,
        'tranche_pnl':  dict(tranche_pnl),
    }

# ── Summary comparison ─────────────────────────────────────────
print("=" * 70)
print("LIFECYCLE SIMULATION — MODEL COMPARISON")
print("=" * 70)
print(f"{'Model':<25} {'Total P&L':>12} {'vs Actual':>12} {'Win Rate':>10}")
print("-" * 60)
print(f"  {'Actual system':<23} ${actual_total:>10,.0f} {'---':>12} "
      f"{'---':>10}")
for name, r in results.items():
    marker = " ◄" if r['pnl'] == max(x['pnl'] for x in results.values()) else ""
    print(f"  {name:<23} ${r['pnl']:>10,.0f} "
          f"${r['pnl']-actual_total:>+10,.0f} "
          f"{r['win_rate']:>9.1f}%{marker}")

# ── P&L by DTE tranche ─────────────────────────────────────────
print()
print("=" * 70)
print("P&L BY DTE TRANCHE AT ENTRY")
print("=" * 70)
tranche_order = ["0 DTE", "1-2 DTE", "3-5 DTE", "6-14 DTE", "15+ DTE"]
print(f"{'Tranche':<12}", end="")
for name in MODELS:
    print(f" {name[:15]:>16}", end="")
print()
print("-" * 70)
for tranche in tranche_order:
    print(f"  {tranche:<12}", end="")
    for name, r in results.items():
        pnl = r['tranche_pnl'].get(tranche, 0)
        print(f" ${pnl:>14,.0f}", end="")
    print()

# ── Exit breakdown ─────────────────────────────────────────────
print()
print("=" * 70)
print("EXIT BREAKDOWN BY MODEL")
print("=" * 70)
all_reasons = set()
for r in results.values():
    all_reasons.update(r['exits'].keys())
print(f"{'Exit Reason':<15}", end="")
for name in MODELS:
    print(f" {name[:15]:>16}", end="")
print()
print("-" * 70)
for reason in sorted(all_reasons):
    print(f"  {reason:<15}", end="")
    for r in results.values():
        print(f" {r['exits'].get(reason,0):>16}", end="")
    print()

# ── Contracts handled differently ─────────────────────────────
print()
print("=" * 70)
print("CONTRACTS WHERE MODELS DIVERGE (top 10 by impact)")
print("=" * 70)

# Find trades where A and C give different results
divergent = []
for t in all_trades:
    snaps = trade_snaps.get(t['id'], [])
    pnl_a = simulate_lifecycle(t, snaps, MODELS["A. Current live"])[0]
    pnl_b = simulate_lifecycle(t, snaps, MODELS["B. Optimizer"])[0]
    pnl_c = simulate_lifecycle(t, snaps, MODELS["C. Hybrid proposed"])[0]

    if abs(pnl_a - pnl_c) > 50:
        divergent.append({
            'id':       t['id'],
            'contract': t['signal_contract'],
            'dte':      t['dte_at_entry'],
            'actual':   t['actual_pnl'],
            'model_a':  pnl_a,
            'model_b':  pnl_b,
            'model_c':  pnl_c,
            'a_vs_c':   pnl_c - pnl_a,
        })

divergent.sort(key=lambda x: abs(x['a_vs_c']), reverse=True)

print(f"{'ID':<6} {'Contract':<28} {'DTE':>4} "
      f"{'Actual':>9} {'Model A':>9} {'Model B':>9} "
      f"{'Model C':>9} {'C vs A':>9}")
print("-" * 90)
for t in divergent[:10]:
    print(f"  #{t['id']:<4} {t['contract']:<28} {t['dte']:>4} "
          f"${t['actual']:>8,.0f} ${t['model_a']:>8,.0f} "
          f"${t['model_b']:>8,.0f} ${t['model_c']:>8,.0f} "
          f"${t['a_vs_c']:>+8,.0f}")

# ── Trades hurt by hybrid vs current ──────────────────────────
print()
hurt_by_hybrid = [t for t in divergent if t['a_vs_c'] < -50]
if hurt_by_hybrid:
    print(f"TRADES WHERE HYBRID (C) UNDERPERFORMS CURRENT (A):")
    print(f"{'ID':<6} {'Contract':<28} {'DTE':>4} "
          f"{'Actual':>9} {'Model A':>9} {'Model C':>9} {'Diff':>9}")
    print("-" * 75)
    for t in sorted(hurt_by_hybrid, key=lambda x: x['a_vs_c'])[:10]:
        print(f"  #{t['id']:<4} {t['contract']:<28} {t['dte']:>4} "
              f"${t['actual']:>8,.0f} ${t['model_a']:>8,.0f} "
              f"${t['model_c']:>8,.0f} ${t['a_vs_c']:>+8,.0f}")
else:
    print("  ✅ Hybrid (C) never underperforms Current (A)")

conn.close()
print()
print("Done.")