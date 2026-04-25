import requests
import os
import time
import pytz
from dotenv import load_dotenv
from datetime import datetime, timezone
from journal import init_database, log_signal, check_duplicate, display_recent_signals, display_logging_summary, log_scan_event
from thesis_generator import generate_thesis

# Load our secret key from the .env file
load_dotenv()
SECRET_KEY = os.getenv("PUBLIC_SECRET_KEY")

BASE_URL = "https://api.public.com"

# =============================================================================
# CONFIGURATION
# =============================================================================

# Our target universe — high liquidity names with deep options markets
# These are stocks where unusual flow is meaningful, not random noise
WATCHLIST = [
    # Mega-cap tech — deepest options markets
    "AAPL", "NVDA", "MSFT", "AMZN", "META", "GOOGL", "TSLA",
    
    # Broad market ETFs — essential for macro flow detection
    "SPY", "QQQ", "IWM",
    
    # Financial sector
    "JPM", "GS", "BAC",
    
    # Other high-conviction names
    "AMD", "NFLX", "CRM", "UBER",
    
    # Sector ETFs
    "XLF", "XLE"
]

# Number of nearest expirations to scan per ticker
# 4 covers roughly the 0-4 week conviction window
EXPIRATIONS_TO_SCAN = 4

# Delay between API calls in seconds
# Keeps us well within Public's 10 requests/second rate limit
API_DELAY = 0.15

# Timezone for all market hour calculations
MARKET_TIMEZONE = "US/Eastern"


# =============================================================================
# STEP 1: AUTHENTICATION
# Exchange our secret key for a temporary access token
# =============================================================================

def get_access_token():
    """
    Exchange the secret key for a short-lived access token.
    Public's API requires this two-step authentication process.
    The token is valid for the number of minutes we request.
    
    Returns:
        str: The access token, or None if authentication failed
    """
    
    url = f"{BASE_URL}/userapiauthservice/personal/access-tokens"
    
    payload = {
        "secret": SECRET_KEY,
        "validityInMinutes": 60  # Token valid for 1 hour
    }
    
    print("Authenticating with Public API...")
    
    response = requests.post(url, json=payload)
    
    if response.status_code != 200:
        print(f"Authentication failed: {response.status_code}")
        print(f"Response: {response.text}")
        return None
    
    token = response.json().get("accessToken")
    print("Authentication successful.\n")
    return token


# =============================================================================
# STEP 2: GET ACCOUNT ID
# We need the account ID to make market data requests
# =============================================================================

def get_account_id(token):
    """
    Fetch account information and extract the account ID.
    Public requires the account ID for market data endpoints.
    
    Parameters:
        token (str): The access token from authentication
    
    Returns:
        str: The account ID, or None if the request failed
    """
    
    url = f"{BASE_URL}/userapigateway/trading/account"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        print(f"Failed to fetch account: {response.status_code}")
        print(f"Response: {response.text}")
        return None
    
    data = response.json()
    accounts = data.get("accounts", [])
    
    if not accounts:
        print("No accounts found.")
        return None
    
    # Display ALL accounts so we can see what's available
    print(f"Accounts found: {len(accounts)}\n")

    brokerage_account = None

    for i, account in enumerate(accounts):
        account_id = account.get("accountId")
        account_type = account.get("accountType")
        options_level = account.get("optionsLevel")
        permissions = account.get("tradePermissions")
    
        print(f"Account #{i+1}:")
        print(f"  Account ID:    {account_id}")
        print(f"  Account Type:  {account_type}")
        print(f"  Options Level: {options_level}")
        print(f"  Permissions:   {permissions}\n")
    
        # Automatically select the brokerage account
        if account_type == "BROKERAGE":
            brokerage_account = account

    if brokerage_account:
        selected_id = brokerage_account.get("accountId")
        print(f"Selected brokerage account: {selected_id}\n")
        return selected_id
    else:
        print("No brokerage account found. Using first account.")
        return accounts[0].get("accountId")


# =============================================================================
# STEP 3: FETCH OPTION CHAIN AND GREEKS
# Get all contracts for a stock on a specific expiration date
# =============================================================================

def get_option_expirations(token, account_id, ticker):
    """
    Fetch available expiration dates for a given stock's options.
    
    Parameters:
        token (str): The access token
        account_id (str): The Public account ID
        ticker (str): The stock symbol e.g. "AAPL"
    
    Returns:
        list: Available expiration dates as strings
    """
    
    url = f"{BASE_URL}/userapigateway/marketdata/{account_id}/option-expirations"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "instrument": {
            "symbol": ticker,
            "type": "EQUITY"
        }
    }

    
    response = requests.post(url, json=payload, headers=headers)
    
    
    if response.status_code != 200:
        print(f"  ✗ Expirations request failed (status {response.status_code})")
        return []
    
    data = response.json()
    expirations = data.get("expirations", [])
    return expirations


def fetch_option_chain(token, account_id, ticker, expiration_date):
    """
    Fetch the full options chain for a stock on a given expiration date.
    Returns all calls and puts with volume, open interest, and pricing.
    
    Parameters:
        token (str): The access token
        account_id (str): The Public account ID
        ticker (str): The stock symbol e.g. "AAPL"
        expiration_date (str): Expiration date in YYYY-MM-DD format
    
    Returns:
        dict: The full option chain response containing calls and puts
    """
    
    url = f"{BASE_URL}/userapigateway/marketdata/{account_id}/option-chain"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "instrument": {
            "symbol": ticker,
            "type": "EQUITY"
        },
        "expirationDate": expiration_date
    }
    
    print(f"Fetching option chain for {ticker} expiring {expiration_date}...")
    
    response = requests.post(url, json=payload, headers=headers)
    
    if response.status_code != 200:
        print(f"  ✗ Option chain request failed (status {response.status_code})")
        return {}
    
    return response.json()

