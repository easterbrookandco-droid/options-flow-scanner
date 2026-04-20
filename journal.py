import sqlite3
import os
from datetime import datetime

# Database file lives in the project folder
# SQLite stores everything in this single file
DB_PATH = "signals.db"


def init_database():
    """
    Create the database and signals table if they don't exist yet.
    This runs every time the scanner starts — if the table already
    exists, SQLite simply skips creation. Safe to call repeatedly.
    
    Think of this like setting up a spreadsheet with column headers
    before you start entering data.
    """
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_time         TEXT NOT NULL,
            ticker            TEXT NOT NULL,
            contract          TEXT NOT NULL,
            contract_type     TEXT NOT NULL,
            strike            REAL,
            expiration        TEXT NOT NULL,
            bid               REAL,
            ask               REAL,
            volume            INTEGER,
            open_interest     INTEGER,
            vol_oi_ratio      REAL,
            premium           REAL,
            composite_score   REAL,
            signal_tier       TEXT,
            outcome           TEXT,
            outcome_notes     TEXT,
            created_at        TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_time   TEXT NOT NULL,
            tickers_scanned INTEGER,
            signals_found   INTEGER,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Add share_price column if it doesn't exist yet (safe to run repeatedly)
    try:
        cursor.execute("ALTER TABLE signals ADD COLUMN share_price REAL")
        conn.commit()
        print("  ✓ share_price column added to signals table")
    except sqlite3.OperationalError:
        pass  # Column already exists — no action needed
    
    conn.commit()
    conn.close()
    
    print(f"  ✓ Journal database ready ({DB_PATH})")


def log_signal(ticker, contract, contract_type, strike, expiration,
               bid, ask, volume, open_interest, vol_oi_ratio,
               premium, composite_score, signal_tier,share_price=None):
    """
    Write a single signal record to the database.
    Called automatically by the scanner when a signal is detected.
    
    Parameters:
        ticker (str): Underlying stock symbol e.g. "AAPL"
        contract (str): Full options symbol e.g. "AAPL260420P00270000"
        contract_type (str): "CALL" or "PUT"
        strike (float): Strike price
        expiration (str): Expiration date YYYY-MM-DD
        bid (float): Bid price at scan time
        ask (float): Ask price at scan time
        volume (int): Contracts traded today
        open_interest (int): Existing open contracts
        vol_oi_ratio (float): Volume/OI ratio
        premium (float): Total dollar premium
        composite_score (float): Our signal score
        signal_tier (str): "HIGH", "INST", or "WATCH"
    
    Returns:
        int: The ID of the newly created record
    """
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    cursor.execute("""
        INSERT INTO signals (
            scan_time, ticker, contract, contract_type, strike,
            expiration, bid, ask, volume, open_interest,
            vol_oi_ratio, premium, composite_score, signal_tier,
            share_price
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        scan_time, ticker, contract, contract_type, strike,
        expiration, bid, ask, volume, open_interest,
        vol_oi_ratio, premium, composite_score, signal_tier,
        share_price
    ))
    
    signal_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return signal_id


def check_duplicate(contract, scan_date):
    """
    Check if we've already logged this contract today.
    Prevents the same signal from being logged multiple times
    when the scanner runs repeatedly during market hours.
    
    Parameters:
        contract (str): The options contract symbol
        scan_date (str): Today's date YYYY-MM-DD
    
    Returns:
        bool: True if already logged today, False if new
    """
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id FROM signals 
        WHERE contract = ? 
        AND scan_time LIKE ?
        LIMIT 1
    """, (contract, f"{scan_date}%"))
    
    result = cursor.fetchone()
    conn.close()
    
    return result is not None


