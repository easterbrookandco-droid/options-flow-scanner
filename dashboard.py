from flask import Flask, render_template_string, jsonify
import sqlite3
from datetime import datetime
import pytz
import requests
import os
from dotenv import load_dotenv

load_dotenv()
SECRET_KEY = os.getenv("PUBLIC_SECRET_KEY")
BASE_URL = "https://api.public.com"

def decode_contract(symbol):
    """
    Decode an options contract symbol into human-readable components.
    
    Format: {TICKER}{YYMMDD}{TYPE}{STRIKE8}
    Example: SPY260420P00710000
             → ticker: SPY
             → expiry: Apr 20 2026
             → type: Put
             → strike: $710.00
    
    Parameters:
        symbol (str): Full options contract symbol
    
    Returns:
        dict: Decoded components, or raw symbol if parsing fails
    """
    try:
        # Work backwards from fixed-length suffix
        # Last 8 chars = strike (padded integer, divide by 1000)
        # Char before that = C or P
        # 6 chars before that = YYMMDD
        # Everything remaining = ticker
        
        strike_str = symbol[-8:]
        contract_type_char = symbol[-9]
        date_str = symbol[-15:-9]
        ticker = symbol[:-15]
        
        # Parse strike
        strike = float(strike_str) / 1000
        
        # Parse contract type
        contract_type = "Call" if contract_type_char == "C" else "Put"
        
        # Parse expiration date
        year = int("20" + date_str[0:2])
        month = int(date_str[2:4])
        day = int(date_str[4:6])
        expiry = datetime(year, month, day)
        
        # Format expiration as readable string
        expiry_display = expiry.strftime("%b %d")  # e.g. "Apr 20"
        
        # Calculate days to expiration
        eastern = pytz.timezone(MARKET_TIMEZONE)
        now = datetime.now(eastern)
        days_out = (expiry.date() - now.date()).days
        
        if days_out == 0:
            dte_display = "0DTE"
        elif days_out == 1:
            dte_display = "1 day"
        else:
            dte_display = f"{days_out}d"
        
        return {
            'ticker': ticker,
            'strike': strike,
            'strike_display': f"${strike:,.0f}" if strike == int(strike) else f"${strike:,.1f}",
            'contract_type': contract_type,
            'expiry_display': expiry_display,
            'dte_display': dte_display,
            'days_out': days_out,
            'readable': f"{ticker} {expiry_display} ${strike:,.0f} {contract_type}"
        }
    
    except Exception:
        # If parsing fails for any reason, return safe fallback
        return {
            'ticker': symbol,
            'strike': 0,
            'strike_display': '',
            'contract_type': '',
            'expiry_display': '',
            'dte_display': '',
            'days_out': 999,
            'readable': symbol
        }

app = Flask(__name__)

DB_PATH = "signals.db"
MARKET_TIMEZONE = "US/Eastern"

# =============================================================================
# DATABASE HELPERS
# =============================================================================

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_todays_signals():
    """Fetch all signals logged today."""
    conn = get_db_connection()
    cursor = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    
    cursor.execute("""
        SELECT * FROM signals
        WHERE scan_time LIKE ?
        ORDER BY composite_score DESC, premium DESC
    """, (f"{today}%",))
    
    signals = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return signals


def get_expiring_today():
    """Fetch all signals expiring today."""
    conn = get_db_connection()
    cursor = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    
    cursor.execute("""
        SELECT * FROM signals
        WHERE expiration = ?
        AND signal_tier IN ('HIGH', 'INST')
        ORDER BY composite_score DESC
    """, (today,))
    
    signals = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return signals


def get_performance_stats():
    """Calculate win rate statistics by tier."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT
            signal_tier,
            COUNT(*) as total,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN outcome = 'FLAT' THEN 1 ELSE 0 END) as flats
        FROM signals
        WHERE outcome IS NOT NULL
        GROUP BY signal_tier
        ORDER BY CASE signal_tier
            WHEN 'HIGH' THEN 1
            WHEN 'INST' THEN 2
            WHEN 'WATCH' THEN 3
        END
    """, )
    
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results


def get_directional_bias():
    """Calculate call vs put bias from today's HIGH signals."""
    conn = get_db_connection()
    cursor = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    
    cursor.execute("""
        SELECT
            contract_type,
            COUNT(*) as count,
            SUM(premium) as total_premium
        FROM signals
        WHERE scan_time LIKE ?
        AND signal_tier = 'HIGH'
        GROUP BY contract_type
    """, (f"{today}%",))
    
    results = {row['contract_type']: dict(row) 
               for row in cursor.fetchall()}
    conn.close()
    return results


def get_journal_summary():
    """Get overall journal statistics."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN signal_tier = 'HIGH' THEN 1 ELSE 0 END) as high_count,
            SUM(CASE WHEN signal_tier = 'INST' THEN 1 ELSE 0 END) as inst_count,
            SUM(CASE WHEN signal_tier = 'WATCH' THEN 1 ELSE 0 END) as watch_count
        FROM signals
    """)
    
    result = dict(cursor.fetchone())
    conn.close()
    return result


def get_signal_history(days=30):
    """
    Fetch signal counts and premium volume grouped by scan date.
    Returns last N days of data for the history chart.
    
    Each row contains:
        scan_date   — YYYY-MM-DD string
        high_count  — number of HIGH signals that day
        inst_count  — number of INST signals that day
        watch_count — number of WATCH signals that day
        total_premium — sum of premium for all signals that day
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            DATE(scan_time) as scan_date,
            SUM(CASE WHEN signal_tier = 'HIGH'  THEN 1 ELSE 0 END) as high_count,
            SUM(CASE WHEN signal_tier = 'INST'  THEN 1 ELSE 0 END) as inst_count,
            SUM(CASE WHEN signal_tier = 'WATCH' THEN 1 ELSE 0 END) as watch_count,
            SUM(premium) as total_premium
        FROM signals
        WHERE scan_time >= DATE('now', ?)
        GROUP BY DATE(scan_time)
        ORDER BY scan_date ASC
    """, (f"-{days} days",))

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_api_token():
    """
    Get a fresh access token from Public API.
    Called when dashboard needs to fetch live Greeks.
    Token is valid for 60 minutes.
    
    Returns:
        str: Access token or None if auth fails
    """
    try:
        url = f"{BASE_URL}/userapiauthservice/personal/access-tokens"
        response = requests.post(url, json={
            "secret": SECRET_KEY,
            "validityInMinutes": 60
        })
        if response.status_code == 200:
            return response.json().get("accessToken")
    except Exception as e:
        print(f"  Auth error: {e}")
    return None


