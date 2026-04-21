import sqlite3
import pytz
from datetime import datetime
from journal import DB_PATH

MARKET_TIMEZONE = "US/Eastern"

# =============================================================================
# SAMPLE SIZE THRESHOLDS
# =============================================================================

THRESHOLD_EARLY     = 5    # Show data but flag as very early
THRESHOLD_MEANINGFUL = 20  # Caveat disappears
THRESHOLD_SIGNIFICANT = 50 # Statistically significant, no caveats


def sample_caveat(n):
    """
    Return a caveat string based on sample size.
    Returns empty string once data is meaningful.
    """
    if n < THRESHOLD_EARLY:
        return (f"  ⚠️  Very early data ({n} trades) — "
                f"patterns not yet reliable")
    elif n < THRESHOLD_MEANINGFUL:
        return (f"  ⚠️  Early data ({n} trades, need {THRESHOLD_MEANINGFUL} "
                f"for meaningful read)")
    elif n < THRESHOLD_SIGNIFICANT:
        return (f"  ℹ️  Growing dataset ({n} trades, need "
                f"{THRESHOLD_SIGNIFICANT} for statistical significance)")
    return ""


# =============================================================================
# DATABASE HELPERS
# =============================================================================

def get_closed_trades_raw():
    """
    Fetch all closed trades as raw dicts for analysis.
    Returns empty list if none exist.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM paper_trades
        WHERE status = 'CLOSED'
        ORDER BY exit_date ASC
    """)
    trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return trades


def get_all_trades_raw():
    """Fetch all trades regardless of status."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM paper_trades
        ORDER BY entry_date ASC
    """)
    trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return trades


# =============================================================================
# FORMATTING HELPERS
# =============================================================================

def fmt_pnl(pnl):
    if pnl is None:
        return "—"
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.2f}"


def fmt_pct(pct):
    if pct is None:
        return "—"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def fmt_rate(wins, total):
    """Format win rate with fraction."""
    if total == 0:
        return "—"
    rate = (wins / total) * 100
    return f"{rate:.1f}%  ({wins}/{total})"


def section(title):
    """Print a section header."""
    print(f"\n  {'─'*60}")
    print(f"  {title}")
    print(f"  {'─'*60}")


def subsection(title):
    """Print a subsection header."""
    print(f"\n  {title}")
    print(f"  {'·'*50}")


# =============================================================================
# SIGNAL QUALITY ANALYSIS
# =============================================================================

def win_rate_by_verdict(trades):
    """
    Win rate broken down by verdict at entry.
    Core question: do QUALIFIED signals actually outperform REVIEW?
    """
    section("📊 WIN RATE BY VERDICT")

    verdicts = ["QUALIFIED", "REVIEW", "CAUTION", "SKIP"]
    any_data = False

    for verdict in verdicts:
        bucket = [t for t in trades if t.get("verdict_at_entry") == verdict]
        if not bucket:
            continue

        any_data   = True
        wins       = sum(1 for t in bucket if (t.get("pnl") or 0) > 0)
        losses     = sum(1 for t in bucket if (t.get("pnl") or 0) <= 0)
        total_pnl  = sum(t.get("pnl") or 0 for t in bucket)
        avg_pct    = (sum(t.get("pnl_pct") or 0 for t in bucket)
                      / len(bucket))

        caveat = sample_caveat(len(bucket))

        print(f"\n  {verdict:<12} "
              f"Win rate: {fmt_rate(wins, len(bucket)):<20} "
              f"Total P&L: {fmt_pnl(total_pnl):<14} "
              f"Avg return: {fmt_pct(avg_pct)}")
        if caveat:
            print(f"  {caveat}")

    if not any_data:
        print("  No closed trades with verdict data yet.")