def record_outcome(contract, outcome, notes=""):
    """
    Update a signal record with what actually happened after expiration.
    This is how we build our track record and validate the system.
    
    outcome options:
        "WIN"  — stock moved in the direction the signal suggested
        "LOSS" — stock moved against the signal
        "FLAT" — no meaningful movement
    
    Parameters:
        contract (str): The options contract symbol
        outcome (str): "WIN", "LOSS", or "FLAT"  
        notes (str): Optional description of what happened
    """
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE signals 
        SET outcome = ?, outcome_notes = ?
        WHERE contract = ? AND outcome IS NULL
    """, (outcome, notes, contract))
    
    rows_updated = cursor.rowcount
    conn.commit()
    conn.close()
    
    if rows_updated > 0:
        print(f"  ✓ Outcome recorded for {contract}: {outcome}")
    else:
        print(f"  ✗ No open signal found for {contract}")


def get_recent_signals(days=7, tier=None):
    """
    Retrieve recent signals from the journal for review.
    
    Parameters:
        days (int): How many days back to look (default 7)
        tier (str): Filter by tier — "HIGH", "INST", "WATCH", or None for all
    
    Returns:
        list: Signal records as dictionaries
    """
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # Returns rows as dict-like objects
    cursor = conn.cursor()
    
    if tier:
        cursor.execute("""
            SELECT * FROM signals 
            WHERE scan_time >= datetime('now', ?)
            AND signal_tier = ?
            ORDER BY composite_score DESC, premium DESC
        """, (f"-{days} days", tier))
    else:
        cursor.execute("""
            SELECT * FROM signals 
            WHERE scan_time >= datetime('now', ?)
            ORDER BY composite_score DESC, premium DESC
        """, (f"-{days} days",))
    
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return results


def get_performance_summary():
    """
    Calculate win/loss statistics across all signals with recorded outcomes.
    This is the track record — the proof that the system works (or doesn't).
    
    Returns:
        dict: Performance statistics by signal tier
    """
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            signal_tier,
            COUNT(*) as total,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN outcome = 'FLAT' THEN 1 ELSE 0 END) as flats,
            AVG(composite_score) as avg_score,
            AVG(premium) as avg_premium
        FROM signals
        WHERE outcome IS NOT NULL
        GROUP BY signal_tier
        ORDER BY signal_tier
    """)
    
    results = cursor.fetchall()
    conn.close()
    
    return results


def display_recent_signals(days=7, max_signals=30):
    """
    Display a curated daily review of the most actionable signals.
    
    This is your morning report — not a dump of everything logged,
    but a ranked view of the highest conviction signals worth 
    reviewing and potentially acting on.
    
    Ranking priority:
    1. Signal tier (HIGH > INST > WATCH)
    2. Composite score within tier
    3. Premium dollar value as tiebreaker
    
    Parameters:
        days (int): How many days back to look
        max_signals (int): Maximum signals to display (default 30)
    """
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Pull all signals from the lookback period
    # We rank them in Python for flexibility
    cursor.execute("""
        SELECT * FROM signals
        WHERE scan_time >= datetime('now', ?)
        AND outcome IS NULL
        ORDER BY 
            CASE signal_tier 
                WHEN 'HIGH' THEN 1 
                WHEN 'INST' THEN 2 
                WHEN 'WATCH' THEN 3 
                ELSE 4 
            END,
            composite_score DESC,
            premium DESC
    """, (f"-{days} days",))
    
    all_signals = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    if not all_signals:
        print(f"\n  No pending signals in the last {days} days.")
        return
    
    # Deduplicate by contract — keep highest score version
    # This handles cases where same contract logged across multiple scans
    seen_contracts = {}
    for signal in all_signals:
        contract = signal["contract"]
        if contract not in seen_contracts:
            seen_contracts[contract] = signal
        else:
            # Keep whichever has the higher score
            if signal["composite_score"] > seen_contracts[contract]["composite_score"]:
                seen_contracts[contract] = signal
    
    deduplicated = list(seen_contracts.values())
    
    # Re-sort after deduplication
    tier_order = {"HIGH": 1, "INST": 2, "WATCH": 3}
    deduplicated.sort(key=lambda x: (
        tier_order.get(x["signal_tier"], 4),
        -x["composite_score"],
        -x["premium"]
    ))
    
    # Take top N for display
    display_signals = deduplicated[:max_signals]
    total_in_db = len(deduplicated)
    
    print(f"\n{'='*80}")
    print(f"  📋 Daily Signal Review — Top {len(display_signals)} of {total_in_db} signals")
    print(f"  Showing highest conviction signals across last {days} days")
    print(f"{'='*80}")
    
    # Group by tier for cleaner reading
    high_signals = [s for s in display_signals if s["signal_tier"] == "HIGH"]
    inst_signals = [s for s in display_signals if s["signal_tier"] == "INST"]
    watch_signals = [s for s in display_signals if s["signal_tier"] == "WATCH"]
    
    def print_signal_group(signals, icon, label):
        if not signals:
            return
        print(f"\n  {icon} {label} ({len(signals)} signals)")
        print(f"  {'─'*75}")
        print(f"  {'Date':<12} {'Ticker':<8} {'Contract':<28} {'Type':<6} {'Score':<7} {'Premium':<12} Outcome")
        print(f"  {'─'*75}")
        
        for s in signals:
            premium = s["premium"]
            if premium >= 1_000_000:
                premium_display = f"${premium/1_000_000:.1f}M"
            elif premium >= 1_000:
                premium_display = f"${premium/1_000:.0f}K"
            else:
                premium_display = f"${premium:.0f}"
            
            outcome = s["outcome"] or "pending"
            date = s["scan_time"][:10]
            
            print(f"  {date:<12} {s['ticker']:<8} {s['contract']:<28} {s['contract_type']:<6} {s['composite_score']:<7} {premium_display:<12} {outcome}")
    
    print_signal_group(high_signals, "🔥", "HIGH CONVICTION")
    print_signal_group(inst_signals, "💰", "INSTITUTIONAL")
    print_signal_group(watch_signals, "⚡", "WATCH LIST")
    
    # Quick stats
    print(f"\n  {'─'*50}")
    print(f"  📊 Database contains {total_in_db} unique pending signals")
    print(f"  📊 Showing top {len(display_signals)} for daily review")
    
    # Directional bias of displayed signals
    calls = [s for s in display_signals if s["contract_type"] == "CALL"]
    puts = [s for s in display_signals if s["contract_type"] == "PUT"]
    call_premium = sum(s["premium"] for s in calls)
    put_premium = sum(s["premium"] for s in puts)
    total_premium = call_premium + put_premium
    
    if total_premium > 0:
        call_pct = round((call_premium / total_premium) * 100, 1)
        put_pct = round((put_premium / total_premium) * 100, 1)
        bias = "BULLISH" if call_pct > 55 else "BEARISH" if put_pct > 55 else "NEUTRAL"
        print(f"  📊 Top signal bias: {call_pct}% calls / {put_pct}% puts → {bias}")

