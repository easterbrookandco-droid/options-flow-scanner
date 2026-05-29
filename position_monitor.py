import time
import sqlite3
import requests
import os
import sys
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

import strategy_config as strat

load_dotenv()

SECRET_KEY    = os.getenv("PUBLIC_SECRET_KEY")
BASE_URL      = "https://api.public.com"
MARKET_TIMEZONE = "US/Eastern"

# =============================================================================
# CONFIGURATION
# =============================================================================

# How often to check prices during market hours (seconds)
CHECK_INTERVAL_SECONDS = 120  # 2 minutes

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
    """
    Fetch all active paper trades from DB.
    
    Returns both OPEN and STOP_TRIGGERED positions — STOP_TRIGGERED
    means the theoretical stop was hit but we continue tracking the
    position through expiration for data collection purposes.
    """
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM paper_trades
        WHERE status IN ('OPEN', 'STOP_TRIGGERED')
        ORDER BY entry_date ASC, entry_time ASC
    """)

    trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return trades


def get_last_snapshot_price(trade_id):
    """Most recent snapshot mid price for a trade, or None if no snapshots.
    Used as a fallback exit price when an expired contract no longer quotes."""
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    cursor.execute("""
        SELECT current_price FROM position_snapshots
        WHERE trade_id = ? AND current_price IS NOT NULL
        ORDER BY id DESC LIMIT 1
    """, (trade_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def log_price_snapshot(
    trade_id, current_price, bid, ask, pnl, pnl_pct,
    dynamic_stop=None, current_dte=None,
    market_context=None,
    stop_triggered=0, target_triggered=0,
    hurdle_crossed=0, running_max_price=None, running_max_pnl=None
):
    """
    Log a price check to position_snapshots table.
    Builds a full price history for each position over its lifetime,
    including market context and dynamic stop at each moment.

    Parameters:
        trade_id (int): Paper trade ID
        current_price (float): Mid price at snapshot time
        bid (float): Bid price
        ask (float): Ask price
        pnl (float): Current P&L in dollars
        pnl_pct (float): Current P&L as percentage
        dynamic_stop (float): Current stop threshold (DTE-aware)
        current_dte (int): Days to expiration at snapshot time
        market_context (dict): Output of fetch_market_context()
                               keyed by ticker with price/chg_pct
        stop_triggered (int): 1 if this snapshot triggered the stop
        target_triggered (int): 1 if this snapshot triggered the target
        hurdle_crossed (int): 1 once the +HURDLE_PCT hurdle has been met
                              (at this scan or any prior one), else 0
        running_max_price (float): Peak contract price since the hurdle was
                                   crossed — the level the trailing stop
                                   trails. None before the hurdle is crossed.
        running_max_pnl (float): P&L equivalent of running_max_price.
                                 None before the hurdle is crossed.
    """
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()

    eastern = pytz.timezone(MARKET_TIMEZONE)
    now = datetime.now(eastern).strftime("%Y-%m-%d %H:%M:%S")

    # Extract market context safely
    def ctx(ticker, field):
        if not market_context:
            return None
        return market_context.get(ticker, {}).get(field)

    cursor.execute("""
        INSERT INTO position_snapshots (
            trade_id, snapshot_time,
            current_price, bid, ask, pnl, pnl_pct,
            dynamic_stop, current_dte,
            spy_price, spy_chg_pct,
            qqq_price, qqq_chg_pct,
            iwm_price, iwm_chg_pct,
            tlt_price, tlt_chg_pct,
            vix_price,
            stop_triggered, target_triggered,
            hurdle_crossed, running_max_price, running_max_pnl
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?
        )
    """, (
        trade_id, now,
        current_price, bid, ask, pnl, pnl_pct,
        dynamic_stop, current_dte,
        ctx("SPY", "price"), ctx("SPY", "chg_pct"),
        ctx("QQQ", "price"), ctx("QQQ", "chg_pct"),
        ctx("IWM", "price"), ctx("IWM", "chg_pct"),
        ctx("TLT", "price"), ctx("TLT", "chg_pct"),
        ctx("VIX", "price"),
        stop_triggered, target_triggered,
        hurdle_crossed, running_max_price, running_max_pnl
    ))

    # Update max_value_seen on the trade if this is a new high
    cursor.execute("""
        UPDATE paper_trades
        SET max_value_seen = ?
        WHERE id = ?
        AND (max_value_seen IS NULL OR ? > max_value_seen)
    """, (current_price, trade_id, current_price))

    conn.commit()
    conn.close()


def fetch_market_context(token, account_id):
    """
    Fetch current prices for key market indicators.

    Called once per monitor cycle and passed to every snapshot
    so each price check has full market backdrop recorded.

    Tickers tracked:
        SPY  — S&P 500 broad market
        QQQ  — Nasdaq 100 tech-heavy
        IWM  — Russell 2000 small caps
        TLT  — 20yr Treasury bonds (inverse rate proxy)
        VIX  — CBOE Volatility Index (fear gauge)

    Returns:
        dict: Keyed by ticker, each value contains:
              price, chg_pct — or empty dict if call fails
    """
    CONTEXT_TICKERS = ["SPY", "QQQ", "IWM", "TLT"]

    try:
        response = requests.post(
            f"{BASE_URL}/userapigateway/marketdata/{account_id}/quotes",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={
                "instruments": [
                    {"symbol": t, "type": "EQUITY"} for t in CONTEXT_TICKERS
                ]
            }
        )

        if response.status_code != 200:
            return {}

        context = {}
        for q in response.json().get("quotes", []):
            if q.get("outcome") == "SUCCESS":
                symbol = q["instrument"]["symbol"]
                try:
                    last = float(q.get("last") or 0)

                    # Try API previousClose first, fall back to stored close
                    prev = float(q.get("previousClose") or 0)
                    if not prev:
                        from journal import get_previous_close
                        prev = get_previous_close(symbol) or 0

                    chg_pct = round(((last - prev) / prev) * 100, 3) if prev else 0
                    context[symbol] = {
                        "price":      last,
                        "chg_pct":    chg_pct,
                        "has_change": prev > 0,
                    }
                except (ValueError, TypeError):
                    pass

        # VIX requires a separate call as an INDEX type
        try:
            vix_resp = requests.post(
                f"{BASE_URL}/userapigateway/marketdata/{account_id}/quotes",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                },
                json={"instruments": [{"symbol": "VIX", "type": "INDEX"}]}
            )
            if vix_resp.status_code == 200:
                for q in vix_resp.json().get("quotes", []):
                    if q.get("outcome") == "SUCCESS":
                        context["VIX"] = {
                            "price":   float(q.get("last") or 0),
                            "chg_pct": None,
                        }
        except Exception:
            pass  # VIX is nice to have, not critical

        return context

    except Exception as e:
        print(f"  ✗ Market context fetch error: {e}")
        return {}
    

def mark_stop_triggered(trade_id, triggered_price, dynamic_stop):
    """
    Record that the dynamic stop threshold was crossed.
    
    Writes a formal exit record at the stop price so P&L is
    accurately captured, then sets status to STOP_TRIGGERED
    so the position continues being tracked through expiration
    for data collection purposes.
    
    This means we have two data points:
        1. exit_price / pnl — what we would have realized exiting at stop
        2. continued snapshots — what happened after the stop if held
    """
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    exit_date = now.strftime("%Y-%m-%d")
    exit_time = now.strftime("%H:%M:%S")

    # Fetch the trade to calculate P&L
    cursor.execute("""
        SELECT entry_price, contracts, total_cost
        FROM paper_trades
        WHERE id = ? AND status = 'OPEN'
    """, (trade_id,))
    trade = cursor.fetchone()

    if not trade:
        conn.close()
        return

    trade       = dict(trade)
    entry_price = trade["entry_price"]
    contracts   = trade["contracts"]
    total_cost  = trade["total_cost"]

    pnl     = round((triggered_price - entry_price) * contracts * 100, 2)
    pnl_pct = round((pnl / total_cost) * 100, 2) if total_cost else 0

    cursor.execute("""
        UPDATE paper_trades SET
            status      = 'STOP_TRIGGERED',
            exit_date   = ?,
            exit_time   = ?,
            exit_price  = ?,
            exit_reason = 'STOP',
            pnl         = ?,
            pnl_pct     = ?,
            hold_days   = julianday(?) - julianday(entry_date),
            notes       = COALESCE(notes || ' | ', '') ||
                          'Stop triggered at $' || ? ||
                          ' (threshold: $' || ? || ') on ' || ?
        WHERE id = ? AND status = 'OPEN'
    """, (
        exit_date, exit_time,
        triggered_price,
        pnl, pnl_pct,
        exit_date,
        triggered_price, dynamic_stop, now_str,
        trade_id
    ))

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


def get_dynamic_stop(entry_price, expiration_date):
    """
    Calculate the current stop price based on remaining DTE.

    Rather than a fixed stop set at entry, the stop tightens
    automatically as expiration approaches. This reflects the
    shrinking recovery window as time value bleeds away.

    Stop tiers:
        DTE > 14  : 20% of entry price — lots of runway, loose stop
        DTE 6-14  : 30% of entry price — moderate time, tighter stop
        DTE <= 5  : 50% of entry price — short fuse, cut losses fast

    Parameters:
        entry_price (float): Original entry price of the position
        expiration_date (str): Contract expiration in YYYY-MM-DD format

    Returns:
        tuple: (stop_price, current_dte)
            stop_price — the current dynamic stop threshold
            current_dte — days remaining until expiration
    """
    eastern = pytz.timezone(MARKET_TIMEZONE)
    today = datetime.now(eastern).date()

    try:
        exp_date = datetime.strptime(expiration_date, "%Y-%m-%d").date()
        dte = (exp_date - today).days
    except Exception:
        dte = 0

    if dte <= 5:
        stop_mult = 0.50
    elif dte <= 14:
        stop_mult = 0.30
    else:
        stop_mult = 0.20

    stop_price = round(entry_price * stop_mult, 2)
    return stop_price, dte


def close_expired_tracking(trade_id):
    """
    Close a STOP_TRIGGERED position that has reached expiration.
    Only updates status — preserves all exit data recorded at stop time.
    """
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE paper_trades
        SET status = 'CLOSED'
        WHERE id = ?
        AND status = 'STOP_TRIGGERED'
    """, (trade_id,))
    conn.commit()
    conn.close()


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


