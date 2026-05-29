# -*- coding: utf-8 -*-
"""
exit_expiring.py
Take profit on all OPEN positions expiring today that are in the money.
Usage: python exit_expiring.py
"""

import sqlite3
import requests
import os
import pytz
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY      = os.getenv("PUBLIC_SECRET_KEY")
BASE_URL        = "https://api.public.com"
ACCOUNT_ID      = "5LT39200"
MARKET_TIMEZONE = "US/Eastern"


def get_auth_token():
    response = requests.post(
        f"{BASE_URL}/userapiauthservice/personal/access-tokens",
        json={"secret": SECRET_KEY, "validityInMinutes": 30}
    )
    return response.json().get("accessToken") if response.status_code == 200 else None


def fetch_live_prices(contracts, token):
    try:
        response = requests.post(
            f"{BASE_URL}/userapigateway/marketdata/{ACCOUNT_ID}/quotes",
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
            return {}
        prices = {}
        for q in response.json().get("quotes", []):
            if q.get("outcome") == "SUCCESS":
                symbol = q["instrument"]["symbol"]
                try:
                    bid = float(q.get("bid") or 0)
                    ask = float(q.get("ask") or 0)
                    mid = round((bid + ask) / 2, 4) if bid and ask else 0
                    prices[symbol] = {"bid": bid, "ask": ask, "mid": mid}
                except (ValueError, TypeError):
                    pass
        return prices
    except Exception as e:
        print(f"  ✗ Price fetch error: {e}")
        return {}


def parse_expiry_from_contract(contract):
    """Parse expiration date from contract symbol e.g. TSLA260515C00430000"""
    try:
        date_str = contract[-15:-9]
        return datetime.strptime(date_str, "%y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return None


def get_expiring_open_positions(today):
    from journal import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, signal_contract, entry_price,
               contracts, total_cost, status
        FROM paper_trades
        WHERE status = 'OPEN'
    """)
    all_open = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # Filter to today's expiration by parsing contract symbol
    expiring = [
        p for p in all_open
        if parse_expiry_from_contract(p["signal_contract"]) == today
    ]
    return expiring


def main():
    eastern = pytz.timezone(MARKET_TIMEZONE)
    today   = datetime.now(eastern).strftime("%Y-%m-%d")
    now_str = datetime.now(eastern).strftime("%Y-%m-%d %H:%M:%S %Z")

    print(f"\n{'='*65}")
    print(f"  💰 EXPIRATION DAY TAKE-PROFIT")
    print(f"  {now_str}")
    print(f"  Looking for OPEN positions expiring: {today}")
    print(f"{'='*65}")

    print(f"\n  Authenticating...")
    token = get_auth_token()
    if not token:
        print(f"  ✗ Auth failed — check PUBLIC_SECRET_KEY in .env")
        return

    positions = get_expiring_open_positions(today)
    if not positions:
        print(f"\n  No OPEN positions expiring today.")
        return

    print(f"\n  Found {len(positions)} OPEN position(s) expiring today")
    print(f"  Fetching live prices...")

    contracts = [p["signal_contract"] for p in positions]
    prices    = fetch_live_prices(contracts, token)

    print(f"\n  {'─'*65}")
    print(f"  {'ID':<5} {'Contract':<28} {'Entry':>7} {'Mid':>7} {'P&L':>9} {'%':>7}  Action")
    print(f"  {'─'*65}")

    to_exit = []
    to_skip = []

    for p in positions:
        contract      = p["signal_contract"]
        entry_price   = p["entry_price"]
        num_contracts = p["contracts"]
        total_cost    = p["total_cost"]
        trade_id      = p["id"]

        price_data = prices.get(contract)
        if not price_data or price_data["mid"] == 0:
            print(f"  {trade_id:<5} {contract:<28} ${entry_price:>6.2f}  {'N/A':>7}  ⚠️  No price")
            continue

        mid     = price_data["mid"]
        pnl     = round((mid - entry_price) * num_contracts * 100, 2)
        pnl_pct = round((pnl / total_cost) * 100, 2) if total_cost else 0

        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        pct_str = f"+{pnl_pct:.1f}%" if pnl_pct >= 0 else f"{pnl_pct:.1f}%"

        if mid > entry_price:
            action = "✅ EXIT"
            to_exit.append({**p, "mid": mid, "pnl": pnl, "pnl_pct": pnl_pct})
        else:
            action = "⏭  SKIP"
            to_skip.append(p)

        print(f"  {trade_id:<5} {contract:<28} ${entry_price:>6.2f} ${mid:>6.2f} {pnl_str:>9} {pct_str:>7}  {action}")

    print(f"  {'─'*65}")
    print(f"\n  Profitable (will exit): {len(to_exit)}")
    print(f"  At loss    (will skip): {len(to_skip)}")

    if not to_exit:
        print(f"\n  No profitable positions to exit.")
        return

    total_pnl = sum(p["pnl"] for p in to_exit)
    print(f"  Total P&L to lock in:  +${total_pnl:.2f}")

    print(f"\n  ⚠️  Will close {len(to_exit)} position(s) at current mid prices.")
    confirm = input("  Confirm? (y/n): ").strip().lower()

    if confirm != "y":
        print(f"\n  Aborted — no positions closed.")
        return

    from journal import close_paper_trade
    closed = 0
    failed = 0

    print(f"\n  Closing...")
    for p in to_exit:
        result = close_paper_trade(
            trade_id     = p["id"],
            exit_price   = p["mid"],
            exit_reason  = "MANUAL",
            notes        = "Take-profit exit — expiration day, position profitable but target unlikely"
        )
        if result:
            print(f"  ✅ #{p['id']} {p['signal_contract']} "
                  f"closed at ${p['mid']:.2f}  "
                  f"P&L: +${p['pnl']:.2f} (+{p['pnl_pct']:.1f}%)")
            closed += 1
        else:
            print(f"  ✗ #{p['id']} failed")
            failed += 1

    print(f"\n{'='*65}")
    print(f"  📊 DONE")
    print(f"  Closed:  {closed}  Failed: {failed}  Skipped: {len(to_skip)}")
    print(f"  P&L locked in: +${total_pnl:.2f}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()