def fetch_greeks(token, account_id, contracts):
    """
    Fetch Greeks for a list of options contracts.
    
    Uses a GET request with osiSymbols as query parameters.
    Max 250 contracts per request per Public's API limits.
    
    Greeks returned:
        delta: Price sensitivity to $1 stock move (0 to 1 calls, -1 to 0 puts)
        theta: Daily time decay in dollars per contract
        impliedVolatility: Market's expected future volatility (annualized %)
        gamma: Rate of delta change (how fast delta moves)
        vega: Sensitivity to volatility changes
    
    Parameters:
        token (str): Access token
        account_id (str): Brokerage account ID
        contracts (list): List of contract symbols e.g. ["AAPL260420P00270000"]
    
    Returns:
        dict: Greeks keyed by contract symbol for easy lookup
    """
    
    if not contracts:
        return {}
    
    url = f"{BASE_URL}/userapigateway/option-details/{account_id}/greeks"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    params = {"osiSymbols": contracts}
    
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code != 200:
        print(f"  ✗ Greeks request failed ({response.status_code}): {response.text[:100]}")
        return {}
    
    data = response.json()
    greeks_list = data.get("greeks", [])
    
    # Key by symbol for easy lookup
    greeks_by_symbol = {}
    for item in greeks_list:
        symbol = item.get("symbol", "")
        if symbol:
            greeks_by_symbol[symbol] = item.get("greeks", {})
    
    return greeks_by_symbol


def get_share_prices(tickers, token, account_id):
    """
    Fetch current share prices for a list of tickers in one batched call.
    Uses Public's quotes endpoint with instrument type EQUITY.

    Called once per scan run after all signals are collected. Batching
    all tickers into a single request keeps API overhead minimal — one
    call covers the entire watchlist regardless of how many signals fired.

    Parameters:
        tickers (list): List of ticker strings e.g. ["AAPL", "NVDA"]
        token (str): Valid access token
        account_id (str): Brokerage account ID

    Returns:
        dict: Last trade price keyed by ticker e.g. {"AAPL": 213.42}
              Returns empty dict if the call fails — non-fatal, signals
              still log normally with share_price=None.
    """

    if not tickers:
        return {}

    try:
        url = f"{BASE_URL}/userapigateway/marketdata/{account_id}/quotes"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        body = {
            "instruments": [
                {"symbol": t, "type": "EQUITY"} for t in tickers
            ]
        }

        response = requests.post(url, headers=headers, json=body)

        if response.status_code != 200:
            print(f"  ✗ Share price fetch failed: {response.status_code}")
            return {}

        prices = {}
        for q in response.json().get("quotes", []):
            if q.get("outcome") == "SUCCESS":
                symbol = q["instrument"]["symbol"]
                last = q.get("last")
                if last is not None:
                    try:
                        prices[symbol] = float(last)
                    except (ValueError, TypeError):
                        pass

        print(f"  ✓ Share prices fetched for {len(prices)} tickers")
        return prices

    except Exception as e:
        print(f"  ✗ Share price error: {e}")
        return {}
    
    
def get_market_overview(token, account_id):
    """
    Fetch current prices for key market indicators.
    Called once per scan run for context in the terminal output.

    Tickers:
        SPY  — S&P 500
        QQQ  — Nasdaq 100
        IWM  — Russell 2000 (small caps)
        TLT  — 20yr Treasury bonds (inverse relationship to rates)
        GLD  — Gold
        USO  — Crude oil (WTI proxy)
        GDX  — Gold miners (risk sentiment)
        BITX — 2x Bitcoin ETF (crypto proxy, available as equity)

    Returns:
        dict: Keyed by ticker with price and day change data,
              empty dict if call fails.
    """

    MARKET_TICKERS = ["SPY", "QQQ", "IWM", "TLT", "GLD", "USO"]

    try:
        url = f"{BASE_URL}/userapigateway/marketdata/{account_id}/quotes"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        body = {
            "instruments": [
                {"symbol": t, "type": "EQUITY"} for t in MARKET_TICKERS
            ]
        }

        response = requests.post(url, headers=headers, json=body)

        if response.status_code != 200:
            print(f"  ✗ Market overview fetch failed: {response.status_code}")
            return {}

        overview = {}
        for q in response.json().get("quotes", []):
            if q.get("outcome") == "SUCCESS":
                symbol = q["instrument"]["symbol"]
                try:
                    last     = float(q.get("last") or 0)
                    prev     = float(q.get("previousClose") or 0)
                    chg      = last - prev if prev else 0
                    chg_pct  = (chg / prev * 100) if prev else 0
                    overview[symbol] = {
                        "price":    last,
                        "change":   chg,
                        "chg_pct":  chg_pct,
                        "arrow":    "▲" if chg >= 0 else "▼",
                        "sign":     "+" if chg >= 0 else "",
                        "has_change": prev > 0, # flag for display
                    }
                except (ValueError, TypeError):
                    pass

        return overview

    except Exception as e:
        print(f"  ✗ Market overview error: {e}")
        return {}