def win_rate_by_score_band(trades):
    """
    Win rate by composite score band.
    Does score 8+ actually outperform score 6-7?
    """
    section("📊 WIN RATE BY SCORE BAND")

    bands = [
        ("9",     lambda s: s == 9),
        ("8–8.9", lambda s: 8 <= s < 9),
        ("7–7.9", lambda s: 7 <= s < 8),
        ("6–6.9", lambda s: 6 <= s < 7),
        ("<6",    lambda s: s < 6),
    ]

    any_data = False

    for label, fn in bands:
        bucket = [t for t in trades
                  if t.get("score_at_entry") is not None
                  and fn(t["score_at_entry"])]
        if not bucket:
            continue

        any_data  = True
        wins      = sum(1 for t in bucket if (t.get("pnl") or 0) > 0)
        total_pnl = sum(t.get("pnl") or 0 for t in bucket)
        avg_pct   = (sum(t.get("pnl_pct") or 0 for t in bucket)
                     / len(bucket))

        caveat = sample_caveat(len(bucket))

        print(f"\n  Score {label:<8} "
              f"Win rate: {fmt_rate(wins, len(bucket)):<20} "
              f"Total P&L: {fmt_pnl(total_pnl):<14} "
              f"Avg return: {fmt_pct(avg_pct)}")
        if caveat:
            print(f"  {caveat}")

    if not any_data:
        print("  No closed trades with score data yet.")


def win_rate_by_dte_band(trades):
    """
    Win rate by DTE at entry.
    Core question: which DTE window performs best?
    """
    section("📊 WIN RATE BY DTE AT ENTRY")

    bands = [
        ("0DTE",   lambda d: d == 0),
        ("1–2d",   lambda d: 1 <= d <= 2),
        ("3–7d",   lambda d: 3 <= d <= 7),
        ("8–14d",  lambda d: 8 <= d <= 14),
        ("15–30d", lambda d: 15 <= d <= 30),
        ("30d+",   lambda d: d > 30),
    ]

    any_data = False

    for label, fn in bands:
        bucket = [t for t in trades
                  if t.get("dte_at_entry") is not None
                  and fn(t["dte_at_entry"])]
        if not bucket:
            continue

        any_data  = True
        wins      = sum(1 for t in bucket if (t.get("pnl") or 0) > 0)
        total_pnl = sum(t.get("pnl") or 0 for t in bucket)
        avg_pct   = (sum(t.get("pnl_pct") or 0 for t in bucket)
                     / len(bucket))

        caveat = sample_caveat(len(bucket))

        print(f"\n  DTE {label:<8} "
              f"Win rate: {fmt_rate(wins, len(bucket)):<20} "
              f"Total P&L: {fmt_pnl(total_pnl):<14} "
              f"Avg return: {fmt_pct(avg_pct)}")
        if caveat:
            print(f"  {caveat}")

    if not any_data:
        print("  No closed trades with DTE data yet.")


def win_rate_by_check(trades):
    """
    For each framework check, compare win rate when check passed vs failed.
    Core question: which individual checks actually add predictive value?
    """
    section("📊 INDIVIDUAL CHECK CORRELATION")
    print("  Does each framework check actually predict outcomes?")
    print("  Comparing win rate when check PASSED vs when it did not.\n")

    # We infer check pass/fail from the stored fields
    checks = [
        {
            "name":   "IV Moderate",
            "passed": lambda t: (
                t.get("iv_at_entry") is not None and
                t.get("iv_at_entry", 0) < 0.6
            ),
        },
        {
            "name":   "DTE 5-14 days",
            "passed": lambda t: (
                t.get("dte_at_entry") is not None and
                5 <= t.get("dte_at_entry", 0) <= 14
            ),
        },
        {
            "name":   "Score >= 8",
            "passed": lambda t: (t.get("score_at_entry") or 0) >= 8,
        },
        {
            "name":   "Bullish signal in bullish market",
            "passed": lambda t: (
                t.get("market_bias_at_entry") == "BULLISH" and
                t.get("ticker_bias_at_entry") == "BULLISH"
            ),
        },
        {
            "name":   "Bearish signal in bearish market",
            "passed": lambda t: (
                t.get("market_bias_at_entry") == "BEARISH" and
                t.get("ticker_bias_at_entry") == "BEARISH"
            ),
        },
    ]

    for check in checks:
        passed = [t for t in trades if check["passed"](t)]
        failed = [t for t in trades if not check["passed"](t)]

        passed_wins = sum(1 for t in passed if (t.get("pnl") or 0) > 0)
        failed_wins = sum(1 for t in failed if (t.get("pnl") or 0) > 0)

        passed_rate = (passed_wins / len(passed) * 100) if passed else None
        failed_rate = (failed_wins / len(failed) * 100) if failed else None

        edge = None
        if passed_rate is not None and failed_rate is not None:
            edge = passed_rate - failed_rate

        print(f"  {check['name']}")
        print(f"    Passed: {fmt_rate(passed_wins, len(passed)):<25} "
              f"Failed: {fmt_rate(failed_wins, len(failed))}", end="")

        if edge is not None:
            direction = "✅ adds edge" if edge > 5 else \
                        "❌ reduces edge" if edge < -5 else \
                        "➖ neutral"
            print(f"    Edge: {edge:+.1f}pp  {direction}")
        else:
            print()

        caveat = sample_caveat(len(trades))
        if caveat:
            print(f"    {caveat}")
        print()