def get_account_id(token):
    """
    Get the brokerage account ID.
    Needed for the Greeks endpoint URL.
    
    Returns:
        str: Account ID or None
    """
    try:
        url = f"{BASE_URL}/userapigateway/trading/account"
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            accounts = response.json().get("accounts", [])
            for account in accounts:
                if account.get("accountType") == "BROKERAGE":
                    return account.get("accountId")
    except Exception as e:
        print(f"  Account fetch error: {e}")
    return None


def get_greeks_for_signals(signals):
    """
    Fetch Greeks for a list of signals from Public API.
    
    Only fetches for HIGH tier signals to minimize API calls.
    Returns a dict keyed by contract symbol for easy lookup.
    
    Parameters:
        signals (list): List of signal dicts containing 'contract' key
    
    Returns:
        dict: Greeks keyed by contract symbol, empty dict if unavailable
    """
    
    if not signals:
        return {}
    
    try:
        # Authenticate
        token = get_api_token()
        if not token:
            return {}
        
        account_id = get_account_id(token)
        if not account_id:
            return {}
        
        # Build symbol list
        symbols = [s['contract'] for s in signals]
        
        url = f"{BASE_URL}/userapigateway/option-details/{account_id}/greeks"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        params = {"osiSymbols": symbols}
        
        response = requests.get(url, headers=headers, params=params)
        
        if response.status_code != 200:
            print(f"  Greeks fetch failed: {response.status_code}")
            return {}
        
        data = response.json()
        greeks_list = data.get("greeks", [])
        
        # Key by symbol for easy template lookup
        greeks_by_symbol = {}
        for item in greeks_list:
            symbol = item.get("symbol", "")
            if symbol:
                g = item.get("greeks", {})
                try:
                    delta = float(g.get("delta", 0) or 0)
                    iv = float(g.get("impliedVolatility", 0) or 0)
                    theta = float(g.get("theta", 0) or 0)
                    
                    # Delta context
                    abs_delta = abs(delta)
                    if abs_delta >= 0.7:
                        moneyness = "ITM"
                        moneyness_color = "#22c55e"
                    elif abs_delta >= 0.3:
                        moneyness = "ATM"
                        moneyness_color = "#f59e0b"
                    else:
                        moneyness = "OTM"
                        moneyness_color = "#64748b"
                    
                    greeks_by_symbol[symbol] = {
                        'delta': f"{delta:+.3f}",
                        'delta_raw': delta,
                        'theta': f"{theta:.3f}",
                        'iv': f"{iv*100:.1f}%",
                        'moneyness': moneyness,
                        'moneyness_color': moneyness_color
                    }
                except (ValueError, TypeError):
                    pass
        
        return greeks_by_symbol
    
    except Exception as e:
        print(f"  Greeks error: {e}")
        return {}


def is_market_open():
    """Check if market is currently open."""
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now = datetime.now(eastern)
    if now.weekday() > 4:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now < market_close


# =============================================================================
# ROUTES
# =============================================================================