def print_market_overview(overview):
    """
    Print a compact market overview table to the terminal.
    Called at the start of each scan run.
    """

    if not overview:
        print("  Market overview unavailable")
        return

    LABELS = {
        "SPY": "S&P 500 (SPY)",
        "QQQ": "Nasdaq  (QQQ)",
        "IWM": "Russell (IWM)",
        "TLT": "Bonds   (TLT)",
        "GLD": "Gold    (GLD)",
        "USO": "Crude   (USO)",
    }

    print(f"\n  {'─'*48}")
    print(f"  📈 MARKET OVERVIEW")
    print(f"  {'─'*48}")

    for ticker, label in LABELS.items():
        if ticker not in overview:
            continue
        d = overview[ticker]
        color_marker = "🟢" if d["change"] >= 0 else "🔴"
        print(f"  {color_marker} {label:<16} "
              f"${d['price']:>8.2f}   "
              f"{d['arrow']} {d['sign']}{d['change']:.2f} "
              f"({d['sign']}{d['chg_pct']:.2f}%)")
        if d.get("has_change"):
            print(f"  {color_marker} {label:<16} "
                  f"${d['price']:>8.2f}   "
                  f"{d['arrow']} {d['sign']}{d['change']:.2f} "
                  f"({d['sign']}{d['chg_pct']:.2f}%)")
        else:
            print(f"  {color_marker} {label:<16} "
                  f"${d['price']:>8.2f}   "
                  f"(change unavailable after hours)")

    print(f"  {'─'*48}")


# Per-ticker IV baselines — rough "normal" ranges based on historical behavior
# We'll refine these as our own data accumulates
IV_BASELINES = {
    "SPY":  {"low": 10, "moderate": 20, "high": 35},
    "QQQ":  {"low": 12, "moderate": 22, "high": 38},
    "IWM":  {"low": 15, "moderate": 25, "high": 40},
    "AAPL": {"low": 20, "moderate": 32, "high": 50},
    "NVDA": {"low": 35, "moderate": 55, "high": 80},
    "TSLA": {"low": 40, "moderate": 65, "high": 90},
    "AMZN": {"low": 20, "moderate": 32, "high": 50},
    "META": {"low": 22, "moderate": 35, "high": 55},
    "MSFT": {"low": 18, "moderate": 28, "high": 45},
    "GOOGL":{"low": 20, "moderate": 30, "high": 48},
    "JPM":  {"low": 18, "moderate": 28, "high": 45},
    "GS":   {"low": 20, "moderate": 30, "high": 48},
    "AMD":  {"low": 35, "moderate": 55, "high": 80},
    "NFLX": {"low": 25, "moderate": 40, "high": 60},
}
IV_BASELINES_DEFAULT = {"low": 20, "moderate": 35, "high": 55}


def evaluate_trade_quality(signal, all_signals_today, market_overview):
    """
    Evaluate a HIGH signal against the decision framework.
    Returns a formatted assessment string for terminal output.

    Framework rules:
        1. Score threshold met for DTE band
        2. Premium threshold met
        3. DTE in actionable range (5-14 days preferred, 0DTE auto-skip)
        4. IV not already spiked relative to ticker baseline
        5. Directional lean — signal type matches both ticker flow
           AND overall market flow

    Parameters:
        signal (dict): A logged signal dict from the DB
        all_signals_today (list): All signals from today's scan
                                  for flow bias calculation
        market_overview (dict): Output of get_market_overview()

    Returns:
        str: Multi-line formatted assessment block
    """

    ticker        = signal.get("ticker", "")
    contract      = signal.get("contract", "")
    contract_type = signal.get("contract_type", "")  # CALL or PUT
    score         = signal.get("composite_score", 0)
    premium       = signal.get("premium", 0)
    iv_raw        = signal.get("iv", 0) or 0
    dte           = signal.get("decoded", {}).get("days_out", 0) \
                    if "decoded" in signal \
                    else 999

    lines   = []
    checks  = []
    verdict = "QUALIFIED"


    # ── Check 0: Contract character — deep ITM filter ─────────────────────
    # Contracts with |delta| >= 0.95 behave like stock, not options
    # They're extremely expensive and offer no real leverage benefit
    delta_raw = signal.get("delta_raw", 0) or 0
    if abs(delta_raw) >= 0.95:
        checks.append(
            f"❌ Deep ITM (delta {delta_raw:+.3f}) — "
            f"behaves like stock, not an options trade. "
            f"Ask price likely far exceeds budget."
        )
        verdict = "SKIP"

    
    # ── Check 1: DTE ──────────────────────────────────────────────────────
    if dte == 0:
        checks.append("❌ 0DTE — avoid, no time buffer")
        verdict = "SKIP"
    elif dte <= 2:
        checks.append(f"⚠️  Very short DTE ({dte}d) — high risk, fast resolution")
    elif dte <= 14:
        checks.append(f"✅ DTE in range ({dte}d — target 5–14)")
    else:
        checks.append(f"⚠️  Long DTE ({dte}d) — slower to resolve, "
                      f"more theta budget")

    # ── Check 2: Score ────────────────────────────────────────────────────
    if score >= 8:
        checks.append(f"✅ Score strong ({score})")
    elif score >= 6:
        checks.append(f"✅ Score meets threshold ({score})")
    else:
        checks.append(f"⚠️  Score marginal ({score})")

    # ── Check 3: Premium ──────────────────────────────────────────────────
    if premium >= 5_000_000:
        checks.append(f"✅ Premium institutional (${premium/1e6:.1f}M)")
    elif premium >= 1_000_000:
        checks.append(f"✅ Premium strong (${premium/1e6:.1f}M)")
    else:
        checks.append(f"⚠️  Premium moderate (${premium/1e3:.0f}K)")

