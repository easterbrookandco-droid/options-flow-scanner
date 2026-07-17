# analyze_recovery_matrix.py
"""
Empirically derives optimal trailing stop thresholds by DTE.

For every snapshot across all closed trades, calculates:
  - Current drawdown from peak P&L so far
  - DTE at that moment
  - Whether the trade eventually ended profitably

Outputs a recovery rate matrix:
  rows    = DTE buckets
  columns = drawdown from peak buckets
  values  = % of snapshots in this bucket that came from winning trades

The optimal trailing stop for each DTE bucket is the drawdown level
where recovery rate drops below 50%.
"""
import sqlite3
import statistics
from collections import defaultdict

conn = sqlite3.connect('signals.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# ── Step 1: Load all closed trades with their final outcome ────
print("Loading closed trades...")
cursor.execute("""
    SELECT id, pnl, total_cost, exit_reason, dte_at_entry
    FROM paper_trades
    WHERE status = 'CLOSED'
    AND pnl IS NOT NULL
    AND total_cost > 0
""")
trades = {r['id']: dict(r) for r in cursor.fetchall()}
print(f"  {len(trades)} closed trades loaded")

# ── Step 2: Load all snapshots for these trades ────────────────
print("Loading snapshots...")
cursor.execute("""
    SELECT trade_id, pnl, current_dte, snapshot_time
    FROM position_snapshots
    WHERE trade_id IN ({})
    AND pnl IS NOT NULL
    ORDER BY trade_id ASC, id ASC
""".format(','.join('?' * len(trades))), list(trades.keys()))

all_snaps = cursor.fetchall()
print(f"  {len(all_snaps):,} snapshots loaded")

# Group snapshots by trade
from collections import defaultdict
snaps_by_trade = defaultdict(list)
for s in all_snaps:
    snaps_by_trade[s['trade_id']].append(dict(s))

# ── Step 3: For each snapshot, compute drawdown from peak ──────
print("Computing drawdown from peak for each snapshot...")

# Each data point: (dte_bucket, drawdown_bucket, won)
data_points = []

# Also track: for each trade, what was the peak P&L and when
trade_peaks = {}

for trade_id, snaps in snaps_by_trade.items():
    trade = trades.get(trade_id)
    if not trade:
        continue

    final_pnl  = trade['pnl']
    total_cost = trade['total_cost']
    won        = 1 if final_pnl > 0 else 0

    running_max = 0

    for s in snaps:
        pnl = s['pnl'] or 0
        dte = s.get('current_dte')

        # Update running peak
        if pnl > running_max:
            running_max = pnl

        # Only analyze snapshots where position was profitable at peak
        # (no peak = no drawdown to measure)
        if running_max <= 0:
            continue

        # Calculate drawdown from peak
        drawdown_pct = (running_max - pnl) / running_max * 100

        # Only care about drawdowns > 0 (we're below peak)
        if drawdown_pct < 1:
            continue

        # DTE bucket
        if dte is None:
            dte_bucket = "Unknown"
        elif dte <= 0:
            dte_bucket = "0 DTE"
        elif dte <= 2:
            dte_bucket = "1-2 DTE"
        elif dte <= 5:
            dte_bucket = "3-5 DTE"
        elif dte <= 14:
            dte_bucket = "6-14 DTE"
        else:
            dte_bucket = "15+ DTE"

        # Drawdown bucket (5% increments)
        dd_floor = int(drawdown_pct // 5) * 5
        dd_ceil  = dd_floor + 5
        if dd_floor >= 95:
            dd_bucket = "95-100%"
        else:
            dd_bucket = f"{dd_floor}-{dd_ceil}%"

        data_points.append({
            'dte_bucket':      dte_bucket,
            'drawdown_bucket': dd_bucket,
            'drawdown_floor':  dd_floor,
            'won':             won,
            'pnl':             pnl,
            'running_max':     running_max,
            'drawdown_pct':    drawdown_pct,
            'trade_id':        trade_id,
        })

print(f"  {len(data_points):,} data points computed")
print()

# ── Step 4: Build the recovery rate matrix ────────────────────
dte_order = ["0 DTE", "1-2 DTE", "3-5 DTE", "6-14 DTE", "15+ DTE"]
dd_floors = list(range(0, 100, 5))

# Group data points
from collections import defaultdict
matrix = defaultdict(lambda: defaultdict(list))
for dp in data_points:
    matrix[dp['dte_bucket']][dp['drawdown_floor']].append(dp['won'])

# ── Print the full matrix ──────────────────────────────────────
print("=" * 80)
print("RECOVERY RATE MATRIX")
print("% of snapshots in each bucket from trades that eventually ended profitable")
print("Optimal exit = where recovery rate drops below 50%")
print("=" * 80)

# Header
header = f"{'Drawdown':<12}"
for dte in dte_order:
    header += f" {dte:>12}"
print(header)
print("-" * 80)

# Find optimal stop for each DTE (first bucket where recovery < 50%)
optimal_stops = {}

for dd_floor in dd_floors:
    dd_label = f"{dd_floor}-{dd_floor+5}%"
    row = f"{dd_label:<12}"
    for dte in dte_order:
        outcomes = matrix[dte][dd_floor]
        if not outcomes:
            row += f" {'  ---':>12}"
        else:
            rate = sum(outcomes) / len(outcomes) * 100
            count = len(outcomes)
            cell = f"{rate:.0f}% ({count})"
            row += f" {cell:>12}"

            # Track optimal stop
            if dte not in optimal_stops and rate < 50:
                optimal_stops[dte] = (dd_floor, rate, count)
    print(row)

print()

# ── Optimal stop summary ───────────────────────────────────────
print("=" * 80)
print("OPTIMAL TRAILING STOP BY DTE")
print("(First drawdown level where recovery rate drops below 50%)")
print("=" * 80)
print()
for dte in dte_order:
    if dte in optimal_stops:
        floor, rate, count = optimal_stops[dte]
        print(f"  {dte:<12} → exit at {floor}% drawdown from peak  "
              f"(recovery rate {rate:.0f}%, n={count})")
    else:
        print(f"  {dte:<12} → no clear exit point found "
              f"(recovery stays above 50% even at deep drawdowns)")

print()

# ── Additional analysis: peak characteristics ─────────────────
print("=" * 80)
print("PEAK P&L CHARACTERISTICS BY DTE")
print("(How large are peaks typically? Helps set meaningful hurdle)")
print("=" * 80)

# For each trade, find peak P&L and DTE when peak occurred
peak_data = defaultdict(list)

for trade_id, snaps in snaps_by_trade.items():
    trade = trades.get(trade_id)
    if not trade:
        continue

    final_pnl  = trade['pnl']
    total_cost = trade['total_cost']
    won        = final_pnl > 0

    max_pnl     = max((s['pnl'] or 0) for s in snaps)
    max_pnl_pct = (max_pnl / total_cost * 100) if total_cost else 0

    if max_pnl <= 0:
        continue

    # DTE at entry bucket
    dte = trade['dte_at_entry']
    if dte is None:
        dte_bucket = "Unknown"
    elif dte <= 2:
        dte_bucket = "1-2 DTE"
    elif dte <= 5:
        dte_bucket = "3-5 DTE"
    elif dte <= 14:
        dte_bucket = "6-14 DTE"
    else:
        dte_bucket = "15+ DTE"

    peak_data[dte_bucket].append({
        'max_pnl':     max_pnl,
        'max_pnl_pct': max_pnl_pct,
        'won':         won,
        'final_pnl':   final_pnl,
        'total_cost':  total_cost,
    })

print()
print(f"{'DTE':<12} {'Count':>6} {'Median Peak%':>14} {'Avg Peak%':>11} "
      f"{'Peaks>10%':>11} {'Peaks>30%':>11} {'Peaks>50%':>11}")
print("-" * 80)

for dte in dte_order:
    data = peak_data.get(dte, [])
    if not data:
        continue
    pcts        = [d['max_pnl_pct'] for d in data]
    median_pct  = statistics.median(pcts)
    avg_pct     = statistics.mean(pcts)
    over_10     = sum(1 for p in pcts if p > 10)
    over_30     = sum(1 for p in pcts if p > 30)
    over_50     = sum(1 for p in pcts if p > 50)
    print(f"  {dte:<12} {len(data):>6} {median_pct:>13.1f}% {avg_pct:>10.1f}% "
          f"{over_10:>11} {over_30:>11} {over_50:>11}")

print()

# ── Hurdle recommendation ──────────────────────────────────────
print("=" * 80)
print("HURDLE RECOMMENDATION")
print("(Hurdle should be set above median peak to filter noise)")
print("=" * 80)
print()
for dte in dte_order:
    data = peak_data.get(dte, [])
    if not data:
        continue
    pcts       = [d['max_pnl_pct'] for d in data]
    median_pct = statistics.median(pcts)
    p25        = sorted(pcts)[int(len(pcts) * 0.25)]
    p75        = sorted(pcts)[int(len(pcts) * 0.75)]
    # Hurdle = 25th percentile of peaks (catch 75% of meaningful moves)
    suggested  = p25
    print(f"  {dte:<12} median peak={median_pct:.0f}%  "
          f"25th pct={p25:.0f}%  75th pct={p75:.0f}%  "
          f"→ suggested hurdle: {suggested:.0f}%")

conn.close()
print()
print("Done.")