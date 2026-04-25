import sqlite3
import os
import sys
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

MARKET_TIMEZONE = "US/Eastern"


# =============================================================================
# SETUP — Dual output to terminal and file
# =============================================================================

def setup_summary_output(date_str):
    """
    Configure output to write to both terminal and a summary log file.
    Returns the log file path.
    """
    log_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "logs"
    )
    os.makedirs(log_dir, exist_ok=True)

    date_compact = date_str.replace("-", "")
    log_path     = os.path.join(log_dir, f"summary_{date_compact}.log")

    # Tee output to both terminal and file
    class Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, message):
            for s in self.streams:
                s.write(message)
                s.flush()

        def flush(self):
            for s in self.streams:
                s.flush()

    log_file   = open(log_path, "w", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, log_file)

    return log_path, log_file


# =============================================================================
# DATABASE HELPERS
# =============================================================================

def get_db_connection():
    from journal import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# =============================================================================
# SUMMARY SECTIONS
# =============================================================================

def print_market_summary(date_str):
    """
    Pull market overview from today's scanner log file.
    Falls back to a note if log isn't available.
    """
    print(f"\n  {'─'*60}")
    print(f"  📈 MARKET OVERVIEW")
    print(f"  {'─'*60}")

    from logger_setup import get_log_path_for_date
    log_path = get_log_path_for_date("scanner", date_str)

    if not log_path:
        print(f"  Scanner log not found for {date_str}")
        return

    # Extract the market overview block from the log
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Find the market overview block
        start_marker = "📈 MARKET OVERVIEW"
        end_marker   = "────────────────────────────────────────────────"

        start_idx = content.find(start_marker)
        if start_idx == -1:
            print(f"  Market overview not found in scanner log")
            return

        # Find the closing separator after the market data
        end_idx = content.find(end_marker, start_idx + len(start_marker))
        if end_idx == -1:
            print(f"  Could not parse market overview block")
            return

        block = content[start_idx:end_idx + len(end_marker)]

        # Print each line of the block, stripping the header we already printed
        for line in block.split("\n"):
            if line.strip() and start_marker not in line:
                print(f"  {line.strip()}")

    except Exception as e:
        print(f"  Could not read scanner log: {e}")