# ── Check 4: IV vs baseline ───────────────────────────────────────────
    baseline = IV_BASELINES.get(ticker, IV_BASELINES_DEFAULT)
    iv_pct   = iv_raw * 100 if iv_raw <= 5 else iv_raw

    if iv_pct == 0 and delta_raw is not None and abs(delta_raw) >= 0.95:
        # Deep ITM — IV of zero is accurate, not missing
        # Flag it as a characteristic, not a data gap
        checks.append(
            f"ℹ️  Deep ITM contract (delta {delta_raw:+.3f}) — "
            f"IV near zero is expected, intrinsic value dominates"
        )
        # Deep ITM contracts are expensive per contract — flag for budget check
        if verdict not in ("SKIP",):
            checks.append(
                "⚠️  Deep ITM — verify ask price fits $300-600 budget "
                "before considering entry"
            )
    elif iv_pct == 0:
        # Genuine missing data
        checks.append("⚠️  IV unavailable — Greeks endpoint returned no data")
    elif iv_pct > baseline["high"]:
        checks.append(
            f"❌ IV spiked ({iv_pct:.0f}% vs {ticker} "
            f"baseline high {baseline['high']}%) — expensive entry"
        )
        if verdict != "SKIP":
            verdict = "CAUTION"
    elif iv_pct > baseline["moderate"]:
        checks.append(
            f"⚠️  IV elevated ({iv_pct:.0f}%) — above normal "
            f"for {ticker}, price accordingly"
        )
    else:
        checks.append(
            f"✅ IV moderate ({iv_pct:.0f}%) — reasonable entry cost"
        )

    # ── Check 5: Directional lean ─────────────────────────────────────────
    # 5a: Does this signal match ticker's dominant flow today?
    ticker_signals = [s for s in all_signals_today
                      if s.get("ticker") == ticker
                      and s.get("signal_tier") in ["HIGH", "INST"]]
    ticker_calls   = sum(1 for s in ticker_signals
                         if s.get("contract_type") == "CALL")
    ticker_puts    = sum(1 for s in ticker_signals
                         if s.get("contract_type") == "PUT")
    ticker_total   = ticker_calls + ticker_puts

    if ticker_total > 0:
        ticker_call_pct = ticker_calls / ticker_total * 100
        ticker_put_pct  = ticker_puts  / ticker_total * 100
        ticker_bias     = ("BULLISH" if ticker_call_pct > 55
                           else "BEARISH" if ticker_put_pct > 55
                           else "NEUTRAL")
    else:
        ticker_bias = "NEUTRAL"

    signal_direction = "BULLISH" if contract_type == "CALL" else "BEARISH"

    ticker_aligned = (
        ticker_bias == "NEUTRAL" or
        ticker_bias == signal_direction
    )

    # 5b: Does ticker bias align with overall market (SPY/QQQ)?
    spy  = market_overview.get("SPY", {})
    qqq  = market_overview.get("QQQ", {})
    spy_chg  = spy.get("chg_pct", 0)
    qqq_chg  = qqq.get("chg_pct", 0)
    avg_mkt  = (spy_chg + qqq_chg) / 2

    market_bias = ("BULLISH" if avg_mkt > 0.3
                   else "BEARISH" if avg_mkt < -0.3
                   else "NEUTRAL")

    market_aligned = (
        market_bias == "NEUTRAL" or
        market_bias == signal_direction
    )

    if ticker_aligned and market_aligned:
        checks.append(
            f"✅ Directional lean confirmed — signal ({signal_direction}), "
            f"ticker flow ({ticker_bias}), market ({market_bias}) all aligned"
        )
    elif ticker_aligned and not market_aligned:
        checks.append(
            f"⚠️  Partial lean — signal matches ticker flow ({ticker_bias}) "
            f"but market trending {market_bias}"
        )
        if verdict == "QUALIFIED":
            verdict = "REVIEW"
    elif not ticker_aligned and market_aligned:
        checks.append(
            f"⚠️  Partial lean — signal matches market ({market_bias}) "
            f"but ticker flow is {ticker_bias}"
        )
        if verdict == "QUALIFIED":
            verdict = "REVIEW"
    else:
        checks.append(
            f"❌ Directional conflict — signal is {signal_direction} but "
            f"ticker flow is {ticker_bias} and market is {market_bias}"
        )
        if verdict not in ("SKIP",):
            verdict = "CAUTION"

    # ── Suggested position size ───────────────────────────────────────────
    # Based on verdict — very rough educational guidance only
    if verdict == "SKIP":
        sizing = "Position size: $0 — do not trade"
    elif verdict == "CAUTION":
        sizing = "Position size: paper trade only until conditions improve"
    elif verdict == "REVIEW":
        sizing = "Position size: 1 contract max, risk no more than $300"
    else:
        sizing = "Position size: 1–2 contracts, risk $300–600 max"

    # ── Assemble output ───────────────────────────────────────────────────
    verdict_icon = {
        "SKIP":      "🚫",
        "CAUTION":   "⚠️ ",
        "REVIEW":    "🔍",
        "QUALIFIED": "✅",
    }.get(verdict, "❓")

    lines.append(f"\n  {'·'*60}")
    lines.append(f"  📋 TRADE ASSESSMENT: {contract}")
    for c in checks:
        lines.append(f"     {c}")
    lines.append(f"     💰 {sizing}")
    lines.append(f"     {verdict_icon} VERDICT: {verdict}")
    lines.append(f"  {'·'*60}")

    return "\n".join(lines)


# =============================================================================
# STEP 4: DISPLAY AND ANALYZE
# Surface the most interesting contracts by volume/OI ratio
# =============================================================================

