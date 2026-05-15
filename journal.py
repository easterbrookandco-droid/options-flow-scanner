import sqlite3
import os
import pytz
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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_closes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date  TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            close_price REAL NOT NULL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trade_date, ticker)
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


def save_market_close(prices_dict):
    """
    Store end-of-day closing prices for market context tickers.
    Called once at market close by scheduler.py.

    Parameters:
        prices_dict (dict): Keyed by ticker symbol, value is closing price
                            e.g. {"SPY": 739.28, "QQQ": 694.73, ...}
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    eastern = pytz.timezone("US/Eastern")
    today   = datetime.now(eastern).strftime("%Y-%m-%d")

    for ticker, price in prices_dict.items():
        if price and price > 0:
            cursor.execute("""
                INSERT INTO market_closes (trade_date, ticker, close_price)
                VALUES (?, ?, ?)
                ON CONFLICT(trade_date, ticker) DO UPDATE SET
                    close_price = excluded.close_price
            """, (today, ticker, price))

    conn.commit()
    conn.close()


def get_previous_close(ticker):
    """
    Get the most recent stored closing price for a ticker.
    Used by fetch_market_context() to calculate day change.

    Parameters:
        ticker (str): Ticker symbol e.g. "SPY"

    Returns:
        float: Most recent closing price, or None if not available
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT close_price
        FROM market_closes
        WHERE ticker = ?
        ORDER BY trade_date DESC
        LIMIT 1
    """, (ticker,))

    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def init_paper_trades_table():
    """
    Create the paper_trades table if it doesn't exist.
    Called once at startup from paper_trade.py.
    
    Separate from init_database() intentionally — paper trading
    is an optional layer on top of the signal scanner. Keeping
    the table creation separate means the scanner can run cleanly
    without any paper trading infrastructure if needed.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,

            -- Signal linkage
            signal_contract             TEXT NOT NULL,
            signal_id                   INTEGER,

            -- Entry details
            entry_date                  TEXT NOT NULL,
            entry_time                  TEXT NOT NULL,
            entry_price                 REAL NOT NULL,
            entry_underlying_price      REAL,
            contracts                   INTEGER NOT NULL DEFAULT 1,
            total_cost                  REAL NOT NULL,
            thesis                      TEXT NOT NULL,

            -- Assessment context at entry
            verdict_at_entry            TEXT,
            score_at_entry              REAL,
            dte_at_entry                INTEGER,
            iv_at_entry                 REAL,
            delta_at_entry              REAL,
            market_bias_at_entry        TEXT,
            ticker_bias_at_entry        TEXT,

            -- Exit rules
            target_price                REAL NOT NULL,
            stop_price                  REAL NOT NULL,

            -- Market environment at entry
            spy_chg_pct_at_entry        REAL,
            qqq_chg_pct_at_entry        REAL,
            iwm_chg_pct_at_entry        REAL,
            tlt_chg_pct_at_entry        REAL,
            gld_chg_pct_at_entry        REAL,
            uso_chg_pct_at_entry        REAL,

            -- Exit details (null until closed)
            exit_date                   TEXT,
            exit_time                   TEXT,
            exit_price                  REAL,
            exit_underlying_price       REAL,
            exit_underlying_change_pct  REAL,
            exit_reason                 TEXT,

            -- Market environment at exit
            spy_chg_pct_at_exit         REAL,
            qqq_chg_pct_at_exit         REAL,
            iwm_chg_pct_at_exit         REAL,
            tlt_chg_pct_at_exit         REAL,
            gld_chg_pct_at_exit         REAL,
            uso_chg_pct_at_exit         REAL,

            -- Outcome (null until closed)
            pnl                         REAL,
            pnl_pct                     REAL,
            hold_days                   INTEGER,
            hit_target_first            INTEGER,
            max_value_seen              REAL,

            -- Metadata
            notes                       TEXT,
            status                      TEXT NOT NULL DEFAULT 'OPEN',
            created_at                  TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()
    print(f"  ✓ Paper trades table ready")


