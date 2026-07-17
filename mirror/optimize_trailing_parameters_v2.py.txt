# optimize_trailing_parameters_v2.py
"""
Grid search optimization of hurdle% and trailing stop% by DTE tranche.

KEY IMPROVEMENT over v1:
Uses current_dte from each SNAPSHOT (not dte_at_entry) to determine
which parameters apply at each moment in the position's life.

This means as a contract moves from 9 DTE → 5 DTE → 0 DTE, the
trailing stop parameters tighten dynamically with the contract.
"""
import sqlite3
import itertools
from collections import defaultdict

conn = sqlite3.connect('signals_vm.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# ── Load data ──────────────────────────────────────────────────
print("Loading trades and snapshots...")

cursor.execute("""
    SELECT pt.id, pt.signal_contract, pt.entry_price,
           pt.pnl as actual_pnl, pt.total_cost,
           pt.exit_reason, pt.dte_at_entry,
           pt.entry_date
    FROM paper_trades pt
    WHERE pt.status = 'CLOSED'
    AND pt.pnl IS NOT NULL
    AND pt.total_cost > 0
    ORDER BY pt.entry_date ASC
""")
all_trades = [dict(r) for r in cursor.fetchall()]

trade_snaps = {}
for t in all_trades:
    cursor.execute("""
        SELECT pnl, current_dte
        FROM position_snapshots
        WHERE trade_id = ?
        AND pnl IS NOT NULL
        ORDER BY id ASC
    """, (t['id'],))
    snaps = [dict(r) for r in cursor.fetchall()]
    if snaps:
        trade_snaps[t['id']] = snaps

print(f"  {len(all_trades)} trades loaded")

# ── Train/validation split ─────────────────────────────────────
VALIDATION_START = "2026-05-21"
train_trades = [t for t in all_trades if t['entry_date'] < VALIDATION_START]
val_trades   = [t for t in all_trades if t['entry_date'] >= VALIDATION_START]
print(f"  Training: {len(train_trades)} trades | Validation: {len(val_trades)} trades")
print()

# ── DTE tranche lookup ─────────────────────────────────────────
TRANCHES = [
    ("0 DTE",    0,  0),
    ("1-2 DTE",  1,  2),
    ("3-5 DTE",  3,  5),
    ("6-14 DTE", 6, 14),
    ("15+ DTE", 15, 999),
]

def get_tranche(dte):
    if dte is None:
        return "3-5 DTE"  # default fallback
    for name, low, high in TRANCHES:
        if low <= dte <= high:
            return name
    return "6-14 DTE"

def get_backstop(dte):
    if dte is None or dte <= 2:
        return 0.60
    elif dte <= 14:
        return 0.70
    else:
        return 0.80

# ── Core simulation ────────────────────────────────────────────
def simulate_trade(trade, snaps, tranche_params):
    """
    Simulate using current_dte from each snapshot to determine
    which parameters apply at that moment.
    """
    actual_pnl  = trade['actual_pnl']
    cost        = trade['total_cost']
    exit_reason = trade['exit_reason']

    if exit_reason in ('TARGET', 'MANUAL'):
        return actual_pnl
    if not snaps:
        return actual_pnl

    hurdle_crossed = False
    running_max    = 0
    sim_exit_pnl   = None

    for s in snaps:
        pnl = s['pnl'] or 0
        dte = s.get('current_dte')

        # Look up parameters for THIS snapshot's DTE
        tranche = get_tranche(dte)
        hurdle_pct, trailing_pct = tranche_params.get(
            tranche, (0.01, 0.20)
        )

        hurdle_pnl    = cost * hurdle_pct
        backstop_loss = -(cost * get_backstop(dte))

        # Backstop
        if pnl <= backstop_loss:
            sim_exit_pnl = pnl
            break

        # Trailing stop — hurdle uses current DTE params
        # but once crossed, stays crossed regardless of DTE change
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
                    break

    if sim_exit_pnl is not None and sim_exit_pnl > actual_pnl:
        return sim_exit_pnl
    return actual_pnl


def total_pnl(trades, tranche_params):
    return sum(
        simulate_trade(t, trade_snaps.get(t['id'], []), tranche_params)
        for t in trades
    )


# ── Grid search ───────────────────────────────────────────────
HURDLES = [0.01, 0.05, 0.10, 0.13, 0.20, 0.25, 0.30, 0.34, 0.40, 0.50]
TRAILS  = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.75, 0.90]

print("Running grid search (snapshot-level DTE awareness)...")
print()

# Start from current live parameters
best_params = {
    "0 DTE":    (0.01, 0.20),
    "1-2 DTE":  (0.13, 0.20),
    "3-5 DTE":  (0.10, 0.20),
    "6-14 DTE": (0.30, 0.20),
    "15+ DTE":  (0.50, 0.20),
}