# =============================================================================
# MARKET CONTEXT ANALYSIS
# =============================================================================

def win_rate_by_market_bias(trades):
    """
    Win rate by overall market bias at entry.
    Does trading with the market trend improve outcomes?
    """
    section("📊 WIN RATE BY MARKET BIAS AT ENTRY")

    biases = ["BULLISH", "BEARISH", "NEUTRAL"]

    any_data = False

    for bias in biases:
        bucket = [t for t in trades
                  if t.get("market_bias_at_entry") == bias]
        if not bucket:
            continue

        any_data  = True
        wins      = sum(1 for t in bucket if (t.get("pnl") or 0) > 0)
        total_pnl = sum(t.get("pnl") or 0 for t in bucket)
        avg_pct   = (sum(t.get("pnl_pct") or 0 for t in bucket)
                     / len(bucket))

        caveat = sample_caveat(len(bucket))

        print(f"\n  Market {bias:<10} "
              f"Win rate: {fmt_rate(wins, len(bucket)):<20} "
              f"Total P&L: {fmt_pnl(total_pnl):<14} "
              f"Avg return: {fmt_pct(avg_pct)}")
        if caveat:
            print(f"  {caveat}")

    if not any_data:
        print("  No closed trades with market bias data yet.")


def win_rate_by_spy_move(trades):
    """
    Win rate grouped by SPY day change at entry.
    Does entry timing within the day matter?
    """
    section("📊 WIN RATE BY SPY MOVE ON ENTRY DAY")

    bands = [
        ("SPY >+1%",      lambda s: s > 1.0),
        ("SPY +0.3–1%",   lambda s: 0.3 <= s <= 1.0),
        ("SPY Flat",      lambda s: -0.3 < s < 0.3),
        ("SPY -0.3–-1%",  lambda s: -1.0 <= s <= -0.3),
        ("SPY <-1%",      lambda s: s < -1.0),
    ]

    any_data = False

    for label, fn in bands:
        bucket = [t for t in trades
                  if t.get("spy_chg_pct_at_entry") is not None
                  and fn(t["spy_chg_pct_at_entry"])]
        if not bucket:
            continue

        any_data  = True
        wins      = sum(1 for t in bucket if (t.get("pnl") or 0) > 0)
        total_pnl = sum(t.get("pnl") or 0 for t in bucket)
        avg_pct   = (sum(t.get("pnl_pct") or 0 for t in bucket)
                     / len(bucket))

        caveat = sample_caveat(len(bucket))

        print(f"\n  {label:<18} "
              f"Win rate: {fmt_rate(wins, len(bucket)):<20} "
              f"Total P&L: {fmt_pnl(total_pnl):<14} "
              f"Avg return: {fmt_pct(avg_pct)}")
        if caveat:
            print(f"  {caveat}")

    if not any_data:
        print("  No closed trades with SPY change data yet.")


def directional_lean_value(trades):
    """
    Do trades where signal, ticker flow, AND market all aligned
    outperform trades where there was a conflict?
    """
    section("📊 DIRECTIONAL LEAN ALIGNMENT VALUE")

    aligned = [t for t in trades
               if t.get("market_bias_at_entry") != "NEUTRAL"
               and t.get("ticker_bias_at_entry") != "NEUTRAL"
               and t.get("market_bias_at_entry") == t.get("ticker_bias_at_entry")]

    conflicted = [t for t in trades
                  if t not in aligned]

    for label, bucket in [("All aligned", aligned),
                           ("Conflict/neutral", conflicted)]:
        if not bucket:
            continue
        wins      = sum(1 for t in bucket if (t.get("pnl") or 0) > 0)
        total_pnl = sum(t.get("pnl") or 0 for t in bucket)
        avg_pct   = (sum(t.get("pnl_pct") or 0 for t in bucket)
                     / len(bucket))

        caveat = sample_caveat(len(bucket))

        print(f"\n  {label:<20} "
              f"Win rate: {fmt_rate(wins, len(bucket)):<20} "
              f"Total P&L: {fmt_pnl(total_pnl):<14} "
              f"Avg return: {fmt_pct(avg_pct)}")
        if caveat:
            print(f"  {caveat}")