def get_trailing_stop_state(trade_id, hurdle_price,
                            current_price=None, current_dte=None):
    """
    Calculate two-stage trailing stop state from snapshot PRICE history.
    Stateless — recalculates from all snapshots on every cycle.

    Stage 1: Contract price must reach hurdle_price (entry + HURDLE_PCT)
             before the trailing stop activates (filters noise).
    Stage 2: Once the hurdle is crossed, track the running peak PRICE.
             The caller exits if price drops trailing_stop_pct below
             that peak. Drawdown is measured against peak PRICE, not
             peak gain — a small price pullback is a small percentage.

    The current scan's (current_price, current_dte) can be folded in so
    the returned state already reflects this scan. This lets the caller
    compute and persist the state *before* writing the snapshot row,
    avoiding a read-after-insert.

    Parameters:
        trade_id (int): Paper trade ID
        hurdle_price (float): Price that activates the trailing stop
        current_price (float): This scan's mid price (optional)
        current_dte (int): This scan's DTE (optional)

    Returns:
        tuple: (hurdle_crossed, running_max_price, trailing_stop_pct)
               running_max_price is 0.0 until the hurdle is crossed.
               trailing_stop_pct reflects the most recent DTE.
    """
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    cursor.execute("""
        SELECT current_price, current_dte FROM position_snapshots
        WHERE trade_id = ?
        AND current_price IS NOT NULL
        ORDER BY id ASC
    """, (trade_id,))
    points = [(row[0], row[1]) for row in cursor.fetchall()]
    conn.close()

    # Fold in the current scan (not yet persisted) so the state is current
    if current_price is not None:
        points.append((current_price, current_dte))

    hurdle_crossed    = False
    running_max_price = 0.0
    trailing_stop_pct = strat.trailing_stop_pct(current_dte)

    for price, snap_dte in points:
        trailing_stop_pct = strat.trailing_stop_pct(snap_dte)
        if price is None:
            continue
        if not hurdle_crossed and hurdle_price is not None and price >= hurdle_price:
            hurdle_crossed    = True
            running_max_price = price
        elif hurdle_crossed and price > running_max_price:
            running_max_price = price

    return hurdle_crossed, running_max_price, trailing_stop_pct