def print_signal_summary(date_str):
    """
    Today's signal counts, tier breakdown, and flow bias.
    """
    print(f"\n  {'─'*60}")
    print(f"  🎯 SIGNAL SUMMARY")
    print(f"  {'─'*60}")

    conn   = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            COUNT(*)                                            as total,
            COUNT(DISTINCT ticker)                             as tickers,
            SUM(CASE WHEN signal_tier = 'HIGH'  THEN 1 ELSE 0 END) as high,
            SUM(CASE WHEN signal_tier = 'INST'  THEN 1 ELSE 0 END) as inst,
            SUM(CASE WHEN signal_tier = 'WATCH' THEN 1 ELSE 0 END) as watch
        FROM signals
        WHERE scan_time LIKE ?
    """, (f"{date_str}%",))

    row = cursor.fetchone()

    if not row or row["total"] == 0:
        print(f"  No signals logged for {date_str}")
        conn.close()
        return

    print(f"  Total signals logged:  {row['total']}")
    print(f"  Tickers with signals:  {row['tickers']}")
    print(f"  🔥 HIGH:               {row['high']}")
    print(f"  💰 INST:               {row['inst']}")
    print(f"  ⚡ WATCH:              {row['watch']}")

    # Directional bias — HIGH signals only
    cursor.execute("""
        SELECT
            contract_type,
            COUNT(*)      as count,
            SUM(premium)  as total_premium
        FROM signals
        WHERE scan_time LIKE ?
        AND signal_tier = 'HIGH'
        GROUP BY contract_type
    """, (f"{date_str}%",))

    bias_rows = {row["contract_type"]: dict(row)
                 for row in cursor.fetchall()}

    call_data    = bias_rows.get("CALL", {"count": 0, "total_premium": 0})
    put_data     = bias_rows.get("PUT",  {"count": 0, "total_premium": 0})
    call_premium = call_data["total_premium"] or 0
    put_premium  = put_data["total_premium"]  or 0
    total_prem   = call_premium + put_premium

    if total_prem > 0:
        call_pct   = round((call_premium / total_prem) * 100, 1)
        put_pct    = round((put_premium  / total_prem) * 100, 1)
        bias_label = ("BULLISH" if call_pct > 55
                      else "BEARISH" if put_pct > 55
                      else "NEUTRAL")

        def fmt(p):
            return (f"${p/1_000_000:.1f}M" if p >= 1_000_000
                    else f"${p/1_000:.0f}K")

        print(f"\n  Flow bias (HIGH signals):")
        print(f"  ▲ Calls: {call_pct}%  {fmt(call_premium)}")
        print(f"  ▼ Puts:  {put_pct}%  {fmt(put_premium)}")
        print(f"  Bias:    {bias_label}")

    conn.close()


def print_qualified_signals(date_str):
    """
    List QUALIFIED signals from today with their thesis if available.
    Pulls from the scanner log since assessments aren't stored in DB yet.
    """
    print(f"\n  {'─'*60}")
    print(f"  ✅ QUALIFIED ASSESSMENTS TODAY")
    print(f"  {'─'*60}")

    from logger_setup import get_log_path_for_date
    log_path = get_log_path_for_date("scanner", date_str)

    if not log_path:
        print(f"  Scanner log not found — assessments unavailable")
        return

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Extract all QUALIFIED assessment blocks
        qualified_blocks = []
        lines            = content.split("\n")
        in_block         = False
        current_block    = []
        is_qualified     = False

        for line in lines:
            if "📋 TRADE ASSESSMENT:" in line:
                in_block      = True
                is_qualified  = False
                current_block = [line]
            elif in_block:
                current_block.append(line)
                if "VERDICT: QUALIFIED" in line:
                    is_qualified = True
                if "·" * 10 in line and len(current_block) > 2:
                    if is_qualified:
                        qualified_blocks.append(current_block[:])
                    in_block      = False
                    current_block = []

        if not qualified_blocks:
            print(f"  No QUALIFIED signals found in today's log")
            return

        print(f"  {len(qualified_blocks)} QUALIFIED signal(s) today:\n")

        for block in qualified_blocks:
            for line in block:
                stripped = line.strip()
                if stripped and "·" * 10 not in stripped:
                    print(f"  {stripped}")
            print()

    except Exception as e:
        print(f"  Could not parse scanner log: {e}")


def print_position_activity(date_str):
    """
    Positions opened today, closed today, and currently open.
    """
    print(f"\n  {'─'*60}")
    print(f"  📂 POSITION ACTIVITY")
    print(f"  {'─'*60}")

    conn   = get_db_connection()
    cursor = conn.cursor()

    # Opened today
    cursor.execute("""
        SELECT * FROM paper_trades
        WHERE entry_date = ?
        ORDER BY entry_time ASC
    """, (date_str,))
    opened = [dict(row) for row in cursor.fetchall()]

    # Closed today
    cursor.execute("""
        SELECT * FROM paper_trades
        WHERE exit_date = ?
        ORDER BY exit_time ASC
    """, (date_str,))
    closed = [dict(row) for row in cursor.fetchall()]

    # Currently open
    cursor.execute("""
        SELECT * FROM paper_trades
        WHERE status = 'OPEN'
        ORDER BY entry_date ASC
    """)
    open_positions = [dict(row) for row in cursor.fetchall()]

    conn.close()

    def fmt_price(p):
        return f"${p:.2f}" if p else "—"

    def fmt_pnl(p):
        if p is None:
            return "—"
        sign = "+" if p >= 0 else ""
        return f"{sign}${p:,.2f}"

    def fmt_pct(p):
        if p is None:
            return "—"
        sign = "+" if p >= 0 else ""
        return f"{sign}{p:.1f}%"

    # Opened today
    if opened:
        print(f"\n  Opened today ({len(opened)}):")
        for t in opened:
            print(f"  #{t['id']}  {t['signal_contract']}")
            print(f"       Entry: {fmt_price(t['entry_price'])}  "
                  f"× {t['contracts']} contract(s)  "
                  f"= ${t['total_cost']:,.2f}")
            print(f"       Target: {fmt_price(t['target_price'])}  "
                  f"Stop: {fmt_price(t['stop_price'])}")
            if t.get("thesis"):
                print(f"       💭 {t['thesis']}")
            print()
    else:
        print(f"\n  No positions opened today")

    # Closed today
    if closed:
        print(f"\n  Closed today ({len(closed)}):")
        for t in closed:
            icon = "🟢" if (t.get("pnl") or 0) >= 0 else "🔴"
            print(f"  {icon} #{t['id']}  {t['signal_contract']}")
            print(f"       Entry: {fmt_price(t['entry_price'])}  "
                  f"→  Exit: {fmt_price(t.get('exit_price'))}")
            print(f"       Reason: {t.get('exit_reason', '—')}  "
                  f"P&L: {fmt_pnl(t.get('pnl'))} "
                  f"({fmt_pct(t.get('pnl_pct'))})")
            if t.get("notes"):
                print(f"       📝 {t['notes']}")
            print()
    else:
        print(f"\n  No positions closed today")

    # Currently open
    if open_positions:
        eastern = pytz.timezone(MARKET_TIMEZONE)
        today   = datetime.now(eastern).date()

        print(f"\n  Currently open ({len(open_positions)}):")
        for t in open_positions:
            try:
                entry_dt  = datetime.strptime(
                    t["entry_date"], "%Y-%m-%d"
                ).date()
                days_held = (today - entry_dt).days
            except Exception:
                days_held = 0

            print(f"  #{t['id']}  {t['signal_contract']}")
            print(f"       Entry: {fmt_price(t['entry_price'])}  "
                  f"Target: {fmt_price(t['target_price'])}  "
                  f"Stop: {fmt_price(t['stop_price'])}")
            print(f"       Held: {days_held}d  "
                  f"Verdict at entry: {t.get('verdict_at_entry', '—')}")
            if t.get("thesis"):
                print(f"       💭 {t['thesis']}")
            print()
    else:
        print(f"\n  No open positions")


def print_outcome_summary(date_str):
    """
    Outcomes recorded today — WIN/LOSS breakdown for expired contracts.
    """
    print(f"\n  {'─'*60}")
    print(f"  📊 OUTCOMES RECORDED TODAY")
    print(f"  {'─'*60}")

    conn   = get_db_connection()
    cursor = conn.cursor()

    # Outcomes written today (by outcome_notes timestamp approximation)
    # We use expiration = date_str as the proxy
    cursor.execute("""
        SELECT
            outcome,
            COUNT(DISTINCT contract) as contracts,
            COUNT(*)                 as total_rows
        FROM signals
        WHERE expiration = ?
        AND outcome IN ('WIN', 'LOSS', 'FLAT')
        GROUP BY outcome
    """, (date_str,))

    rows = {row["outcome"]: dict(row) for row in cursor.fetchall()}

    wins   = rows.get("WIN",  {}).get("contracts", 0)
    losses = rows.get("LOSS", {}).get("contracts", 0)
    flats  = rows.get("FLAT", {}).get("contracts", 0)
    total  = wins + losses + flats

    if total == 0:
        print(f"  No outcomes recorded for contracts expiring {date_str}")
        conn.close()
        return

    win_rate = round((wins / total) * 100, 1) if total > 0 else 0

    print(f"  Contracts expiring {date_str}:")
    print(f"  ✅ WIN:   {wins}")
    print(f"  ❌ LOSS:  {losses}")
    print(f"  ➖ FLAT:  {flats}")
    print(f"  Total:    {total}")
    print(f"  Win rate: {win_rate}%")

    # Top wins by premium
    cursor.execute("""
        SELECT DISTINCT contract, contract_type, strike,
                        ticker, premium, composite_score
        FROM signals
        WHERE expiration = ?
        AND outcome = 'WIN'
        ORDER BY premium DESC
        LIMIT 5
    """, (date_str,))

    top_wins = cursor.fetchall()
    if top_wins:
        print(f"\n  Top wins by premium:")
        for row in top_wins:
            prem = row["premium"]
            prem_display = (f"${prem/1_000_000:.1f}M" if prem >= 1_000_000
                            else f"${prem/1_000:.0f}K")
            print(f"  ✅ {row['ticker']:<6} {row['contract']:<28} "
                  f"{row['contract_type']:<5} "
                  f"Strike ${row['strike']:,.2f}  "
                  f"Premium {prem_display}")

    conn.close()


def print_notable_forward_signals(date_str):
    """
    Non-0DTE HIGH signals logged today that carry forward to future dates.
    These are the ones worth watching tomorrow.
    """
    print(f"\n  {'─'*60}")
    print(f"  🔭 SIGNALS TO WATCH TOMORROW")
    print(f"  {'─'*60}")

    conn   = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT DISTINCT
            contract, contract_type, strike, ticker,
            expiration, premium, composite_score, signal_tier
        FROM signals
        WHERE scan_time LIKE ?
        AND signal_tier IN ('HIGH', 'INST')
        AND expiration > ?
        AND outcome IS NULL
        ORDER BY composite_score DESC, premium DESC
        LIMIT 10
    """, (f"{date_str}%", date_str))

    signals = cursor.fetchall()
    conn.close()

    if not signals:
        print(f"  No forward-dated HIGH/INST signals from today")
        return

    print(f"  {len(signals)} signal(s) carrying forward:\n")
    print(f"  {'Ticker':<8} {'Contract':<28} {'Type':<6} "
          f"{'Expiry':<12} {'Premium':<12} {'Score'}")
    print(f"  {'-'*75}")

    for row in signals:
        prem = row["premium"]
        prem_display = (f"${prem/1_000_000:.1f}M" if prem >= 1_000_000
                        else f"${prem/1_000:.0f}K")
        tier_icon = "🔥" if row["signal_tier"] == "HIGH" else "💰"
        print(f"  {tier_icon} {row['ticker']:<6} {row['contract']:<28} "
              f"{row['contract_type']:<6} "
              f"{row['expiration']:<12} "
              f"{prem_display:<12} "
              f"{row['composite_score']}")


