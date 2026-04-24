import time
import sqlite3
import requests
import os
import sys
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY    = os.getenv("PUBLIC_SECRET_KEY")
BASE_URL      = "https://api.public.com"
MARKET_TIMEZONE = "US/Eastern"

# =============================================================================
# CONFIGURATION
# =============================================================================

# How often to check prices during market hours (seconds)
CHECK_INTERVAL_SECONDS = 180  # 3 minutes

# Alert thresholds — notify before hitting hard limits
TARGET_ALERT_PCT  = 80   # Alert when position is 80% of the way to target
STOP_ALERT_PCT    = 80   # Alert when position is 80% of the way to stop loss

# After-hours behavior
CHECK_AFTER_HOURS = False  # Set True to monitor during extended hours


# =============================================================================
# DATABASE
# =============================================================================

def get_db_path():
    """Get DB path from journal module."""
    from journal import DB_PATH
    return DB_PATH


def get_open_positions():
    """Fetch all open paper trades from DB."""
    conn = sqlite3.connect(get_db_path())
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


def log_price_snapshot(trade_id, current_price, bid, ask, pnl, pnl_pct):
    """
    Log a price check to position_snapshots table.
    Builds a full price history for each position over its lifetime.
    """
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()

    # Create table if it doesn't exist yet
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS position_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id        INTEGER NOT NULL,
            snapshot_time   TEXT NOT NULL,
            current_price   REAL,
            bid             REAL,
            ask             REAL,
            pnl             REAL,
            pnl_pct         REAL,
            created_at      TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    eastern   = pytz.timezone(MARKET_TIMEZONE)
    now       = datetime.now(eastern).strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("""
        INSERT INTO position_snapshots
            (trade_id, snapshot_time, current_price, bid, ask, pnl, pnl_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (trade_id, now, current_price, bid, ask, pnl, pnl_pct))

    # Update max_value_seen on the trade if this is a new high
    cursor.execute("""
        UPDATE paper_trades
        SET max_value_seen = ?
        WHERE id = ?
        AND (max_value_seen IS NULL OR ? > max_value_seen)
    """, (current_price, trade_id, current_price))

    conn.commit()
    conn.close()


def auto_close_position(trade_id, exit_price, exit_reason, bid, ask):
    """
    Automatically close a position when stop or target is hit.
    Uses mid price for P&L, records bid/ask for reference.
    """
    from journal import close_paper_trade
    result = close_paper_trade(
        trade_id=trade_id,
        exit_price=exit_price,
        exit_reason=exit_reason,
        notes=f"Auto-closed by position monitor. Bid:{bid} Ask:{ask}"
    )
    return result


# =============================================================================
# MARKET HOURS
# =============================================================================

def is_market_open():
    """Check if US market is currently open."""
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)
    if now.weekday() > 4:
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now < market_close


def is_monitoring_active():
    """Determine if monitor should be checking prices right now."""
    if is_market_open():
        return True
    if CHECK_AFTER_HOURS:
        eastern = pytz.timezone(MARKET_TIMEZONE)
        now     = datetime.now(eastern)
        # Extended hours: 4am - 8pm ET
        extended_open  = now.replace(hour=4,  minute=0, second=0)
        extended_close = now.replace(hour=20, minute=0, second=0)
        return extended_open <= now < extended_close
    return False


def seconds_until_market_open():
    """Seconds until next market open."""
    eastern   = pytz.timezone(MARKET_TIMEZONE)
    now       = datetime.now(eastern)
    next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now >= next_open:
        next_open += timedelta(days=1)
    while next_open.weekday() > 4:
        next_open += timedelta(days=1)
    return max(0, int((next_open - now).total_seconds()))


# =============================================================================
# PUBLIC API — OPTION PRICE FETCHING
# =============================================================================

def get_auth_token():
    """Get a fresh access token."""
    try:
        response = requests.post(
            f"{BASE_URL}/userapiauthservice/personal/access-tokens",
            json={"secret": SECRET_KEY, "validityInMinutes": 60}
        )
        if response.status_code == 200:
            return response.json().get("accessToken")
    except Exception as e:
        print(f"  ✗ Auth error: {e}")
    return None


def get_account_id(token):
    """Get brokerage account ID."""
    try:
        response = requests.get(
            f"{BASE_URL}/userapigateway/trading/account",
            headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code == 200:
            for account in response.json().get("accounts", []):
                if account.get("accountType") == "BROKERAGE":
                    return account.get("accountId")
    except Exception as e:
        print(f"  ✗ Account error: {e}")
    return None


def fetch_option_prices(contracts, token, account_id):
    """
    Fetch current bid/ask/last for a list of option contracts.
    Uses the quotes endpoint with type OPTION.

    Parameters:
        contracts (list): List of contract symbol strings
        token (str): Valid access token
        account_id (str): Brokerage account ID

    Returns:
        dict: Keyed by contract symbol, values contain bid/ask/mid/last
              Empty dict if call fails.
    """
    if not contracts:
        return {}

    try:
        response = requests.post(
            f"{BASE_URL}/userapigateway/marketdata/{account_id}/quotes",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={
                "instruments": [
                    {"symbol": c, "type": "OPTION"} for c in contracts
                ]
            }
        )

        if response.status_code != 200:
            print(f"  ✗ Price fetch failed: {response.status_code}")
            return {}

        prices = {}
        for q in response.json().get("quotes", []):
            if q.get("outcome") == "SUCCESS":
                symbol = q["instrument"]["symbol"]
                try:
                    bid  = float(q.get("bid")  or 0)
                    ask  = float(q.get("ask")  or 0)
                    last = float(q.get("last") or 0)
                    mid  = round((bid + ask) / 2, 4) if bid and ask else last

                    prices[symbol] = {
                        "bid":  bid,
                        "ask":  ask,
                        "last": last,
                        "mid":  mid,
                    }
                except (ValueError, TypeError):
                    pass

        return prices

    except Exception as e:
        print(f"  ✗ Price fetch error: {e}")
        return {}


# =============================================================================
# DISPLAY HELPERS
# =============================================================================

def fmt_pnl(pnl):
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.2f}"


def fmt_pct(pct):
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def progress_bar(current, entry, target, stop, width=20):
    """
    Visual bar showing where current price sits between stop and target.
    Stop ←————————|—————————→ Target
    """
    total_range = target - stop
    if total_range == 0:
        return "—"
    position = (current - stop) / total_range
    position = max(0, min(1, position))
    filled   = int(position * width)
    bar      = "─" * filled + "●" + "─" * (width - filled)
    return f"[{bar}]"


# =============================================================================
# CORE MONITOR LOGIC
# =============================================================================

def evaluate_positions(positions, prices, eastern):
    """
    Evaluate all open positions against current prices.
    Prints status, fires alerts, auto-closes when thresholds hit.

    Returns:
        list: Trade IDs that were auto-closed this cycle
    """
    closed_ids = []
    now_str    = datetime.now(eastern).strftime("%H:%M:%S %Z")

    print(f"\n  {'─'*65}")
    print(f"  📡 POSITION CHECK — {now_str}")
    print(f"  {'─'*65}")

    for trade in positions:
        trade_id    = trade["id"]
        contract    = trade["signal_contract"]
        entry_price = trade["entry_price"]
        contracts   = trade["contracts"]
        target      = trade["target_price"]
        stop        = trade["stop_price"]
        total_cost  = trade["total_cost"]
        thesis      = trade.get("thesis", "")

        price_data  = prices.get(contract)

        if not price_data:
            print(f"\n  ⚠️  #{trade_id} {contract}")
            print(f"     Price unavailable — skipping this cycle")
            continue

        mid     = price_data["mid"]
        bid     = price_data["bid"]
        ask     = price_data["ask"]

        # P&L calculations using mid price
        pnl     = round((mid - entry_price) * contracts * 100, 2)
        pnl_pct = round((pnl / total_cost) * 100, 2) if total_cost else 0

        # Log snapshot
        log_price_snapshot(trade_id, mid, bid, ask, pnl, pnl_pct)

        # Progress toward target/stop
        bar = progress_bar(mid, entry_price, target, stop)

        # P&L indicator
        icon = "🟢" if pnl >= 0 else "🔴"

        print(f"\n  {icon} #{trade_id}  {contract}")
        print(f"     Entry: ${entry_price:.2f}  │  "
              f"Bid: ${bid:.2f}  Mid: ${mid:.2f}  Ask: ${ask:.2f}")
        print(f"     P&L:   {fmt_pnl(pnl)} ({fmt_pct(pnl_pct)})  │  "
              f"Target: ${target:.2f}  Stop: ${stop:.2f}")
        print(f"     {bar}  Stop ←→ Target")
        if thesis:
            print(f"     💭 {thesis[:70]}{'...' if len(thesis) > 70 else ''}")

        # ── Auto-close checks ──────────────────────────────────────────

        # TARGET HIT
        if mid >= target:
            print(f"\n  {'='*65}")
            print(f"  🎯 TARGET HIT — #{trade_id} {contract}")
            print(f"     Entry: ${entry_price:.2f}  →  Current: ${mid:.2f}")
            print(f"     P&L: {fmt_pnl(pnl)} ({fmt_pct(pnl_pct)})")
            print(f"  {'='*65}")

            result = auto_close_position(trade_id, mid, "TARGET", bid, ask)
            if result:
                print(f"  ✅ Position auto-closed at TARGET")
                closed_ids.append(trade_id)
            continue

        # STOP HIT
        if mid <= stop:
            print(f"\n  {'='*65}")
            print(f"  🛑 STOP HIT — #{trade_id} {contract}")
            print(f"     Entry: ${entry_price:.2f}  →  Current: ${mid:.2f}")
            print(f"     P&L: {fmt_pnl(pnl)} ({fmt_pct(pnl_pct)})")
            print(f"  {'='*65}")

            result = auto_close_position(trade_id, mid, "STOP", bid, ask)
            if result:
                print(f"  ✅ Position auto-closed at STOP")
                closed_ids.append(trade_id)
            continue

        # ── Alert checks (approaching but not yet hit) ─────────────────

        # Approaching target
        target_progress = (mid - entry_price) / (target - entry_price) \
                          if target != entry_price else 0
        if target_progress >= (TARGET_ALERT_PCT / 100):
            remaining = target - mid
            print(f"  ⚡ TARGET APPROACHING — ${remaining:.2f} away "
                  f"({target_progress*100:.0f}% of the way there)")

        # Approaching stop
        stop_progress = (entry_price - mid) / (entry_price - stop) \
                        if entry_price != stop else 0
        if stop_progress >= (STOP_ALERT_PCT / 100):
            remaining = mid - stop
            print(f"  ⚠️  STOP APPROACHING — ${remaining:.2f} away "
                  f"({stop_progress*100:.0f}% of the way to stop)")

        # DTE warning — if expiring within 2 days
        try:
            contract_date = datetime.strptime(
                contract[-15:-9], "%y%m%d"
            ).date()
            dte = (contract_date - datetime.now(eastern).date()).days
            if dte <= 1:
                print(f"  ⏰ EXPIRING SOON — {dte} day(s) remaining")
            if dte == 0 and not is_market_open():
                # Market closed, contract expires today — auto-close as EXPIRED
                print(f"\n  ⏰ CONTRACT EXPIRING TODAY — auto-closing")
                result = auto_close_position(
                    trade_id, mid, "EXPIRED", bid, ask
                )
                if result:
                    print(f"  ✅ Position auto-closed as EXPIRED")
                    closed_ids.append(trade_id)
        except Exception:
            pass

    return closed_ids


# =============================================================================
# SUMMARY AFTER AUTO-CLOSES
# =============================================================================

def print_session_summary(auto_closed_count, check_count):
    """Print end-of-session summary."""
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)

    print(f"\n  {'='*65}")
    print(f"  📊 SESSION SUMMARY — {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"  {'='*65}")
    print(f"  Price checks run:    {check_count}")
    print(f"  Auto-closes today:   {auto_closed_count}")

    # Pull updated summary from DB
    from journal import get_paper_trade_summary
    summary = get_paper_trade_summary()

    total_pnl  = summary.get("total_pnl") or 0
    open_count = summary.get("open_count") or 0
    wins       = summary.get("wins") or 0
    losses     = summary.get("losses") or 0
    closed     = summary.get("closed_count") or 0
    win_rate   = round((wins / closed) * 100, 1) if closed > 0 else 0

    sign = "+" if total_pnl >= 0 else ""
    print(f"  Open positions:      {open_count}")
    print(f"  Total P&L:           {sign}${total_pnl:,.2f}")
    print(f"  Win rate:            {win_rate}%  ({wins}W/{losses}L)")
    print(f"  {'='*65}")


# =============================================================================
# MAIN MONITOR LOOP
# =============================================================================

def main():
    eastern         = pytz.timezone(MARKET_TIMEZONE)
    check_count     = 0
    auto_closed_total = 0

    # Auth once at startup — token valid for 60 min, refresh as needed
    token      = None
    account_id = None
    token_time = None
    TOKEN_TTL  = 55 * 60  # Refresh token every 55 minutes

    print(f"\n{'='*65}")
    print(f"  📡 POSITION MONITOR")
    print(f"  Started: {datetime.now(eastern).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Check interval: every {CHECK_INTERVAL_SECONDS // 60} minutes")
    print(f"  Auto-close: TARGET and STOP hits executed automatically")
    print(f"  Press Ctrl+C to stop")
    print(f"{'='*65}")

    try:
        while True:
            now = datetime.now(eastern)

            # Check if we should be monitoring
            if not is_monitoring_active():
                secs   = seconds_until_market_open()
                hours  = secs // 3600
                mins   = (secs % 3600) // 60
                next_open = (now + timedelta(seconds=secs)).strftime(
                    '%Y-%m-%d %H:%M %Z'
                )
                print(f"\n  Market closed. Next open: {next_open} "
                      f"({hours}h {mins}m)")
                print(f"  Sleeping until market open...")

                # Print session summary before sleeping
                print_session_summary(auto_closed_total, check_count)

                # Sleep in chunks
                sleep_chunk = min(secs, 3600)
                time.sleep(sleep_chunk)
                continue

            # Check for open positions
            positions = get_open_positions()

            if not positions:
                print(f"\n  {now.strftime('%H:%M:%S')} — "
                      f"No open positions. Sleeping {CHECK_INTERVAL_SECONDS // 60}m...")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # Refresh auth token if needed
            token_age = (time.time() - token_time) if token_time else TOKEN_TTL + 1
            if token is None or token_age >= TOKEN_TTL:
                print(f"\n  Refreshing auth token...")
                token      = get_auth_token()
                account_id = get_account_id(token) if token else None
                token_time = time.time()

                if not token or not account_id:
                    print(f"  ✗ Auth failed — will retry next cycle")
                    time.sleep(CHECK_INTERVAL_SECONDS)
                    continue

            # Fetch prices for all open contracts in one batched call
            contracts  = [t["signal_contract"] for t in positions]
            prices     = fetch_option_prices(contracts, token, account_id)

            # Evaluate and potentially auto-close
            closed_ids = evaluate_positions(positions, prices, eastern)
            auto_closed_total += len(closed_ids)
            check_count       += 1

            # Sleep until next check
            print(f"\n  ⏳ Next check in {CHECK_INTERVAL_SECONDS // 60} minutes...")
            time.sleep(CHECK_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print(f"\n\n  Monitor stopped by user.")
        print_session_summary(auto_closed_total, check_count)


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    main()