def analyze_and_display(chain_data, ticker, expiration_date, quiet=False, share_prices=None):
    """
    Analyze the options chain and surface contracts showing
    unusual activity based on our signal criteria.
    
    Our primary signal here: Volume/OI ratio
    High ratio = fresh money entering a position today
    
    Parameters:
        chain_data (dict): The raw option chain from the API
        ticker (str): The stock ticker for display
        expiration_date (str): The expiration date for display
        quiet (bool): Suppress per-contract display output
        share_prices (dict): Optional dict of ticker -> share price at scan time.
                             Passed through to log_signal() for DB storage.
                             If None or ticker not present, logs as NULL.
    """
    
    calls = chain_data.get("calls", [])
    puts = chain_data.get("puts", [])
    
    if not quiet:
        print(f"\n{'='*70}")
        print(f"  Options Chain: {ticker} — Expiration {expiration_date}")
        print(f"  {len(calls)} Calls | {len(puts)} Puts")
        print(f"{'='*70}")
        print(f"\n  {'Symbol':<30} {'Type':<6} {'Ask':<8} {'Volume':<10} {'OI':<10} {'Vol/OI':<8} {'Premium':<14} {'Score':<7} Signal")
        print(f"  {'-'*95}")
    
    # Combine calls and puts into one list for unified analysis
    # We tag each with its type so we can display it clearly
    all_contracts = []
    
    for contract in calls:
        contract["_type"] = "CALL"
        all_contracts.append(contract)
    
    for contract in puts:
        contract["_type"] = "PUT"
        all_contracts.append(contract)
    
    # Filter out contracts with no volume — no activity, no signal
    active_contracts = [
        c for c in all_contracts
        if c.get("volume", 0) > 0
    ]
    
    # Calculate Vol/OI ratio for each contract
    # Protect against division by zero
    for contract in active_contracts:
        volume = contract.get("volume", 0)
        oi = contract.get("openInterest", 0)
        
        # Vol/OI ratio — measures unusual activity relative to existing positions
        contract["_vol_oi_ratio"] = round(volume / oi, 2) if oi > 0 else 0
        
        # Premium dollar value — measures real money commitment
        # We use the ask price as a conservative estimate of what buyers paid
        # Each contract controls 100 shares, so multiply by 100
        ask = float(contract.get("ask", 0) or 0)
        contract["_premium"] = ask * volume * 100
        
        # Composite score — combines both signals
        # A high ratio AND high premium = strongest signal
        # A high ratio with tiny premium = likely noise
        ratio_score = min(contract["_vol_oi_ratio"] / 10, 5)  # Cap at 5 points
        
        # Premium score — tiered by dollar value
        # Under $100K = 0 points (noise filter)
        # $100K-$500K = 1 point
        # $500K-$1M = 2 points
        # $1M-$5M = 3 points
        # Over $5M = 4 points
        premium = contract["_premium"]
        if premium < 100_000:
            premium_score = 0
        elif premium < 500_000:
            premium_score = 1
        elif premium < 1_000_000:
            premium_score = 2
        elif premium < 5_000_000:
            premium_score = 3
        else:
            premium_score = 4
        
        contract["_composite_score"] = round(ratio_score + premium_score, 2)
    
    # Sort by composite score — highest combined signal first
    active_contracts.sort(key=lambda x: x["_composite_score"], reverse=True)

    # Tag each contract with its ticker, tier, and expiration for cross-expiration summary
    for contract in active_contracts:
        contract["_expiration"] = expiration_date
        contract["_ticker"] = ticker
        
        # Tag signal tier for master summary
        score   = contract["_composite_score"]
        premium = contract["_premium"]

        # DTE-adjusted thresholds
        # Longer-dated options accumulate more OI, naturally producing
        # lower Vol/OI ratios. Loosen thresholds as DTE increases so
        # genuine conviction flow isn't filtered out on weekly/monthly contracts.
        try:
            exp_date = datetime.strptime(expiration_date, "%Y-%m-%d")
            eastern  = pytz.timezone(MARKET_TIMEZONE)
            dte      = (exp_date.date() - datetime.now(eastern).date()).days
        except Exception:
            dte = 0

        if dte == 0:
            high_score_thresh, high_prem_thresh = 6, 1_000_000
            inst_prem_thresh                    = 5_000_000
            watch_score_thresh, watch_prem_thresh = 3, 100_000
        elif dte <= 2:
            high_score_thresh, high_prem_thresh = 5, 750_000
            inst_prem_thresh                    = 4_000_000
            watch_score_thresh, watch_prem_thresh = 2.5, 75_000
        elif dte <= 7:
            high_score_thresh, high_prem_thresh = 4, 500_000
            inst_prem_thresh                    = 3_000_000
            watch_score_thresh, watch_prem_thresh = 2, 50_000
        else:
            high_score_thresh, high_prem_thresh = 3, 300_000
            inst_prem_thresh                    = 2_000_000
            watch_score_thresh, watch_prem_thresh = 1.5, 30_000

        if score >= high_score_thresh and premium >= high_prem_thresh:
            contract["_signal_tier"] = "HIGH"
        elif premium >= inst_prem_thresh:
            contract["_signal_tier"] = "INST"
        elif score >= watch_score_thresh and premium >= watch_prem_thresh:
            contract["_signal_tier"] = "WATCH"
        else:
            contract["_signal_tier"] = "NONE"
    
    
    # -------------------------------------------------------------------------
    # LOGGING — always runs regardless of quiet mode
    # We log all significant signals to the journal every scan
    # -------------------------------------------------------------------------
    
    today = datetime.now().strftime("%Y-%m-%d")

    # Resolve share price for this ticker once — None if unavailable
    share_price = (share_prices or {}).get(ticker)
    
    for contract in active_contracts:
        
        symbol = contract.get("instrument", {}).get("symbol", "N/A")
        c_type = contract["_type"]
        bid = contract.get("bid", "0")
        ask = contract.get("ask", "0")
        volume = contract.get("volume", 0)
        oi = contract.get("openInterest", 0)
        ratio = contract["_vol_oi_ratio"]
        score = contract["_composite_score"]
        premium = contract["_premium"]
        tier = contract.get("_signal_tier", "NONE")
        
        # Only log meaningful signals
        should_log = (
            tier in ["HIGH", "INST", "WATCH"]
        )

        if should_log:
            if not check_duplicate(symbol, today):
                try:
                    strike = float(symbol[-8:]) / 1000
                except:
                    strike = 0
                
                log_signal(
                    ticker=ticker,
                    contract=symbol,
                    contract_type=c_type,
                    strike=strike,
                    expiration=expiration_date,
                    bid=float(bid or 0),
                    ask=float(ask or 0),
                    volume=volume,
                    open_interest=oi,
                    vol_oi_ratio=ratio,
                    premium=premium,
                    composite_score=score,
                    signal_tier=tier,
                    share_price=share_price
                )

    # -------------------------------------------------------------------------
    # DISPLAY — only runs when not in quiet mode
    # Shows the full chain table with individual contract rows
    # -------------------------------------------------------------------------
    
    if not quiet:
        for contract in active_contracts[:15]:
            
            symbol = contract.get("instrument", {}).get("symbol", "N/A")
            c_type = contract["_type"]
            ask = contract.get("ask", "0")
            volume = contract.get("volume", 0)
            oi = contract.get("openInterest", 0)
            ratio = contract["_vol_oi_ratio"]
            score = contract["_composite_score"]
            premium = contract["_premium"]
            tier = contract.get("_signal_tier", "NONE")
            
            if tier == "HIGH":
                signal = "🔥 HIGH"
            elif tier == "INST":
                signal = "💰 INST"
            elif tier == "WATCH":
                signal = "⚡ WATCH"
            else:
                signal = "—"
            
            if premium >= 1_000_000:
                premium_display = f"${premium/1_000_000:.1f}M"
            elif premium >= 1_000:
                premium_display = f"${premium/1_000:.0f}K"
            else:
                premium_display = f"${premium:.0f}"
            
            print(f"  {symbol:<30} {c_type:<6} ${ask:<7} {volume:<10} {oi:<10} {ratio:<8} {premium_display:<14} {score:<7} {signal}")
        
        # Summary counts
        high_conviction = [c for c in active_contracts
                          if c["_composite_score"] >= 6 and c["_premium"] >= 1_000_000]
        institutional = [c for c in active_contracts
                        if c["_premium"] >= 5_000_000 and c not in high_conviction]
        watch_list = [c for c in active_contracts
                     if c["_composite_score"] >= 3 and c["_premium"] >= 100_000
                     and c not in high_conviction and c not in institutional]
        
        print(f"\n  Summary:")
        print(f"  🔥 High conviction (Score ≥ 6, Premium ≥ $1M):  {len(high_conviction)}")
        print(f"  💰 Institutional  (Premium ≥ $5M):               {len(institutional)}")
        print(f"  ⚡ Watch list     (Score ≥ 3, Premium ≥ $100K):  {len(watch_list)}")
        print(f"  Total active contracts:                           {len(active_contracts)}")

    # Return high conviction signals for cross-expiration aggregation
    return [c for c in active_contracts if c.get("_signal_tier") in ["HIGH", "INST", "WATCH"]]