def print_bankroll_status():
    """
    Current paper trading bankroll and exposure summary.
    """
    print(f"\n  {'─'*60}")
    print(f"  💰 BANKROLL STATUS")
    print(f"  {'─'*60}")

    conn   = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            COUNT(*)                                           as total_trades,
            SUM(CASE WHEN status = 'OPEN'   THEN 1 ELSE 0 END) as open_count,
            SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) as closed_count,
            SUM(CASE WHEN pnl > 0           THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl <= 0          THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END) as total_pnl,
            SUM(CASE WHEN status = 'OPEN'
                     THEN total_cost ELSE 0 END)              as open_exposure
        FROM paper_trades
    """)

    row = dict(cursor.fetchone())
    conn.close()

    starting_bankroll = 10_000.00
    total_pnl         = row["total_pnl"] or 0
    open_exposure     = row["open_exposure"] or 0
    current_bankroll  = starting_bankroll + total_pnl
    exposure_pct      = (open_exposure / current_bankroll * 100
                         if current_bankroll > 0 else 0)

    closed = row["closed_count"] or 0
    wins   = row["wins"] or 0
    win_rate = round((wins / closed) * 100, 1) if closed > 0 else 0

    sign = "+" if total_pnl >= 0 else ""

    print(f"  Starting bankroll:   $10,000.00")
    print(f"  Total P&L:           {sign}${total_pnl:,.2f}")
    print(f"  Current bankroll:    ${current_bankroll:,.2f}")
    print(f"  Open exposure:       ${open_exposure:,.2f} "
          f"({exposure_pct:.1f}% of bankroll)")
    print(f"  Max allowed (20%):   ${current_bankroll * 0.20:,.2f}")
    print(f"\n  Paper trades:")
    print(f"  Total:               {row['total_trades'] or 0}")
    print(f"  Open:                {row['open_count'] or 0}")
    print(f"  Closed:              {closed}")
    print(f"  Win rate:            {win_rate}%  "
          f"({wins}W / {row['losses'] or 0}L)")

    # Bankroll health check
    drawdown_pct = ((starting_bankroll - current_bankroll)
                    / starting_bankroll * 100)
    if drawdown_pct >= 50:
        print(f"\n  🚨 HARD STOP TRIGGERED — "
              f"drawdown {drawdown_pct:.1f}% exceeds 50% limit")
    elif drawdown_pct >= 30:
        print(f"\n  ⚠️  WARNING — drawdown {drawdown_pct:.1f}% "
              f"approaching 50% hard stop. Reduce position sizes.")
    elif total_pnl > 0:
        print(f"\n  ✅ Bankroll healthy — "
              f"{sign}{(total_pnl/starting_bankroll)*100:.1f}% "
              f"return on starting capital")


# =============================================================================
# MASTER SUMMARY
# =============================================================================

def run_summary(date_str=None):
    """
    Run the complete daily summary for a given date.
    Defaults to today if no date provided.

    Parameters:
        date_str (str): Date in YYYY-MM-DD format, or None for today
    """
    eastern  = pytz.timezone(MARKET_TIMEZONE)
    now      = datetime.now(eastern)

    if date_str is None:
        date_str = now.strftime("%Y-%m-%d")

    # Parse display date
    try:
        display_date = datetime.strptime(
            date_str, "%Y-%m-%d"
        ).strftime("%A, %B %d %Y")
    except Exception:
        display_date = date_str

    # Setup dual output
    log_path, log_file = setup_summary_output(date_str)

    print(f"\n{'='*65}")
    print(f"  📅 DAILY SUMMARY — {display_date}")
    print(f"  Generated: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"{'='*65}")

    print_market_summary(date_str)
    print_signal_summary(date_str)
    print_qualified_signals(date_str)
    print_position_activity(date_str)
    print_outcome_summary(date_str)
    print_notable_forward_signals(date_str)
    print_bankroll_status()

    print(f"\n{'='*65}")
    print(f"  End of daily summary — {date_str}")
    print(f"  Saved to: {log_path}")
    print(f"{'='*65}\n")

    # Close the log file
    log_file.close()

    # Restore stdout
    sys.stdout = sys.__stdout__
    print(f"✓ Summary saved to {log_path}")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    import sys as _sys

    # Optional date argument: python daily_summary.py 2026-04-23
    if len(_sys.argv) > 1:
        run_summary(_sys.argv[1])
    else:
        run_summary()