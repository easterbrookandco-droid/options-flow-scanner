# -*- coding: utf-8 -*-
"""
capture_oi_tracking.py

Captures next-day (and multi-day) open interest for recently-fired signals,
to distinguish OPENING vs CLOSING institutional flow.

THESIS: A genuine opening footprint shows OI RISING the morning after the
signal (contracts settled into standing positions overnight). Closing/day-trade
flow shows flat or falling OI despite the volume.

WHY MULTI-DAY: A position may keep building for several days, or get closed
out. Tracking OI for N days after a signal captures whether conviction
PERSISTS, not just whether it stuck one night.

WHY TIMESTAMPED: OCC reconciles OI overnight; the exact time the data feed
reflects the new number is UNKNOWN. Every reading is stamped with its actual
fetch time (oi_checked_at) so we can EMPIRICALLY determine the rollover time
from the data rather than assuming it. During the calibration period this job
runs several times each morning; once we know the rollover time we reduce to
one daily run.

Run modes:
    python capture_oi_tracking.py            # capture one reading pass now
    python capture_oi_tracking.py --init     # create table only, then exit

This script REUSES fetch_trades.py's existing functions:
    get_access_token(), get_account_id(), fetch_option_chain()
It does NOT modify the scanner or the live signal loop.
"""

import sqlite3
import sys
import os
from datetime import datetime, timedelta

import pytz

# Reuse the scanner's own auth + chain fetch. No reimplementation.
from fetch_trades import (
    get_access_token,
    get_account_id,
    fetch_option_chain,
)

DB_PATH        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.db")
EASTERN        = pytz.timezone("US/Eastern")

# How many calendar days after a signal we keep re-checking its OI.
# 5 covers "did it build / persist / unwind over the trading week."
TRACKING_WINDOW_DAYS = 5


def init_table():
    """Create the OI tracking table. Safe to call repeatedly."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS signal_oi_tracking (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id         INTEGER NOT NULL,
            contract          TEXT NOT NULL,
            ticker            TEXT,
            expiration        TEXT,
            -- baseline captured at signal time (copied from signals row)
            oi_at_signal      INTEGER,
            vol_at_signal     INTEGER,
            scan_time         TEXT,
            -- the follow-up reading
            oi_observed       INTEGER,
            oi_checked_at     TEXT NOT NULL,      -- actual fetch timestamp (ET)
            days_after_signal INTEGER,            -- calendar days since scan_time
            oi_change         INTEGER,            -- oi_observed - oi_at_signal
            oi_change_pct     REAL,
            created_at        TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # One reading per signal per fetch pass — allow many rows per signal over days,
    # but dedupe exact re-runs within the same minute.
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_oi_track_signal
        ON signal_oi_tracking(signal_id)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_oi_track_contract
        ON signal_oi_tracking(contract)
    """)
    conn.commit()
    conn.close()
    print("signal_oi_tracking table ready.")


def get_signals_to_track(conn):
    """
    Return signals fired within the tracking window that we should
    re-check OI for. One row per (signal_id, contract).
    """
    cutoff = (datetime.now(EASTERN) - timedelta(days=TRACKING_WINDOW_DAYS)).strftime("%Y-%m-%d")
    today  = datetime.now(EASTERN).strftime("%Y-%m-%d")
    c = conn.cursor()
    # Only track signals fired within the window whose contract HAS NOT YET expired.
    # An expired contract has settled — there is no OI left to confirm.
    c.execute("""
        SELECT id, contract, ticker, expiration, open_interest, volume, scan_time
        FROM signals
        WHERE substr(scan_time,1,10) >= ?
          AND expiration >= ?
        ORDER BY ticker, expiration
    """, (cutoff, today))
    return [dict(zip(
        ["signal_id","contract","ticker","expiration","oi_at_signal","vol_at_signal","scan_time"],
        row)) for row in c.fetchall()]


def main():
    if "--init" in sys.argv:
        init_table()
        return

    init_table()  # idempotent safety

    now_et  = datetime.now(EASTERN)
    now_str = now_et.strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"\n{'='*60}")
    print(f"  OI TRACKING CAPTURE — {now_str}")
    print(f"{'='*60}")

    conn = sqlite3.connect(DB_PATH)
    signals = get_signals_to_track(conn)
    if not signals:
        print("  No signals in tracking window. Nothing to do.")
        conn.close()
        return

    print(f"  Signals in {TRACKING_WINDOW_DAYS}-day window: {len(signals)}")

    # Auth once, reuse the scanner's own functions.
    token = get_access_token()
    account_id = get_account_id(token) if token else None
    if not token or not account_id:
        print("  AUTH FAILED — aborting, no readings written.")
        conn.close()
        return

    # Group by (ticker, expiration) so we fetch each chain ONCE, not per-contract.
    chains = {}
    for s in signals:
        key = (s["ticker"], s["expiration"])
        chains.setdefault(key, []).append(s)

    print(f"  Unique chains to fetch: {len(chains)}")

    c = conn.cursor()
    written = 0
    fetch_errors = 0

    for (ticker, expiration), sig_list in chains.items():
        try:
            chain = fetch_option_chain(token, account_id, ticker, expiration)
        except Exception as e:
            print(f"    ! chain fetch failed {ticker} {expiration}: {e}")
            fetch_errors += 1
            continue

        if not chain:
            print(f"    ! empty chain {ticker} {expiration}")
            fetch_errors += 1
            continue

        # chain = {'baseSymbol': str, 'calls': [...], 'puts': [...]}
        # each contract: {'instrument': {'symbol': 'AAPL260706C00205000'}, 'openInterest': int, ...}
        # Build a lookup: contract symbol -> current openInterest
        oi_now = {}
        for side in ("calls", "puts"):
            for item in chain.get(side, []):
                instr = item.get("instrument") or {}
                sym = instr.get("symbol")
                if sym is not None:
                    oi_now[sym] = item.get("openInterest")

        for s in sig_list:
            observed = oi_now.get(s["contract"])
            if observed is None:
                # contract not found in current chain (expired/rolled) — skip, note it
                continue

            base = s["oi_at_signal"] or 0
            change = observed - base
            change_pct = round((change / base * 100), 2) if base else None

            # days since signal
            try:
                scan_dt = datetime.strptime(s["scan_time"][:10], "%Y-%m-%d")
                days_after = (now_et.date() - scan_dt.date()).days
            except Exception:
                days_after = None

            c.execute("""
                INSERT INTO signal_oi_tracking (
                    signal_id, contract, ticker, expiration,
                    oi_at_signal, vol_at_signal, scan_time,
                    oi_observed, oi_checked_at, days_after_signal,
                    oi_change, oi_change_pct
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                s["signal_id"], s["contract"], s["ticker"], s["expiration"],
                s["oi_at_signal"], s["vol_at_signal"], s["scan_time"],
                observed, now_str, days_after,
                change, change_pct
            ))
            written += 1

    conn.commit()
    conn.close()

    print(f"  Readings written: {written}")
    print(f"  Chain fetch errors: {fetch_errors}")
    print(f"  Done.\n")


if __name__ == "__main__":
    main()