# =============================================================================
# POSITION MANAGEMENT ANALYSIS
# =============================================================================

def target_vs_stop_rate(trades):
    """
    How often does the target get hit vs the stop?
    Are the exit rules calibrated correctly?
    """
    section("📊 TARGET VS STOP HIT RATE")

    target_hits = [t for t in trades if t.get("exit_reason") == "TARGET"]
    stop_hits   = [t for t in trades if t.get("exit_reason") == "STOP"]
    manual      = [t for t in trades if t.get("exit_reason") == "MANUAL"]
    expired     = [t for t in trades if t.get("exit_reason") == "EXPIRED"]
    total       = len(trades)

    caveat = sample_caveat(total)

    print(f"\n  TARGET hit:   {len(target_hits):>3}  "
          f"({len(target_hits)/total*100:.1f}% of closes)" if total else
          f"\n  TARGET hit:   0")
    print(f"  STOP hit:     {len(stop_hits):>3}  "
          f"({len(stop_hits)/total*100:.1f}% of closes)" if total else
          f"  STOP hit:     0")
    print(f"  MANUAL close: {len(manual):>3}  "
          f"({len(manual)/total*100:.1f}% of closes)" if total else
          f"  MANUAL close: 0")
    print(f"  EXPIRED:      {len(expired):>3}  "
          f"({len(expired)/total*100:.1f}% of closes)" if total else
          f"  EXPIRED:      0")

    if target_hits:
        avg_target_pnl = sum(t.get("pnl") or 0 for t in target_hits) \
                         / len(target_hits)
        print(f"\n  Avg P&L when target hit:  {fmt_pnl(avg_target_pnl)}")

    if stop_hits:
        avg_stop_pnl = sum(t.get("pnl") or 0 for t in stop_hits) \
                       / len(stop_hits)
        print(f"  Avg P&L when stop hit:    {fmt_pnl(avg_stop_pnl)}")

    if caveat:
        print(f"\n  {caveat}")


def avg_hold_time(trades):
    """
    Average hold time on winners vs losers.
    Do winning trades resolve faster?
    """
    section("📊 AVERAGE HOLD TIME")

    winners = [t for t in trades if (t.get("pnl") or 0) > 0]
    losers  = [t for t in trades if (t.get("pnl") or 0) <= 0]

    def avg_days(bucket):
        days = [t.get("hold_days") for t in bucket
                if t.get("hold_days") is not None]
        return sum(days) / len(days) if days else None

    w_avg = avg_days(winners)
    l_avg = avg_days(losers)
    a_avg = avg_days(trades)

    print(f"\n  All trades:  {f'{a_avg:.1f} days' if a_avg else '—'}")
    print(f"  Winners:     {f'{w_avg:.1f} days' if w_avg else '—'}")
    print(f"  Losers:      {f'{l_avg:.1f} days' if l_avg else '—'}")

    if w_avg and l_avg:
        if w_avg < l_avg:
            print(f"\n  ✅ Winners resolve faster — "
                  f"{l_avg - w_avg:.1f} days quicker than losers")
        else:
            print(f"\n  ⚠️  Losers resolve faster — "
                  f"winners may be held too long")

    caveat = sample_caveat(len(trades))
    if caveat:
        print(f"\n  {caveat}")


# =============================================================================
# TICKER ANALYSIS
# =============================================================================