def evaluate_positions(positions, prices, market_context, eastern):
    """
    Evaluate all active positions against current prices.

    Handles three position states:
        OPEN           — normal monitoring, check stop and target
        STOP_TRIGGERED — stop already hit, keep tracking for data

    For OPEN positions:
        - Calculate dynamic stop based on current DTE
        - If stop crossed → mark STOP_TRIGGERED, keep tracking
        - If target hit → auto-close as TARGET
        - Log snapshot with full market context

    For STOP_TRIGGERED positions:
        - Keep logging snapshots through expiration
        - No further stop/target checks
        - Auto-close as EXPIRED when DTE hits 0

    Parameters:
        positions (list): Active trade dicts (OPEN + STOP_TRIGGERED)
        prices (dict): Current option prices keyed by contract symbol
        market_context (dict): Current market indicator prices/changes
        eastern: Eastern timezone object

    Returns:
        list: Trade IDs that were fully closed this cycle
    """
    closed_ids = []
    now_str = datetime.now(eastern).strftime("%H:%M:%S %Z")

    print(f"\n  {'─'*65}")
    print(f"  📡 POSITION CHECK — {now_str}")
    print(f"  {'─'*65}")

    for trade in positions:
        trade_id      = trade["id"]
        contract      = trade["signal_contract"]
        entry_price   = trade["entry_price"]
        contracts     = trade["contracts"]
        target        = trade["target_price"]
        total_cost    = trade["total_cost"]
        status        = trade["status"]
        thesis        = trade.get("thesis", "")

        # ── Parse expiration from contract symbol ──────────────────────
        # Format: TICKER[YYMMDD][C/P][STRIKE]
        # e.g. TSLA260513P00420000 → expiry = 2026-05-13
        try:
            date_str    = contract[-15:-9]   # YYMMDD
            expiration  = datetime.strptime(date_str, "%y%m%d").strftime("%Y-%m-%d")
        except Exception:
            expiration  = None

        # ── Dynamic stop and DTE ───────────────────────────────────────
        if expiration:
            dynamic_stop, current_dte = get_dynamic_stop(entry_price, expiration)
        else:
            dynamic_stop = trade["stop_price"]  # fallback to stored stop
            current_dte  = None

        # ── Get current price ──────────────────────────────────────────
        price_data = prices.get(contract)

        # ── Expiration close (before the missing-price skip) ───────────
        # DTE is whole calendar days (date − date), so the expiry day is
        # exactly 0 all day and < 0 means the contract has already expired.
        # Book OPEN positions as EXPIRED at close of business on the expiry
        # day, and sweep any that slipped past (monitor was down near close,
        # or the quote vanished) on a later session. Handled before the
        # price-skip below because expired contracts often no longer quote.
        if status == "OPEN" and current_dte is not None:
            now_et   = datetime.now(eastern)
            at_close = now_et.hour > 15 or (now_et.hour == 15 and now_et.minute >= 55)
            if current_dte < 0 or (current_dte == 0 and at_close):
                if price_data:
                    ex_mid = price_data["mid"]
                    ex_bid = price_data["bid"]
                    ex_ask = price_data["ask"]
                else:
                    ex_mid = get_last_snapshot_price(trade_id) or 0.0
                    ex_bid = ex_ask = ex_mid
                print(f"\n  {'='*65}")
                print(f"  ⏰ EXPIRED — #{trade_id} {contract}  (DTE {current_dte})")
                print(f"      Booking as EXPIRED at ${ex_mid:.2f}")
                print(f"  {'='*65}")
                result = auto_close_position(trade_id, ex_mid, "EXPIRED", ex_bid, ex_ask)
                if result:
                    print(f"  ✅ Closed as EXPIRED")
                    closed_ids.append(trade_id)
                continue

        if not price_data:
            print(f"\n  ⚠️  #{trade_id} {contract}")
            print(f"      Price unavailable — skipping this cycle")
            continue

        mid = price_data["mid"]
        bid = price_data["bid"]
        ask = price_data["ask"]

        # ── P&L calculations ───────────────────────────────────────────
        pnl     = round((mid - entry_price) * contracts * 100, 2)
        pnl_pct = round((pnl / total_cost) * 100, 2) if total_cost else 0

        # ── Display ────────────────────────────────────────────────────
        icon = "🟢" if pnl >= 0 else "🔴"
        status_badge = "⏸ TRACKING" if status == "STOP_TRIGGERED" else "▶ OPEN"

        print(f"\n  {icon} #{trade_id} {contract}  [{status_badge}]")
        print(f"      Entry: ${entry_price:.2f} │ "
              f"Bid: ${bid:.2f}  Mid: ${mid:.2f}  Ask: ${ask:.2f}")
        print(f"      P&L: {fmt_pnl(pnl)} ({fmt_pct(pnl_pct)}) │ "
              f"Target: ${target:.2f}  Stop: ${dynamic_stop:.2f}  DTE: {current_dte}d")

        if status == "STOP_TRIGGERED":
            print(f"      ⚠️  Stop was triggered — continuing to track for data")

        if thesis:
            print(f"      💭 {thesis[:70]}{'...' if len(thesis) > 70 else ''}")

        # ── Market context display ─────────────────────────────────────
        if market_context:
            spy = market_context.get("SPY", {})
            qqq = market_context.get("QQQ", {})
            vix = market_context.get("VIX", {})
            spy_str = f"SPY {spy.get('chg_pct', 0):+.2f}%" if spy else ""
            qqq_str = f"QQQ {qqq.get('chg_pct', 0):+.2f}%" if qqq else ""
            vix_str = f"VIX {vix.get('price', 0):.1f}" if vix else ""
            print(f"      🌍 {spy_str}  {qqq_str}  {vix_str}")

        # ── Two-stage trailing stop state (price-peak based) ───────────
        # Computed before logging so the snapshot row can record it.
        # Stage 1: price must reach hurdle_price (entry + HURDLE_PCT).
        # Stage 2: trail the running PEAK PRICE; exit on a trailing_pct
        #          drop from that peak.
        hurdle_p = trade.get("hurdle_price") or strat.hurdle_price(entry_price)
        hurdle_crossed, running_max_price, trailing_pct = get_trailing_stop_state(
            trade_id, hurdle_p, current_price=mid, current_dte=current_dte
        )
        running_max_pnl = (
            round((running_max_price - entry_price) * contracts * 100, 2)
            if hurdle_crossed else None
        )
        trail_fields = dict(
            hurdle_crossed    = 1 if hurdle_crossed else 0,
            running_max_price = running_max_price if hurdle_crossed else None,
            running_max_pnl   = running_max_pnl,
        )

        # ── Log snapshot with full context ─────────────────────────────
        log_price_snapshot(
            trade_id       = trade_id,
            current_price  = mid,
            bid            = bid,
            ask            = ask,
            pnl            = pnl,
            pnl_pct        = pnl_pct,
            dynamic_stop   = dynamic_stop,
            current_dte    = current_dte,
            market_context = market_context,
            stop_triggered = 0,
            target_triggered = 0,
            **trail_fields,
        )

        # ── Skip stop/target checks for already-triggered positions ────
        if status == "STOP_TRIGGERED":
            if current_dte is not None and current_dte <= 0:
                print(f"\n  ⏰ CONTRACT EXPIRED — closing tracking #{trade_id}")
                close_expired_tracking(trade_id)
                closed_ids.append(trade_id)
            continue

        # ── TWO-STAGE TRAILING STOP ────────────────────────────────────
        # Exit if price falls trailing_pct below the running peak PRICE.
        if hurdle_crossed and running_max_price > 0:
            drawdown = (running_max_price - mid) / running_max_price
            if drawdown > trailing_pct:
                print(f"\n  {'='*65}")
                print(f"  📉 TRAILING STOP — #{trade_id} {contract}")
                print(f"      Peak price: ${running_max_price:.2f}  "
                      f"Current: ${mid:.2f}")
                print(f"      Drawdown from peak: {drawdown*100:.1f}% > "
                      f"{trailing_pct*100:.0f}%")
                print(f"      Recording exit, continuing to track for data")
                print(f"  {'='*65}")

                log_price_snapshot(
                    trade_id=trade_id, current_price=mid,
                    bid=bid, ask=ask, pnl=pnl, pnl_pct=pnl_pct,
                    dynamic_stop=dynamic_stop, current_dte=current_dte,
                    market_context=market_context,
                    stop_triggered=1, target_triggered=0,
                    **trail_fields,
                )

                mark_stop_triggered(trade_id, mid, dynamic_stop)
                closed_ids.append(trade_id)
                continue

        # ── TIERED DTE BACKSTOP ────────────────────────────────────────
        # Last-resort floor — catches positions bleeding to zero that
        # never crossed the trailing stop hurdle.
        # Empirically validated: kills zero eventual winners.
        if status == "OPEN":
            backstop = strat.backstop_pct(current_dte)
            backstop_loss = -(total_cost * backstop)

            if pnl <= backstop_loss:
                print(f"\n  {'='*65}")
                print(f"  🚨 BACKSTOP — #{trade_id} {contract}")
                print(f"      Down {pnl_pct:.1f}% — exceeds "
                      f"{backstop*100:.0f}% DTE backstop")
                print(f"      Entry: ${entry_price:.2f} → Current: ${mid:.2f}")
                print(f"      P&L: {fmt_pnl(pnl)} ({fmt_pct(pnl_pct)})")
                print(f"  {'='*65}")

                log_price_snapshot(
                    trade_id=trade_id, current_price=mid,
                    bid=bid, ask=ask, pnl=pnl, pnl_pct=pnl_pct,
                    dynamic_stop=dynamic_stop, current_dte=current_dte,
                    market_context=market_context,
                    stop_triggered=1, target_triggered=0,
                    **trail_fields,
                )

                mark_stop_triggered(trade_id, mid, dynamic_stop)
                closed_ids.append(trade_id)
                continue

        # ── ITM SAFETY EXIT ────────────────────────────────────────────
        # DTE=0, profitable, after 3:45pm — prevent auto-exercise
        if status == "OPEN" and current_dte == 0 and pnl > 0:
            now_et = datetime.now(eastern)
            if now_et.hour > 15 or (now_et.hour == 15 and now_et.minute >= 45):
                print(f"\n  {'='*65}")
                print(f"  🔒 ITM SAFETY EXIT — #{trade_id} {contract}")
                print(f"      DTE=0, profitable at {now_et.strftime('%H:%M')} ET")
                print(f"      Preventing auto-exercise into stock position")
                print(f"      Entry: ${entry_price:.2f} → Current: ${mid:.2f}")
                print(f"      P&L: {fmt_pnl(pnl)} ({fmt_pct(pnl_pct)})")
                print(f"  {'='*65}")

                log_price_snapshot(
                    trade_id=trade_id, current_price=mid,
                    bid=bid, ask=ask, pnl=pnl, pnl_pct=pnl_pct,
                    dynamic_stop=dynamic_stop, current_dte=current_dte,
                    market_context=market_context,
                    stop_triggered=0, target_triggered=1,
                    **trail_fields,
                )

                mark_stop_triggered(trade_id, mid, dynamic_stop)
                closed_ids.append(trade_id)
                continue

        # ── Approaching alerts ─────────────────────────────────────────
        target_progress = (mid - entry_price) / (target - entry_price) \
            if target != entry_price else 0
        if target_progress >= (TARGET_ALERT_PCT / 100):
            print(f"  ⚡ TARGET APPROACHING — "
                  f"${target - mid:.2f} away ({target_progress*100:.0f}%)")

        stop_progress = (entry_price - mid) / (entry_price - dynamic_stop) \
            if entry_price != dynamic_stop else 0
        if stop_progress >= (STOP_ALERT_PCT / 100):
            print(f"  ⚠️  STOP APPROACHING — "
                  f"${mid - dynamic_stop:.2f} away ({stop_progress*100:.0f}%)")

        # ── Expiration warning ─────────────────────────────────────────
        if current_dte is not None and current_dte <= 1:
            print(f"  ⏰ EXPIRING SOON — {current_dte} day(s) remaining")
        # Note: expiration close is handled near the top of the loop, before
        # the missing-price skip (see "Expiration close" block above).

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
    from logger_setup import setup_logger
    log_path = setup_logger("monitor")
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
            contracts = [t["signal_contract"] for t in positions]
            prices = fetch_option_prices(contracts, token, account_id)

            # Fetch market context once per cycle — shared across all positions
            market_context = fetch_market_context(token, account_id)

            # Evaluate and potentially auto-close
            closed_ids = evaluate_positions(positions, prices, market_context, eastern)

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