@app.route('/')
def dashboard():
    """Main dashboard page."""
    
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now = datetime.now(eastern)
    
    # Gather all data
    todays_signals = get_todays_signals()
    expiring_today = get_expiring_today()
    performance = get_performance_stats()
    bias = get_directional_bias()
    summary = get_journal_summary()
    market_open = is_market_open()

    # Fetch live Greeks for HIGH signals
    # Only fetch during market hours to avoid unnecessary API calls
    high_for_greeks = [s for s in todays_signals 
                        if s['signal_tier'] == 'HIGH'][:15]
    greeks_data = get_greeks_for_signals(high_for_greeks)
    
    # Calculate directional bias
    call_data = bias.get('CALL', {'count': 0, 'total_premium': 0})
    put_data = bias.get('PUT', {'count': 0, 'total_premium': 0})
    call_premium = call_data['total_premium'] or 0
    put_premium = put_data['total_premium'] or 0
    total_premium = call_premium + put_premium
    
    if total_premium > 0:
        call_pct = round((call_premium / total_premium) * 100, 1)
        put_pct = round((put_premium / total_premium) * 100, 1)
        bias_label = "BEARISH" if put_pct > 55 else "BULLISH" if call_pct > 55 else "NEUTRAL"
        bias_color = "#ef4444" if bias_label == "BEARISH" else "#22c55e" if bias_label == "BULLISH" else "#f59e0b"
    else:
        call_pct = put_pct = 0
        bias_label = "NO DATA"
        bias_color = "#6b7280"
    
    # Split signals by tier
    high_signals = [s for s in todays_signals if s['signal_tier'] == 'HIGH'][:15]
    inst_signals = [s for s in todays_signals if s['signal_tier'] == 'INST'][:10]
    
    # Format premium helper
    def fmt_premium(p):
        if p >= 1_000_000:
            return f"${p/1_000_000:.1f}M"
        elif p >= 1_000:
            return f"${p/1_000:.0f}K"
        return f"${p:.0f}"
    
    # Add formatted premium and decoded symbol to each signal
    for s in high_signals + inst_signals + expiring_today:
        s['premium_display'] = fmt_premium(s['premium'])
        s['decoded'] = decode_contract(s['contract'])
        s['greeks'] = greeks_data.get(s['contract'], {})

    # Signal history for chart — serialize to JSON for JS consumption
    import json
    signal_history = get_signal_history(days=30)
    # Format premium for each day
    for day in signal_history:
        p = day['total_premium'] or 0
        day['total_premium'] = p
        day['premium_display'] = fmt_premium(p)
    signal_history_json = json.dumps(signal_history)

    # Pass high/inst signals as JSON for DTE filter (days_out already decoded above)
    high_signals_json = json.dumps([{
        'contract': s['contract'],
        'contract_type': s['contract_type'],
        'premium_display': s['premium_display'],
        'composite_score': s['composite_score'],
        'days_out': s['decoded']['days_out'],
        'ticker': s['decoded']['ticker'],
        'strike_display': s['decoded']['strike_display'],
        'contract_type_display': s['decoded']['contract_type'],
        'expiry_display': s['decoded']['expiry_display'],
        'dte_display': s['decoded']['dte_display'],
        'delta': s['greeks'].get('delta', ''),
        'iv': s['greeks'].get('iv', ''),
        'moneyness': s['greeks'].get('moneyness', ''),
        'moneyness_color': s['greeks'].get('moneyness_color', ''),
        'has_greeks': bool(s['greeks']),
        'share_price': f"${s['share_price']:,.2f}" if s.get('share_price') else '',
    } for s in high_signals])

    inst_signals_json = json.dumps([{
        'contract': s['contract'],
        'contract_type': s['contract_type'],
        'premium_display': s['premium_display'],
        'composite_score': s['composite_score'],
        'days_out': s['decoded']['days_out'],
        'ticker': s['decoded']['ticker'],
        'strike_display': s['decoded']['strike_display'],
        'contract_type_display': s['decoded']['contract_type'],
        'expiry_display': s['decoded']['expiry_display'],
        'dte_display': s['decoded']['dte_display'],
        'share_price': f"${s['share_price']:,.2f}" if s.get('share_price') else '',
    } for s in inst_signals])
    
    return render_template_string(DASHBOARD_HTML,
        now=now.strftime('%Y-%m-%d %H:%M:%S %Z'),
        market_open=market_open,
        market_status="OPEN" if market_open else "CLOSED",
        market_color="#22c55e" if market_open else "#ef4444",
        high_signals=high_signals,
        inst_signals=inst_signals,
        expiring_today=expiring_today,
        performance=performance,
        summary=summary,
        call_pct=call_pct,
        put_pct=put_pct,
        call_premium_display=fmt_premium(call_premium),
        put_premium_display=fmt_premium(put_premium),
        bias_label=bias_label,
        bias_color=bias_color,
        todays_signal_count=len(todays_signals),
        greeks_data=greeks_data,
        signal_history_json=signal_history_json,
        high_signals_json=high_signals_json,
        inst_signals_json=inst_signals_json,
    )


