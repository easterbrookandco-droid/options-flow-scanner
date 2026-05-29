import sys
import os
import pytz
import requests
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY      = os.getenv("PUBLIC_SECRET_KEY")
BASE_URL        = "https://api.public.com"
MARKET_TIMEZONE = "US/Eastern"


# =============================================================================
# USAGE
# =============================================================================

USAGE = """
  evaluate.py — On-demand signal evaluator

  Usage:
    python evaluate.py <contract_symbol>
    python evaluate.py <contract_symbol> --no-thesis

  Examples:
    python evaluate.py SPY260427P00712000
    python evaluate.py TSLA260429C00392500 --no-thesis
    python evaluate.py AMD260501P00295000

  What it does:
    - Decodes the contract symbol (ticker, strike, type, expiry, DTE)
    - Fetches current bid/ask/last from Public API
    - Fetches Greeks (delta, IV, theta)
    - Fetches current underlying stock price
    - Fetches market overview (SPY, QQQ, IWM)
    - Pulls today's flow bias for the ticker from the DB
    - Runs the full trade quality assessment
    - Generates a thesis via Claude (unless --no-thesis)
"""


# =============================================================================
# CONTRACT DECODING
# =============================================================================

def decode_contract(symbol):
    """
    Decode a contract symbol into its components.
    Format: {TICKER}{YYMMDD}{C/P}{STRIKE8}
    """
    try:
        strike_str        = symbol[-8:]
        contract_type_char = symbol[-9]
        date_str          = symbol[-15:-9]
        ticker            = symbol[:-15]

        strike        = float(strike_str) / 1000
        contract_type = "CALL" if contract_type_char == "C" else "PUT"

        year  = int("20" + date_str[0:2])
        month = int(date_str[2:4])
        day   = int(date_str[4:6])
        expiry = datetime(year, month, day)

        eastern   = pytz.timezone(MARKET_TIMEZONE)
        now       = datetime.now(eastern)
        days_out  = (expiry.date() - now.date()).days

        if days_out == 0:
            dte_display = "0DTE"
        elif days_out == 1:
            dte_display = "1 day"
        else:
            dte_display = f"{days_out}d"

        return {
            "ticker":        ticker,
            "strike":        strike,
            "strike_display": (f"${strike:,.0f}" if strike == int(strike)
                               else f"${strike:,.1f}"),
            "contract_type": contract_type,
            "expiry_display": expiry.strftime("%b %d %Y"),
            "expiry_date":   expiry.strftime("%Y-%m-%d"),
            "dte_display":   dte_display,
            "days_out":      days_out,
        }
    except Exception as e:
        print(f"  ✗ Could not decode contract symbol: {e}")
        return None


# =============================================================================
# API HELPERS
# =============================================================================