def display_performance():
    """
    Print a formatted performance summary table.
    """
    
    results = get_performance_summary()
    
    if not results:
        print("\n  No completed signals yet. Outcomes pending.")
        return
    
    print(f"\n{'='*60}")
    print(f"  📊 Performance Summary")
    print(f"{'='*60}")
    print(f"\n  {'Tier':<8} {'Total':<8} {'Wins':<8} {'Losses':<8} {'Win %':<8}")
    print(f"  {'-'*40}")
    
    for row in results:
        total = row[1]
        wins = row[2]
        win_pct = round((wins / total) * 100, 1) if total > 0 else 0
        print(f"  {row[0]:<8} {total:<8} {wins:<8} {row[3]:<8} {win_pct}%")

def display_logging_summary():
    """
    Print a diagnostic summary of what was logged in the last 24 hours,
    broken down by ticker and signal tier.
    Confirms that multi-ticker logging is working correctly.
    """
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT ticker, signal_tier, COUNT(*) as count
        FROM signals
        WHERE scan_time >= datetime('now', '-1 day')
        GROUP BY ticker, signal_tier
        ORDER BY ticker, signal_tier
    """)
    
    results = cursor.fetchall()
    conn.close()
    
    if not results:
        print("  No signals logged in the last 24 hours.")
        return
    
    print(f"\n{'='*50}")
    print(f"  📝 Logging Summary — Last 24 Hours")
    print(f"{'='*50}")
    print(f"\n  {'Ticker':<10} {'Tier':<8} {'Count'}")
    print(f"  {'-'*28}")
    
    total = 0
    for row in results:
        print(f"  {row[0]:<10} {row[1]:<8} {row[2]}")
        total += row[2]
    
    print(f"  {'-'*28}")
    print(f"  {'TOTAL':<10} {'':8} {total}")


def log_scan_event(tickers_scanned, signals_found):
    """
    Record that a scan completed.
    Called once per full scanner run regardless of signal count.
    Used by the dashboard to detect when fresh data is available.
    
    Parameters:
        tickers_scanned (int): Number of tickers scanned
        signals_found (int): Total signals detected this run
    """
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    cursor.execute("""
        INSERT INTO scan_log (scan_time, tickers_scanned, signals_found)
        VALUES (?, ?, ?)
    """, (scan_time, tickers_scanned, signals_found))
    
    conn.commit()
    conn.close()