def win_rate_by_ticker(trades):
    """
    Win rate broken down by underlying ticker.
    Which names produce the most reliable signals?
    """
    section("📊 WIN RATE BY TICKER")

    # Extract ticker from contract symbol
    def ticker_from_contract(contract):
        # Contract format: TICKER + YYMMDD + C/P + STRIKE
        # Strip last 15 chars to get ticker
        try:
            return contract[:-15]
        except Exception:
            return "UNKNOWN"

    tickers = {}
    for t in trades:
        ticker = ticker_from_contract(t.get("signal_contract", ""))
        if ticker not in tickers:
            tickers[ticker] = []
        tickers[ticker].append(t)

    if not tickers:
        print("  No closed trades yet.")
        return

    # Sort by total trades descending
    sorted_tickers = sorted(tickers.items(),
                             key=lambda x: len(x[1]),
                             reverse=True)

    print(f"\n  {'Ticker':<8} {'Trades':<8} {'Win Rate':<20} "
          f"{'Total P&L':<14} {'Avg Return'}")
    print(f"  {'-'*65}")

    for ticker, bucket in sorted_tickers:
        wins      = sum(1 for t in bucket if (t.get("pnl") or 0) > 0)
        total_pnl = sum(t.get("pnl") or 0 for t in bucket)
        avg_pct   = (sum(t.get("pnl_pct") or 0 for t in bucket)
                     / len(bucket))

        caveat = " ⚠️" if len(bucket) < THRESHOLD_EARLY else ""

        print(f"  {ticker:<8} {len(bucket):<8} "
              f"{fmt_rate(wins, len(bucket)):<20} "
              f"{fmt_pnl(total_pnl):<14} "
              f"{fmt_pct(avg_pct)}{caveat}")


def iv_baseline_accuracy(trades):
    """
    Are the IV baselines calibrated correctly?
    Compare win rate when IV was flagged as moderate vs elevated vs spiked.
    """
    section("📊 IV BASELINE CALIBRATION")
    print("  Are our IV thresholds correctly identifying expensive entries?\n")

    # We use iv_at_entry vs the ticker's baseline to classify
    # Since we don't store the classification directly, we re-derive it
    from fetch_trades import IV_BASELINES, IV_BASELINES_DEFAULT

    def iv_class(t):
        ticker = t.get("signal_contract", "")[:-15]
        iv_raw = t.get("iv_at_entry") or 0
        iv_pct = iv_raw * 100 if iv_raw <= 5 else iv_raw
        baseline = IV_BASELINES.get(ticker, IV_BASELINES_DEFAULT)
        if iv_pct == 0:
            return "Unknown"
        elif iv_pct > baseline["high"]:
            return "Spiked"
        elif iv_pct > baseline["moderate"]:
            return "Elevated"
        else:
            return "Moderate"

    classes = {"Moderate": [], "Elevated": [], "Spiked": [], "Unknown": []}
    for t in trades:
        classes[iv_class(t)].append(t)

    for label in ("Moderate", "Elevated", "Spiked"):
        bucket = classes[label]
        if not bucket:
            continue
        wins      = sum(1 for t in bucket if (t.get("pnl") or 0) > 0)
        total_pnl = sum(t.get("pnl") or 0 for t in bucket)
        avg_pct   = (sum(t.get("pnl_pct") or 0 for t in bucket)
                     / len(bucket))

        caveat = sample_caveat(len(bucket))

        print(f"  IV {label:<10} "
              f"Win rate: {fmt_rate(wins, len(bucket)):<20} "
              f"Total P&L: {fmt_pnl(total_pnl):<14} "
              f"Avg return: {fmt_pct(avg_pct)}")
        if caveat:
            print(f"  {caveat}")
        print()


# =============================================================================
# PORTFOLIO VIEW
# =============================================================================

def total_pnl_over_time(trades):
    """
    Cumulative P&L curve — are we trending up over time?
    """
    section("📊 CUMULATIVE P&L OVER TIME")

    if not trades:
        print("  No closed trades yet.")
        return

    cumulative = 0
    print(f"\n  {'Date':<14} {'Trade':<28} {'P&L':<12} {'Cumulative'}")
    print(f"  {'-'*65}")

    for t in sorted(trades, key=lambda x: x.get("exit_date", "")):
        pnl        = t.get("pnl") or 0
        cumulative += pnl
        icon       = "🟢" if pnl >= 0 else "🔴"
        contract   = t.get("signal_contract", "")[:26]
        date       = t.get("exit_date", "—")

        print(f"  {date:<14} {icon} {contract:<26} "
              f"{fmt_pnl(pnl):<12} {fmt_pnl(cumulative)}")

    caveat = sample_caveat(len(trades))
    if caveat:
        print(f"\n  {caveat}")