# =============================================================================
# STEP 5: UTILITY CALL
# Delay the next API call to maintain rate limits
# =============================================================================

def safe_api_call(func, *args, **kwargs):
    """
    Wrapper that adds a small delay before every API call.
    This keeps us within rate limits when scanning many tickers.
    
    The time.sleep() pauses execution for API_DELAY seconds.
    Small enough to be fast, large enough to be safe.
    """
    time.sleep(API_DELAY)
    return func(*args, **kwargs)


# =============================================================================
# MAIN — Orchestrates all the steps
# =============================================================================

def main():
    
    print("\n" + "="*70)
    print("  🔍 OPTIONS FLOW SCANNER")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Scanning {len(WATCHLIST)} tickers × {EXPIRATIONS_TO_SCAN} expirations")
    print("="*70)
    
    # Step 1: Authenticate
    token = get_access_token()
    if not token:
        print("✗ Authentication failed. Check your API key.")
        return
    
    # Step 2: Get brokerage account ID
    account_id = get_account_id(token)
    if not account_id:
        print("✗ No brokerage account found.")
        return
    
    # Step 3: Initialize journal database
    print("\nInitializing signal journal...")
    init_database()

    # Step 4: Fetch market overview once — used for context and trade assessments
    print("\n  Fetching market overview...")
    market_overview = get_market_overview(token, account_id)
    print_market_overview(market_overview)
    
    # Step 5: Scan each ticker in the watchlist
    all_signals = []
    scan_errors = []
    
    for i, ticker in enumerate(WATCHLIST, 1):
        
        print(f"\n[{i}/{len(WATCHLIST)}] Scanning {ticker}...")
        
        expirations = safe_api_call(
            get_option_expirations, token, account_id, ticker
        )
        
        if not expirations:
            print(f"  ✗ No expirations found for {ticker} — skipping")
            scan_errors.append(ticker)
            continue
        
        expirations_to_scan = expirations[:EXPIRATIONS_TO_SCAN]
        ticker_signals = []
        
        for expiration in expirations_to_scan:
            
            chain = safe_api_call(
                fetch_option_chain, token, account_id, ticker, expiration
            )
            
            if not chain:
                continue
            
            # share_prices not available yet at this stage — fetched after
            # the full scan completes and passed in for the logging re-pass below
            signals = analyze_and_display(chain, ticker, expiration, quiet=True)
            
            if signals:
                ticker_signals.extend(signals)
        
        if ticker_signals:
            all_signals.extend(ticker_signals)
        
        high = len([s for s in ticker_signals if s.get("_signal_tier") == "HIGH"])
        inst = len([s for s in ticker_signals if s.get("_signal_tier") == "INST"])
        watch = len([s for s in ticker_signals if s.get("_signal_tier") == "WATCH"])
        
        if high + inst + watch > 0:
            print(f"  → 🔥 {high} HIGH  💰 {inst} INST  ⚡ {watch} WATCH")
        else:
            print(f"  → No significant signals")

    # -------------------------------------------------------------------------
    # Step 5b: Fetch share prices for all tickers that produced signals
    # One batched call covers everything — done after the scan so we don't
    # add latency to the per-ticker loop. Signals are already logged at this
    # point, so we UPDATE the share_price column for today's records.
    # -------------------------------------------------------------------------

    tickers_with_signals = list({
        s.get("_ticker") for s in all_signals if s.get("_ticker")
    })

    share_prices = {}
    if tickers_with_signals:
        print(f"\n  Fetching share prices for {len(tickers_with_signals)} tickers...")
        time.sleep(API_DELAY)
        share_prices = get_share_prices(tickers_with_signals, token, account_id)

        if share_prices:
            # UPDATE share_price on records already written this scan
            # Signals are logged during analyze_and_display() before we have
            # prices, so we patch them in now with a targeted UPDATE.
            import sqlite3
            from journal import DB_PATH
            today = datetime.now().strftime("%Y-%m-%d")
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            updated_total = 0
            for ticker, price in share_prices.items():
                cursor.execute("""
                    UPDATE signals
                    SET share_price = ?
                    WHERE ticker = ?
                    AND scan_time LIKE ?
                    AND share_price IS NULL
                """, (price, ticker, f"{today}%"))
                updated_total += cursor.rowcount
            conn.commit()
            conn.close()
            print(f"  ✓ Share prices written to {updated_total} signal records")
    
    # Step 6: Master summary
    print(f"\n\n{'='*70}")
    print(f"  🎯 MASTER SIGNAL SUMMARY")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}")
    
    if all_signals:
        
        # Sort by composite score descending
        all_signals.sort(key=lambda x: x["_composite_score"], reverse=True)
        
        # Fetch Greeks for top HIGH signals only
        top_symbols = [
            c.get("instrument", {}).get("symbol", "")
            for c in all_signals[:20]
            if c.get("_signal_tier") == "HIGH"
            and c.get("instrument", {}).get("symbol", "")
        ]
        
        greeks_data = {}
        if top_symbols:
            print(f"\n  Fetching Greeks for {len(top_symbols)} HIGH signals...")
            time.sleep(API_DELAY)
            greeks_data = fetch_greeks(token, account_id, top_symbols)
            
            if greeks_data:
                print(f"  ✓ Greeks retrieved for {len(greeks_data)} contracts")
            else:
                print(f"  ✗ Greeks unavailable — displaying without")
        
        # Master summary table with Greeks
        print(f"\n  {'Ticker':<8} {'Contract':<30} {'Type':<6} {'Premium':<12} {'Score':<7} {'Delta':<8} {'Theta':<8} {'IV':<8} {'Stock':<10} Tier")
        print(f"  {'-'*105}")
        
        for contract in all_signals[:20]:
            symbol = contract.get("instrument", {}).get("symbol", "N/A")
            c_type = contract.get("_type", "N/A")
            premium = contract["_premium"]
            score = contract["_composite_score"]
            tier = contract.get("_signal_tier", "N/A")
            ticker = contract.get("_ticker", "N/A")
            
            if premium >= 1_000_000:
                premium_display = f"${premium/1_000_000:.1f}M"
            elif premium >= 1_000:
                premium_display = f"${premium/1_000:.0f}K"
            else:
                premium_display = f"${premium:.0f}"
            
            tier_icon = "🔥" if tier == "HIGH" else "💰" if tier == "INST" else "⚡"
            
            # Pull Greeks if available for this contract
            greeks = greeks_data.get(symbol, {})
            
            try:
                delta = float(greeks.get("delta", 0) or 0)
                theta = float(greeks.get("theta", 0) or 0)
                iv = float(greeks.get("impliedVolatility", 0) or 0)
            except (ValueError, TypeError):
                delta = theta = iv = 0
            
            delta_display = f"{delta:+.3f}" if delta != 0 else "—"
            theta_display = f"{theta:.3f}" if theta != 0 else "—"
            iv_display = f"{iv*100:.1f}%" if iv != 0 else "—"

            # Share price at scan time
            price = share_prices.get(ticker)
            price_display = f"${price:,.2f}" if price else "—"
            
            # Delta context — tells us ITM/ATM/OTM character of the signal
            abs_delta = abs(delta)
            if delta == 0:
                delta_context = ""
            elif abs_delta >= 0.7:
                delta_context = " ITM"
            elif abs_delta >= 0.3:
                delta_context = " ATM"
            else:
                delta_context = " OTM"
            
            print(f"  {ticker:<8} {symbol:<30} {c_type:<6} {premium_display:<12} {score:<7} {delta_display:<8} {theta_display:<8} {iv_display:<8} {price_display:<10} {tier_icon} {tier}{delta_context}")
        
        # Directional bias — HIGH signals only
        high_signals_only = [s for s in all_signals if s.get("_signal_tier") == "HIGH"]
        all_calls = [s for s in high_signals_only if s.get("_type") == "CALL"]
        all_puts = [s for s in high_signals_only if s.get("_type") == "PUT"]
        call_premium = sum(s["_premium"] for s in all_calls)
        put_premium = sum(s["_premium"] for s in all_puts)
        total_premium = call_premium + put_premium
        
        print(f"\n  {'─'*50}")
        print(f"  📊 Directional Flow Summary (HIGH signals only)")
        print(f"  {'─'*50}")
        print(f"  Bullish (Calls): {len(all_calls):>3} signals  ${call_premium/1_000_000:.1f}M premium")
        print(f"  Bearish (Puts):  {len(all_puts):>3} signals  ${put_premium/1_000_000:.1f}M premium")
        
        if total_premium > 0:
            call_pct = round((call_premium / total_premium) * 100, 1)
            put_pct = round((put_premium / total_premium) * 100, 1)
            bias = "BULLISH" if call_pct > 55 else "BEARISH" if put_pct > 55 else "NEUTRAL"
            print(f"  Flow Bias:       {call_pct}% calls / {put_pct}% puts → {bias}")

        # ── Trade quality assessments for HIGH signals ──
        print(f"\n\n  {'='*60}")
        print(f"  📋 TRADE QUALITY ASSESSMENTS — HIGH SIGNALS")
        print(f"  {'='*60}")
        print(f"  Framework: Score ✓  Premium ✓  DTE 5-14d  "
              f"IV moderate  Flow aligned")

        from journal import get_todays_signals_for_assessment
        todays_db_signals = get_todays_signals_for_assessment()

        eastern = pytz.timezone(MARKET_TIMEZONE)
        today   = datetime.now(eastern).date()

        # Build candidate list — all HIGH signals
        high_signals_for_assessment = [
            c for c in all_signals if c.get("_signal_tier") == "HIGH"
        ]

        # Pass 1 — evaluate every candidate, collect (verdict, output, score, dte)
        VERDICT_RANK = {"QUALIFIED": 0, "REVIEW": 1, "CAUTION": 2, "SKIP": 3}

        evaluated = []
        thesis_count = 0
        for contract in high_signals_for_assessment:
            symbol = contract.get("instrument", {}).get("symbol", "")
            ticker = contract.get("_ticker", "")
            exp    = contract.get("_expiration", "2099-01-01")
            try:
                dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
            except Exception:
                dte = 0

            raw_g     = greeks_data.get(symbol, {})
            delta_raw = float(raw_g.get("delta") or 0)
            iv_val    = float(raw_g.get("impliedVolatility") or 0)

            sig = {
                "ticker":          ticker,
                "contract":        symbol,
                "contract_type":   contract.get("_type", ""),
                "composite_score": contract.get("_composite_score", 0),
                "premium":         contract.get("_premium", 0),
                "iv":              iv_val,
                "decoded":         {"days_out": dte},
                "ask":             float(contract.get("ask") or 0),
                "delta_raw":       delta_raw,
            }

            assessment_text = evaluate_trade_quality(
                sig, todays_db_signals, market_overview
            )

            # Extract verdict from the output for sorting
            verdict = "SKIP"
            for v in ("QUALIFIED", "REVIEW", "CAUTION", "SKIP"):
                if f"VERDICT: {v}" in assessment_text:
                    verdict = v
                    break

            # Generate thesis for QUALIFIED and REVIEW signals only
            # Cap at 10 to control API spend
            thesis = ""
            if verdict in ("QUALIFIED", "REVIEW") and thesis_count < 10 and dte > 2:
                thesis = generate_thesis(
                    sig,
                    greeks_data.get(
                        contract.get("instrument", {}).get("symbol", ""), {}
                    ),
                    market_overview,
                    {
                        "call_pct":   call_pct,
                        "put_pct":    put_pct,
                        "bias_label": bias,
                    }
                )
                thesis_count += 1

            evaluated.append({
                "verdict":      verdict,
                "verdict_rank": VERDICT_RANK.get(verdict, 9),
                "score":        contract.get("_composite_score", 0),
                "dte":          dte,
                "text":         assessment_text,
                "thesis":       thesis,
            })


        # Pass 2 — sort: QUALIFIED first, then REVIEW, CAUTION, SKIP
        #                within each verdict tier: higher score first
        #                within same score: lower DTE first (more urgent)
        evaluated.sort(key=lambda x: (
            x["verdict_rank"],
            0 if x["dte"] > 2 else 1,   # thesis-eligible signals first
            -x["score"],
            x["dte"]
        ))

        # Print top 10
        for entry in evaluated[:10]:
            print(entry["text"])
            if entry.get("thesis"):
                print(f"\n  📝 Proposed thesis:")
                print(f"     {entry['thesis']}")

    else:
        print("\n  No significant signals detected across watchlist.")
    
    # Report any scan errors
    if scan_errors:
        print(f"\n  ⚠ Scan errors: {', '.join(scan_errors)}")
    
    # Log that this scan completed — used by dashboard refresh detection
    log_scan_event(
        tickers_scanned=len(WATCHLIST) - len(scan_errors),
        signals_found=len(all_signals)
    )

    # Open paper positions summary — quick check at end of every scan
    from journal import get_open_positions, get_paper_trade_summary
    open_positions = get_open_positions()
    if open_positions:
        print(f"\n{'='*70}")
        print(f"  📂 OPEN PAPER POSITIONS — {len(open_positions)} active")
        print(f"{'='*70}")
        from paper_trade import print_open_positions
        print_open_positions(open_positions, header=False)
    else:
        print(f"\n  📂 No open paper positions")
    
    # Step 7: Journal review
    print(f"\n")
    display_logging_summary()
    display_recent_signals(days=7)


if __name__ == "__main__":
    main()