def authenticate():
    """Get token and account ID."""
    try:
        response = requests.post(
            f"{BASE_URL}/userapiauthservice/personal/access-tokens",
            json={"secret": SECRET_KEY, "validityInMinutes": 60}
        )
        if response.status_code != 200:
            return None, None
        token = response.json().get("accessToken")

        response = requests.get(
            f"{BASE_URL}/userapigateway/trading/account",
            headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code != 200:
            return token, None

        for account in response.json().get("accounts", []):
            if account.get("accountType") == "BROKERAGE":
                return token, account.get("accountId")

    except Exception as e:
        print(f"  ✗ Auth error: {e}")

    return None, None


def fetch_option_quote(symbol, token, account_id):
    """Fetch current bid/ask/last for a single option contract."""
    try:
        response = requests.post(
            f"{BASE_URL}/userapigateway/marketdata/{account_id}/quotes",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={"instruments": [{"symbol": symbol, "type": "OPTION"}]}
        )

        if response.status_code != 200:
            return None

        for q in response.json().get("quotes", []):
            if q.get("outcome") == "SUCCESS":
                try:
                    bid  = float(q.get("bid")  or 0)
                    ask  = float(q.get("ask")  or 0)
                    last = float(q.get("last") or 0)
                    mid  = round((bid + ask) / 2, 4) if bid and ask else last
                    vol  = q.get("volume", 0)
                    oi   = q.get("openInterest", 0)
                    return {
                        "bid":  bid,
                        "ask":  ask,
                        "last": last,
                        "mid":  mid,
                        "volume": vol,
                        "open_interest": oi,
                    }
                except (ValueError, TypeError):
                    pass

    except Exception as e:
        print(f"  ✗ Quote fetch error: {e}")

    return None


def fetch_greeks(symbol, token, account_id):
    """Fetch Greeks for a single contract."""
    try:
        response = requests.get(
            f"{BASE_URL}/userapigateway/option-details/{account_id}/greeks",
            headers={"Authorization": f"Bearer {token}"},
            params={"osiSymbols": [symbol]}
        )

        if response.status_code != 200:
            return {}

        for item in response.json().get("greeks", []):
            if item.get("symbol") == symbol:
                g = item.get("greeks", {})
                try:
                    delta = float(g.get("delta") or 0)
                    iv    = float(g.get("impliedVolatility") or 0)
                    theta = float(g.get("theta") or 0)
                    gamma = float(g.get("gamma") or 0)

                    abs_delta = abs(delta)
                    moneyness = ("ITM" if abs_delta >= 0.7
                                 else "ATM" if abs_delta >= 0.3
                                 else "OTM")

                    iv_pct = iv * 100 if iv <= 5 else iv

                    return {
                        "delta":     delta,
                        "iv":        iv,
                        "iv_pct":    iv_pct,
                        "theta":     theta,
                        "gamma":     gamma,
                        "moneyness": moneyness,
                    }
                except (ValueError, TypeError):
                    pass

    except Exception as e:
        print(f"  ✗ Greeks fetch error: {e}")

    return {}


def fetch_stock_price(ticker, token, account_id):
    """Fetch current stock price for the underlying."""
    try:
        response = requests.post(
            f"{BASE_URL}/userapigateway/marketdata/{account_id}/quotes",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={"instruments": [{"symbol": ticker, "type": "EQUITY"}]}
        )

        if response.status_code != 200:
            return None

        for q in response.json().get("quotes", []):
            if q.get("outcome") == "SUCCESS":
                last = q.get("last")
                prev = q.get("previousClose")
                if last:
                    price   = float(last)
                    chg_pct = None
                    if prev:
                        prev_f  = float(prev)
                        chg_pct = round(
                            ((price - prev_f) / prev_f) * 100, 2
                        ) if prev_f else None
                    return {"price": price, "chg_pct": chg_pct}

    except Exception as e:
        print(f"  ✗ Stock price fetch error: {e}")

    return None


def fetch_market_overview(token, account_id):
    """Fetch SPY/QQQ/IWM for market context."""
    tickers = ["SPY", "QQQ", "IWM"]
    try:
        response = requests.post(
            f"{BASE_URL}/userapigateway/marketdata/{account_id}/quotes",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={
                "instruments": [
                    {"symbol": t, "type": "EQUITY"} for t in tickers
                ]
            }
        )

        if response.status_code != 200:
            return {}

        overview = {}
        for q in response.json().get("quotes", []):
            if q.get("outcome") == "SUCCESS":
                sym  = q["instrument"]["symbol"]
                last = float(q.get("last") or 0)
                prev = float(q.get("previousClose") or 0)
                chg  = last - prev if prev else 0
                chg_pct = (chg / prev * 100) if prev else 0
                overview[sym] = {
                    "price":      last,
                    "chg_pct":    chg_pct,
                    "has_change": prev > 0,
                }

        return overview

    except Exception as e:
        print(f"  ✗ Market overview error: {e}")
        return {}


# =============================================================================
# DB — TODAY'S FLOW BIAS FOR TICKER
# =============================================================================

def get_ticker_flow_bias(ticker):
    """
    Pull today's call/put flow bias for a specific ticker from the DB.
    Used for the directional lean check in the assessment.
    """
    try:
        from journal import DB_PATH
        import sqlite3

        eastern = pytz.timezone(MARKET_TIMEZONE)
        today   = datetime.now(eastern).strftime("%Y-%m-%d")

        conn   = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                contract_type,
                COUNT(*) as count
            FROM signals
            WHERE ticker = ?
            AND scan_time LIKE ?
            AND signal_tier IN ('HIGH', 'INST')
            GROUP BY contract_type
        """, (ticker, f"{today}%"))

        rows        = {row["contract_type"]: row["count"]
                       for row in cursor.fetchall()}
        conn.close()

        calls = rows.get("CALL", 0)
        puts  = rows.get("PUT",  0)
        total = calls + puts

        if total == 0:
            return [], "NEUTRAL"

        call_pct = calls / total * 100
        put_pct  = puts  / total * 100
        bias     = ("BULLISH" if call_pct > 55
                    else "BEARISH" if put_pct > 55
                    else "NEUTRAL")

        # Build a minimal signal list for evaluate_trade_quality
        signals = (
            [{"ticker": ticker, "contract_type": "CALL",
              "signal_tier": "HIGH"}] * calls +
            [{"ticker": ticker, "contract_type": "PUT",
              "signal_tier": "HIGH"}] * puts
        )
        return signals, bias

    except Exception:
        return [], "NEUTRAL"


# =============================================================================
# MAIN EVALUATION
# =============================================================================

def evaluate(contract_symbol, generate_thesis=True):
    """
    Full evaluation of a single contract symbol.

    Parameters:
        contract_symbol (str): e.g. "SPY260427P00712000"
        generate_thesis (bool): Whether to call Claude for thesis
    """

    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)

    print(f"\n{'='*65}")
    print(f"  🔍 ON-DEMAND EVALUATOR")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Contract: {contract_symbol}")
    print(f"{'='*65}")

    # ── Decode contract ───────────────────────────────────────────────────
    decoded = decode_contract(contract_symbol)
    if not decoded:
        return

    ticker        = decoded["ticker"]
    strike        = decoded["strike"]
    contract_type = decoded["contract_type"]
    days_out      = decoded["days_out"]

    print(f"\n  📋 CONTRACT DETAILS")
    print(f"  {'─'*50}")
    print(f"  Ticker:     {ticker}")
    print(f"  Strike:     {decoded['strike_display']}")
    print(f"  Type:       {contract_type}")
    print(f"  Expiry:     {decoded['expiry_display']}")
    print(f"  DTE:        {decoded['dte_display']}")

    # ── Authenticate ──────────────────────────────────────────────────────
    print(f"\n  Fetching live data...")
    token, account_id = authenticate()

    if not token or not account_id:
        print(f"  ✗ Authentication failed")
        return

    time.sleep(0.15)

    # ── Option quote ──────────────────────────────────────────────────────
    quote = fetch_option_quote(contract_symbol, token, account_id)
    time.sleep(0.15)

    # ── Greeks ────────────────────────────────────────────────────────────
    greeks = fetch_greeks(contract_symbol, token, account_id)
    time.sleep(0.15)

    # ── Stock price ───────────────────────────────────────────────────────
    stock = fetch_stock_price(ticker, token, account_id)
    time.sleep(0.15)

    # ── Market overview ───────────────────────────────────────────────────
    market_overview = fetch_market_overview(token, account_id)

    # ── Print live data ───────────────────────────────────────────────────
    print(f"\n  📊 LIVE MARKET DATA")
    print(f"  {'─'*50}")

    if quote:
        cost_per_contract = quote["ask"] * 100
        print(f"  Bid:        ${quote['bid']:.2f}")
        print(f"  Ask:        ${quote['ask']:.2f}  "
              f"(${cost_per_contract:,.0f}/contract)")
        print(f"  Mid:        ${quote['mid']:.2f}")
        print(f"  Last:       ${quote['last']:.2f}")
        print(f"  Volume:     {quote['volume']:,}")
        print(f"  OI:         {quote['open_interest']:,}")
    else:
        print(f"  Quote unavailable")

    if stock:
        chg_str = (f"  ({'+' if stock['chg_pct'] >= 0 else ''}"
                   f"{stock['chg_pct']:.2f}%)"
                   if stock["chg_pct"] is not None else "")
        print(f"  {ticker} price: ${stock['price']:,.2f}{chg_str}")

        # Distance from strike
        if contract_type == "CALL":
            dist     = stock["price"] - strike
            dist_str = (f"${abs(dist):.2f} {'above' if dist >= 0 else 'below'} "
                        f"strike")
        else:
            dist     = strike - stock["price"]
            dist_str = (f"${abs(dist):.2f} {'above' if dist >= 0 else 'below'} "
                        f"strike")
        print(f"  vs Strike:  {dist_str}")

    if greeks:
        print(f"\n  Greeks:")
        print(f"  Delta:      {greeks['delta']:+.4f}  ({greeks['moneyness']})")
        print(f"  IV:         {greeks['iv_pct']:.1f}%")
        print(f"  Theta:      {greeks['theta']:.4f}")
        print(f"  Gamma:      {greeks['gamma']:.4f}")

    if market_overview:
        print(f"\n  Market context:")
        for sym in ["SPY", "QQQ", "IWM"]:
            m = market_overview.get(sym, {})
            if m:
                chg = m.get("chg_pct", 0)
                if m.get("has_change"):
                    arrow = "▲" if chg >= 0 else "▼"
                    sign  = "+" if chg >= 0 else ""
                    print(f"  {sym}:        ${m['price']:,.2f}  "
                          f"{arrow} {sign}{chg:.2f}%")
                else:
                    print(f"  {sym}:        ${m['price']:,.2f}  "
                          f"(after hours)")

    # ── Build signal dict for assessment ──────────────────────────────────
    ticker_signals, ticker_bias = get_ticker_flow_bias(ticker)

    iv_raw    = greeks.get("iv", 0) if greeks else 0
    delta_raw = greeks.get("delta", 0) if greeks else 0
    ask_price = quote["ask"] if quote else 0

    sig = {
        "ticker":          ticker,
        "contract":        contract_symbol,
        "contract_type":   contract_type,
        "composite_score": 0,   # Not available for on-demand
        "premium":         0,   # Not available without volume context
        "iv":              iv_raw,
        "decoded":         {"days_out": days_out},
        "ask":             ask_price,
        "delta_raw":       delta_raw,
    }

    # ── Run assessment ────────────────────────────────────────────────────
    print(f"\n  Running trade quality assessment...")

    from fetch_trades import evaluate_trade_quality
    assessment = evaluate_trade_quality(sig, ticker_signals, market_overview)

    print(assessment)

    # ── Extract verdict for thesis decision ───────────────────────────────
    verdict = "SKIP"
    for v in ("QUALIFIED", "REVIEW", "CAUTION", "SKIP"):
        if f"VERDICT: {v}" in assessment:
            verdict = v
            break

    # ── Generate thesis ───────────────────────────────────────────────────
    if generate_thesis and verdict in ("QUALIFIED", "REVIEW"):
        print(f"\n  Generating thesis...")

        from thesis_generator import generate_thesis as gen_thesis

        greeks_for_thesis = {
            "delta":             f"{delta_raw:+.4f}",
            "impliedVolatility": iv_raw,
            "moneyness":         greeks.get("moneyness", "") if greeks else "",
            "moneyness_color":   "",
            "iv":                f"{greeks.get('iv_pct', 0):.1f}%" if greeks else "",
        }

        # Build flow bias dict
        spy     = market_overview.get("SPY", {})
        qqq     = market_overview.get("QQQ", {})
        spy_chg = spy.get("chg_pct", 0) if spy.get("has_change") else 0
        qqq_chg = qqq.get("chg_pct", 0) if qqq.get("has_change") else 0
        avg_mkt = (spy_chg + qqq_chg) / 2
        bias_label = ("BULLISH" if avg_mkt > 0.3
                      else "BEARISH" if avg_mkt < -0.3
                      else "NEUTRAL")

        thesis = gen_thesis(
            sig,
            greeks_for_thesis,
            market_overview,
            {
                "call_pct":   50,
                "put_pct":    50,
                "bias_label": bias_label,
            }
        )

        print(f"\n  📝 Thesis:")
        print(f"     {thesis}")

        # ── Entry command ─────────────────────────────────────────────────
        if quote and verdict == "QUALIFIED":
            ask     = quote["ask"]
            cost_1  = ask * 100
            cost_2  = ask * 200
            target  = round(ask * 2, 2)
            stop    = round(ask * 0.5, 2)

            print(f"\n  {'─'*55}")
            print(f"  📌 TO PAPER TRADE THIS SIGNAL:")
            print(f"  {'─'*55}")
            print(f"  Current ask:  ${ask:.2f}/share")
            print(f"  1 contract:   ${cost_1:,.0f}  "
                  f"({'✅ fits budget' if cost_1 <= 600 else '⚠️ over $600'})")
            print(f"  2 contracts:  ${cost_2:,.0f}  "
                  f"({'✅ fits budget' if cost_2 <= 600 else '⚠️ over $600'})")
            print(f"\n  Target (2x):  ${target:.2f}  "
                  f"(+${(target-ask)*100:,.0f} per contract)")
            print(f"  Stop (50%):   ${stop:.2f}  "
                  f"(-${(ask-stop)*100:,.0f} per contract)")
            print(f"\n  Command (use single quotes in PowerShell):")
            print(f"  python paper_trade.py enter {contract_symbol} "
                  f"{ask:.2f} 1 '{thesis}'")
            print(f"  {'─'*55}")

    elif verdict == "SKIP":
        print(f"\n  ⛔ Signal does not meet entry criteria — no thesis generated")

    print(f"\n{'='*65}\n")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(0)

    contract_sym   = sys.argv[1].upper()
    no_thesis_flag = "--no-thesis" in sys.argv

    evaluate(contract_sym, generate_thesis=not no_thesis_flag)