@app.route('/api/record-outcome', methods=['POST'])
def record_outcome():
    """API endpoint to record signal outcomes from the dashboard."""
    from flask import request
    data = request.get_json()
    
    contract = data.get('contract')
    outcome = data.get('outcome')
    notes = data.get('notes', '')
    
    if not contract or outcome not in ['WIN', 'LOSS', 'FLAT']:
        return jsonify({'success': False, 'error': 'Invalid input'})
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE signals
        SET outcome = ?, outcome_notes = ?
        WHERE contract = ? AND outcome IS NULL
    """, (outcome, notes, contract))
    updated = cursor.rowcount
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'updated': updated})


@app.route('/api/data')
def api_data():
    """JSON endpoint for live data refresh."""
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now = datetime.now(eastern)
    
    return jsonify({
        'timestamp': now.strftime('%Y-%m-%d %H:%M:%S %Z'),
        'market_open': is_market_open(),
        'summary': get_journal_summary(),
        'bias': get_directional_bias(),
    })

@app.route('/api/last-scan')
def last_scan():
    """
    Returns the timestamp of the most recent completed scan.
    Reads from scan_log table which updates every run regardless
    of whether new signals were logged — reliable refresh trigger.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT MAX(scan_time) as last_scan, 
                   COUNT(*) as total_scans
            FROM scan_log
        """)
        result = cursor.fetchone()
        last = result['last_scan'] if result else None
    except Exception:
        # scan_log table might not exist yet on first run
        last = None
    
    conn.close()
    
    return jsonify({'last_scan': last})


@app.route('/api/signal-history')
def signal_history():
    """
    Returns last 30 days of signals grouped by date.
    Used for the history chart on the dashboard.
    """
    rows = get_signal_history(days=30)
    return jsonify(rows)


# =============================================================================
# DASHBOARD HTML TEMPLATE
# =============================================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Options Flow Scanner</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            background: #0f172a;
            color: #e2e8f0;
            font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
            font-size: 13px;
            min-height: 100vh;
        }
        
        /* ── Header ── */
        .header {
            background: #1e293b;
            border-bottom: 1px solid #334155;
            padding: 16px 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        
        .header-left {
            display: flex;
            align-items: center;
            gap: 16px;
        }
        
        .header h1 {
            font-size: 16px;
            font-weight: 600;
            color: #f1f5f9;
            letter-spacing: 0.05em;
        }
        
        .market-badge {
            padding: 3px 10px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.1em;
            background: {{ market_color }}22;
            color: {{ market_color }};
            border: 1px solid {{ market_color }}44;
        }
        
        .header-right {
            display: flex;
            align-items: center;
            gap: 16px;
            color: #64748b;
            font-size: 11px;
        }
        
        .refresh-btn {
            background: #334155;
            border: none;
            color: #94a3b8;
            padding: 4px 12px;
            border-radius: 4px;
            cursor: pointer;
            font-family: inherit;
            font-size: 11px;
        }
        
        .refresh-btn:hover { background: #475569; color: #e2e8f0; }
        
        /* ── Layout ── */
        .main {
            padding: 20px 24px;
            display: grid;
            grid-template-columns: 1fr 1fr;
            grid-template-rows: auto auto auto;
            gap: 16px;
            max-width: 1400px;
            margin: 0 auto;
        }

        /* Full-width cards span both columns */
        .full-width {
            grid-column: 1 / -1;
        }
        
        /* ── Cards ── */
        .card {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 16px;
        }
        
        .card-title {
            font-size: 11px;
            font-weight: 600;
            color: #64748b;
            letter-spacing: 0.1em;
            text-transform: uppercase;
            margin-bottom: 14px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .card-title .count {
            background: #334155;
            color: #94a3b8;
            padding: 1px 7px;
            border-radius: 10px;
            font-size: 10px;
        }
        
        /* ── Stats Row ── */
        .stats-row {
            grid-column: 1 / -1;
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
        }
        
        .stat-card {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 14px 16px;
        }
        
        .stat-label {
            font-size: 10px;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 6px;
        }
        
        .stat-value {
            font-size: 24px;
            font-weight: 700;
            color: #f1f5f9;
            line-height: 1;
        }
        
        .stat-sub {
            font-size: 10px;
            color: #64748b;
            margin-top: 4px;
        }

        /* ── Signal History Chart ── */
        .history-card {
            grid-column: 1 / -1;
        }

        .chart-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 14px;
        }

        .chart-legend {
            display: flex;
            gap: 16px;
            font-size: 10px;
            color: #64748b;
        }

        .legend-dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 2px;
            margin-right: 4px;
            vertical-align: middle;
        }

        .legend-line {
            display: inline-block;
            width: 14px;
            height: 2px;
            background: #e2e8f0;
            border-radius: 1px;
            vertical-align: middle;
            margin-right: 4px;
        }

        #historyChart {
            width: 100%;
            height: 160px;
            display: block;
        }

        .chart-empty {
            height: 160px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #475569;
            font-style: italic;
            font-size: 12px;
        }
        
        /* ── Bias Bar ── */
        .bias-card {
            grid-column: 1 / -1;
        }
        
        .bias-bar-container {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-top: 4px;
        }
        
        .bias-bar {
            flex: 1;
            height: 8px;
            background: #334155;
            border-radius: 4px;
            overflow: hidden;
        }
        
        .bias-bar-fill {
            height: 100%;
            border-radius: 4px;
            background: {{ bias_color }};
            width: {{ put_pct if bias_label == 'BEARISH' else call_pct }}%;
            transition: width 0.5s ease;
        }
        
        .bias-details {
            display: flex;
            gap: 20px;
            margin-top: 10px;
            font-size: 11px;
        }
        
        .bias-call { color: #22c55e; }
        .bias-put  { color: #ef4444; }
        
        .bias-label-display {
            font-size: 18px;
            font-weight: 700;
            color: {{ bias_color }};
            min-width: 80px;
            text-align: right;
        }

        /* ── DTE Filter Tabs ── */
        .dte-tabs {
            display: flex;
            gap: 4px;
            margin-left: auto;  /* push tabs to right of card-title */
        }

        .dte-tab {
            padding: 3px 10px;
            border-radius: 4px;
            border: 1px solid #334155;
            background: transparent;
            color: #64748b;
            font-family: inherit;
            font-size: 10px;
            font-weight: 600;
            letter-spacing: 0.05em;
            cursor: pointer;
            transition: all 0.15s;
            text-transform: uppercase;
        }

        .dte-tab:hover {
            border-color: #475569;
            color: #94a3b8;
        }

        .dte-tab.active {
            background: #334155;
            color: #f1f5f9;
            border-color: #475569;
        }
        
        /* ── Signal Tables ── */
        .signal-table {
            width: 100%;
            border-collapse: collapse;
        }
        
        .signal-table th {
            font-size: 10px;
            color: #475569;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            padding: 0 8px 8px 0;
            text-align: left;
            border-bottom: 1px solid #334155;
        }
        
        .signal-table td {
            padding: 7px 8px 7px 0;
            border-bottom: 1px solid #1e293b;
            vertical-align: middle;
        }
        
        .signal-table tr:last-child td { border-bottom: none; }
        
        .signal-table tr:hover td { background: #ffffff08; }
        
        .contract-cell {
            font-family: 'SF Mono', monospace;
            font-size: 11px;
            color: #cbd5e1;
        }
        
        .type-call { color: #22c55e; font-weight: 600; }
        .type-put  { color: #ef4444; font-weight: 600; }
        
        .premium-cell {
            color: #f1f5f9;
            font-weight: 600;
        }
        
        .score-cell { color: #94a3b8; }
        
        .tier-high  { color: #f97316; }
        .tier-inst  { color: #a855f7; }
        .tier-watch { color: #3b82f6; }

        .stock-price-cell {
            color: #64748b;
            font-size: 11px;
            white-space: nowrap;
        }

        .dte-badge {
            display: inline-block;
            padding: 1px 6px;
            border-radius: 3px;
            font-size: 10px;
            font-weight: 600;
            vertical-align: middle;
        }
        
        /* ── Outcome Buttons ── */
        .outcome-btn {
            padding: 2px 8px;
            border-radius: 3px;
            border: 1px solid;
            cursor: pointer;
            font-family: inherit;
            font-size: 10px;
            font-weight: 600;
            margin-right: 3px;
            background: transparent;
            transition: all 0.15s;
        }
        
        .btn-win  { color: #22c55e; border-color: #22c55e33; }
        .btn-loss { color: #ef4444; border-color: #ef443333; }
        .btn-flat { color: #64748b; border-color: #64748b33; }
        
        .btn-win:hover  { background: #22c55e22; }
        .btn-loss:hover { background: #ef444422; }
        .btn-flat:hover { background: #64748b22; }
        
        .outcome-recorded {
            font-size: 11px;
            font-weight: 600;
            padding: 2px 8px;
            border-radius: 3px;
        }
        
        .outcome-WIN  { color: #22c55e; background: #22c55e11; }
        .outcome-LOSS { color: #ef4444; background: #ef444411; }
        .outcome-FLAT { color: #64748b; background: #64748b11; }
        
        /* ── Performance Table ── */
        .perf-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid #334155;
        }
        
        .perf-row:last-child { border-bottom: none; }
        
        .perf-tier { font-weight: 600; }
        
        .win-rate-bar {
            flex: 1;
            margin: 0 12px;
            height: 4px;
            background: #334155;
            border-radius: 2px;
            overflow: hidden;
        }
        
        .win-rate-fill {
            height: 100%;
            background: #22c55e;
            border-radius: 2px;
        }
        
        .win-rate-label {
            font-size: 12px;
            font-weight: 700;
            min-width: 40px;
            text-align: right;
        }
        
        .no-data {
            color: #475569;
            font-style: italic;
            text-align: center;
            padding: 20px 0;
        }

        .table-empty {
            color: #475569;
            font-style: italic;
            text-align: center;
            padding: 16px 0;
            font-size: 11px;
        }
        
        /* ── Auto refresh indicator ── */
        .live-dot {
            width: 6px;
            height: 6px;
            background: #22c55e;
            border-radius: 50%;
            display: inline-block;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }

        .dte-urgent {
            background: #ef444422 !important;
            color: #ef4444 !important;
            border: 1px solid #ef444433;
        }

        .dte-soon {
            background: #f59e0b22 !important;
            color: #f59e0b !important;
            border: 1px solid #f59e0b33;
        }

        .dte-normal {
            background: #334155;
            color: #64748b;
        }
    </style>
</head>
<body>

<div class="header">
    <div class="header-left">
        <h1>OPTIONS FLOW SCANNER</h1>
        <span class="market-badge">{{ market_status }}</span>
    </div>
    <div class="header-right">
        <span><span class="live-dot"></span> {{ now }}</span>
        <button class="refresh-btn" onclick="location.reload()">↻ Refresh</button>
    </div>
</div>

<div class="main">

    <!-- Stats Row -->
    <div class="stats-row">
        <div class="stat-card">
            <div class="stat-label">Total Signals</div>
            <div class="stat-value">{{ summary.total or 0 }}</div>
            <div class="stat-sub">{{ summary.pending or 0 }} pending outcomes</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Today's Signals</div>
            <div class="stat-value">{{ todays_signal_count }}</div>
            <div class="stat-sub">logged this session</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Wins Recorded</div>
            <div class="stat-value" style="color:#22c55e">{{ summary.wins or 0 }}</div>
            <div class="stat-sub">{{ summary.losses or 0 }} losses · {{ summary.flats or 0 }} flat</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Signal Breakdown</div>
            <div class="stat-value" style="font-size:16px; padding-top:4px">
                <span style="color:#f97316">{{ summary.high_count or 0 }}H</span>
                <span style="color:#64748b; font-size:12px"> · </span>
                <span style="color:#a855f7">{{ summary.inst_count or 0 }}I</span>
                <span style="color:#64748b; font-size:12px"> · </span>
                <span style="color:#3b82f6">{{ summary.watch_count or 0 }}W</span>
            </div>
            <div class="stat-sub">HIGH · INST · WATCH</div>
        </div>
    </div>

    <!-- Signal History Chart -->
    <div class="card history-card">
        <div class="chart-header">
            <div class="card-title" style="margin-bottom:0">📉 Signal History — Last 30 Days</div>
            <div class="chart-legend">
                <span><span class="legend-dot" style="background:#f97316"></span>HIGH</span>
                <span><span class="legend-dot" style="background:#a855f7"></span>INST</span>
                <span><span class="legend-dot" style="background:#3b82f6"></span>WATCH</span>
                <span><span class="legend-line"></span>Premium Flow</span>
            </div>
        </div>
        <div id="chartContainer">
            <canvas id="historyChart"></canvas>
        </div>
    </div>

    <!-- Directional Bias -->
    <div class="card bias-card">
        <div class="card-title">📊 Directional Flow Bias — HIGH Signals Today</div>
        <div class="bias-bar-container">
            <div style="font-size:11px; color:#64748b; min-width:60px">
                {{ call_pct }}% calls
            </div>
            <div class="bias-bar">
                <div class="bias-bar-fill"></div>
            </div>
            <div style="font-size:11px; color:#64748b; min-width:60px; text-align:right">
                {{ put_pct }}% puts
            </div>
            <div class="bias-label-display">{{ bias_label }}</div>
        </div>
        <div class="bias-details">
            <span class="bias-call">▲ Calls: {{ call_premium_display }}</span>
            <span class="bias-put">▼ Puts: {{ put_premium_display }}</span>
        </div>
    </div>

    <!-- High Conviction Signals — full width -->
    <div class="card full-width">
        <div class="card-title">
            🔥 HIGH Conviction
            <span class="count" id="high-count">{{ high_signals|length }}</span>
            <div class="dte-tabs">
                <button class="dte-tab active" onclick="setDteFilter('all')">All</button>
                <button class="dte-tab" onclick="setDteFilter('0dte')">0DTE</button>
                <button class="dte-tab" onclick="setDteFilter('1-2d')">1–2d</button>
                <button class="dte-tab" onclick="setDteFilter('week')">Week</button>
                <button class="dte-tab" onclick="setDteFilter('30d+')">30d+</button>
            </div>
        </div>
        <table class="signal-table" id="high-table">
            <thead>
                <tr>
                    <th style="width:70px">Ticker</th>
                    <th style="width:90px">Strike</th>
                    <th style="width:60px">Type</th>
                    <th style="width:90px">Expiry</th>
                    <th style="width:90px">Stock @</th>
                    <th style="width:90px">Premium</th>
                    <th style="width:80px">Delta</th>
                    <th style="width:60px">IV</th>
                    <th style="width:55px">Score</th>
                </tr>
            </thead>
            <tbody id="high-tbody"></tbody>
        </table>
        <div class="table-empty" id="high-empty" style="display:none">No signals match this filter</div>
    </div>

    <!-- Institutional Flow — full width, stacked under HIGH -->
    <div class="card full-width">
        <div class="card-title">
            💰 Institutional Flow
            <span class="count" id="inst-count">{{ inst_signals|length }}</span>
            <div class="dte-tabs">
                <button class="dte-tab active" onclick="setDteFilter('all')">All</button>
                <button class="dte-tab" onclick="setDteFilter('0dte')">0DTE</button>
                <button class="dte-tab" onclick="setDteFilter('1-2d')">1–2d</button>
                <button class="dte-tab" onclick="setDteFilter('week')">Week</button>
                <button class="dte-tab" onclick="setDteFilter('30d+')">30d+</button>
            </div>
        </div>
        <table class="signal-table" id="inst-table">
            <thead>
                <tr>
                    <th style="width:70px">Ticker</th>
                    <th style="width:90px">Strike</th>
                    <th style="width:60px">Type</th>
                    <th style="width:90px">Expiry</th>
                    <th style="width:90px">Stock @</th>
                    <th style="width:90px">Premium</th>
                    <th style="width:55px">Score</th>
                </tr>
            </thead>
            <tbody id="inst-tbody"></tbody>
        </table>
        <div class="table-empty" id="inst-empty" style="display:none">No signals match this filter</div>
    </div>

    <!-- Performance + Expiring Today — side by side -->
    <div class="card">
        <div class="card-title">📈 Scanner Performance</div>
        {% if performance %}
            {% for p in performance %}
            <div class="perf-row">
                <span class="perf-tier tier-{{ p.signal_tier|lower }}">
                    {{ p.signal_tier }}
                </span>
                <div class="win-rate-bar">
                    <div class="win-rate-fill" style="width: {{
                        ((p.wins / p.total) * 100)|round|int if p.total > 0 else 0
                    }}%"></div>
                </div>
                <span class="win-rate-label" style="color:#22c55e">
                    {{ ((p.wins / p.total) * 100)|round(1) if p.total > 0 else 0 }}%
                </span>
                <span style="color:#475569; font-size:10px; margin-left:8px">
                    {{ p.wins }}W / {{ p.losses }}L / {{ p.flats }}F
                </span>
            </div>
            {% endfor %}
        {% else %}
        <div class="no-data">No outcomes recorded yet</div>
        {% endif %}
    </div>

    <div class="card">
        <div class="card-title">
            ⏰ Expiring Today
            <span class="count">{{ expiring_today|length }}</span>
        </div>
        {% if expiring_today %}
        <table class="signal-table">
            <thead>
                <tr>
                    <th>Ticker</th>
                    <th>Strike</th>
                    <th>Type</th>
                    <th>Expiry</th>
                    <th>Premium</th>
                    <th>Outcome</th>
                </tr>
            </thead>
            <tbody>
                {% for s in expiring_today %}
                <tr id="row-{{ loop.index }}">
                    <td style="color:#f1f5f9; font-weight:700; font-size:12px;">
                        {{ s.decoded.ticker }}
                    </td>
                    <td style="color:#cbd5e1; font-size:12px; font-weight:600;">
                        {{ s.decoded.strike_display }}
                    </td>
                    <td>
                        <span class="{{ 'type-call' if s.contract_type == 'CALL' else 'type-put' }}">
                            {{ s.decoded.contract_type }}
                        </span>
                    </td>
                    <td style="color:#64748b; font-size:11px;">
                        {{ s.decoded.expiry_display }}
                        <span class="dte-badge {% if s.decoded.days_out == 0 %}dte-urgent{% elif s.decoded.days_out <= 2 %}dte-soon{% else %}dte-normal{% endif %}"
                              style="margin-left:4px;">
                            {{ s.decoded.dte_display }}
                        </span>
                    </td>
                    <td class="premium-cell">{{ s.premium_display }}</td>
                    <td>
                        {% if s.outcome %}
                        <span class="outcome-recorded outcome-{{ s.outcome }}">
                            {{ s.outcome }}
                        </span>
                        {% else %}
                        <button class="outcome-btn btn-win"
                            onclick="recordOutcome('{{ s.contract }}', 'WIN', {{ loop.index }})">W</button>
                        <button class="outcome-btn btn-loss"
                            onclick="recordOutcome('{{ s.contract }}', 'LOSS', {{ loop.index }})">L</button>
                        <button class="outcome-btn btn-flat"
                            onclick="recordOutcome('{{ s.contract }}', 'FLAT', {{ loop.index }})">F</button>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="no-data">No signals expiring today</div>
        {% endif %}
    </div>

</div>

<script>
{% raw %}

// ─────────────────────────────────────────────
// DATA (injected server-side, parsed once)
// ─────────────────────────────────────────────
{% endraw %}
const SIGNAL_HISTORY = {{ signal_history_json | safe }};
const HIGH_SIGNALS   = {{ high_signals_json | safe }};
const INST_SIGNALS   = {{ inst_signals_json | safe }};
{% raw %}

// ─────────────────────────────────────────────
// SIGNAL HISTORY CHART
// ─────────────────────────────────────────────
(function renderHistoryChart() {
    const canvas = document.getElementById('historyChart');
    if (!canvas) return;

    if (!SIGNAL_HISTORY || SIGNAL_HISTORY.length === 0) {
        document.getElementById('chartContainer').innerHTML =
            '<div class="chart-empty">No historical data yet — signals will appear here after the scanner runs on multiple days</div>';
        return;
    }

    // ── Layout constants ──
    const PADDING = { top: 12, right: 60, bottom: 32, left: 36 };
    const BAR_GAP = 0.25; // fraction of slot width used as gap between groups

    // ── Colour palette ──
    const COLORS = {
        high:    '#f97316',
        inst:    '#a855f7',
        watch:   '#3b82f6',
        premium: '#e2e8f0',
    };

    // ── Sizing: fill container width, fixed height ──
    const container = canvas.parentElement;
    const W = container.clientWidth || 800;
    const H = 160;
    canvas.width  = W;
    canvas.height = H;

    const ctx = canvas.getContext('2d');
    const innerW = W - PADDING.left - PADDING.right;
    const innerH = H - PADDING.top  - PADDING.bottom;

    // ── Data ranges ──
    const days      = SIGNAL_HISTORY;
    const n         = days.length;
    const maxCount  = Math.max(1, ...days.map(d => (d.high_count || 0) + (d.inst_count || 0) + (d.watch_count || 0)));
    const maxPrem   = Math.max(1, ...days.map(d => d.total_premium || 0));

    // ── Helpers ──
    const xSlot  = i  => PADDING.left + (i / n) * innerW;
    const slotW  = () => innerW / n;
    const yCount = v  => PADDING.top + innerH - (v / maxCount) * innerH;
    const yPrem  = v  => PADDING.top + innerH - (v / maxPrem)  * innerH;

    // ── Grid lines (count axis) ──
    const gridLines = 4;
    ctx.strokeStyle = '#334155';
    ctx.lineWidth   = 1;
    for (let i = 0; i <= gridLines; i++) {
        const y = PADDING.top + (innerH / gridLines) * i;
        ctx.beginPath();
        ctx.moveTo(PADDING.left, y);
        ctx.lineTo(W - PADDING.right, y);
        ctx.stroke();
    }

    // ── Stacked bars ──
    const bw = slotW() * (1 - BAR_GAP);  // bar group width
    const bx = i => xSlot(i) + slotW() * BAR_GAP / 2;  // group left edge

    days.forEach((d, i) => {
        let base = PADDING.top + innerH;
        const tiers = [
            { key: 'watch_count', color: COLORS.watch },
            { key: 'inst_count',  color: COLORS.inst  },
            { key: 'high_count',  color: COLORS.high  },
        ];

        tiers.forEach(({ key, color }) => {
            const count = d[key] || 0;
            if (count === 0) return;
            const h = (count / maxCount) * innerH;
            ctx.fillStyle = color;
            ctx.fillRect(bx(i), base - h, bw, h);
            base -= h;
        });
    });

    // ── Premium line (right-axis) ──
    ctx.beginPath();
    ctx.strokeStyle = COLORS.premium;
    ctx.lineWidth   = 1.5;
    ctx.setLineDash([3, 3]);

    days.forEach((d, i) => {
        const x = bx(i) + bw / 2;
        const y = yPrem(d.total_premium || 0);
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.setLineDash([]);

    // ── Premium dots ──
    days.forEach((d, i) => {
        const x = bx(i) + bw / 2;
        const y = yPrem(d.total_premium || 0);
        ctx.beginPath();
        ctx.arc(x, y, 2.5, 0, Math.PI * 2);
        ctx.fillStyle = COLORS.premium;
        ctx.fill();
    });

    // ── X-axis date labels (show every ~5 days to avoid crowding) ──
    ctx.fillStyle  = '#475569';
    ctx.font       = '9px SF Mono, Consolas, monospace';
    ctx.textAlign  = 'center';
    const labelStep = Math.max(1, Math.ceil(n / 6));

    days.forEach((d, i) => {
        if (i % labelStep !== 0 && i !== n - 1) return;
        const x = bx(i) + bw / 2;
        const label = d.scan_date ? d.scan_date.slice(5) : '';  // MM-DD
        ctx.fillText(label, x, H - 6);
    });

    // ── Left Y-axis labels (count) ──
    ctx.textAlign = 'right';
    ctx.fillStyle = '#475569';
    for (let i = 0; i <= gridLines; i++) {
        const v = Math.round((maxCount / gridLines) * (gridLines - i));
        const y = PADDING.top + (innerH / gridLines) * i;
        ctx.fillText(v, PADDING.left - 4, y + 3);
    }

    // ── Right Y-axis labels (premium) ──
    ctx.textAlign = 'left';
    ctx.fillStyle = '#64748b';
    const premSteps = [0, 0.5, 1];
    premSteps.forEach(frac => {
        const v = maxPrem * frac;
        const y = yPrem(v);
        let label;
        if (v >= 1_000_000) label = '$' + (v / 1_000_000).toFixed(1) + 'M';
        else if (v >= 1_000) label = '$' + Math.round(v / 1_000) + 'K';
        else label = '$' + Math.round(v);
        ctx.fillText(label, W - PADDING.right + 4, y + 3);
    });

    // ── Tooltip on hover ──
    canvas.addEventListener('mousemove', (e) => {
        const rect  = canvas.getBoundingClientRect();
        const mx    = e.clientX - rect.left;
        const myRel = e.clientY - rect.top;

        const idx = Math.floor(((mx - PADDING.left) / innerW) * n);
        if (idx < 0 || idx >= n) {
            canvas.title = '';
            return;
        }
        const d = days[idx];
        const total = (d.high_count || 0) + (d.inst_count || 0) + (d.watch_count || 0);
        canvas.title =
            `${d.scan_date}\n` +
            `Signals: ${total} (H:${d.high_count || 0} I:${d.inst_count || 0} W:${d.watch_count || 0})\n` +
            `Premium: ${d.premium_display || '$0'}`;
    });
})();


// ─────────────────────────────────────────────
// DTE FILTER TABS
// ─────────────────────────────────────────────

// Active DTE bucket — shared across both tables
let activeDte = 'all';

// Classify a signal's days_out into a bucket
function dteBucket(daysOut) {
    if (daysOut === 0)               return '0dte';
    if (daysOut >= 1 && daysOut <= 2) return '1-2d';
    if (daysOut >= 3 && daysOut <= 7) return 'week';
    if (daysOut > 7)                 return '30d+';
    return 'other';
}

// Returns true if signal passes the current filter
function passesFilter(signal) {
    if (activeDte === 'all') return true;
    return dteBucket(signal.days_out) === activeDte;
}

// DTE badge class helper
function dteBadgeClass(daysOut) {
    if (daysOut === 0)      return 'dte-urgent';
    if (daysOut <= 2)       return 'dte-soon';
    return 'dte-normal';
}

// Render HIGH table
function renderHighTable() {
    const tbody  = document.getElementById('high-tbody');
    const empty  = document.getElementById('high-empty');
    const count  = document.getElementById('high-count');
    const filtered = HIGH_SIGNALS.filter(passesFilter);

    count.textContent = filtered.length;

    if (filtered.length === 0) {
        tbody.innerHTML = '';
        empty.style.display = 'block';
        return;
    }
    empty.style.display = 'none';

    tbody.innerHTML = filtered.map(s => `
        <tr>
            <td style="color:#f1f5f9; font-weight:700; font-size:12px;">${s.ticker}</td>
            <td style="color:#cbd5e1; font-size:12px; font-weight:600;">${s.strike_display}</td>
            <td>
                <span class="${s.contract_type === 'CALL' ? 'type-call' : 'type-put'}">
                    ${s.contract_type_display}
                </span>
            </td>
            <td style="color:#64748b; font-size:11px; white-space:nowrap;">
                ${s.expiry_display}
                <span class="dte-badge ${dteBadgeClass(s.days_out)}" style="margin-left:4px;">
                    ${s.dte_display}
                </span>
            </td>
            <td class="stock-price-cell">
                ${s.share_price ? s.share_price : '<span style="color:#334155;">—</span>'}
            </td>
            <td class="premium-cell">${s.premium_display}</td>
            <td>
                ${s.has_greeks ? `
                    <span style="font-size:12px; font-weight:600; color:#e2e8f0;">${s.delta}</span>
                    <span style="font-size:10px; margin-left:4px; color:${s.moneyness_color};">${s.moneyness}</span>
                ` : '<span style="color:#475569;">—</span>'}
            </td>
            <td>
                ${s.has_greeks
                    ? `<span style="font-size:12px; color:#94a3b8;">${s.iv}</span>`
                    : '<span style="color:#475569;">—</span>'}
            </td>
            <td class="score-cell">${s.composite_score}</td>
        </tr>
    `).join('');
}

// Render INST table
function renderInstTable() {
    const tbody  = document.getElementById('inst-tbody');
    const empty  = document.getElementById('inst-empty');
    const count  = document.getElementById('inst-count');
    const filtered = INST_SIGNALS.filter(passesFilter);

    count.textContent = filtered.length;

    if (filtered.length === 0) {
        tbody.innerHTML = '';
        empty.style.display = 'block';
        return;
    }
    empty.style.display = 'none';

    tbody.innerHTML = filtered.map(s => `
        <tr>
            <td style="color:#f1f5f9; font-weight:700; font-size:12px;">${s.ticker}</td>
            <td style="color:#cbd5e1; font-size:12px; font-weight:600;">${s.strike_display}</td>
            <td>
                <span class="${s.contract_type === 'CALL' ? 'type-call' : 'type-put'}">
                    ${s.contract_type_display}
                </span>
            </td>
            <td style="color:#64748b; font-size:11px; white-space:nowrap;">
                ${s.expiry_display}
                <span class="dte-badge ${dteBadgeClass(s.days_out)}" style="margin-left:4px;">
                    ${s.dte_display}
                </span>
            </td>
            <td class="stock-price-cell">
                ${s.share_price ? s.share_price : '<span style="color:#334155;">—</span>'}
            </td>
            <td class="premium-cell">${s.premium_display}</td>
            <td class="score-cell">${s.composite_score}</td>
        </tr>
    `).join('');
}

// Tab click handler — updates both tables and all tab buttons simultaneously
function setDteFilter(bucket) {
    activeDte = bucket;

    // Update all tab buttons (two sets — one per card)
    document.querySelectorAll('.dte-tab').forEach(btn => {
        const btnBucket = btn.getAttribute('onclick').match(/'(.+?)'/)[1];
        btn.classList.toggle('active', btnBucket === bucket);
    });

    renderHighTable();
    renderInstTable();
}

// Initial render
renderHighTable();
renderInstTable();


// ─────────────────────────────────────────────
// OUTCOME RECORDING
// ─────────────────────────────────────────────
async function recordOutcome(contract, outcome, rowIndex) {
    try {
        const response = await fetch('/api/record-outcome', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ contract, outcome, notes: '' })
        });
        
        const data = await response.json();
        
        if (data.success) {
            const td = document.querySelector(`#row-${rowIndex} td:last-child`);
            td.innerHTML = `
                <span class="outcome-recorded outcome-${outcome}">
                    ${outcome}
                </span>`;
        } else {
            alert('Failed to record outcome');
        }
    } catch (err) {
        console.error('Error:', err);
    }
}


// ─────────────────────────────────────────────
// AUTO-REFRESH POLLING (scan-based)
// ─────────────────────────────────────────────
let lastKnownScan = null;

async function checkForNewScan() {
    try {
        const response = await fetch('/api/last-scan');
        const data = await response.json();
        
        if (lastKnownScan === null) {
            lastKnownScan = data.last_scan;
        } else if (data.last_scan !== lastKnownScan) {
            location.reload();
        }
    } catch (err) {
        console.log('Refresh check failed:', err);
    }
}

setInterval(checkForNewScan, 60 * 1000);
checkForNewScan();

{% endraw %}
</script>

</body>
</html>
"""

# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    print("\n" + "="*50)
    print("  OPTIONS FLOW SCANNER — DASHBOARD")
    print("  Open your browser to: http://localhost:5000")
    print("  Press Ctrl+C to stop")
    print("="*50 + "\n")
    
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(debug=False, host='0.0.0.0', port=5000)