import sqlite3
import requests
import os
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("PUBLIC_SECRET_KEY")
BASE_URL   = "https://api.public.com"
MARKET_TIMEZONE = "US/Eastern"

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
# PRICE FETCHING
# =============================================================================

def get_closing_prices(tickers, token, account_id, use_previous_close=False):
    """
    Fetch closing prices for a list of equity tickers.

    For today's expirations after market close:
        use_previous_close=False → use 'last' price (today's close)
    For yesterday's expirations:
        use_previous_close=True → use 'previousClose' field

    Parameters:
        tickers (list): List of ticker strings
        token (str): Valid access token
        account_id (str): Brokerage account ID
        use_previous_close (bool): Whether to use previousClose field

    Returns:
        dict: Closing price keyed by ticker, empty dict if call fails
    """
    if not tickers:
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
                    {"symbol": t, "type": "EQUITY"} for t in tickers
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
                    if use_previous_close:
                        price = float(q.get("previousClose") or 0)
                    else:
                        price = float(q.get("last") or 0)

                    if price > 0:
                        prices[symbol] = price
                except (ValueError, TypeError):
                    pass

        return prices

    except Exception as e:
        print(f"  ✗ Price fetch error: {e}")
        return {}


# =============================================================================
# OUTCOME LOGIC
# =============================================================================

def determine_outcome(contract_type, strike, closing_price):
    """
    Determine WIN/LOSS/FLAT based on contract type, strike, and close.

    For signals table, outcome is purely directional:
        WIN  — contract expired in the money (direction was correct)
        LOSS — contract expired out of the money (direction was wrong)
        FLAT — expired exactly at strike (treated conservatively)

    Parameters:
        contract_type (str): 'CALL' or 'PUT'
        strike (float): Strike price
        closing_price (float): Underlying closing price at expiration

    Returns:
        str: 'WIN', 'LOSS', or 'FLAT'
    """
    if closing_price <= 0:
        return None  # Can't determine — no price data

    if contract_type == "CALL":
        if closing_price > strike:
            return "WIN"
        elif closing_price == strike:
            return "FLAT"
        else:
            return "LOSS"

    elif contract_type == "PUT":
        if closing_price < strike:
            return "WIN"
        elif closing_price == strike:
            return "FLAT"
        else:
            return "LOSS"

    return None


def extract_ticker_from_contract(contract_symbol):
    """
    Extract the underlying ticker from a contract symbol.
    Format: {TICKER}{YYMMDD}{C/P}{STRIKE8}
    Last 15 chars are date+type+strike, remainder is ticker.
    """
    try:
        return contract_symbol[:-15]
    except Exception:
        return None


# =============================================================================
# CORE RECORDING LOGIC
# =============================================================================

def record_outcomes_for_date(target_date, token, account_id,
                              use_previous_close=False):
    """
    Fetch all unresolved contracts expiring on target_date,
    determine outcomes using closing prices, and write to DB.

    Parameters:
        target_date (str): YYYY-MM-DD date to process
        token (str): Valid access token
        account_id (str): Brokerage account ID
        use_previous_close (bool): Use previousClose field for pricing

    Returns:
        dict: Summary with wins, losses, flats, skipped, total
    """
    from journal import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Fetch all unresolved contracts expiring on this date
    # Use DISTINCT on contract to avoid processing duplicates
    # Take the highest-scored version of each contract
    cursor.execute("""
        SELECT
            contract,
            contract_type,
            strike,
            ticker,
            MAX(composite_score) as best_score
        FROM signals
        WHERE expiration = ?
        AND outcome IS NULL
        GROUP BY contract
        ORDER BY best_score DESC
    """, (target_date,))

    contracts = [dict(row) for row in cursor.fetchall()]
    conn.close()

    if not contracts:
        print(f"  No unresolved contracts found for {target_date}")
        return {"wins": 0, "losses": 0, "flats": 0,
                "skipped": 0, "total": 0}

    print(f"  Found {len(contracts)} unique contracts expiring {target_date}")

    # Get unique tickers needing closing prices
    tickers = list({c["ticker"] for c in contracts if c["ticker"]})

    print(f"  Fetching closing prices for {len(tickers)} tickers...")
    closing_prices = get_closing_prices(
        tickers, token, account_id,
        use_previous_close=use_previous_close
    )

    if not closing_prices:
        print(f"  ✗ Could not fetch closing prices — outcomes not recorded")
        return {"wins": 0, "losses": 0, "flats": 0,
                "skipped": len(contracts), "total": len(contracts)}

    print(f"  ✓ Closing prices fetched for {len(closing_prices)} tickers")

    # Determine and record outcomes
    wins = losses = flats = skipped = 0

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    for c in contracts:
        ticker        = c["ticker"]
        contract      = c["contract"]
        contract_type = c["contract_type"]
        strike        = c["strike"]

        closing_price = closing_prices.get(ticker)

        if not closing_price:
            # No price available for this ticker — mark as EXPIRED_UNRESOLVED
            cursor.execute("""
                UPDATE signals
                SET outcome = 'EXPIRED',
                    outcome_notes = 'Auto-outcome: closing price unavailable'
                WHERE contract = ?
                AND outcome IS NULL
            """, (contract,))
            skipped += 1
            continue

        outcome = determine_outcome(contract_type, strike, closing_price)

        if outcome is None:
            skipped += 1
            continue

        # Write outcome to ALL rows for this contract
        # (same contract may be logged across multiple scan days)
        note = (f"Auto-outcome: {ticker} closed at ${closing_price:.2f} "
                f"vs ${strike:.2f} strike")

        cursor.execute("""
            UPDATE signals
            SET outcome = ?,
                outcome_notes = ?
            WHERE contract = ?
            AND outcome IS NULL
        """, (outcome, note, contract))

        if outcome == "WIN":
            wins += 1
        elif outcome == "LOSS":
            losses += 1
        elif outcome == "FLAT":
            flats += 1

    conn.commit()
    conn.close()

    return {
        "wins":    wins,
        "losses":  losses,
        "flats":   flats,
        "skipped": skipped,
        "total":   len(contracts)
    }


