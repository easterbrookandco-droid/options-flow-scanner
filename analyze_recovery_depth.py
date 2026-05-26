# analyze_recovery_depth.py
"""
For all contracts that expired PROFITABLY but were significantly negative
at some point during their life — what was the maximum drawdown before recovery?

This tells us: how deep do winning positions typically go before they recover?
Setting the backstop deeper than this preserves winners while cutting true losers.
"""
import sqlite3
import statistics

conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# Get all contracts that closed profitably (any exit reason with pnl > 0)
cursor.execute("""
    SELECT pt.id, pt.signal_contract, pt.pnl as final_pnl,
           pt.total_cost, pt.exit_reason, pt.dte_at_entry
    FROM paper_trades pt
    WHERE pt.status = 'CLOSED'
    AND pt.pnl > 0
    AND pt.total_cost > 0
""")
winners = [dict(r) for r in cursor.fetchall()]

print(f"Total winning positions: {len(winners)}")
print()

# For each winner, find the maximum negative drawdown during its life
MATERIAL_THRESHOLD = 0.10  # must have been down at least 10% to count

deep_drawdowns = []
never_negative  = 0
shallow_only    = 0

for w in winners:
    cursor.execute("""
        SELECT pnl, pnl_pct, snapshot_time, current_dte
        FROM position_snapshots
        WHERE trade_id = ?
        AND pnl IS NOT NULL
        ORDER BY id ASC
    """, (w['id'],))
    snaps = [dict(r) for r in cursor.fetchall()]

    if not snaps:
        continue

    # Find minimum pnl_pct during position life
    pnl_pcts = [s['pnl_pct'] for s in snaps if s['pnl_pct'] is not None]
    if not pnl_pcts:
        continue

    min_pct = min(pnl_pcts)

    if min_pct >= 0:
        never_negative += 1
        continue

    if min_pct > -(MATERIAL_THRESHOLD * 100):
        shallow_only += 1
        continue

    # This winner was materially negative at some point
    deep_drawdowns.append({
        'id':          w['id'],
        'contract':    w['signal_contract'],
        'final_pnl':   w['final_pnl'],
        'exit_reason': w['exit_reason'],
        'dte_at_entry': w['dte_at_entry'],
        'max_drawdown_pct': min_pct,
        'total_cost':  w['total_cost']
    })

print(f"Winners that were never negative:          {never_negative}")
print(f"Winners only shallowly negative (<10%):    {shallow_only}")
print(f"Winners materially negative (>10% down):   {len(deep_drawdowns)}")
print()

if not deep_drawdowns:
    print("No materially negative winners found.")
    conn.close()
    exit()

# Tranche analysis
tranches = [
    (-10,  -20,  "10-20% down"),
    (-20,  -30,  "20-30% down"),
    (-30,  -40,  "30-40% down"),
    (-40,  -50,  "40-50% down"),
    (-50,  -60,  "50-60% down"),
    (-60,  -70,  "60-70% down"),
    (-70,  -80,  "70-80% down"),
    (-80,  -90,  "80-90% down"),
    (-90, -100,  "90-100% down"),
]

print(f"DRAWDOWN TRANCHE ANALYSIS")
print(f"(How deep did winning positions go before recovering?)")
print()
print(f"{'Tranche':<18} {'Count':>6} {'% of total':>11} "
      f"{'Avg final P&L':>14} {'Avg max drawdown':>17}")
print("-" * 70)

total = len(deep_drawdowns)
cumulative = 0

for upper, lower, label in tranches:
    bucket = [d for d in deep_drawdowns
              if d['max_drawdown_pct'] <= upper
              and d['max_drawdown_pct'] > lower]

    if not bucket:
        continue

    cumulative += len(bucket)
    avg_pnl      = statistics.mean(d['final_pnl'] for d in bucket)
    avg_drawdown = statistics.mean(d['max_drawdown_pct'] for d in bucket)
    pct_of_total = len(bucket) / total * 100

    print(f"  {label:<16} {len(bucket):>6} {pct_of_total:>10.1f}% "
          f"${avg_pnl:>13,.0f} {avg_drawdown:>16.1f}%")

