# -*- coding: utf-8 -*-
import time
import sqlite3
from blinker import signal
import requests
import os
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

from strategy_config import OPTIMIZING_MAX_ASK_PER_CONTRACT

load_dotenv()

SECRET_KEY  = os.getenv("PUBLIC_SECRET_KEY")
BASE_URL    = "https://api.public.com"
ACCOUNT_ID  = "5LT39200"
MARKET_TIMEZONE = "US/Eastern"

# =============================================================================
# CONFIGURATION
# =============================================================================

# How often the agent checks for new signals (seconds)
CHECK_INTERVAL_SECONDS = 300  # 5 minutes

# Staleness threshold — if current ask has moved more than this %
# above the scanned ask, the signal is too stale to enter
# e.g. 0.25 = skip if ask is more than 25% higher than scan price
STALENESS_THRESHOLD = 0.25

# Minimum score gap between top two candidates to enter
# If two signals are within this gap, skip both — no edge cases
MIN_SCORE_GAP = 0.25

# Maximum ask price per contract — budget ceiling per position.
# Sourced from strategy_config (OPTIMIZING_MAX_ASK_PER_CONTRACT) so this cap
# lives alongside the other strategy knobs but is referenced only here.
MAX_ASK_PER_CONTRACT = OPTIMIZING_MAX_ASK_PER_CONTRACT

# Market indicators fetched each cycle for context and thesis
CONTEXT_TICKERS = ["SPY", "QQQ", "IWM", "TLT"]

# =============================================================================
# OPTIMIZING MODE FILTERS
# =============================================================================

MODE = "OPTIMIZING"
MIN_COMPOSITE_SCORE = 6.0
MAX_COMPOSITE_SCORE = 7.9
MIN_PREMIUM = 100_000
MAX_PREMIUM = 2_000_000
ALLOWED_DTE_RANGES = [(1, 2), (6, 14)]

# =============================================================================
# AGENT STATE
# =============================================================================

# Track how many positions entered this session for terminal output
session_entries   = 0
session_skips     = 0
session_errors    = 0


# =============================================================================
# AUTHENTICATION
# =============================================================================

def get_auth_token():
    """Get a fresh access token from Public API."""
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


# =============================================================================
# SIGNAL QUERIES
# =============================================================================

def get_qualified_signals_today():
    """
    Fetch today's QUALIFIED signals from the DB that the agent
    hasn't already acted on.

    A signal is eligible if:
        - Logged today
        - Signal tier is HIGH or INST (scanner already filtered these)
        - Not already entered as a paper trade today
        - Contract is not already in an open or stop-triggered position

    Returns:
        list: Signal dicts ordered by composite_score descending
    """
    from journal import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    today = datetime.now(pytz.timezone(MARKET_TIMEZONE)).strftime("%Y-%m-%d")

    # Fetch today's HIGH and INST signals
    cursor.execute("""
        SELECT s.*
        FROM signals s
        WHERE s.scan_time LIKE ?
        AND s.signal_tier IN ('HIGH', 'INST')
        AND s.composite_score > 0

        -- Exclude 0DTE contracts — expiration date is today
        AND s.expiration != ?

        AND s.contract NOT IN (
            SELECT signal_contract
            FROM paper_trades
            WHERE entry_date = ?
        )
        AND s.contract NOT IN (
            SELECT signal_contract
            FROM paper_trades
            WHERE status IN ('OPEN', 'STOP_TRIGGERED')
        )
        ORDER BY s.composite_score DESC
    """, (f"{today}%", today, today))

    signals = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return signals