tranche_results = {}
current_total = sum(t['actual_pnl'] for t in train_trades)

print(f"{'Tranche':<12} {'Best Hurdle':>12} {'Best Trail':>11} "
      f"{'Train P&L':>11} {'vs Actual':>11} {'n':>5}")
print("-" * 70)

for tranche_name, low, high in TRANCHES:
    best_pnl   = None
    best_combo = None
    all_results = {}

    for hurdle, trail in itertools.product(HURDLES, TRAILS):
        test_params = dict(best_params)
        test_params[tranche_name] = (hurdle, trail)
        pnl = total_pnl(train_trades, test_params)
        all_results[(hurdle, trail)] = pnl

        if best_pnl is None or pnl > best_pnl:
            best_pnl   = pnl
            best_combo = (hurdle, trail)

    baseline_pnl = total_pnl(train_trades, best_params)
    h, t = best_combo
    best_params[tranche_name] = best_combo

    n = sum(1 for tr in train_trades
            if tr['dte_at_entry'] is not None
            and low <= tr['dte_at_entry'] <= high)

    print(f"  {tranche_name:<12} {h*100:>11.0f}% {t*100:>10.0f}% "
          f"${best_pnl:>10,.0f} ${best_pnl-current_total:>+10,.0f} {n:>5}")

    tranche_results[tranche_name] = {
        'best_combo':  best_combo,
        'best_pnl':    best_pnl,
        'baseline_pnl': baseline_pnl,
        'all_results': all_results,
        'n':           n,
    }

# ── Full portfolio ─────────────────────────────────────────────
print()
print("=" * 70)
print("FULL PORTFOLIO — TRAINING SET")
print("=" * 70)
optimized_train = total_pnl(train_trades, best_params)
print(f"  Actual P&L:     ${current_total:,.0f}")
print(f"  Optimized P&L:  ${optimized_train:,.0f}")
print(f"  Improvement:    ${optimized_train-current_total:+,.0f}")

# ── Out-of-sample validation ───────────────────────────────────
print()
print("=" * 70)
print("OUT-OF-SAMPLE VALIDATION — WEEK 3")
print("=" * 70)
if val_trades:
    val_actual    = sum(t['actual_pnl'] for t in val_trades)
    val_optimized = total_pnl(val_trades, best_params)
    print(f"  Validation trades: {len(val_trades)}")
    print(f"  Actual P&L:        ${val_actual:,.0f}")
    print(f"  Optimized P&L:     ${val_optimized:,.0f}")
    print(f"  Improvement:       ${val_optimized-val_actual:+,.0f}")
    print()
    if val_optimized > val_actual:
        pct = (val_optimized - val_actual) / abs(val_actual) * 100
        print(f"  ✅ Parameters generalize — {pct:.0f}% improvement on unseen data")
    else:
        print(f"  ⚠️  Parameters may be overfit — underperform on unseen data")

# ── Optimized parameters ───────────────────────────────────────
print()
print("=" * 70)
print("FINAL OPTIMIZED PARAMETERS (snapshot-level DTE aware)")
print("=" * 70)
print()
for tranche_name, _, _ in TRANCHES:
    if tranche_name in best_params:
        h, t = best_params[tranche_name]
        n    = tranche_results.get(tranche_name, {}).get('n', 0)
        print(f"  {tranche_name:<12}  hurdle={h*100:.0f}%  "
              f"trail={t*100:.0f}%  (n={n} training trades)")

# ── Sensitivity ───────────────────────────────────────────────
print()
print("=" * 70)
print("SENSITIVITY — TOP 5 COMBOS PER TRANCHE")
print("(Tight cluster = robust. Wide spread = fragile)")
print("=" * 70)

for tranche_name, _, _ in TRANCHES:
    if tranche_name not in tranche_results:
        continue
    results  = tranche_results[tranche_name]['all_results']
    best_pnl = tranche_results[tranche_name]['best_pnl']
    sorted_r = sorted(results.items(), key=lambda x: x[1], reverse=True)

    top5_pnls = [pnl for _, pnl in sorted_r[:5]]
    spread    = top5_pnls[0] - top5_pnls[-1]

    print(f"\n  {tranche_name}  (top-5 spread: ${spread:,.0f})")
    print(f"  {'Hurdle':>8} {'Trail':>8} {'P&L':>10} {'vs best':>10}")
    print(f"  {'-'*40}")
    for (h, t), pnl in sorted_r[:5]:
        diff = pnl - best_pnl
        print(f"  {h*100:>7.0f}% {t*100:>7.0f}% "
              f"${pnl:>9,.0f} ${diff:>+9,.0f}")

conn.close()
print()
print("Done.")