print("-" * 70)
print(f"  {'TOTAL':<16} {total:>6}")
print()

# Cumulative — what % of winners would be saved at each backstop level
print(f"CUMULATIVE SURVIVAL RATES")
print(f"(If backstop at X%, what % of eventual winners survive?)")
print()
print(f"{'Backstop at':<15} {'Winners saved':>14} {'Winners lost':>13} "
      f"{'Survival rate':>14}")
print("-" * 60)

backstops = [30, 40, 50, 60, 70, 80, 90]
for bs in backstops:
    saved = sum(1 for d in deep_drawdowns if d['max_drawdown_pct'] > -bs)
    lost  = sum(1 for d in deep_drawdowns if d['max_drawdown_pct'] <= -bs)
    # Also count never_negative and shallow as saved
    total_winners = len(winners)
    total_saved   = never_negative + shallow_only + saved
    survival_rate = total_saved / total_winners * 100
    print(f"  -{bs}%{'':<10} {total_saved:>14} {lost:>13} {survival_rate:>13.1f}%")

print()

# DTE breakdown — do shorter DTE winners go deeper?
print(f"DRAWDOWN BY DTE AT ENTRY")
print(f"(Do shorter DTE positions need more room to recover?)")
print()
dte_buckets = [
    (0, 2,  "1-2 DTE"),
    (3, 5,  "3-5 DTE"),
    (6, 14, "6-14 DTE"),
    (15, 999, "15+ DTE"),
]
for low, high, label in dte_buckets:
    bucket = [d for d in deep_drawdowns
              if d['dte_at_entry'] is not None
              and low <= d['dte_at_entry'] <= high]
    if not bucket:
        continue
    avg_dd  = statistics.mean(d['max_drawdown_pct'] for d in bucket)
    worst   = min(d['max_drawdown_pct'] for d in bucket)
    avg_pnl = statistics.mean(d['final_pnl'] for d in bucket)
    print(f"  {label:<12} n={len(bucket):>3}  "
          f"avg max drawdown: {avg_dd:.1f}%  "
          f"worst: {worst:.1f}%  "
          f"avg final P&L: ${avg_pnl:,.0f}")

print()

# Exit reason breakdown
print(f"DRAWDOWN BY EXIT REASON")
print(f"(Do TARGET hits need more room than MANUAL exits?)")
print()
for reason in ['TARGET', 'MANUAL', 'EXPIRED']:
    bucket = [d for d in deep_drawdowns if d['exit_reason'] == reason]
    if not bucket:
        continue
    avg_dd  = statistics.mean(d['max_drawdown_pct'] for d in bucket)
    worst   = min(d['max_drawdown_pct'] for d in bucket)
    print(f"  {reason:<10} n={len(bucket):>3}  "
          f"avg max drawdown: {avg_dd:.1f}%  worst: {worst:.1f}%")

print()

# The key recommendation
print(f"{'='*60}")
print(f"RECOMMENDATION")
print(f"{'='*60}")
all_drawdowns = [d['max_drawdown_pct'] for d in deep_drawdowns]
median_dd = statistics.median(all_drawdowns)
p75_dd    = sorted(all_drawdowns)[int(len(all_drawdowns) * 0.75)]
p90_dd    = sorted(all_drawdowns)[int(len(all_drawdowns) * 0.90)]
print(f"  Median max drawdown of eventual winners: {median_dd:.1f}%")
print(f"  75th percentile:                         {p75_dd:.1f}%")
print(f"  90th percentile:                         {p90_dd:.1f}%")
print(f"  Suggested backstop (75th pct + buffer):  {p75_dd - 10:.1f}%")
print()

conn.close()