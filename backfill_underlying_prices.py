# -*- coding: utf-8 -*-
"""
backfill_underlying_prices.py

Backfills daily OHLC for the watchlist from a start date, into a new
`underlying_prices` table. Two purposes:

1. RETROACTIVE FIX — stamp `underlying_price_at_check` on the ~52K OI readings
   captured before enrichment existed. Our capture fires at 09:35 ET, so the
   daily OPEN is the correct proxy (not the prior close — there is a real
   overnight gap; e.g. AAPL 2026-07-02 close 308.63 -> 07-06 open 307.36).

2. PREMISE TEST — every signal already has share_price at scan time (verified:
   54,817 signals, ZERO nulls, Apr-Jul). With subsequent daily prices we can
   finally ask the foundational question: does the flow we detect actually
   predict underlying direction? That test needs no new collection.

Usage:
    python backfill_underlying_prices.py              # backfill from 2026-04-01
    python backfill_underlying_prices.py --stamp      # also stamp OI readings
    python backfill_underlying_prices.py --start 2026-05-01

SAFE TO RERUN: uses INSERT OR REPLACE on (ticker, date).
Read-only w.r.t. signals and paper_trades. Only writes its own table
(and, with --stamp, backfills a NULL column on signal_oi_tracking).
"""

import sqlite3
import sys
import os
from datetime import datetime

import pytz

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.db")
EASTERN = pytz.timezone("US/Eastern")

WATCHLIST = [
    "AAPL", "NVDA", "MSFT", "AMZN", "META", "GOOGL", "TSLA",
    "SPY", "QQQ", "IWM",
    "JPM", "GS", "BAC",
    "AMD", "NFLX", "CRM", "UBER",
    "XLF", "XLE",
]

DEFAULT_START = "2026-04-01"


def init_table(conn):
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS underlying_prices (
            ticker      TEXT NOT NULL,
            date        TEXT NOT NULL,      -- YYYY-MM-DD
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            volume      INTEGER,
            fetched_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ticker, date)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_up_ticker_date ON underlying_prices(ticker, date)")
    conn.commit()


def backfill(conn, start_date, end_date):
    import yfinance as yf

    print(f"  Fetching {len(WATCHLIST)} tickers from {start_date} to {end_date}...")
    # Fetch one ticker at a time. Slower, but avoids the multi-index column
    # structure yfinance returns for lists, which silently corrupts parsing.
    total_rows = 0
    for ticker in WATCHLIST:
        try:
            df = yf.download(ticker, start=start_date, end=end_date,
                             progress=False, auto_adjust=False)
        except Exception as e:
            print(f"    ! {ticker}: fetch failed: {e}")
            continue

        if df is None or df.empty:
            print(f"    ! {ticker}: no data returned")
            continue

        # Flatten multi-index columns if present (happens even for single tickers
        # in some yfinance versions).
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)

        c = conn.cursor()
        rows = 0
        for idx, row in df.iterrows():
            date_str = idx.strftime("%Y-%m-%d")

            def val(col):
                try:
                    v = row[col]
                    return None if v != v else float(v)   # NaN check
                except Exception:
                    return None

            vol = val("Volume")
            c.execute("""
                INSERT OR REPLACE INTO underlying_prices
                    (ticker, date, open, high, low, close, volume, fetched_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                ticker, date_str,
                val("Open"), val("High"), val("Low"), val("Close"),
                int(vol) if vol is not None else None,
                datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z"),
            ))
            rows += 1
        conn.commit()
        total_rows += rows
        print(f"    {ticker}: {rows} days")

    print(f"  Total rows written: {total_rows}")


def stamp_oi_readings(conn):
    """
    Retroactively fill underlying_price_at_check on OI readings that predate
    the enrichment. Capture runs at 09:35 ET -> the daily OPEN is the match.
    Only touches rows where the column is currently NULL.
    """
    c = conn.cursor()
    before = c.execute("""
        SELECT COUNT(*) FROM signal_oi_tracking
        WHERE underlying_price_at_check IS NULL
    """).fetchone()[0]

    c.execute("""
        UPDATE signal_oi_tracking
        SET underlying_price_at_check = (
            SELECT up.open FROM underlying_prices up
            WHERE up.ticker = signal_oi_tracking.ticker
              AND up.date   = substr(signal_oi_tracking.oi_checked_at, 1, 10)
        )
        WHERE underlying_price_at_check IS NULL
          AND EXISTS (
            SELECT 1 FROM underlying_prices up
            WHERE up.ticker = signal_oi_tracking.ticker
              AND up.date   = substr(signal_oi_tracking.oi_checked_at, 1, 10)
        )
    """)
    conn.commit()

    after = c.execute("""
        SELECT COUNT(*) FROM signal_oi_tracking
        WHERE underlying_price_at_check IS NULL
    """).fetchone()[0]

    print(f"  OI readings missing underlying price: {before} -> {after}")
    print(f"  Stamped: {before - after}")
    if after:
        print(f"  ({after} still null - likely non-trading dates or tickers "
              f"outside the watchlist)")


def main():
    start = DEFAULT_START
    if "--start" in sys.argv:
        start = sys.argv[sys.argv.index("--start") + 1]
    end = datetime.now(EASTERN).strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"  UNDERLYING PRICE BACKFILL")
    print(f"  Range: {start} -> {end}")
    print(f"{'='*60}")

    conn = sqlite3.connect(DB_PATH)
    init_table(conn)
    backfill(conn, start, end)

    if "--stamp" in sys.argv:
        print(f"\n  Stamping OI readings with underlying price...")
        stamp_oi_readings(conn)

    conn.close()
    print(f"  Done.\n")


if __name__ == "__main__":
    main()