def get_open_position_tickers():
    """
    Return set of tickers currently in open or stop-triggered positions.
    Used to prevent entering multiple positions on the same ticker.
    """
    from journal import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT DISTINCT signal_contract
        FROM paper_trades
        WHERE status IN ('OPEN', 'STOP_TRIGGERED')
    """)

    contracts = [row["signal_contract"] for row in cursor.fetchall()]
    conn.close()

    # Parse ticker from contract symbol — everything before the date portion
    # e.g. TSLA260513P00420000 → TSLA
    # e.g. SPY260507P00734000 → SPY
    tickers = set()
    for contract in contracts:
        # Date portion is 6 digits — find where it starts
        for i, ch in enumerate(contract):
            if ch.isdigit():
                tickers.add(contract[:i])
                break

    return tickers


# =============================================================================
# LIVE PRICE FETCH
# =============================================================================

def fetch_live_option_price(contract, token, account_id):
    """
    Fetch current bid/ask/mid for a single option contract.

    Used by the agent to verify price hasn't moved too far
    from the scanned ask before entering a position.

    Parameters:
        contract (str): Contract symbol e.g. "TSLA260513P00420000"
        token (str): Valid access token
        account_id (str): Brokerage account ID

    Returns:
        dict: {bid, ask, mid, last} or None if fetch fails
    """
    try:
        response = requests.post(
            f"{BASE_URL}/userapigateway/marketdata/{account_id}/quotes",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={
                "instruments": [
                    {"symbol": contract, "type": "OPTION"}
                ]
            }
        )

        if response.status_code != 200:
            return None

        for q in response.json().get("quotes", []):
            if q.get("outcome") == "SUCCESS":
                try:
                    bid  = float(q.get("bid") or 0)
                    ask  = float(q.get("ask") or 0)
                    last = float(q.get("last") or 0)
                    mid  = round((bid + ask) / 2, 4) if bid and ask else last
                    return {"bid": bid, "ask": ask, "mid": mid, "last": last}
                except (ValueError, TypeError):
                    return None

    except Exception as e:
        print(f"  ✗ Price fetch error for {contract}: {e}")

    return None


def fetch_market_context(token, account_id):
    """
    Fetch current prices for key market indicators.

    Called once per agent cycle and attached to every entry
    so each paper trade has full market backdrop at entry time.

    Returns:
        dict: Keyed by ticker with price and chg_pct,
              or empty dict if call fails
    """
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

        return context

    except Exception as e:
        print(f"  ✗ Market context error: {e}")
        return {}


# =============================================================================
# STALENESS CHECK
# =============================================================================

def is_signal_stale(signal, live_price):
    """
    Determine if a signal is too stale to enter.

    Compares the ask price at scan time against the current
    live ask. If the price has moved up more than the staleness
    threshold, the easy money is likely gone and we skip.

    We only check upward moves — if the ask has dropped, that's
    actually a better entry and we should still take it.

    Parameters:
        signal (dict): Signal record from DB with 'ask' field
        live_price (dict): Current price data from fetch_live_option_price()

    Returns:
        tuple: (is_stale, reason_string)
    """
    scanned_ask = float(signal.get("ask") or 0)
    live_ask    = float(live_price.get("ask") or 0)

    # Can't evaluate if we have no prices
    if scanned_ask == 0 or live_ask == 0:
        return True, "Missing price data — cannot verify staleness"

    # If ask has dropped or stayed flat — better or equal entry
    if live_ask <= scanned_ask:
        return False, f"Ask dropped ${scanned_ask:.2f} → ${live_ask:.2f} — better entry"

    # Calculate how much it moved up
    pct_move = (live_ask - scanned_ask) / scanned_ask

    if pct_move > STALENESS_THRESHOLD:
        return True, (
            f"Ask moved +{pct_move*100:.1f}% since scan "
            f"(${scanned_ask:.2f} → ${live_ask:.2f}) — "
            f"exceeds {STALENESS_THRESHOLD*100:.0f}% threshold"
        )

    return False, (
        f"Ask moved +{pct_move*100:.1f}% since scan "
        f"(${scanned_ask:.2f} → ${live_ask:.2f}) — within threshold"
    )

# =============================================================================
# PARSE DTE FROM CONTRACT
# =============================================================================
def get_dte_from_contract(contract):
    """
    Parse days to expiration directly from contract symbol.
    More reliable than relying on cached _dte key.

    Format: TICKER[YYMMDD][C/P][STRIKE]
    e.g. TSLA260511P00430000 → exp 2026-05-11 → DTE calculated from today

    Parameters:
        contract (str): Full contract symbol

    Returns:
        int: Days to expiration, or 0 if parsing fails
    """
    try:
        date_str = contract[-15:-9]
        exp_date = datetime.strptime(date_str, "%y%m%d").date()
        eastern  = pytz.timezone(MARKET_TIMEZONE)
        return (exp_date - datetime.now(eastern).date()).days
    except Exception:
        return 0


# =============================================================================
# ENTRY DECISION LOGIC
# =============================================================================

def select_entry_candidate(signals, open_tickers):
    """
    Apply the entry decision framework to today's qualified signals.

    Rules (in order):
        1. Filter out signals on tickers already in open positions
        2. Filter out 0DTE contracts — no time buffer
        3. Apply OPTIMIZING mode filters (score range, premium range, DTE range)
        4. Filter out contracts exceeding MAX_ASK_PER_CONTRACT
        5. If no candidates remain — wait
        6. If one candidate remains — enter it
        7. If multiple candidates remain:
               Top two must be separated by MIN_SCORE_GAP
               If gap is too small — skip all, wait for clearer signal
               If gap is sufficient — enter the top scorer

    The "no edge cases" principle: when in doubt, do nothing.
    A missed trade is better than a low-confidence trade.

    Parameters:
        signals (list): Today's QUALIFIED signals from DB,
                        ordered by composite_score DESC
        open_tickers (set): Tickers currently in open positions

    Returns:
        tuple: (candidate, reason, skipped_list)
            candidate    — signal dict to enter, or None
            reason       — string explaining the decision
            skipped_list — list of (signal, reason) tuples for logging
    """
    skipped = []

    # ── Step 1: Ticker deduplication — DISABLED for paper trading ─────
    # Re-enable for live trading to prevent correlated risk and
    # burning through budget on multiple positions per ticker.
    # ─────────────────────────────────────────────────────────────────
    # eligible = []
    # for s in signals:
    #     ticker = s.get("ticker", "")
    #     if ticker in open_tickers:
    #         skipped.append((s, f"Already holding {ticker} position"))
    #     else:
    #         eligible.append(s)
    # if not eligible:
    #     return None, "No eligible signals — all tickers already in open positions", skipped
    # ─────────────────────────────────────────────────────────────────
    eligible = list(signals)  # Paper trading: enter all eligible signals

    if not eligible:
        return None, "No eligible signals found", skipped

    # ── Step 2: Filter 0DTE ───────────────────────────────────────────
    non_zero_dte = []
    for s in eligible:
        contract = s.get("contract", "")
        try:
            date_str = contract[-15:-9]
            exp_date = datetime.strptime(date_str, "%y%m%d").date()
            eastern  = pytz.timezone(MARKET_TIMEZONE)
            dte      = (exp_date - datetime.now(eastern).date()).days
            s["_dte"] = dte  # cache for later use
            if dte == 0:
                skipped.append((s, f"0DTE — no time buffer"))
            else:
                non_zero_dte.append(s)
        except Exception:
            skipped.append((s, "Could not parse expiration date"))

    if not non_zero_dte:
        return None, "No eligible signals — all remaining are 0DTE", skipped

    eligible = non_zero_dte

    # ── Step 3: OPTIMIZING mode filters ──────────────────────────────
    dte_range_str = ", ".join(f"{lo}-{hi}d" for lo, hi in ALLOWED_DTE_RANGES)
    optimizing_filtered = []
    for s in eligible:
        score = float(s.get("composite_score", 0))
        if score < MIN_COMPOSITE_SCORE or score > MAX_COMPOSITE_SCORE:
            skipped.append((
                s,
                f"Score {score} outside OPTIMIZING range "
                f"[{MIN_COMPOSITE_SCORE}, {MAX_COMPOSITE_SCORE}]"
            ))
            continue

        premium = float(s.get("premium", 0))
        if premium < MIN_PREMIUM or premium > MAX_PREMIUM:
            prem_str = f"${premium/1000:.0f}K" if premium < 1_000_000 else f"${premium/1_000_000:.1f}M"
            skipped.append((
                s,
                f"Premium {prem_str} outside OPTIMIZING range "
                f"[${MIN_PREMIUM//1000}K, ${MAX_PREMIUM//1_000_000:.0f}M]"
            ))
            continue

        dte = s.get("_dte", 0)
        in_range = any(lo <= dte <= hi for lo, hi in ALLOWED_DTE_RANGES)
        if not in_range:
            skipped.append((
                s,
                f"DTE {dte} not in OPTIMIZING allowed ranges [{dte_range_str}]"
            ))
            continue

        optimizing_filtered.append(s)

    if not optimizing_filtered:
        return None, "No eligible signals — all filtered by OPTIMIZING mode constraints", skipped

    eligible = optimizing_filtered

    # ── Step 4: Filter by max ask price ───────────────────────────────
    # Note: uses scanned ask as proxy — live price verified at entry time
    affordable = []
    for s in eligible:
        ask = float(s.get("ask") or 0)
        if ask > MAX_ASK_PER_CONTRACT:
            skipped.append((
                s,
                f"Ask ${ask:.2f} exceeds ceiling ${MAX_ASK_PER_CONTRACT:.2f}"
            ))
        else:
            affordable.append(s)

    if not affordable:
        return None, "No eligible signals — all exceed ask price ceiling", skipped

    eligible = affordable

    # ── Step 5: Single candidate ──────────────────────────────────────
    if len(eligible) == 1:
        return eligible[0], "Single qualified candidate — clear entry", skipped

    # ── Step 6: Multiple candidates — check score gap ─────────────────
    top    = eligible[0]
    second = eligible[1]

    top_score    = float(top.get("composite_score", 0))
    second_score = float(second.get("composite_score", 0))
    gap          = round(top_score - second_score, 3)

    # ── Score gap check — DISABLED for paper trading data collection ──
    # Re-enable once scoring model is refined empirically.
    # Need data first to know what gap threshold actually predicts
    # better outcomes before enforcing it as an entry filter.
    # ─────────────────────────────────────────────────────────────────
    # if gap < MIN_SCORE_GAP and gap != 0.0:
    #     skipped.append((top,    f"Score gap too small ({gap:.2f} < {MIN_SCORE_GAP}) — skipping"))
    #     skipped.append((second, f"Score gap too small ({gap:.2f} < {MIN_SCORE_GAP}) — skipping"))
    #     return None, (
    #         f"Top two signals too close in score "
    #         f"({top_score} vs {second_score}, gap {gap:.2f}) — "
    #         f"no clear winner, waiting for next scan"
    #     ), skipped

    if gap == 0.0:
        # Exact tie — fall through to tiebreaker logic below
        pass

    # Clear winner — enter the top scorer
    # If scores are exactly equal, use premium as tiebreaker
    if gap == 0.0:
        top_premium    = float(top.get("premium", 0))
        second_premium = float(second.get("premium", 0))
        prem_gap       = abs(top_premium - second_premium)
        prem_gap_pct   = prem_gap / max(top_premium, second_premium) if max(top_premium, second_premium) > 0 else 0

        if prem_gap_pct < 0.10:
            # Score and premium both tied — use DTE as final tiebreaker
            # Prefer longer-dated contract for more recovery runway
            top_dte    = top.get("_dte") or get_dte_from_contract(top.get("contract", ""))
            second_dte = second.get("_dte") or get_dte_from_contract(second.get("contract", ""))
            print(f"  DEBUG: top_dte={top_dte} second_dte={second_dte} top_contract={top.get('contract')} second_contract={second.get('contract')}")

            if top_dte == second_dte:
                # Truly identical — nothing to choose, skip both
                skipped.append((top,    f"Score, premium, and DTE all tied — skipping"))
                skipped.append((second, f"Score, premium, and DTE all tied — skipping"))
                return None, (
                    f"Perfect tie on score, premium, and DTE — "
                    f"no basis for decision, waiting for next scan"
                ), skipped

            # Pick longer DTE
            if second_dte > top_dte:
                top, second = second, top
                top_dte, second_dte = second_dte, top_dte

            return top, (
                f"Score and premium tied — DTE breaks tie "
                f"({top_dte}d vs {second_dte}d, preferring longer expiration)"
            ), skipped

        # Premium breaks the tie — pick higher premium
        if second_premium > top_premium:
            top, second = second, top
            top_premium, second_premium = second_premium, top_premium

        return top, (
            f"Score tie ({top_score}) broken by premium "
            f"(${top_premium/1000:.0f}K vs ${second_premium/1000:.0f}K)"
        ), skipped

    return top, (
        f"Entering top scorer — score {top_score} vs next {second_score} "
        f"(gap {gap:.2f} — gap check disabled for data collection)"
    ), skipped


# =============================================================================
# ENTRY EXECUTION
# =============================================================================

def build_rule_based_thesis(signal, live_price, market_context, dte):
    """
    Generate a plain-language thesis from signal data.
    Used as fallback if thesis_generator.py fails.

    Parameters:
        signal (dict): Signal record from DB
        live_price (dict): Current price data
        market_context (dict): Current market indicators
        dte (int): Days to expiration

    Returns:
        str: One-sentence thesis
    """
    ticker        = signal.get("ticker", "")
    contract_type = signal.get("contract_type", "")
    score         = signal.get("composite_score", 0)
    premium       = signal.get("premium", 0)
    tier          = signal.get("signal_tier", "")
    direction     = "bullish" if contract_type == "CALL" else "bearish"

    # Premium display
    if premium >= 1_000_000:
        prem_str = f"${premium/1_000_000:.1f}M"
    else:
        prem_str = f"${premium/1_000:.0f}K"

    # Market context
    spy = market_context.get("SPY", {})
    spy_str = ""
    if spy.get("has_change"):
        chg = spy.get("chg_pct", 0)
        spy_str = f", SPY {chg:+.2f}%"

    return (
        f"{tier} {ticker} {contract_type.lower()} signal — "
        f"score {score}, {prem_str} premium, {dte} DTE, "
        f"{direction} thesis{spy_str}. "
        f"Agent auto-entry at ${live_price.get('mid', 0):.2f} mid."
    )


def execute_entry(signal, token, account_id, market_context):
    """
    Execute a paper trade entry for a qualified signal.

    Steps:
        1. Fetch live price
        2. Check staleness
        3. Check ask ceiling
        4. Generate thesis (AI first, rule-based fallback)
        5. Insert paper trade record
        6. Print detailed entry synopsis

    Parameters:
        signal (dict): Signal record from DB
        token (str): Valid access token
        account_id (str): Brokerage account ID
        market_context (dict): Current market indicators

    Returns:
        tuple: (success, trade_id, reason)
            success  — True if trade was entered
            trade_id — ID of new paper trade, or None
            reason   — string explaining outcome
    """
    global session_entries, session_errors

    contract      = signal.get("contract", "")
    ticker        = signal.get("ticker", "")
    contract_type = signal.get("contract_type", "")
    score         = signal.get("composite_score", 0)
    premium       = signal.get("premium", 0)
    tier          = signal.get("signal_tier", "")
    dte           = signal.get("_dte") or get_dte_from_contract(contract)
    signal_id     = signal.get("id")

    # ── Step 1: Fetch live price ───────────────────────────────────────
    live_price = fetch_live_option_price(contract, token, account_id)
    if not live_price:
        session_errors += 1
        return False, None, "Could not fetch live price — skipping"

    # ── Step 2: Staleness check ────────────────────────────────────────
    stale, stale_reason = is_signal_stale(signal, live_price)
    if stale:
        return False, None, f"Signal stale — {stale_reason}"

    # ── Step 3: Ask ceiling check (live price) ─────────────────────────
    live_ask = live_price.get("ask", 0)
    if live_ask > MAX_ASK_PER_CONTRACT:
        return False, None, (
            f"Live ask ${live_ask:.2f} exceeds ceiling "
            f"${MAX_ASK_PER_CONTRACT:.2f} — skipping"
        )

    # Use mid price as entry price — realistic fill assumption
    entry_price = live_price.get("mid", 0)
    if entry_price <= 0:
        session_errors += 1
        return False, None, "Invalid mid price — skipping"

    # ── Step 4: Generate thesis ────────────────────────────────────────
    thesis = None

    # Try AI thesis first
    try:
        from thesis_generator import generate_thesis

        # Build greeks dict from signal fields
        greeks = {
            "delta":     signal.get("delta_raw", "unavailable"),
            "iv":        signal.get("iv", "unavailable"),
            "moneyness": "ITM" if signal.get("delta_raw", 0) and abs(float(signal.get("delta_raw", 0))) > 0.5 else "OTM",
        }

        # Build flow bias from contract type
        is_call = signal.get("contract_type", "") == "CALL"
        flow_bias = {
            "call_pct":   100 if is_call else 0,
            "put_pct":    0 if is_call else 100,
            "bias_label": "BULLISH" if is_call else "BEARISH",
        }

        # Build market overview from context we already have
        market_overview = {}
        for ctx_ticker in ["SPY", "QQQ", "IWM"]:
            ctx = market_context.get(ctx_ticker, {})
            if ctx:
                chg = ctx.get("chg_pct", 0)
                market_overview[ctx_ticker] = {
                    "price":    ctx.get("price", 0),
                    "chg_pct":  chg,
                    "has_change": ctx.get("has_change", False),
                    "sign":     "+" if chg >= 0 else "",
                }

        thesis = generate_thesis(signal, greeks, market_overview, flow_bias)

    except Exception as e:
        print(f"  ⚠️  Thesis generator failed ({e}) — using rule-based fallback")

    # Rule-based fallback
    if not thesis:
        thesis = build_rule_based_thesis(signal, live_price, market_context, dte)

    # ── Step 5: Insert paper trade ─────────────────────────────────────
    try:
        from journal import insert_paper_trade

        # Parse expiration for DTE and delta/IV context
        iv_at_entry    = signal.get("impliedVolatility") or signal.get("iv")
        delta_at_entry = signal.get("delta") or signal.get("delta_raw")

        # Market bias from context
        spy = market_context.get("SPY", {})
        qqq = market_context.get("QQQ", {})
        avg_chg = ((spy.get("chg_pct", 0) + qqq.get("chg_pct", 0)) / 2)
        market_bias = (
            "BULLISH" if avg_chg > 0.3
            else "BEARISH" if avg_chg < -0.3
            else "NEUTRAL"
        )

        # Ticker bias from contract type vs signal tier
        ticker_bias = (
            "BULLISH" if contract_type == "CALL"
            else "BEARISH"
        )

        trade_id = insert_paper_trade(
            signal_contract          = contract,
            entry_price              = entry_price,
            contracts                = 1,
            thesis                   = thesis,
            entry_underlying_price   = signal.get("share_price"),
            verdict_at_entry         = "QUALIFIED",
            score_at_entry           = score,
            dte_at_entry             = dte,
            iv_at_entry              = iv_at_entry,
            delta_at_entry           = delta_at_entry,
            market_bias_at_entry     = market_bias,
            ticker_bias_at_entry     = ticker_bias,
            market_snapshot          = market_context,
            signal_id                = signal_id,
            mode                     = MODE,
        )

        session_entries += 1

        # ── Step 6: Print entry synopsis ──────────────────────────────
        eastern = pytz.timezone(MARKET_TIMEZONE)
        now_str = datetime.now(eastern).strftime("%H:%M:%S %Z")

        print(f"\n  {'='*65}")
        print(f"  🤖 OPTIMIZING AGENT ENTRY — {now_str}")
        print(f"  {'='*65}")
        print(f"  Contract : {contract}")
        print(f"  Ticker   : {ticker}  {contract_type}  DTE: {dte}d")
        print(f"  Entry    : ${entry_price:.2f} mid  "
              f"(Bid: ${live_price['bid']:.2f} / Ask: ${live_price['ask']:.2f})")
        print(f"  Score    : {score}  Tier: {tier}  Premium: ", end="")
        if premium >= 1_000_000:
            print(f"${premium/1_000_000:.1f}M")
        else:
            print(f"${premium/1_000:.0f}K")
        print(f"  Market   : SPY {spy.get('chg_pct', 0):+.2f}%  "
              f"QQQ {qqq.get('chg_pct', 0):+.2f}%  "
              f"Bias: {market_bias}")
        print(f"  Thesis   : {thesis}")
        print(f"  Trade ID : #{trade_id}")
        print(f"  {'='*65}")

        return True, trade_id, "Entry executed successfully"

    except Exception as e:
        session_errors += 1
        return False, None, f"Insert failed — {e}"


# =============================================================================
# MARKET HOURS
# =============================================================================

def is_market_open():
    """Check if US market is currently open."""
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now = datetime.now(eastern)
    if now.weekday() > 4:
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now < market_close


def seconds_until_market_open():
    """Seconds until next market open."""
    eastern  = pytz.timezone(MARKET_TIMEZONE)
    now      = datetime.now(eastern)
    next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now >= next_open:
        next_open += timedelta(days=1)
    while next_open.weekday() > 4:
        next_open += timedelta(days=1)
    return max(0, int((next_open - now).total_seconds()))


# =============================================================================
# MAIN LOOP
# =============================================================================

def main():
    global session_entries, session_skips, session_errors

    eastern = pytz.timezone(MARKET_TIMEZONE)

    # Token management
    token        = None
    account_id   = None
    token_time   = None
    TOKEN_TTL    = 55 * 60  # Refresh every 55 minutes

    dte_range_str = ", ".join(f"{lo}-{hi}d" for lo, hi in ALLOWED_DTE_RANGES)

    print(f"\n{'='*65}")
    print(f"  🤖 OPTIMIZING AGENT")
    print(f"  Started: {datetime.now(eastern).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Check interval  : every {CHECK_INTERVAL_SECONDS // 60} minutes")
    print(f"  Min score gap   : {MIN_SCORE_GAP}")
    print(f"  Staleness limit : {STALENESS_THRESHOLD*100:.0f}%")
    print(f"  Max ask ceiling : ${MAX_ASK_PER_CONTRACT:.2f}")
    print(f"  {'─'*61}")
    print(f"  OPTIMIZING FILTERS")
    print(f"  Score range     : {MIN_COMPOSITE_SCORE} – {MAX_COMPOSITE_SCORE}")
    print(f"  Premium range   : ${MIN_PREMIUM//1000}K – ${MAX_PREMIUM//1_000_000:.0f}M")
    print(f"  DTE ranges      : {dte_range_str}")
    print(f"  Mode            : {MODE}")
    print(f"  {'─'*61}")
    print(f"  Press Ctrl+C to stop")
    print(f"{'='*65}")

    try:
        while True:
            now = datetime.now(eastern)

            # ── Market hours check ─────────────────────────────────────
            if not is_market_open():
                secs      = seconds_until_market_open()
                hours     = secs // 3600
                mins      = (secs % 3600) // 60
                next_open = (now + timedelta(seconds=secs)).strftime(
                    "%Y-%m-%d %H:%M %Z"
                )
                print(f"\n  Market closed. Next open: {next_open} "
                      f"({hours}h {mins}m)")
                print(f"  Session totals — "
                      f"Entries: {session_entries}  "
                      f"Skips: {session_skips}  "
                      f"Errors: {session_errors}")

                # Sleep in 1-hour chunks so we wake up near market open
                time.sleep(min(secs, 3600))
                continue

            # ── Auth token refresh ─────────────────────────────────────
            token_age = (time.time() - token_time) if token_time else TOKEN_TTL + 1
            if token is None or token_age >= TOKEN_TTL:
                print(f"\n  Refreshing auth token...")
                token      = get_auth_token()
                account_id = get_account_id(token) if token else None
                token_time = time.time()

                if not token or not account_id:
                    print(f"  ✗ Auth failed — will retry in "
                          f"{CHECK_INTERVAL_SECONDS // 60} minutes")
                    time.sleep(CHECK_INTERVAL_SECONDS)
                    continue

            # ── Fetch market context once per cycle ────────────────────
            market_context = fetch_market_context(token, account_id)

            # ── Query eligible signals ─────────────────────────────────
            signals = get_qualified_signals_today()
            # open_tickers = get_open_position_tickers()

            now_str = now.strftime("%H:%M:%S %Z")
            print(f"\n  {'─'*65}")
            print(f"  🔍 OPTIMIZING AGENT SCAN — {now_str}")
            print(f"  Eligible signals: {len(signals)}")
            print(f"  {'─'*65}")

            if not signals:
                print(f"  No new QUALIFIED signals to evaluate — waiting")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # ── Entry decision ─────────────────────────────────────────
            candidate, decision_reason, skipped_list = select_entry_candidate(
                signals, set()  # Pass empty set — ticker filter disabled for paper trading
                # signals, open_tickers  # Restore for live trading
            )

            # Print skipped signals with reasons
            if skipped_list:
                print(f"\n  Signals evaluated and skipped:")
                for skipped_signal, skip_reason in skipped_list:
                    print(f"    ✗ {skipped_signal.get('contract', '')} "
                          f"— {skip_reason}")

            # Print decision reason
            print(f"\n  Decision: {decision_reason}")

            if candidate is None:
                session_skips += 1
                print(f"  No entry this cycle — "
                      f"session totals: {session_entries} entered, "
                      f"{session_skips} skipped")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # ── Execute entry ──────────────────────────────────────────
            success, trade_id, entry_reason = execute_entry(
                candidate, token, account_id, market_context
            )

            if not success:
                session_skips += 1
                print(f"\n  ✗ Entry not executed — {entry_reason}")
            else:
                print(f"\n  ✅ Session totals — "
                      f"Entries: {session_entries}  "
                      f"Skips: {session_skips}  "
                      f"Errors: {session_errors}")

            # ── Sleep until next cycle ─────────────────────────────────
            print(f"\n  ⏳ Next scan in "
                  f"{CHECK_INTERVAL_SECONDS // 60} minutes...")
            time.sleep(CHECK_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print(f"\n\n  Optimizing agent stopped by user.")
        print(f"  {'='*65}")
        print(f"  📊 FINAL SESSION SUMMARY")
        print(f"  {'='*65}")
        print(f"  Entries : {session_entries}")
        print(f"  Skips   : {session_skips}")
        print(f"  Errors  : {session_errors}")
        print(f"  {'='*65}")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    main()
