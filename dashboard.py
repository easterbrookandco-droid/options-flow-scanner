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

    <!-- High Conviction Signals -->
    <div class="card">
        <div class="card-title">
            🔥 HIGH Conviction
            <span class="count">{{ high_signals|length }}</span>
        </div>
        {% if high_signals %}
        <table class="signal-table">
            <thead>
                <tr>
                    <th>Ticker</th>
                    <th>Strike / Type / Expiry</th>
                    <th>Premium</th>
                    <th>Delta</th>
                    <th>IV</th>
                    <th>Score</th>
                </tr>
            </thead>
            <tbody>
                {% for s in high_signals %}
                <tr>
                    <td style="color:#f1f5f9; font-weight:700; font-size:12px;">
                        {{ s.decoded.ticker }}
                    </td>
                    <td>
                        <div style="color:#cbd5e1; font-size:12px; font-weight:600;">
                            {{ s.decoded.strike_display }}
                            <span class="{{ 'type-call' if s.contract_type == 'CALL' else 'type-put' }}">
                                {{ s.decoded.contract_type }}
                            </span>
                        </div>
                        <div style="color:#475569; font-size:10px; margin-top:2px;">
                            {{ s.decoded.expiry_display }}
                            <span style="padding:1px 5px; border-radius:3px; margin-left:4px;"
                                class="{% if s.decoded.days_out == 0 %}dte-urgent{% elif s.decoded.days_out <= 2 %}dte-soon{% else %}dte-normal{% endif %}">
                                {{ s.decoded.dte_display }}
                            </span>
                        </div>
                    </td>
                    <td class="premium-cell">{{ s.premium_display }}</td>
                    <td>
                        {% if s.greeks %}
                        <div style="font-size:12px; font-weight:600; color:#e2e8f0;">
                            {{ s.greeks.delta }}
                        </div>
                        <div style="font-size:10px; margin-top:2px;">
                            <span style="color:{{ s.greeks.moneyness_color }};">
                                {{ s.greeks.moneyness }}
                            </span>
                        </div>
                        {% else %}
                        <span style="color:#475569;">—</span>
                        {% endif %}
                    </td>
                    <td>
                        {% if s.greeks %}
                        <span style="font-size:12px; color:#94a3b8;">
                            {{ s.greeks.iv }}
                        </span>
                        {% else %}
                        <span style="color:#475569;">—</span>
                        {% endif %}
                    </td>
                    <td class="score-cell">{{ s.composite_score }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="no-data">No HIGH signals today yet</div>
        {% endif %}
    </div>

    <!-- Institutional Signals -->
    <div class="card">
        <div class="card-title">
            💰 Institutional Flow
            <span class="count">{{ inst_signals|length }}</span>
        </div>
        {% if inst_signals %}
        <table class="signal-table">
            <thead>
                <tr>
                    <th>Ticker</th>
                    <th>Strike / Type / Expiry</th>
                    <th>Premium</th>
                    <th>Score</th>
                </tr>
            </thead>
            <tbody>
                {% for s in inst_signals %}
                <tr>
                    <td style="color:#f1f5f9; font-weight:700; font-size:12px;">
                        {{ s.decoded.ticker }}
                    </td>
                    <td>
                        <div style="color:#cbd5e1; font-size:12px; font-weight:600;">
                            {{ s.decoded.strike_display }}
                            <span class="{{ 'type-call' if s.contract_type == 'CALL' else 'type-put' }}">
                                {{ s.decoded.contract_type }}
                            </span>
                        </div>
                        <div style="color:#475569; font-size:10px; margin-top:2px;">
                            {{ s.decoded.expiry_display }}
                            <span style="padding:1px 5px; border-radius:3px; margin-left:4px;"
                                class="{% if s.decoded.days_out == 0 %}dte-urgent{% elif s.decoded.days_out <= 2 %}dte-soon{% else %}dte-normal{% endif %}">
                                {{ s.decoded.dte_display }}
                            </span>
                        </div>
                    </td>
                    <td class="premium-cell">{{ s.premium_display }}</td>
                    <td class="score-cell">{{ s.composite_score }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="no-data">No INST signals today yet</div>
        {% endif %}
    </div>

    <!-- Performance -->
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

    <!-- Expiring Today -->
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
                    <th>Strike / Type / Expiry</th>
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
                    <td>
                        <div style="color:#cbd5e1; font-size:12px; font-weight:600;">
                            {{ s.decoded.strike_display }}
                            <span class="{{ 'type-call' if s.contract_type == 'CALL' else 'type-put' }}">
                                {{ s.decoded.contract_type }}
                            </span>
                        </div>
                        <div style="color:#475569; font-size:10px; margin-top:2px;">
                            {{ s.decoded.expiry_display }}
                            <span style="padding:1px 5px; border-radius:3px; margin-left:4px;"
                                class="{% if s.decoded.days_out == 0 %}dte-urgent{% elif s.decoded.days_out <= 2 %}dte-soon{% else %}dte-normal{% endif %}">
                                {{ s.decoded.dte_display }}
                            </span>
                        </div>
                    </td>
                    <td class="premium-cell">{{ s.premium_display }}</td>
                    <td>
                        {% if s.outcome %}
                        <span class="outcome-recorded outcome-{{ s.outcome }}">
                            {{ s.outcome }}
                        </span>
                        {% else %}
                        <button class="outcome-btn btn-win"
                            onclick="recordOutcome('{{ s.contract }}', 'WIN', {{ loop.index }})">
                            W
                        </button>
                        <button class="outcome-btn btn-loss"
                            onclick="recordOutcome('{{ s.contract }}', 'LOSS', {{ loop.index }})">
                            L
                        </button>
                        <button class="outcome-btn btn-flat"
                            onclick="recordOutcome('{{ s.contract }}', 'FLAT', {{ loop.index }})">
                            F
                        </button>
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
    // Record outcome via API without page reload
    async function recordOutcome(contract, outcome, rowIndex) {
        try {
            const response = await fetch('/api/record-outcome', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ contract, outcome, notes: '' })
            });
            
            const data = await response.json();
            
            if (data.success) {
                // Update the UI immediately without reload
                const td = document.querySelector(`#row-${rowIndex} td:last-child`);
                const colorMap = {
                    'WIN': '#22c55e', 'LOSS': '#ef4444', 'FLAT': '#64748b'
                };
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
    
    // Poll for new scan data every 60 seconds
    // Only reload when scanner has produced new signals
    let lastKnownScan = null;

    async function checkForNewScan() {
        try {
            const response = await fetch('/api/last-scan');
            const data = await response.json();
            
            console.log('Polling check — last scan:', data.last_scan, '| known:', lastKnownScan);
            
            if (lastKnownScan === null) {
                lastKnownScan = data.last_scan;
                console.log('Baseline set:', lastKnownScan);
            } else if (data.last_scan !== lastKnownScan) {
                console.log('New scan detected — reloading');
                location.reload();
            }
        } catch (err) {
            console.log('Refresh check failed:', err);
        }
    }

    // Check every 60 seconds
    setInterval(checkForNewScan, 60 * 1000);

    // Run immediately on load to establish baseline
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