def insert_paper_trade(
    signal_contract, entry_price, contracts, thesis,
    entry_underlying_price=None,
    verdict_at_entry=None, score_at_entry=None,
    dte_at_entry=None, iv_at_entry=None, delta_at_entry=None,
    market_bias_at_entry=None, ticker_bias_at_entry=None,
    market_snapshot=None, signal_id=None
):
    """
    Record a new paper trade entry.

    Parameters:
        signal_contract (str): Contract symbol e.g. "TSLA260429C00392500"
        entry_price (float): Option ask price per share at entry
        contracts (int): Number of contracts (1-2 per framework)
        thesis (str): One sentence explaining the trade rationale
        entry_underlying_price (float): Stock price at entry
        verdict_at_entry (str): QUALIFIED / REVIEW etc
        score_at_entry (float): Composite score at time of entry
        dte_at_entry (int): Days to expiration at entry
        iv_at_entry (float): Implied volatility at entry
        delta_at_entry (float): Delta at entry
        market_bias_at_entry (str): BULLISH / BEARISH / NEUTRAL
        ticker_bias_at_entry (str): Same for specific ticker
        market_snapshot (dict): Output of get_market_overview()
                                keyed by ticker with chg_pct values
        signal_id (int): FK to signals table if known

    Returns:
        int: ID of the newly created paper trade record
    """

    eastern = pytz.timezone("US/Eastern")
    now = datetime.now(eastern)
    entry_date = now.strftime("%Y-%m-%d")
    entry_time = now.strftime("%H:%M:%S")

    total_cost   = round(entry_price * contracts * 100, 2)
    target_price = round(entry_price * 2, 2)

    # DTE-aware stop loss — give longer-dated positions more room to breathe
    if dte_at_entry is None or dte_at_entry <= 2:
        stop_mult = 0.50   # 50% stop — very short DTE, cut fast
    elif dte_at_entry <= 5:
        stop_mult = 0.65   # 35% stop
    elif dte_at_entry <= 14:
        stop_mult = 0.75   # 25% stop
    else:
        stop_mult = 0.80   # 20% stop — long DTE, lots of runway
    stop_price = round(entry_price * stop_mult, 2)

    # Extract macro snapshot values safely
    def chg(ticker):
        if not market_snapshot:
            return None
        t = market_snapshot.get(ticker, {})
        return t.get("chg_pct") if t.get("has_change") else None

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO paper_trades (
            signal_contract, signal_id,
            entry_date, entry_time,
            entry_price, entry_underlying_price,
            contracts, total_cost, thesis,
            verdict_at_entry, score_at_entry,
            dte_at_entry, iv_at_entry, delta_at_entry,
            market_bias_at_entry, ticker_bias_at_entry,
            target_price, stop_price,
            spy_chg_pct_at_entry, qqq_chg_pct_at_entry,
            iwm_chg_pct_at_entry, tlt_chg_pct_at_entry,
            gld_chg_pct_at_entry, uso_chg_pct_at_entry,
            status
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN'
        )
    """, (
        signal_contract, signal_id,
        entry_date, entry_time,
        entry_price, entry_underlying_price,
        contracts, total_cost, thesis,
        verdict_at_entry, score_at_entry,
        dte_at_entry, iv_at_entry, delta_at_entry,
        market_bias_at_entry, ticker_bias_at_entry,
        target_price, stop_price,
        chg("SPY"), chg("QQQ"),
        chg("IWM"), chg("TLT"),
        chg("GLD"), chg("USO"),
    ))

    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def close_paper_trade(
    trade_id, exit_price, exit_reason,
    exit_underlying_price=None,
    market_snapshot=None,
    notes=None
):
    """
    Record the exit of an open paper trade and calculate P&L.

    Parameters:
        trade_id (int): ID of the paper trade to close
        exit_price (float): Option price per share at exit
        exit_reason (str): TARGET / STOP / MANUAL / EXPIRED
        exit_underlying_price (float): Stock price at exit
        market_snapshot (dict): Market overview at exit time
        notes (str): Optional freeform notes on the exit

    Returns:
        dict: Summary of the closed trade including P&L,
              or None if trade not found
    """

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Fetch the open trade
    cursor.execute("""
        SELECT * FROM paper_trades
        WHERE id = ? AND status = 'OPEN'
    """, (trade_id,))
    trade = cursor.fetchone()

    if not trade:
        conn.close()
        return None

    trade = dict(trade)

    eastern = pytz.timezone("US/Eastern")
    now = datetime.now(eastern)
    exit_date = now.strftime("%Y-%m-%d")
    exit_time = now.strftime("%H:%M:%S")

    # P&L calculations
    contracts  = trade["contracts"]
    entry_price = trade["entry_price"]
    total_cost  = trade["total_cost"]

    pnl     = round((exit_price - entry_price) * contracts * 100, 2)
    pnl_pct = round((pnl / total_cost) * 100, 2) if total_cost else 0

    # Hold time
    try:
        entry_dt  = datetime.strptime(trade["entry_date"], "%Y-%m-%d")
        exit_dt   = datetime.strptime(exit_date, "%Y-%m-%d")
        hold_days = (exit_dt - entry_dt).days
    except Exception:
        hold_days = 0

    # Did it hit target before stop?
    hit_target = 1 if exit_reason == "TARGET" else 0

    # Underlying price change entry to exit
    underlying_change = None
    if exit_underlying_price and trade.get("entry_underlying_price"):
        underlying_change = round(
            ((exit_underlying_price - trade["entry_underlying_price"])
             / trade["entry_underlying_price"]) * 100, 2
        )

    # Macro snapshot at exit
    def chg(ticker):
        if not market_snapshot:
            return None
        t = market_snapshot.get(ticker, {})
        return t.get("chg_pct") if t.get("has_change") else None

    cursor.execute("""
        UPDATE paper_trades SET
            exit_date                   = ?,
            exit_time                   = ?,
            exit_price                  = ?,
            exit_underlying_price       = ?,
            exit_underlying_change_pct  = ?,
            exit_reason                 = ?,
            spy_chg_pct_at_exit         = ?,
            qqq_chg_pct_at_exit         = ?,
            iwm_chg_pct_at_exit         = ?,
            tlt_chg_pct_at_exit         = ?,
            gld_chg_pct_at_exit         = ?,
            uso_chg_pct_at_exit         = ?,
            pnl                         = ?,
            pnl_pct                     = ?,
            hold_days                   = ?,
            hit_target_first            = ?,
            notes                       = ?,
            status                      = 'CLOSED'
        WHERE id = ?
    """, (
        exit_date, exit_time,
        exit_price, exit_underlying_price,
        underlying_change, exit_reason,
        chg("SPY"), chg("QQQ"),
        chg("IWM"), chg("TLT"),
        chg("GLD"), chg("USO"),
        pnl, pnl_pct,
        hold_days, hit_target,
        notes, trade_id
    ))

    conn.commit()
    conn.close()

    return {
        "trade_id":   trade_id,
        "contract":   trade["signal_contract"],
        "pnl":        pnl,
        "pnl_pct":    pnl_pct,
        "exit_reason": exit_reason,
        "hold_days":  hold_days,
    }


def get_open_positions():
    """
    Fetch all currently open paper trades.

    Returns:
        list: Open trade dicts ordered by entry date ascending
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM paper_trades
        WHERE status = 'OPEN'
        ORDER BY entry_date ASC, entry_time ASC
    """)

    trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return trades


def get_closed_trades(limit=20):
    """
    Fetch recently closed paper trades for review display.

    Returns:
        list: Closed trade dicts ordered by exit date descending
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM paper_trades
        WHERE status = 'CLOSED'
        ORDER BY exit_date DESC, exit_time DESC
        LIMIT ?
    """, (limit,))

    trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return trades


def get_paper_trade_summary():
    """
    Calculate high-level P&L summary across all paper trades.
    Used for the scan-end display and review command.

    Returns:
        dict: Summary stats — total, open, closed, win rate, total P&L
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            COUNT(*)                                        as total,
            SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END)   as open_count,
            SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END)  as closed_count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)            as wins,
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END)           as losses,
            SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END)  as total_pnl,
            AVG(CASE WHEN pnl IS NOT NULL THEN pnl_pct ELSE NULL END) as avg_pnl_pct
        FROM paper_trades
    """)

    result = dict(cursor.fetchone())
    conn.close()
    return result


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


def check_duplicate(contract, date, current_score=None):
    """
    Check if a contract should be skipped to avoid redundant logging.

    Logic:
        - If contract has never been logged → not a duplicate, log it
        - If contract was logged today → always skip (already have today's read)
        - If contract was logged on a previous day:
            - If score hasn't changed materially (within 0.5 pts) → skip
            - If score has escalated significantly (>= 0.5 pts higher) → log it
              This captures meaningful conviction increases mid-week

    Parameters:
        contract (str): Contract symbol
        date (str): Today's date YYYY-MM-DD
        current_score (float): Current composite score, used for
                               escalation detection. If None, always
                               skips previously seen contracts.

    Returns:
        bool: True if should skip (is duplicate), False if should log
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Check if logged today already
    cursor.execute("""
        SELECT COUNT(*) as n FROM signals
        WHERE contract = ?
        AND scan_time LIKE ?
    """, (contract, f"{date}%"))
    if cursor.fetchone()["n"] > 0:
        conn.close()
        return True  # Already logged today, always skip

    # Check if logged on a previous day
    cursor.execute("""
        SELECT composite_score
        FROM signals
        WHERE contract = ?
        AND scan_time NOT LIKE ?
        ORDER BY scan_time DESC
        LIMIT 1
    """, (contract, f"{date}%"))

    row = cursor.fetchone()
    conn.close()

    if row is None:
        return False  # Never logged, don't skip

    # Previously logged — check for score escalation
    if current_score is None:
        return True  # No score provided, skip to be safe

    previous_score = row["composite_score"]
    score_increase = current_score - previous_score

    if score_increase >= 0.5:
        # Meaningful escalation — log it
        return False
    else:
        # No material change — skip
        return True
    

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


def get_todays_signals_for_assessment():
    """
    Fetch today's HIGH and INST signals for trade quality assessment.
    Used by fetch_trades.py to calculate per-ticker directional bias.

    Returns:
        list: Signal dicts with ticker and contract_type fields
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")

    cursor.execute("""
        SELECT ticker, contract_type, signal_tier, premium, composite_score
        FROM signals
        WHERE scan_time LIKE ?
        AND signal_tier IN ('HIGH', 'INST')
    """, (f"{today}%",))

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