def print_outcome_summary(date, results):
    """Print a formatted outcome recording summary."""
    total    = results["total"]
    wins     = results["wins"]
    losses   = results["losses"]
    flats    = results["flats"]
    skipped  = results["skipped"]
    resolved = wins + losses + flats

    win_rate = round((wins / resolved) * 100, 1) if resolved > 0 else 0

    print(f"\n  {'─'*55}")
    print(f"  📊 OUTCOME RECORDING — {date}")
    print(f"  {'─'*55}")
    print(f"  Contracts processed: {total}")
    print(f"  Resolved:            {resolved}")
    print(f"    ✅ WIN:            {wins}")
    print(f"    ❌ LOSS:           {losses}")
    print(f"    ➖ FLAT:           {flats}")
    print(f"  Skipped (no price):  {skipped}")
    print(f"  Win rate:            {win_rate}%")
    print(f"  {'─'*55}")


# =============================================================================
# DATES TO PROCESS
# =============================================================================

def get_dates_needing_outcomes():
    """
    Determine which expiration dates have unresolved contracts.
    Returns today and yesterday if they have pending outcomes.
    Excludes today if market hasn't closed yet.
    """
    from journal import DB_PATH

    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)
    today   = now.date()
    yesterday = (now - timedelta(days=1)).date()

    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    market_closed_today = now >= market_close

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    dates_to_process = []

    # Yesterday — use previousClose pricing
    cursor.execute("""
        SELECT COUNT(DISTINCT contract) as n
        FROM signals
        WHERE expiration = ?
        AND outcome IS NULL
    """, (str(yesterday),))
    if cursor.fetchone()["n"] > 0:
        dates_to_process.append({
            "date": str(yesterday),
            "use_previous_close": True,
            "label": "yesterday"
        })

    # Today — only if market has closed, use last price
    if market_closed_today:
        cursor.execute("""
            SELECT COUNT(DISTINCT contract) as n
            FROM signals
            WHERE expiration = ?
            AND outcome IS NULL
        """, (str(today),))
        if cursor.fetchone()["n"] > 0:
            dates_to_process.append({
                "date": str(today),
                "use_previous_close": False,
                "label": "today"
            })

    conn.close()
    return dates_to_process


# =============================================================================
# MAIN
# =============================================================================

def main():
    """
    Main entry point for outcome recording.
    Can be called standalone or imported by scheduler.
    """
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)

    print(f"\n{'='*55}")
    print(f"  📋 OUTCOME RECORDER")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"{'='*55}")

    # Check which dates need processing
    dates = get_dates_needing_outcomes()

    if not dates:
        print(f"\n  ✓ No unresolved outcomes to record")
        print(f"  (Market may still be open, or all outcomes already recorded)")
        return

    # Authenticate once
    print(f"\n  Authenticating...")
    token = get_auth_token()
    if not token:
        print(f"  ✗ Authentication failed")
        return

    account_id = get_account_id(token)
    if not account_id:
        print(f"  ✗ Could not get account ID")
        return

    print(f"  ✓ Authenticated")

    # Process each date
    all_results = []
    for entry in dates:
        print(f"\n  Processing {entry['label']} ({entry['date']})...")
        results = record_outcomes_for_date(
            target_date         = entry["date"],
            token               = token,
            account_id          = account_id,
            use_previous_close  = entry["use_previous_close"]
        )
        print_outcome_summary(entry["date"], results)
        all_results.append(results)

    # Combined summary if multiple dates
    if len(all_results) > 1:
        total_wins   = sum(r["wins"]    for r in all_results)
        total_losses = sum(r["losses"]  for r in all_results)
        total_flats  = sum(r["flats"]   for r in all_results)
        total_all    = sum(r["total"]   for r in all_results)
        resolved     = total_wins + total_losses + total_flats
        win_rate     = round((total_wins / resolved) * 100, 1) \
                       if resolved > 0 else 0

        print(f"\n  {'='*55}")
        print(f"  📊 COMBINED SUMMARY")
        print(f"  {'='*55}")
        print(f"  Total processed: {total_all}")
        print(f"  Total wins:      {total_wins}")
        print(f"  Total losses:    {total_losses}")
        print(f"  Overall win rate:{win_rate}%")
        print(f"  {'='*55}")


if __name__ == "__main__":
    main()