def best_and_worst_trades(trades):
    """
    Top 5 and bottom 5 trades by P&L percentage.
    """
    section("📊 BEST AND WORST TRADES")

    if not trades:
        print("  No closed trades yet.")
        return

    sorted_trades = sorted(trades,
                           key=lambda t: t.get("pnl_pct") or 0,
                           reverse=True)

    subsection("🏆 Top 5")
    for t in sorted_trades[:5]:
        print(f"  #{t['id']:<4} {t['signal_contract']:<28} "
              f"{fmt_pct(t.get('pnl_pct')):<12} "
              f"{fmt_pnl(t.get('pnl')):<12} "
              f"{t.get('exit_reason', '—')}")

    subsection("💀 Bottom 5")
    for t in sorted_trades[-5:]:
        print(f"  #{t['id']:<4} {t['signal_contract']:<28} "
              f"{fmt_pct(t.get('pnl_pct')):<12} "
              f"{fmt_pnl(t.get('pnl')):<12} "
              f"{t.get('exit_reason', '—')}")


def open_position_risk(all_trades):
    """
    Current open exposure summary.
    """
    section("📊 OPEN POSITION RISK")

    open_trades = [t for t in all_trades if t.get("status") == "OPEN"]

    if not open_trades:
        print("  No open positions.")
        return

    total_at_risk = sum(t.get("total_cost") or 0 for t in open_trades)

    print(f"\n  Open positions:  {len(open_trades)}")
    print(f"  Total at risk:   ${total_at_risk:,.2f}")
    print(f"\n  {'ID':<5} {'Contract':<28} {'Cost':<10} "
          f"{'Target':<10} {'Stop':<10} {'DTE@Entry'}")
    print(f"  {'-'*75}")

    for t in open_trades:
        target = f"${t['target_price']:.2f}" if t.get('target_price') else '—'
        stop   = f"${t['stop_price']:.2f}"   if t.get('stop_price')   else '—'
        print(f"  {t['id']:<5} {t['signal_contract']:<28} "
              f"${t.get('total_cost', 0):>7,.2f}   "
              f"{target:<10} "
              f"{stop:<10} "
              f"{t.get('dte_at_entry', '—')}")


# =============================================================================
# MASTER REPORT
# =============================================================================

def run_full_report():
    """
    Run the complete analytics report.
    Prints all sections in sequence.
    Called via: python analytics.py
    """

    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)

    print(f"\n{'='*65}")
    print(f"  📊 OPTIONS FLOW SCANNER — ANALYTICS REPORT")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"{'='*65}")

    closed = get_closed_trades_raw()
    all_t  = get_all_trades_raw()

    total_closed = len(closed)
    total_open   = len([t for t in all_t if t.get("status") == "OPEN"])

    print(f"\n  Dataset: {total_closed} closed trades, "
          f"{total_open} open positions")

    caveat = sample_caveat(total_closed)
    if caveat:
        print(f"  {caveat}")

    if total_closed == 0 and total_open == 0:
        print("\n  No paper trade data yet.")
        print("  Enter your first trade with:")
        print("  python paper_trade.py enter <contract> "
              "<price> <contracts> \"<thesis>\"")
        return

    # ── Signal Quality ────────────────────────────────────────────────────
    print(f"\n\n  {'#'*65}")
    print(f"  # SIGNAL QUALITY")
    print(f"  {'#'*65}")

    win_rate_by_verdict(closed)
    win_rate_by_score_band(closed)
    win_rate_by_dte_band(closed)
    win_rate_by_check(closed)

    # ── Market Context ────────────────────────────────────────────────────
    print(f"\n\n  {'#'*65}")
    print(f"  # MARKET CONTEXT")
    print(f"  {'#'*65}")

    win_rate_by_market_bias(closed)
    win_rate_by_spy_move(closed)
    directional_lean_value(closed)

    # ── Position Management ───────────────────────────────────────────────
    print(f"\n\n  {'#'*65}")
    print(f"  # POSITION MANAGEMENT")
    print(f"  {'#'*65}")

    target_vs_stop_rate(closed)
    avg_hold_time(closed)

    # ── Ticker Analysis ───────────────────────────────────────────────────
    print(f"\n\n  {'#'*65}")
    print(f"  # TICKER ANALYSIS")
    print(f"  {'#'*65}")

    win_rate_by_ticker(closed)
    iv_baseline_accuracy(closed)

    # ── Portfolio View ────────────────────────────────────────────────────
    print(f"\n\n  {'#'*65}")
    print(f"  # PORTFOLIO VIEW")
    print(f"  {'#'*65}")

    total_pnl_over_time(closed)
    best_and_worst_trades(closed)
    open_position_risk(all_t)

    print(f"\n{'='*65}")
    print(f"  End of report — {total_closed} closed trades analyzed")
    print(f"{'='*65}\n")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    run_full_report()