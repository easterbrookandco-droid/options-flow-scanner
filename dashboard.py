from flask import Flask, render_template_string, jsonify, request
from flask_httpauth import HTTPBasicAuth
import sqlite3
from datetime import datetime
import pytz
import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()
SECRET_KEY      = os.getenv("PUBLIC_SECRET_KEY")
DASHBOARD_USER  = os.getenv("DASHBOARD_USER", "nolan")
DASHBOARD_PASS  = os.getenv("DASHBOARD_PASS", "scanner2026")
BASE_URL = "https://api.public.com"
DB_PATH = "signals.db"
MARKET_TIMEZONE = "US/Eastern"
STARTING_BANKROLL = 10_000


# =============================================================================
# CONTRACT DECODER
# =============================================================================

def decode_contract(symbol):
    try:
        strike_str = symbol[-8:]
        contract_type_char = symbol[-9]
        date_str = symbol[-15:-9]
        ticker = symbol[:-15]
        strike = float(strike_str) / 1000
        contract_type = "Call" if contract_type_char == "C" else "Put"
        year = int("20" + date_str[0:2])
        month = int(date_str[2:4])
        day = int(date_str[4:6])
        expiry = datetime(year, month, day)
        expiry_display = expiry.strftime("%b %d")
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
        return {
            'ticker': symbol, 'strike': 0, 'strike_display': '',
            'contract_type': '', 'expiry_display': '', 'dte_display': '',
            'days_out': 999, 'readable': symbol
        }


# =============================================================================
# FORMAT HELPERS
# =============================================================================

def fmt_premium(p):
    if p is None: return '$0'
    if p >= 1_000_000: return f"${p/1_000_000:.1f}M"
    if p >= 1_000: return f"${p/1_000:.0f}K"
    return f"${p:.0f}"

def fmt_pnl(v):
    if v is None: return '+$0.00'
    return f"+${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"

def fmt_pct(v):
    if v is None: return '0.0%'
    return f"+{v:.1f}%" if v >= 0 else f"{v:.1f}%"


# =============================================================================
# FLASK APP
# =============================================================================

app  = Flask(__name__)
auth = HTTPBasicAuth()

@auth.verify_password
def verify_password(username, password):
    return username == DASHBOARD_USER and password == DASHBOARD_PASS


# =============================================================================
# DB — CONNECTION
# =============================================================================

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# =============================================================================
# DB — SIGNALS TAB
# =============================================================================

def get_todays_signals():
    conn = get_db_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    c = conn.cursor()
    c.execute("""
        SELECT * FROM signals
        WHERE scan_time LIKE ?
        ORDER BY composite_score DESC, premium DESC
    """, (f"{today}%",))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_expiring_today():
    conn = get_db_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    c = conn.cursor()
    c.execute("""
        SELECT * FROM signals
        WHERE expiration = ?
        AND signal_tier IN ('HIGH', 'INST')
        ORDER BY composite_score DESC
    """, (today,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_directional_bias():
    conn = get_db_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    c = conn.cursor()
    c.execute("""
        SELECT contract_type, COUNT(*) as count, SUM(premium) as total_premium
        FROM signals
        WHERE scan_time LIKE ? AND signal_tier = 'HIGH'
        GROUP BY contract_type
    """, (f"{today}%",))
    results = {row['contract_type']: dict(row) for row in c.fetchall()}
    conn.close()
    return results


def get_signal_history(days=30):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
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
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


# =============================================================================
# DB — PORTFOLIO TAB
# =============================================================================

def get_closed_summary():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
            ROUND(SUM(pnl), 2) as total_pnl,
            ROUND(AVG(pnl), 2) as avg_pnl,
            ROUND(AVG(CASE WHEN pnl > 0 THEN pnl END), 2) as avg_win,
            ROUND(AVG(CASE WHEN pnl <= 0 THEN pnl END), 2) as avg_loss,
            ROUND(AVG(hold_days), 1) as avg_hold_days,
            MAX(pnl) as best_trade,
            MIN(pnl) as worst_trade
        FROM paper_trades
        WHERE status IN ('CLOSED', 'STOP_TRIGGERED')
    """)
    result = dict(c.fetchone())
    conn.close()
    return result


def get_today_activity():
    conn = get_db_connection()
    c = conn.cursor()
    import pytz
    eastern = pytz.timezone("US/Eastern")
    today = datetime.now(eastern).strftime("%Y-%m-%d")
    c.execute("""
        WITH last_snaps AS (
            SELECT trade_id, MAX(id) AS max_id
            FROM position_snapshots
            GROUP BY trade_id
        )
        SELECT pt.id, pt.signal_contract, pt.entry_price, pt.contracts,
               pt.status, pt.dte_at_entry, pt.exit_price,
               pt.pnl AS realized_pnl, pt.score_at_entry,
               s.ticker, s.contract_type, s.premium,
               ps.current_price AS last_price,
               ps.pnl AS unrealized_pnl
        FROM paper_trades pt
        JOIN signals s ON pt.signal_contract = s.contract
        LEFT JOIN last_snaps ls ON ls.trade_id = pt.id
        LEFT JOIN position_snapshots ps ON ps.id = ls.max_id
        WHERE pt.entry_date = ?
        ORDER BY pt.entry_time DESC
    """, (today,))
    entered = [dict(r) for r in c.fetchall()]
    c.execute("""
        SELECT pt.id, pt.signal_contract, pt.entry_price, pt.exit_price,
               pt.pnl, pt.pnl_pct, pt.exit_reason, pt.hold_days,
               s.ticker, s.contract_type
        FROM paper_trades pt
        JOIN signals s ON pt.signal_contract = s.contract
        WHERE pt.exit_date = ? AND pt.status = 'CLOSED'
        ORDER BY pt.exit_time DESC
    """, (today,))
    closed_today = [dict(r) for r in c.fetchall()]
    conn.close()
    return entered, closed_today


def get_exit_reason_breakdown():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT
            exit_reason,
            COUNT(*) as count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(SUM(pnl), 2) as total_pnl,
            ROUND(AVG(pnl_pct), 1) as avg_pct
        FROM paper_trades
        WHERE status IN ('CLOSED', 'STOP_TRIGGERED')
        GROUP BY exit_reason
        ORDER BY total_pnl DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_ticker_breakdown():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT
            s.ticker,
            COUNT(*) as count,
            SUM(CASE WHEN pt.pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pt.pnl <= 0 THEN 1 ELSE 0 END) as losses,
            ROUND(SUM(pt.pnl), 2) as total_pnl,
            ROUND(AVG(pt.pnl_pct), 1) as avg_pct
        FROM paper_trades pt
        JOIN signals s ON pt.signal_contract = s.contract
        WHERE pt.status IN ('CLOSED', 'STOP_TRIGGERED')
        GROUP BY s.ticker
        ORDER BY total_pnl DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


# =============================================================================
# DB — POSITIONS TAB
# =============================================================================

def get_open_positions():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        WITH last_snapshots AS (
            SELECT trade_id, MAX(id) as max_id
            FROM position_snapshots
            GROUP BY trade_id
        )
        SELECT
            pt.id,
            pt.signal_contract,
            pt.entry_price,
            pt.contracts,
            pt.total_cost,
            pt.status,
            pt.dte_at_entry,
            pt.score_at_entry,
            pt.entry_date,
            pt.exit_date,
            pt.exit_price,
            pt.pnl          AS realized_pnl,
            pt.pnl_pct      AS realized_pnl_pct,
            pt.hold_days,
            s.premium,
            ps.current_price as last_price,
            ps.pnl           as last_pnl,
            ps.pnl_pct       as last_pnl_pct,
            ps.snapshot_time,
            ps.dynamic_stop,
            ps.current_dte
        FROM paper_trades pt
        LEFT JOIN signals s ON pt.signal_contract = s.contract
        JOIN last_snapshots ls ON ls.trade_id = pt.id
        JOIN position_snapshots ps ON ps.id = ls.max_id
        WHERE pt.status IN ('OPEN', 'STOP_TRIGGERED')
        ORDER BY ps.pnl DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_closed_positions_summary():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        WITH last_snaps AS (
            SELECT trade_id, MAX(id) AS max_id
            FROM position_snapshots
            GROUP BY trade_id
        )
        SELECT
            COUNT(*)                                             AS count,
            SUM(CASE WHEN pt.pnl > 0 THEN 1 ELSE 0 END)        AS wins,
            ROUND(SUM(pt.pnl), 2)                               AS total_realized,
            ROUND(SUM(pt.total_cost), 2)                        AS total_cost,
            ROUND(SUM(
                CASE WHEN ps.current_price IS NOT NULL AND pt.exit_price IS NOT NULL
                     THEN (ps.current_price - pt.exit_price) * 100 * pt.contracts
                     ELSE 0 END
            ), 2)                                               AS total_post_exit
        FROM paper_trades pt
        LEFT JOIN last_snaps ls ON ls.trade_id = pt.id
        LEFT JOIN position_snapshots ps ON ps.id = ls.max_id
        WHERE pt.status = 'CLOSED'
    """)
    result = dict(c.fetchone())
    conn.close()
    return result


# =============================================================================
# DB — ANALYTICS TAB
# =============================================================================

def get_score_bucket_stats():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT
            CASE
                WHEN score_at_entry >= 8.0 THEN '8.0+'
                WHEN score_at_entry >= 7.0 THEN '7.0-7.9'
                WHEN score_at_entry >= 6.0 THEN '6.0-6.9'
                WHEN score_at_entry >= 5.0 THEN '5.0-5.9'
                WHEN score_at_entry >= 4.0 THEN '4.0-4.9'
                ELSE 'Under 4.0'
            END as score_bucket,
            COUNT(*) as total,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(AVG(pnl), 2) as avg_pnl,
            ROUND(AVG(pnl_pct), 1) as avg_pct,
            ROUND(SUM(pnl), 2) as total_pnl
        FROM paper_trades
        WHERE status IN ('CLOSED', 'STOP_TRIGGERED') AND pnl IS NOT NULL AND score_at_entry IS NOT NULL
        GROUP BY score_bucket
        ORDER BY MIN(score_at_entry) DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_dte_bucket_stats():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT
            CASE
                WHEN dte_at_entry = 0  THEN '0DTE'
                WHEN dte_at_entry <= 2 THEN '1-2d'
                WHEN dte_at_entry <= 5 THEN '3-5d'
                WHEN dte_at_entry <= 14 THEN '6-14d'
                WHEN dte_at_entry <= 30 THEN '15-30d'
                ELSE '30d+'
            END as dte_bucket,
            COUNT(*) as total,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(AVG(pnl), 2) as avg_pnl,
            ROUND(AVG(pnl_pct), 1) as avg_pct,
            ROUND(SUM(pnl), 2) as total_pnl
        FROM paper_trades
        WHERE status IN ('CLOSED', 'STOP_TRIGGERED') AND pnl IS NOT NULL
        GROUP BY dte_bucket
        ORDER BY MIN(dte_at_entry)
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_premium_tier_stats():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT
            CASE
                WHEN s.premium >= 5000000 THEN '$5M+'
                WHEN s.premium >= 2000000 THEN '$2M-5M'
                WHEN s.premium >= 1000000 THEN '$1M-2M'
                WHEN s.premium >= 500000  THEN '$500K-1M'
                WHEN s.premium >= 100000  THEN '$100K-500K'
                ELSE 'Under $100K'
            END as premium_tier,
            COUNT(*) as total,
            SUM(CASE WHEN pt.pnl > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(AVG(pt.pnl), 2) as avg_pnl,
            ROUND(AVG(pt.pnl_pct), 1) as avg_pct,
            ROUND(SUM(pt.pnl), 2) as total_pnl
        FROM paper_trades pt
        JOIN signals s ON pt.signal_contract = s.contract
        WHERE pt.status IN ('CLOSED', 'STOP_TRIGGERED') AND pt.pnl IS NOT NULL
        GROUP BY premium_tier
        ORDER BY MIN(s.premium) DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_key_insights():
    """Generate plain English insights from actual DB performance numbers."""
    MIN_SAMPLE = 10  # buckets with fewer trades are too noisy to call

    def wr(r):
        t = r.get('total') or 0
        w = r.get('wins') or 0
        return round((w / t) * 100, 1) if t > 0 else 0

    score_buckets = get_score_bucket_stats()
    dte_buckets   = get_dte_bucket_stats()
    prem_tiers    = get_premium_tier_stats()

    # Threshold stats enriched inline (avoid circular call)
    conn = get_db_connection()
    thresh_results = []
    for thresh in [3.0, 4.0, 5.0, 6.0, 7.0, 7.5, 8.0]:
        c = conn.cursor()
        c.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   ROUND(SUM(pnl), 2) as total_pnl
            FROM paper_trades
            WHERE status IN ('CLOSED', 'STOP_TRIGGERED') AND pnl IS NOT NULL AND score_at_entry >= ?
        """, (thresh,))
        row = dict(c.fetchone())
        row['threshold'] = thresh
        thresh_results.append(row)
    conn.close()

    insights = []

    # ── Score bucket: best and worst ──────────────────────────────────────────
    q_score = [r for r in score_buckets if (r.get('total') or 0) >= MIN_SAMPLE]
    if q_score:
        best  = max(q_score, key=wr)
        worst = min(q_score, key=wr)
        pnl_sign = '+' if (best.get('total_pnl') or 0) >= 0 else ''
        insights.append(
            f"Score {best['score_bucket']} is the sweet spot — {wr(best):.0f}% win rate "
            f"across {best['total']} trades ({pnl_sign}${best.get('total_pnl') or 0:,.0f} total)."
        )
        if worst['score_bucket'] != best['score_bucket']:
            insights.append(
                f"Score {worst['score_bucket']} is the weakest bucket at {wr(worst):.0f}% win rate "
                f"({worst['total']} trades) — likely late-stage or crowded flow."
            )

    # ── DTE: best range ───────────────────────────────────────────────────────
    q_dte = [r for r in dte_buckets if (r.get('total') or 0) >= MIN_SAMPLE]
    if q_dte:
        best_dte  = max(q_dte, key=wr)
        worst_dte = min(q_dte, key=wr)
        insights.append(
            f"{best_dte['dte_bucket']} DTE trades win at {wr(best_dte):.0f}% "
            f"({best_dte['total']} trades) — the strongest time-to-expiry range."
        )
        if worst_dte['dte_bucket'] != best_dte['dte_bucket'] and wr(worst_dte) < 50:
            insights.append(
                f"{worst_dte['dte_bucket']} DTE is the weakest at {wr(worst_dte):.0f}% "
                f"({worst_dte['total']} trades) — consider filtering these entries."
            )

    # ── Score threshold simulation: optimal cutoff ───────────────────────────
    q_thresh = [r for r in thresh_results if (r.get('total') or 0) >= MIN_SAMPLE]
    if q_thresh:
        # Best by total P&L (not just win rate — higher thresholds mean fewer trades)
        best_thresh = max(q_thresh, key=lambda r: r.get('total_pnl') or 0)
        best_wr_thresh = wr(best_thresh)
        pnl_sign = '+' if (best_thresh.get('total_pnl') or 0) >= 0 else ''
        insights.append(
            f"Score ≥ {best_thresh['threshold']:.1f} maximizes total P&L at "
            f"{pnl_sign}${best_thresh.get('total_pnl') or 0:,.0f} "
            f"({best_wr_thresh:.0f}% win rate, {best_thresh['total']} trades)."
        )

    # ── Premium tier: best ────────────────────────────────────────────────────
    q_prem = [r for r in prem_tiers if (r.get('total') or 0) >= MIN_SAMPLE]
    if q_prem:
        best_prem = max(q_prem, key=wr)
        pnl_sign = '+' if (best_prem.get('total_pnl') or 0) >= 0 else ''
        insights.append(
            f"{best_prem['premium_tier']} premium signals win at {wr(best_prem):.0f}% "
            f"({best_prem['total']} trades, {pnl_sign}${best_prem.get('total_pnl') or 0:,.0f} total)."
        )

    return insights


def get_threshold_stats():
    conn = get_db_connection()
    results = []
    for thresh in [3.0, 4.0, 5.0, 6.0, 7.0, 7.5, 8.0]:
        c = conn.cursor()
        c.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   ROUND(SUM(pnl), 2) as total_pnl,
                   ROUND(AVG(pnl_pct), 1) as avg_pct
            FROM paper_trades
            WHERE status IN ('CLOSED', 'STOP_TRIGGERED') AND pnl IS NOT NULL AND score_at_entry >= ?
        """, (thresh,))
        row = dict(c.fetchone())
        row['threshold'] = thresh
        results.append(row)
    conn.close()
    return results


# =============================================================================
# API HELPERS
# =============================================================================

def get_api_token():
    try:
        url = f"{BASE_URL}/userapiauthservice/personal/access-tokens"
        response = requests.post(url, json={"secret": SECRET_KEY, "validityInMinutes": 60})
        if response.status_code == 200:
            return response.json().get("accessToken")
    except Exception as e:
        print(f"  Auth error: {e}")
    return None


def get_account_id(token):
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
    if not signals:
        return {}
    try:
        token = get_api_token()
        if not token: return {}
        account_id = get_account_id(token)
        if not account_id: return {}
        symbols = [s['contract'] for s in signals]
        url = f"{BASE_URL}/userapigateway/option-details/{account_id}/greeks"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        response = requests.get(url, headers=headers, params={"osiSymbols": symbols})
        if response.status_code != 200:
            return {}
        greeks_list = response.json().get("greeks", [])
        result = {}
        for item in greeks_list:
            symbol = item.get("symbol", "")
            if not symbol: continue
            g = item.get("greeks", {})
            try:
                delta = float(g.get("delta", 0) or 0)
                iv = float(g.get("impliedVolatility", 0) or 0)
                theta = float(g.get("theta", 0) or 0)
                abs_delta = abs(delta)
                if abs_delta >= 0.7:
                    moneyness, moneyness_color = "ITM", "#22c55e"
                elif abs_delta >= 0.3:
                    moneyness, moneyness_color = "ATM", "#f59e0b"
                else:
                    moneyness, moneyness_color = "OTM", "#64748b"
                result[symbol] = {
                    'delta': f"{delta:+.3f}", 'delta_raw': delta,
                    'theta': f"{theta:.3f}", 'iv': f"{iv*100:.1f}%",
                    'moneyness': moneyness, 'moneyness_color': moneyness_color
                }
            except (ValueError, TypeError):
                pass
        return result
    except Exception as e:
        print(f"  Greeks error: {e}")
        return {}


def is_market_open():
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
@auth.login_required
def dashboard():
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now = datetime.now(eastern)
    market_open = is_market_open()

    # ── Signals tab data ──────────────────────────────────────────────────────
    todays_signals = get_todays_signals()
    expiring_today = get_expiring_today()
    bias = get_directional_bias()

    high_for_greeks = [s for s in todays_signals if s['signal_tier'] == 'HIGH'][:15]
    greeks_data = get_greeks_for_signals(high_for_greeks)

    call_data = bias.get('CALL', {'count': 0, 'total_premium': 0})
    put_data  = bias.get('PUT',  {'count': 0, 'total_premium': 0})
    call_premium = call_data['total_premium'] or 0
    put_premium  = put_data['total_premium']  or 0
    total_bias_premium = call_premium + put_premium

    if total_bias_premium > 0:
        call_pct = round((call_premium / total_bias_premium) * 100, 1)
        put_pct  = round((put_premium  / total_bias_premium) * 100, 1)
        bias_label = "BEARISH" if put_pct > 55 else "BULLISH" if call_pct > 55 else "NEUTRAL"
        bias_color = "#ef4444" if bias_label == "BEARISH" else "#22c55e" if bias_label == "BULLISH" else "#f59e0b"
    else:
        call_pct = put_pct = 0
        bias_label = "NO DATA"
        bias_color = "#6b7280"

    high_signals = [s for s in todays_signals if s['signal_tier'] == 'HIGH'][:15]
    inst_signals = [s for s in todays_signals if s['signal_tier'] == 'INST'][:10]

    for s in high_signals + inst_signals + expiring_today:
        s['premium_display'] = fmt_premium(s['premium'])
        s['decoded'] = decode_contract(s['contract'])
        s['greeks']  = greeks_data.get(s['contract'], {})

    signal_history = get_signal_history(days=30)
    for day in signal_history:
        p = day['total_premium'] or 0
        day['total_premium'] = p
        day['premium_display'] = fmt_premium(p)
    signal_history_json = json.dumps(signal_history)

    high_signals_json = json.dumps([{
        'contract': s['contract'], 'contract_type': s['contract_type'],
        'premium_display': s['premium_display'], 'composite_score': s['composite_score'],
        'days_out': s['decoded']['days_out'], 'ticker': s['decoded']['ticker'],
        'strike_display': s['decoded']['strike_display'],
        'contract_type_display': s['decoded']['contract_type'],
        'expiry_display': s['decoded']['expiry_display'],
        'dte_display': s['decoded']['dte_display'],
        'delta': s['greeks'].get('delta', ''), 'iv': s['greeks'].get('iv', ''),
        'moneyness': s['greeks'].get('moneyness', ''),
        'moneyness_color': s['greeks'].get('moneyness_color', ''),
        'has_greeks': bool(s['greeks']),
        'share_price': f"${s['share_price']:,.2f}" if s.get('share_price') else '',
    } for s in high_signals])

    inst_signals_json = json.dumps([{
        'contract': s['contract'], 'contract_type': s['contract_type'],
        'premium_display': s['premium_display'], 'composite_score': s['composite_score'],
        'days_out': s['decoded']['days_out'], 'ticker': s['decoded']['ticker'],
        'strike_display': s['decoded']['strike_display'],
        'contract_type_display': s['decoded']['contract_type'],
        'expiry_display': s['decoded']['expiry_display'],
        'dte_display': s['decoded']['dte_display'],
        'share_price': f"${s['share_price']:,.2f}" if s.get('share_price') else '',
    } for s in inst_signals])

    # ── Portfolio tab data ────────────────────────────────────────────────────
    closed = get_closed_summary()
    today_entered, today_closed = get_today_activity()
    exit_reasons = get_exit_reason_breakdown()
    tickers = get_ticker_breakdown()

    total  = closed.get('total') or 0
    wins   = closed.get('wins')  or 0
    losses = closed.get('losses') or 0
    win_rate_pct = round((wins / total) * 100, 1) if total > 0 else 0
    realized_pnl = closed.get('total_pnl') or 0

    for t in today_entered:
        t['decoded'] = decode_contract(t['signal_contract'])
        t['score_display']   = f"{t['score_at_entry']:.1f}" if t.get('score_at_entry') is not None else '—'
        t['premium_display'] = fmt_premium(t.get('premium'))
        lp = t.get('last_price')
        ep = t.get('exit_price')
        rp = t.get('realized_pnl')
        up = t.get('unrealized_pnl')
        t['last_price_display']  = f"${lp:.2f}" if lp is not None else 'n/a'
        t['exit_price_display']  = f"${ep:.2f}" if ep is not None else 'n/a'
        t['realized_display']    = fmt_pnl(rp) if rp is not None else 'n/a'
        t['realized_positive']   = rp >= 0 if rp is not None else None
        if t.get('status') == 'OPEN' and up is not None:
            t['unrealized_display']  = fmt_pnl(up)
            t['unrealized_positive'] = up >= 0
        else:
            t['unrealized_display']  = 'n/a'
            t['unrealized_positive'] = None
        if lp is not None and ep is not None:
            post_exit = (lp - ep) * 100 * (t.get('contracts') or 1)
            t['post_exit_display']  = fmt_pnl(post_exit)
            t['post_exit_positive'] = post_exit > 0  # True → red (left money), False/None → green
        else:
            t['post_exit_display']  = 'n/a'
            t['post_exit_positive'] = None
    sum_unrealized = sum(
        t['unrealized_pnl'] for t in today_entered
        if t.get('status') == 'OPEN' and t.get('unrealized_pnl') is not None
    )
    sum_realized = sum(
        t['realized_pnl'] for t in today_entered
        if t.get('realized_pnl') is not None
    )
    sum_post_exit = sum(
        (t['last_price'] - t['exit_price']) * 100 * (t.get('contracts') or 1)
        for t in today_entered
        if t.get('last_price') is not None and t.get('exit_price') is not None
    )
    today_sums = {
        'unrealized':    fmt_pnl(sum_unrealized),
        'unrealized_pos': sum_unrealized >= 0,
        'realized':      fmt_pnl(sum_realized),
        'realized_pos':  sum_realized >= 0,
        'post_exit':     fmt_pnl(sum_post_exit),
        'post_exit_pos': sum_post_exit > 0,  # strictly positive → red
    }
    for t in today_closed:
        t['decoded'] = decode_contract(t['signal_contract'])
        t['pnl_display'] = fmt_pnl(t.get('pnl'))
        t['pnl_pct_display'] = fmt_pct(t.get('pnl_pct'))
        t['pnl_positive'] = (t.get('pnl') or 0) >= 0

    portfolio = {
        'total_trades': total, 'wins': wins, 'losses': losses,
        'win_rate': win_rate_pct,
        'realized_pnl': realized_pnl,
        'realized_pnl_display': fmt_pnl(realized_pnl),
        'realized_pnl_positive': realized_pnl >= 0,
        'avg_pnl_display': fmt_pnl(closed.get('avg_pnl')),
        'avg_win_display': fmt_pnl(closed.get('avg_win')),
        'avg_loss_display': fmt_pnl(closed.get('avg_loss')),
        'avg_hold_days': closed.get('avg_hold_days') or 0,
        'best_trade_display': fmt_pnl(closed.get('best_trade')),
        'worst_trade_display': fmt_pnl(closed.get('worst_trade')),
        'current_value': STARTING_BANKROLL + realized_pnl,
        'return_pct': round((realized_pnl / STARTING_BANKROLL) * 100, 1),
    }

    return render_template_string(DASHBOARD_HTML,
        now=now.strftime('%Y-%m-%d %H:%M:%S %Z'),
        market_open=market_open,
        market_status="OPEN" if market_open else "CLOSED",
        market_color="#22c55e" if market_open else "#ef4444",
        # signals
        high_signals=high_signals, inst_signals=inst_signals,
        expiring_today=expiring_today,
        todays_signal_count=len(todays_signals),
        call_pct=call_pct, put_pct=put_pct,
        call_premium_display=fmt_premium(call_premium),
        put_premium_display=fmt_premium(put_premium),
        bias_label=bias_label, bias_color=bias_color,
        signal_history_json=signal_history_json,
        high_signals_json=high_signals_json,
        inst_signals_json=inst_signals_json,
        # portfolio
        portfolio=portfolio,
        today_entered=today_entered, today_closed=today_closed,
        today_sums=today_sums,
        exit_reasons=exit_reasons, tickers=tickers,
    )


@app.route('/api/positions')
@auth.login_required
def api_positions():
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now = datetime.now(eastern)
    positions    = get_open_positions()
    closed_stats = get_closed_positions_summary()
    result = []
    for p in positions:
        decoded   = decode_contract(p['signal_contract'])
        status    = p['status']
        lp        = p['last_price']
        ep        = p.get('exit_price')
        rp        = p.get('realized_pnl')
        up        = p['last_pnl'] or 0
        contracts = p['contracts'] or 1

        # Unrealized — OPEN only
        if status == 'OPEN':
            unrealized_disp = fmt_pnl(up)
            unrealized_pos  = up >= 0
        else:
            unrealized_disp = 'n/a'
            unrealized_pos  = None

        # Realized — STOP_TRIGGERED only
        if status == 'STOP_TRIGGERED' and rp is not None:
            realized_disp = fmt_pnl(rp)
            realized_pos  = rp >= 0
        else:
            realized_disp = 'n/a'
            realized_pos  = None

        # % — unrealized % for OPEN, realized % for STOP_TRIGGERED
        if status == 'OPEN':
            pct_val  = p['last_pnl_pct']
            pct_disp = fmt_pct(pct_val) if pct_val is not None else 'n/a'
            pct_pos  = (pct_val or 0) >= 0
        elif status == 'STOP_TRIGGERED' and p.get('realized_pnl_pct') is not None:
            pct_val  = p['realized_pnl_pct']
            pct_disp = fmt_pct(pct_val)
            pct_pos  = pct_val >= 0
        else:
            pct_disp = 'n/a'
            pct_pos  = None

        # Post-Exit — requires both last_price and exit_price
        if lp is not None and ep is not None:
            pe_val  = (lp - ep) * 100 * contracts
            pe_disp = fmt_pnl(pe_val)
            pe_pos  = pe_val > 0  # strictly positive → red (left money on table)
        else:
            pe_val  = None
            pe_disp = 'n/a'
            pe_pos  = None

        # Exit $ display
        ep_disp = f"${ep:.2f}" if ep is not None else 'n/a'

        # Date helpers
        def fmt_date(d):
            if not d: return 'n/a'
            try:    return datetime.strptime(d, '%Y-%m-%d').strftime('%b %d')
            except: return d

        entry_date_disp = fmt_date(p.get('entry_date'))
        exit_date_disp  = fmt_date(p.get('exit_date')) if status != 'OPEN' else 'n/a'

        # Days held
        hd = p.get('hold_days')
        days_held_disp = f"{round(hd, 1)}d" if hd is not None else 'n/a'

        result.append({
            'id':                p['id'],
            'contract':          p['signal_contract'],
            'ticker':            decoded['ticker'],
            'contract_type':     decoded['contract_type'],
            'strike_display':    decoded['strike_display'],
            'expiry_display':    decoded['expiry_display'],
            'score_at_entry':    p.get('score_at_entry'),
            'score_display':     f"{p['score_at_entry']:.1f}" if p.get('score_at_entry') is not None else '—',
            'premium_display':   fmt_premium(p.get('premium')),
            'dte_at_entry':      p['dte_at_entry'],
            'entry_date_display': entry_date_disp,
            'entry_price':       p['entry_price'],
            'contracts':         contracts,
            'last_price':        lp,
            'last_pnl':          up,
            'unrealized_display': unrealized_disp,
            'unrealized_pos':    unrealized_pos,
            'realized_display':  realized_disp,
            'realized_pos':      realized_pos,
            'pct_display':       pct_disp,
            'pct_pos':           pct_pos,
            'exit_price_display': ep_disp,
            'post_exit_display': pe_disp,
            'post_exit_pos':     pe_pos,
            'post_exit_val':     pe_val,
            'total_cost':        p.get('total_cost') or 0,
            'realized_pnl':      rp,
            'exit_date_display': exit_date_disp,
            'days_held_display': days_held_disp,
            'status':            status,
            'current_dte':       p['current_dte'],
            'dynamic_stop':      p['dynamic_stop'],
            'snapshot_time':     p['snapshot_time'],
        })

    open_pos    = [p for p in result if p['status'] == 'OPEN']
    stopped_pos = [p for p in result if p['status'] == 'STOP_TRIGGERED']

    open_unreal   = sum(p['last_pnl'] for p in open_pos)
    stop_realized = sum(p['realized_pnl'] for p in stopped_pos if p['realized_pnl'] is not None)
    stop_pe       = sum(p['post_exit_val'] for p in stopped_pos if p['post_exit_val'] is not None)
    stop_wins     = sum(1 for p in stopped_pos if (p['realized_pnl'] or 0) > 0)

    cl_count    = closed_stats.get('count') or 0
    cl_wins     = closed_stats.get('wins') or 0
    cl_realized = closed_stats.get('total_realized') or 0
    cl_pe       = closed_stats.get('total_post_exit') or 0
    cl_cost     = closed_stats.get('total_cost') or 0

    tot_unreal  = open_unreal
    tot_real    = stop_realized + cl_realized
    tot_pe      = stop_pe + cl_pe
    tot_wins    = stop_wins + cl_wins
    tot_losses  = (len(stopped_pos) - stop_wins) + (cl_count - cl_wins)

    open_cost    = sum(p['total_cost'] for p in open_pos)
    stopped_cost = sum(p['total_cost'] for p in stopped_pos)
    tot_cost     = open_cost + stopped_cost + cl_cost

    def roi_fmt(potential, cost):
        if not cost: return 'n/a', None
        v = round(potential / cost * 100, 1)
        return (f"+{v:.1f}%" if v >= 0 else f"{v:.1f}%"), v >= 0

    def s_block(unreal, real, pe, cost, roi_potential):
        pot = (unreal or 0) + (real or 0)
        roi_disp, roi_pos = roi_fmt(roi_potential if roi_potential is not None else pot, cost)
        return {
            'unrealized':      unreal,
            'unrealized_disp': fmt_pnl(unreal) if unreal is not None else 'n/a',
            'unrealized_pos':  (unreal >= 0) if unreal is not None else None,
            'realized':        real,
            'realized_disp':   fmt_pnl(real) if real is not None else 'n/a',
            'realized_pos':    (real >= 0) if real is not None else None,
            'potential':       pot,
            'potential_disp':  fmt_pnl(pot),
            'potential_pos':   pot >= 0,
            'post_exit':       pe,
            'post_exit_disp':  fmt_pnl(pe) if pe is not None else 'n/a',
            'post_exit_pos':   (pe > 0) if pe is not None else None,
            'cost_disp':       fmt_premium(cost) if cost else '$0',
            'roi_disp':        roi_disp,
            'roi_pos':         roi_pos,
        }

    return jsonify({
        'positions': result,
        'summary': {
            'open': {
                **s_block(open_unreal, None, None, open_cost, open_unreal),
                'count': len(open_pos),
            },
            'stopped': {
                **s_block(None, stop_realized, stop_pe, stopped_cost, stop_realized),
                'count':  len(stopped_pos),
                'wins':   stop_wins,
                'losses': len(stopped_pos) - stop_wins,
            },
            'closed': {
                **s_block(None, cl_realized, cl_pe, cl_cost, cl_realized),
                'count':  cl_count,
                'wins':   cl_wins,
                'losses': cl_count - cl_wins,
            },
            'totals': {
                **s_block(tot_unreal, tot_real, tot_pe, tot_cost, tot_unreal + tot_real),
                'count':  len(result) + cl_count,
                'wins':   tot_wins,
                'losses': tot_losses,
            },
        },
        'timestamp': now.strftime('%Y-%m-%d %H:%M:%S %Z'),
    })


@app.route('/api/analytics')
@auth.login_required
def api_analytics():
    def enrich(rows):
        for r in rows:
            t = r.get('total') or 0
            w = r.get('wins') or 0
            r['win_rate'] = round((w / t) * 100, 1) if t > 0 else 0
            r['total_pnl_display'] = fmt_pnl(r.get('total_pnl'))
            r['avg_pnl_display']   = fmt_pnl(r.get('avg_pnl'))
        return rows

    score_buckets  = enrich(get_score_bucket_stats())
    dte_buckets    = enrich(get_dte_bucket_stats())
    premium_tiers  = enrich(get_premium_tier_stats())
    thresholds     = get_threshold_stats()
    for r in thresholds:
        t = r.get('total') or 0
        w = r.get('wins') or 0
        r['win_rate'] = round((w / t) * 100, 1) if t > 0 else 0
        r['total_pnl_display'] = fmt_pnl(r.get('total_pnl'))

    return jsonify({
        'score_buckets': score_buckets,
        'dte_buckets': dte_buckets,
        'premium_tiers': premium_tiers,
        'thresholds': thresholds,
        'insights': get_key_insights(),
    })


@app.route('/api/record-outcome', methods=['POST'])
@auth.login_required
def record_outcome():
    data = request.get_json()
    contract = data.get('contract')
    outcome  = data.get('outcome')
    notes    = data.get('notes', '')
    if not contract or outcome not in ['WIN', 'LOSS', 'FLAT']:
        return jsonify({'success': False, 'error': 'Invalid input'})
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE signals SET outcome = ?, outcome_notes = ?
        WHERE contract = ? AND outcome IS NULL
    """, (outcome, notes, contract))
    updated = cursor.rowcount
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'updated': updated})


@app.route('/api/data')
@auth.login_required
def api_data():
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now = datetime.now(eastern)
    return jsonify({
        'timestamp': now.strftime('%Y-%m-%d %H:%M:%S %Z'),
        'market_open': is_market_open(),
        'bias': get_directional_bias(),
    })


@app.route('/api/last-scan')
@auth.login_required
def last_scan():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("SELECT MAX(scan_time) as last_scan FROM scan_log")
        result = c.fetchone()
        last = result['last_scan'] if result else None
    except Exception:
        last = None
    conn.close()
    return jsonify({'last_scan': last})


@app.route('/api/signal-history')
@auth.login_required
def signal_history_route():
    rows = get_signal_history(days=30)
    return jsonify(rows)


# =============================================================================
# HTML TEMPLATE
# =============================================================================

DASHBOARD_HTML = """<!DOCTYPE html>
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
    padding: 14px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.header-left { display: flex; align-items: center; gap: 14px; }
.header h1 { font-size: 15px; font-weight: 600; color: #f1f5f9; letter-spacing: 0.05em; }
.market-badge {
    padding: 3px 10px; border-radius: 4px; font-size: 11px;
    font-weight: 700; letter-spacing: 0.1em;
    background: {{ market_color }}22;
    color: {{ market_color }};
    border: 1px solid {{ market_color }}44;
}
.header-right { display: flex; align-items: center; gap: 14px; color: #64748b; font-size: 11px; }
.refresh-btn {
    background: #334155; border: none; color: #94a3b8;
    padding: 4px 12px; border-radius: 4px; cursor: pointer;
    font-family: inherit; font-size: 11px;
}
.refresh-btn:hover { background: #475569; color: #e2e8f0; }
.live-dot {
    width: 6px; height: 6px; background: #22c55e; border-radius: 50%;
    display: inline-block; animation: pulse 2s infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

/* ── Tab Bar ── */
.tab-bar {
    background: #1e293b;
    border-bottom: 1px solid #334155;
    display: flex;
    padding: 0 24px;
    gap: 4px;
}
.tab-btn {
    padding: 11px 18px;
    background: none; border: none;
    border-bottom: 2px solid transparent;
    color: #64748b;
    font-family: inherit; font-size: 11px; font-weight: 600;
    letter-spacing: 0.08em; text-transform: uppercase;
    cursor: pointer; margin-bottom: -1px;
    transition: color 0.15s, border-color 0.15s;
}
.tab-btn:hover { color: #94a3b8; }
.tab-btn.active { color: #f1f5f9; border-bottom-color: #3b82f6; }

/* ── Tab Content ── */
.tab-content { display: none; padding: 20px 24px; max-width: 1440px; margin: 0 auto; }
.tab-content.active { display: block; }

/* ── Cards ── */
.card {
    background: #1e293b; border: 1px solid #334155;
    border-radius: 8px; padding: 16px;
}
.card-title {
    font-size: 11px; font-weight: 600; color: #64748b;
    letter-spacing: 0.1em; text-transform: uppercase;
    margin-bottom: 14px; display: flex; align-items: center; gap: 8px;
}
.card-title .count {
    background: #334155; color: #94a3b8;
    padding: 1px 7px; border-radius: 10px; font-size: 10px;
}

/* ── Grid helpers ── */
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.grid-5 { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 16px; }
.full-width { margin-bottom: 16px; }

/* ── Stat Cards ── */
.stat-card {
    background: #1e293b; border: 1px solid #334155;
    border-radius: 8px; padding: 14px 16px;
}
.stat-label { font-size: 10px; color: #64748b; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 6px; }
.stat-value { font-size: 22px; font-weight: 700; color: #f1f5f9; line-height: 1; }
.stat-value.positive { color: #22c55e; }
.stat-value.negative { color: #ef4444; }
.stat-sub { font-size: 10px; color: #64748b; margin-top: 4px; }

/* ── Tables ── */
.data-table { width: 100%; border-collapse: collapse; }
.data-table th {
    font-size: 10px; color: #475569; text-transform: uppercase;
    letter-spacing: 0.06em; padding: 0 8px 8px 0; text-align: left;
    border-bottom: 1px solid #334155;
}
.data-table td {
    padding: 7px 8px 7px 0; border-bottom: 1px solid #1e293b; vertical-align: middle;
}
.data-table tr:last-child td { border-bottom: none; }
.data-table tr:hover td { background: #ffffff08; }

/* ── Type colors ── */
.type-call { color: #22c55e; font-weight: 600; }
.type-put  { color: #ef4444; font-weight: 600; }

/* ── Win rate bar ── */
.wr-bar { flex:1; margin: 0 10px; height: 4px; background: #334155; border-radius: 2px; overflow: hidden; }
.wr-fill { height: 100%; background: #22c55e; border-radius: 2px; }
.wr-row { display: flex; align-items: center; padding: 7px 0; border-bottom: 1px solid #334155; }
.wr-row:last-child { border-bottom: none; }

/* ── Badges ── */
.badge {
    display: inline-block; padding: 2px 7px; border-radius: 3px;
    font-size: 10px; font-weight: 600; vertical-align: middle;
}
.badge-open     { background: #22c55e22; color: #22c55e; border: 1px solid #22c55e33; }
.badge-tracking { background: #f97316aa; color: #fff; }
.badge-stop     { background: #f9731622; color: #f97316; border: 1px solid #f9731633; }
.badge-win      { background: #22c55e22; color: #22c55e; }
.badge-loss     { background: #ef444422; color: #ef4444; }
.badge-trail    { background: #f59e0b22; color: #f59e0b; border: 1px solid #f59e0b33; }

.dte-badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; }
.dte-urgent { background: #ef444422; color: #ef4444; border: 1px solid #ef444433; }
.dte-soon   { background: #f59e0b22; color: #f59e0b; border: 1px solid #f59e0b33; }
.dte-normal { background: #334155; color: #64748b; }

/* ── Loading / empty states ── */
.loading-msg { color: #475569; font-style: italic; text-align: center; padding: 40px 0; font-size: 12px; }
.empty-msg   { color: #475569; font-style: italic; text-align: center; padding: 16px 0; font-size: 11px; }

/* ── Signal table specifics ── */
.signal-table { width: 100%; border-collapse: collapse; }
.signal-table th {
    font-size: 10px; color: #475569; text-transform: uppercase;
    letter-spacing: 0.06em; padding: 0 8px 8px 0; text-align: left;
    border-bottom: 1px solid #334155;
}
.signal-table td { padding: 7px 8px 7px 0; border-bottom: 1px solid #1e293b; vertical-align: middle; }
.signal-table tr:last-child td { border-bottom: none; }
.signal-table tr:hover td { background: #ffffff08; }

/* ── DTE filter tabs ── */
.dte-tabs { display: flex; gap: 4px; margin-left: auto; }
.dte-tab {
    padding: 3px 10px; border-radius: 4px;
    border: 1px solid #334155; background: transparent;
    color: #64748b; font-family: inherit; font-size: 10px;
    font-weight: 600; letter-spacing: 0.05em; cursor: pointer;
    transition: all 0.15s; text-transform: uppercase;
}
.dte-tab:hover { border-color: #475569; color: #94a3b8; }
.dte-tab.active { background: #334155; color: #f1f5f9; border-color: #475569; }

/* ── Bias bar ── */
.bias-bar-wrap { display: flex; align-items: center; gap: 12px; margin-top: 4px; }
.bias-bar { flex: 1; height: 8px; background: #334155; border-radius: 4px; overflow: hidden; }
.bias-bar-fill { height: 100%; border-radius: 4px; background: {{ bias_color }};
    width: {{ put_pct if bias_label == 'BEARISH' else call_pct }}%; transition: width 0.5s ease; }
.bias-label-big { font-size: 18px; font-weight: 700; color: {{ bias_color }}; min-width: 80px; text-align: right; }
.bias-details { display: flex; gap: 20px; margin-top: 10px; font-size: 11px; }

/* ── Chart ── */
#historyChart { width: 100%; height: 160px; display: block; }
.chart-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.chart-legend { display: flex; gap: 16px; font-size: 10px; color: #64748b; }
.legend-dot { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }
.legend-line { display: inline-block; width: 14px; height: 2px; background: #e2e8f0; border-radius: 1px; vertical-align: middle; margin-right: 4px; }

/* ── Outcome buttons ── */
.outcome-btn {
    padding: 2px 8px; border-radius: 3px; border: 1px solid;
    cursor: pointer; font-family: inherit; font-size: 10px;
    font-weight: 600; margin-right: 3px; background: transparent; transition: all 0.15s;
}
.btn-win  { color: #22c55e; border-color: #22c55e33; }
.btn-loss { color: #ef4444; border-color: #ef443333; }
.btn-flat { color: #64748b; border-color: #64748b33; }
.btn-win:hover  { background: #22c55e22; }
.btn-loss:hover { background: #ef444422; }
.btn-flat:hover { background: #64748b22; }
.outcome-recorded { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 3px; }
.outcome-WIN  { color: #22c55e; background: #22c55e11; }
.outcome-LOSS { color: #ef4444; background: #ef444411; }
.outcome-FLAT { color: #64748b; background: #64748b11; }

/* ── Analytics score highlight ── */
.score-highlight { background: #22c55e18; }
.score-dim       { opacity: 0.6; }

/* ── Positions summary bar ── */
.pos-summary {
    display: flex; gap: 24px; align-items: center;
    background: #1e293b; border: 1px solid #334155;
    border-radius: 8px; padding: 14px 20px; margin-bottom: 16px;
}
.pos-stat-label { font-size: 10px; color: #64748b; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 3px; }
.pos-stat-val   { font-size: 18px; font-weight: 700; }
.pos-divider    { width: 1px; height: 36px; background: #334155; }
.pos-refresh-btn {
    margin-left: auto; background: #334155; border: none; color: #94a3b8;
    padding: 6px 14px; border-radius: 4px; cursor: pointer;
    font-family: inherit; font-size: 11px;
}
.pos-refresh-btn:hover { background: #475569; color: #e2e8f0; }
</style>
</head>
<body>

<!-- ═══════════════════════════════════════════════════════ HEADER ═══ -->
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

<!-- ═══════════════════════════════════════════════════════ TAB BAR ═══ -->
<div class="tab-bar">
    <button class="tab-btn" data-tab="portfolio"  onclick="switchTab('portfolio')">Portfolio</button>
    <button class="tab-btn" data-tab="positions"  onclick="switchTab('positions')">Positions</button>
    <button class="tab-btn" data-tab="signals"    onclick="switchTab('signals')">Signals</button>
    <button class="tab-btn" data-tab="analytics"  onclick="switchTab('analytics')">Analytics</button>
</div>


<!-- ═══════════════════════════════════════════════ TAB: PORTFOLIO ═══ -->
<div id="tab-portfolio" class="tab-content">

    <!-- Hero stats row -->
    <div class="grid-5">
        <div class="stat-card">
            <div class="stat-label">Realized P&L</div>
            <div class="stat-value {{ 'positive' if portfolio.realized_pnl_positive else 'negative' }}">
                {{ portfolio.realized_pnl_display }}
            </div>
            <div class="stat-sub">{{ portfolio.return_pct }}% return on $10K</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Win Rate</div>
            <div class="stat-value">{{ portfolio.win_rate }}%</div>
            <div class="stat-sub">{{ portfolio.wins }}W / {{ portfolio.losses }}L</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Completed Trades</div>
            <div class="stat-value">{{ portfolio.total_trades }}</div>
            <div class="stat-sub">avg {{ portfolio.avg_hold_days }}d hold</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Avg Trade P&L</div>
            <div class="stat-value" style="font-size:18px; padding-top:3px">{{ portfolio.avg_pnl_display }}</div>
            <div class="stat-sub">W: {{ portfolio.avg_win_display }} · L: {{ portfolio.avg_loss_display }}</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Best / Worst</div>
            <div class="stat-value" style="font-size:14px; padding-top:5px; color:#22c55e">{{ portfolio.best_trade_display }}</div>
            <div class="stat-sub" style="color:#ef4444">{{ portfolio.worst_trade_display }}</div>
        </div>
    </div>

    <!-- Today's Entries — full width -->
    <div class="card full-width">
        <div class="card-title">Today's Entries
            <span class="count">{{ today_entered|length }}</span>
        </div>
        {% if today_entered %}
        <table class="data-table">
            <thead>
            <!-- Column sum row — aligned to Unrealized/Realized/Post-Exit columns -->
            <tr style="border-bottom:none">
                <td colspan="10" style="padding:0 0 4px 0;border-bottom:none"></td>
                <td style="text-align:right;padding:0 8px 4px 0;border-bottom:none">
                    <div style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:0.08em;line-height:1.3">Unrealized</div>
                    <div style="font-size:11px;font-weight:700;color:{{ '#22c55e' if today_sums.unrealized_pos else '#ef4444' }}">{{ today_sums.unrealized }}</div>
                </td>
                <td style="text-align:right;padding:0 8px 4px 0;border-bottom:none">
                    <div style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:0.08em;line-height:1.3">Realized</div>
                    <div style="font-size:11px;font-weight:700;color:{{ '#22c55e' if today_sums.realized_pos else '#ef4444' }}">{{ today_sums.realized }}</div>
                </td>
                <td style="text-align:right;padding:0 0 4px 0;border-bottom:none">
                    <div style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:0.08em;line-height:1.3">Post-Exit</div>
                    <div style="font-size:11px;font-weight:700;color:{{ '#ef4444' if today_sums.post_exit_pos else '#22c55e' }}">{{ today_sums.post_exit }}</div>
                </td>
                <td style="border-bottom:none;padding:0"></td>
            </tr>
            <!-- Column headers -->
            <tr>
                <th>Ticker</th>
                <th>Expiry</th>
                <th>Type</th>
                <th>Strike</th>
                <th>DTE</th>
                <th style="text-align:right">Score</th>
                <th style="text-align:right">Premium</th>
                <th style="text-align:right">Entry $</th>
                <th style="text-align:right">Last $</th>
                <th style="text-align:right">Exit $</th>
                <th style="text-align:right">Unrealized</th>
                <th style="text-align:right">Realized</th>
                <th style="text-align:right">Post-Exit</th>
                <th>Status</th>
            </tr>
            </thead>
            <tbody>
            {% for t in today_entered %}
            <tr>
                <td style="color:#f1f5f9;font-weight:700">{{ t.decoded.ticker }}</td>
                <td style="color:#64748b;font-size:11px">{{ t.decoded.expiry_display }}</td>
                <td><span class="{{ 'type-call' if t.contract_type == 'CALL' else 'type-put' }}">
                    {{ t.decoded.contract_type }}</span></td>
                <td style="color:#cbd5e1;font-weight:600">{{ t.decoded.strike_display }}</td>
                <td><span class="dte-badge {{ 'dte-urgent' if t.dte_at_entry == 0 else 'dte-soon' if t.dte_at_entry <= 2 else 'dte-normal' }}">
                    {{ t.dte_at_entry }}d</span></td>
                <td style="text-align:right;color:#94a3b8">{{ t.score_display }}</td>
                <td style="text-align:right;color:#94a3b8">{{ t.premium_display }}</td>
                <td style="text-align:right;color:#94a3b8">${{ "%.2f"|format(t.entry_price) }}</td>
                <td style="text-align:right;color:#e2e8f0">{{ t.last_price_display }}</td>
                <td style="text-align:right;color:#94a3b8">{{ t.exit_price_display }}</td>
                <td style="text-align:right;font-weight:600;color:{{ '#22c55e' if t.unrealized_positive == true else '#ef4444' if t.unrealized_positive == false else '#475569' }}">
                    {{ t.unrealized_display }}</td>
                <td style="text-align:right;font-weight:600;color:{{ '#22c55e' if t.realized_positive == true else '#ef4444' if t.realized_positive == false else '#475569' }}">
                    {{ t.realized_display }}</td>
                <td style="text-align:right;font-weight:600;color:{{ '#ef4444' if t.post_exit_positive == true else '#22c55e' if t.post_exit_positive == false else '#475569' }}">
                    {{ t.post_exit_display }}</td>
                <td><span class="badge {{ 'badge-open' if t.status == 'OPEN' else 'badge-stop' if t.status == 'STOP_TRIGGERED' else 'badge-win' if t.realized_positive == true else 'badge-loss' if t.realized_positive == false else 'badge-open' }}">
                    {{ 'OPEN' if t.status == 'OPEN' else 'STOPPED' if t.status == 'STOP_TRIGGERED' else 'CLOSED' }}</span></td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-msg">No entries today</div>
        {% endif %}
    </div>

    <!-- Today's Exits — full width -->
    <div class="card full-width">
        <div class="card-title">Today's Exits
            <span class="count">{{ today_closed|length }}</span>
        </div>
        {% if today_closed %}
        <table class="data-table">
            <thead><tr>
                <th>Ticker</th><th>Type</th><th>P&L</th><th>%</th><th>Reason</th>
            </tr></thead>
            <tbody>
            {% for t in today_closed %}
            <tr>
                <td style="color:#f1f5f9;font-weight:700">{{ t.decoded.ticker }}</td>
                <td><span class="{{ 'type-call' if t.contract_type == 'CALL' else 'type-put' }}">
                    {{ t.decoded.contract_type }}</span></td>
                <td style="color:{{ '#22c55e' if t.pnl_positive else '#ef4444' }};font-weight:600">
                    {{ t.pnl_display }}</td>
                <td style="color:{{ '#22c55e' if t.pnl_positive else '#ef4444' }}">
                    {{ t.pnl_pct_display }}</td>
                <td style="color:#64748b;font-size:11px">{{ t.exit_reason or '—' }}</td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-msg">No exits today</div>
        {% endif %}
    </div>

    <!-- Exit reason + Ticker breakdown -->
    <div class="grid-2">
        <div class="card">
            <div class="card-title">By Exit Reason</div>
            {% if exit_reasons %}
            {% for r in exit_reasons %}
            <div class="wr-row">
                <span style="color:#94a3b8;min-width:110px;font-size:11px">{{ r.exit_reason or 'Unknown' }}</span>
                <div class="wr-bar">
                    <div class="wr-fill" style="width:{{ ((r.wins / r.count) * 100)|round|int if r.count > 0 else 0 }}%"></div>
                </div>
                <span style="color:#22c55e;font-weight:700;min-width:38px;text-align:right;font-size:12px">
                    {{ ((r.wins / r.count) * 100)|round(1) if r.count > 0 else 0 }}%</span>
                <span style="color:#475569;font-size:10px;margin-left:10px;min-width:60px">
                    {{ r.wins }}/{{ r.count }}</span>
                <span style="color:{{ '#22c55e' if (r.total_pnl or 0) >= 0 else '#ef4444' }};font-size:11px;min-width:80px;text-align:right">
                    {{ '+' if (r.total_pnl or 0) >= 0 else '' }}${{ "{:,.0f}".format(r.total_pnl or 0) }}</span>
            </div>
            {% endfor %}
            {% else %}
            <div class="empty-msg">No closed trades yet</div>
            {% endif %}
        </div>

        <div class="card">
            <div class="card-title">By Ticker</div>
            {% if tickers %}
            <table class="data-table">
                <thead><tr>
                    <th>Ticker</th><th>Trades</th><th>W/L</th><th style="text-align:right">Total P&L</th><th style="text-align:right">Avg%</th>
                </tr></thead>
                <tbody>
                {% for t in tickers %}
                <tr>
                    <td style="color:#f1f5f9;font-weight:700">{{ t.ticker }}</td>
                    <td style="color:#64748b">{{ t.count }}</td>
                    <td style="font-size:11px">
                        <span style="color:#22c55e">{{ t.wins }}W</span>
                        <span style="color:#475569">/</span>
                        <span style="color:#ef4444">{{ t.losses }}L</span>
                    </td>
                    <td style="text-align:right;font-weight:600;color:{{ '#22c55e' if (t.total_pnl or 0) >= 0 else '#ef4444' }}">
                        {{ '+' if (t.total_pnl or 0) >= 0 else '' }}${{ "{:,.0f}".format(t.total_pnl or 0) }}</td>
                    <td style="text-align:right;color:#64748b;font-size:11px">{{ t.avg_pct or 0 }}%</td>
                </tr>
                {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="empty-msg">No closed trades yet</div>
            {% endif %}
        </div>
    </div>

</div><!-- /tab-portfolio -->


<!-- ═══════════════════════════════════════════════ TAB: POSITIONS ═══ -->
<div id="tab-positions" class="tab-content">
    <div id="positions-loading" class="loading-msg">Loading positions...</div>
    <div id="positions-content" style="display:none"></div>
</div>


<!-- ═════════════════════════════════════════════════ TAB: SIGNALS ═══ -->
<div id="tab-signals" class="tab-content">

    <!-- Signal History Chart -->
    <div class="card full-width">
        <div class="chart-header">
            <div class="card-title" style="margin-bottom:0">Signal History — Last 30 Days</div>
            <div class="chart-legend">
                <span><span class="legend-dot" style="background:#f97316"></span>HIGH</span>
                <span><span class="legend-dot" style="background:#a855f7"></span>INST</span>
                <span><span class="legend-dot" style="background:#3b82f6"></span>WATCH</span>
                <span><span class="legend-line"></span>Premium Flow</span>
            </div>
        </div>
        <div id="chartContainer"><canvas id="historyChart"></canvas></div>
    </div>

    <!-- Directional Bias -->
    <div class="card full-width" style="margin-top:16px">
        <div class="card-title">Directional Flow Bias — HIGH Signals Today</div>
        <div class="bias-bar-wrap">
            <div style="font-size:11px;color:#64748b;min-width:60px">{{ call_pct }}% calls</div>
            <div class="bias-bar"><div class="bias-bar-fill"></div></div>
            <div style="font-size:11px;color:#64748b;min-width:60px;text-align:right">{{ put_pct }}% puts</div>
            <div class="bias-label-big">{{ bias_label }}</div>
        </div>
        <div class="bias-details">
            <span style="color:#22c55e">▲ Calls: {{ call_premium_display }}</span>
            <span style="color:#ef4444">▼ Puts: {{ put_premium_display }}</span>
        </div>
    </div>

    <!-- HIGH Conviction Signals -->
    <div class="card full-width" style="margin-top:16px">
        <div class="card-title">
            🔥 HIGH Conviction
            <span class="count" id="high-count">{{ high_signals|length }}</span>
            <div class="dte-tabs">
                <button class="dte-tab active" onclick="setDteFilter('all','high')">All</button>
                <button class="dte-tab" onclick="setDteFilter('0dte','high')">0DTE</button>
                <button class="dte-tab" onclick="setDteFilter('1-2d','high')">1-2d</button>
                <button class="dte-tab" onclick="setDteFilter('week','high')">Week</button>
                <button class="dte-tab" onclick="setDteFilter('30d+','high')">30d+</button>
            </div>
        </div>
        <table class="signal-table" id="high-table">
            <thead><tr>
                <th style="width:70px">Ticker</th>
                <th style="width:90px">Strike</th>
                <th style="width:60px">Type</th>
                <th style="width:90px">Expiry</th>
                <th style="width:90px">Stock @</th>
                <th style="width:90px">Premium</th>
                <th style="width:80px">Delta</th>
                <th style="width:60px">IV</th>
                <th style="width:55px">Score</th>
            </tr></thead>
            <tbody id="high-tbody"></tbody>
        </table>
        <div class="empty-msg" id="high-empty" style="display:none">No signals match this filter</div>
    </div>

    <!-- INST Flow -->
    <div class="card full-width" style="margin-top:16px">
        <div class="card-title">
            💰 Institutional Flow
            <span class="count" id="inst-count">{{ inst_signals|length }}</span>
            <div class="dte-tabs">
                <button class="dte-tab active" onclick="setDteFilter('all','inst')">All</button>
                <button class="dte-tab" onclick="setDteFilter('0dte','inst')">0DTE</button>
                <button class="dte-tab" onclick="setDteFilter('1-2d','inst')">1-2d</button>
                <button class="dte-tab" onclick="setDteFilter('week','inst')">Week</button>
                <button class="dte-tab" onclick="setDteFilter('30d+','inst')">30d+</button>
            </div>
        </div>
        <table class="signal-table" id="inst-table">
            <thead><tr>
                <th style="width:70px">Ticker</th>
                <th style="width:90px">Strike</th>
                <th style="width:60px">Type</th>
                <th style="width:90px">Expiry</th>
                <th style="width:90px">Stock @</th>
                <th style="width:90px">Premium</th>
                <th style="width:55px">Score</th>
            </tr></thead>
            <tbody id="inst-tbody"></tbody>
        </table>
        <div class="empty-msg" id="inst-empty" style="display:none">No signals match this filter</div>
    </div>

    <!-- Expiring Today -->
    <div class="card full-width" style="margin-top:16px">
        <div class="card-title">
            ⏰ Expiring Today
            <span class="count">{{ expiring_today|length }}</span>
        </div>
        {% if expiring_today %}
        <table class="signal-table">
            <thead><tr>
                <th>Ticker</th><th>Strike</th><th>Type</th><th>Expiry</th><th>Premium</th><th>Outcome</th>
            </tr></thead>
            <tbody>
            {% for s in expiring_today %}
            <tr id="exprow-{{ loop.index }}">
                <td style="color:#f1f5f9;font-weight:700;font-size:12px">{{ s.decoded.ticker }}</td>
                <td style="color:#cbd5e1;font-size:12px;font-weight:600">{{ s.decoded.strike_display }}</td>
                <td><span class="{{ 'type-call' if s.contract_type == 'CALL' else 'type-put' }}">{{ s.decoded.contract_type }}</span></td>
                <td style="color:#64748b;font-size:11px">
                    {{ s.decoded.expiry_display }}
                    <span class="dte-badge dte-urgent" style="margin-left:4px">{{ s.decoded.dte_display }}</span>
                </td>
                <td style="color:#f1f5f9;font-weight:600">{{ s.premium_display }}</td>
                <td>
                    {% if s.outcome %}
                    <span class="outcome-recorded outcome-{{ s.outcome }}">{{ s.outcome }}</span>
                    {% else %}
                    <button class="outcome-btn btn-win"  onclick="recordOutcome('{{ s.contract }}','WIN',{{ loop.index }})">W</button>
                    <button class="outcome-btn btn-loss" onclick="recordOutcome('{{ s.contract }}','LOSS',{{ loop.index }})">L</button>
                    <button class="outcome-btn btn-flat" onclick="recordOutcome('{{ s.contract }}','FLAT',{{ loop.index }})">F</button>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-msg">No HIGH/INST signals expiring today</div>
        {% endif %}
    </div>

</div><!-- /tab-signals -->


<!-- ══════════════════════════════════════════════ TAB: ANALYTICS ═══ -->
<div id="tab-analytics" class="tab-content">
    <div id="analytics-loading" class="loading-msg">Loading analytics...</div>
    <div id="analytics-content" style="display:none"></div>
</div>


<!-- ═══════════════════════════════════════════════════════ SCRIPTS ═══ -->
<script>
{% raw %}
// ─── SERVER DATA ────────────────────────────────────────────────────
{% endraw %}
const SIGNAL_HISTORY  = {{ signal_history_json | safe }};
const HIGH_SIGNALS    = {{ high_signals_json   | safe }};
const INST_SIGNALS    = {{ inst_signals_json   | safe }};
{% raw %}

// ─── TAB SWITCHING ──────────────────────────────────────────────────
const loadedTabs = new Set(['portfolio', 'signals']);

function switchTab(name) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
    const content = document.getElementById('tab-' + name);
    if (content) content.classList.add('active');
    const btn = document.querySelector(`.tab-btn[data-tab="${name}"]`);
    if (btn) btn.classList.add('active');
    history.replaceState(null, '', '#' + name);

    if (!loadedTabs.has(name)) {
        loadedTabs.add(name);
        if (name === 'positions') loadPositions();
        if (name === 'analytics') loadAnalytics();
    }
}

function getInitialTab() {
    const hash = window.location.hash.slice(1);
    return ['portfolio', 'positions', 'signals', 'analytics'].includes(hash) ? hash : 'portfolio';
}

// ─── POSITIONS TAB ──────────────────────────────────────────────────
async function loadPositions() {
    try {
        const res = await fetch('/api/positions');
        const data = await res.json();
        renderPositions(data);
    } catch (e) {
        document.getElementById('positions-loading').textContent = 'Failed to load positions.';
    }
}

function renderPositions(data) {
    const { positions, summary: s, timestamp } = data;

    // pc: boolean/null → green/red/gray  |  pcR: reversed (true→red for post-exit)
    const pc       = v => v == null ? '#475569' : v ? '#22c55e' : '#ef4444';
    const pcR      = v => v == null ? '#475569' : v ? '#ef4444' : '#22c55e';
    const colorVal  = (disp, pos) => pos == null
        ? `<span style="color:#475569">${disp}</span>`
        : `<span style="font-weight:600;color:${pos ? '#22c55e' : '#ef4444'}">${disp}</span>`;
    const colorValR = (disp, pos) => pos == null
        ? `<span style="color:#475569">${disp}</span>`
        : `<span style="font-weight:600;color:${pos ? '#ef4444' : '#22c55e'}">${disp}</span>`;
    const dteBadge = dte => {
        if (dte == null) return '—';
        const cls = dte === 0 ? 'dte-urgent' : dte <= 2 ? 'dte-soon' : 'dte-normal';
        return `<span class="dte-badge ${cls}">${dte}d</span>`;
    };
    const wrHtml = (w, l) => {
        if (w == null) return '<span style="color:#475569">n/a</span>';
        const total = (w || 0) + (l || 0);
        if (!total) return '<span style="color:#475569">—</span>';
        const wr = Math.round((w || 0) / total * 100);
        const wrc = wr >= 50 ? '#22c55e' : '#ef4444';
        return `<span style="color:#22c55e">${w}W</span><span style="color:#475569">/</span><span style="color:#ef4444">${l}L</span> <span style="color:${wrc};font-size:10px">(${wr}%)</span>`;
    };
    const thRight = t => `<th style="text-align:right">${t}</th>`;

    // ── Summary header block (4-row table) ──────────────────────────────
    const cols = ['Category','Count','Wins','Cost','Unrealized','Realized','Potential','ROI','Post-Exit'];
    const thRow = cols.map(c => `<th style="font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:0.06em;padding:0 8px 6px 0;text-align:${c==='Category'?'left':'right'}">${c}</th>`).join('');

    const sumRow = (label, labelColor, count, winsHtml, cost, unr, real, pot, roi, pe, isFoot) => {
        const rs = isFoot ? 'border-top:1px solid #475569;' : '';
        return `<tr style="${rs}">
            <td style="color:${labelColor};font-weight:600;font-size:11px;padding:6px 8px 6px 0">${label}</td>
            <td style="color:#e2e8f0;text-align:right;font-size:13px;font-weight:${isFoot?'700':'600'};padding:6px 8px 6px 0">${count}</td>
            <td style="text-align:right;font-size:11px;padding:6px 8px 6px 0">${winsHtml}</td>
            <td style="color:#e2e8f0;text-align:right;font-size:12px;padding:6px 8px 6px 0">${cost}</td>
            <td style="text-align:right;padding:6px 8px 6px 0">${unr}</td>
            <td style="text-align:right;padding:6px 8px 6px 0">${real}</td>
            <td style="text-align:right;font-weight:700;padding:6px 8px 6px 0">${pot}</td>
            <td style="text-align:right;padding:6px 8px 6px 0">${roi}</td>
            <td style="text-align:right;padding:6px 0 6px 0">${pe}</td>
        </tr>`;
    };
    const na = () => '<span style="color:#475569">n/a</span>';

    let html = `
    <div class="card" style="margin-bottom:16px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
            <span style="font-size:11px;font-weight:600;color:#64748b;letter-spacing:0.1em;text-transform:uppercase">Position Summary</span>
            <button class="pos-refresh-btn" onclick="reloadPositions()">↻ Refresh</button>
        </div>
        <table style="width:100%;border-collapse:collapse">
            <thead><tr>${thRow}</tr></thead>
            <tbody>
                ${sumRow('● Open', '#22c55e', s.open.count,
                    na(),
                    s.open.cost_disp,
                    colorVal(s.open.unrealized_disp, s.open.unrealized_pos),
                    na(),
                    colorVal(s.open.potential_disp, s.open.potential_pos),
                    colorVal(s.open.roi_disp, s.open.roi_pos),
                    na(), false)}
                ${sumRow('⏸ Stopped', '#f97316', s.stopped.count,
                    wrHtml(s.stopped.wins, s.stopped.losses),
                    s.stopped.cost_disp,
                    na(),
                    colorVal(s.stopped.realized_disp, s.stopped.realized_pos),
                    colorVal(s.stopped.potential_disp, s.stopped.potential_pos),
                    colorVal(s.stopped.roi_disp, s.stopped.roi_pos),
                    colorValR(s.stopped.post_exit_disp, s.stopped.post_exit_pos), false)}
                ${sumRow('✓ Closed', '#64748b', s.closed.count,
                    wrHtml(s.closed.wins, s.closed.losses),
                    s.closed.cost_disp,
                    na(),
                    colorVal(s.closed.realized_disp, s.closed.realized_pos),
                    colorVal(s.closed.potential_disp, s.closed.potential_pos),
                    colorVal(s.closed.roi_disp, s.closed.roi_pos),
                    colorValR(s.closed.post_exit_disp, s.closed.post_exit_pos), false)}
            </tbody>
            <tfoot>
                ${sumRow('Totals', '#e2e8f0', s.totals.count,
                    wrHtml(s.totals.wins, s.totals.losses),
                    s.totals.cost_disp,
                    colorVal(s.totals.unrealized_disp, s.totals.unrealized_pos),
                    colorVal(s.totals.realized_disp, s.totals.realized_pos),
                    colorVal(s.totals.potential_disp, s.totals.potential_pos),
                    colorVal(s.totals.roi_disp, s.totals.roi_pos),
                    colorValR(s.totals.post_exit_disp, s.totals.post_exit_pos), true)}
            </tfoot>
        </table>
    </div>`;

    if (positions.length === 0) {
        html += '<div class="empty-msg">No open or tracked positions</div>';
    } else {
        // In-table totals for thead summary row (cols 12-15 of 18)
        const tUnreal = colorVal(s.open.unrealized_disp, s.open.unrealized_pos);
        const tReal   = colorVal(s.stopped.realized_disp, s.stopped.realized_pos);
        const tPE     = colorValR(s.stopped.post_exit_disp, s.stopped.post_exit_pos);
        const sumTd   = (content) => `<td style="text-align:right;padding:4px 8px 4px 0;border-bottom:none">${content}</td>`;
        const sumLabel = t => `<div style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:0.08em;line-height:1.3">${t}</div>`;

        html += `
        <div class="card">
            <table class="data-table">
                <thead>
                <tr style="border-bottom:none">
                    <td colspan="12" style="padding:0;border-bottom:none"></td>
                    ${sumTd(sumLabel('Unrealized') + `<div style="font-size:11px;font-weight:700">${tUnreal}</div>`)}
                    ${sumTd(sumLabel('Realized')   + `<div style="font-size:11px;font-weight:700">${tReal}</div>`)}
                    <td style="border-bottom:none;padding:0"></td>
                    ${sumTd(sumLabel('Post-Exit')  + `<div style="font-size:11px;font-weight:700">${tPE}</div>`)}
                    <td colspan="3" style="border-bottom:none;padding:0"></td>
                </tr>
                <tr>
                    <th>Ticker</th>
                    <th>Expiry</th>
                    <th>Type</th>
                    <th>Strike</th>
                    ${thRight('DTE (Entry)')}
                    <th>Entry Date</th>
                    ${thRight('Score')}
                    ${thRight('Premium')}
                    <th>DTE</th>
                    ${thRight('Entry $')}
                    ${thRight('Last $')}
                    ${thRight('Exit $')}
                    ${thRight('Unrealized')}
                    ${thRight('Realized')}
                    ${thRight('%')}
                    ${thRight('Post-Exit')}
                    <th>Status</th>
                    <th>Exit Date</th>
                    ${thRight('Days Held')}
                </tr>
                </thead>
                <tbody>`;

        positions.forEach(p => {
            const isOpen    = p.status === 'OPEN';
            const isStopped = p.status === 'STOP_TRIGGERED';
            const statusBadge = isOpen
                ? '<span class="badge badge-open">OPEN</span>'
                : '<span class="badge badge-tracking">TRACKING</span>';
            const lpDisp = p.last_price != null ? `$${p.last_price.toFixed(2)}` : '—';

            html += `
            <tr>
                <td style="color:#f1f5f9;font-weight:700">${p.ticker}</td>
                <td style="color:#64748b;font-size:11px">${p.expiry_display}</td>
                <td><span class="${p.contract_type === 'Call' ? 'type-call' : 'type-put'}">${p.contract_type}</span></td>
                <td style="color:#cbd5e1;font-weight:600">${p.strike_display}</td>
                <td style="text-align:right;color:#64748b">${p.dte_at_entry != null ? p.dte_at_entry + 'd' : '—'}</td>
                <td style="color:#64748b;font-size:11px">${p.entry_date_display}</td>
                <td style="text-align:right;color:#94a3b8">${p.score_display}</td>
                <td style="text-align:right;color:#94a3b8">${p.premium_display}</td>
                <td>${dteBadge(p.current_dte)}</td>
                <td style="text-align:right;color:#94a3b8">$${p.entry_price.toFixed(2)}</td>
                <td style="text-align:right;color:#e2e8f0">${lpDisp}</td>
                <td style="text-align:right;color:#94a3b8">${p.exit_price_display}</td>
                <td style="text-align:right;font-weight:600;color:${pc(p.unrealized_pos)}">${p.unrealized_display}</td>
                <td style="text-align:right;font-weight:600;color:${pc(p.realized_pos)}">${p.realized_display}</td>
                <td style="text-align:right;color:${pc(p.pct_pos)}">${p.pct_display}</td>
                <td style="text-align:right;font-weight:600;color:${pcR(p.post_exit_pos)}">${p.post_exit_display}</td>
                <td>${statusBadge}</td>
                <td style="color:#64748b;font-size:11px">${p.exit_date_display}</td>
                <td style="text-align:right;color:#64748b;font-size:11px">${p.days_held_display}</td>
            </tr>`;
        });

        html += '</tbody></table></div>';
    }

    html += `<div style="color:#475569;font-size:10px;margin-top:8px;text-align:right">Snapshot: ${timestamp}</div>`;

    document.getElementById('positions-content').innerHTML = html;
    document.getElementById('positions-loading').style.display = 'none';
    document.getElementById('positions-content').style.display = 'block';
}

async function reloadPositions() {
    document.getElementById('positions-loading').style.display = 'block';
    document.getElementById('positions-content').style.display = 'none';
    await loadPositions();
}

// ─── ANALYTICS TAB ──────────────────────────────────────────────────
async function loadAnalytics() {
    try {
        const res = await fetch('/api/analytics');
        const data = await res.json();
        renderAnalytics(data);
    } catch (e) {
        document.getElementById('analytics-loading').textContent = 'Failed to load analytics.';
    }
}

function renderAnalytics(data) {
    const { score_buckets, dte_buckets, premium_tiers, thresholds, insights } = data;

    const pnlColor = v => (v || 0) >= 0 ? '#22c55e' : '#ef4444';
    const barHtml  = pct => `<div style="flex:1;margin:0 8px;height:4px;background:#334155;border-radius:2px;overflow:hidden">
        <div style="width:${pct}%;height:100%;background:#22c55e;border-radius:2px"></div></div>`;

    // Score bucket table (highlight 6.0-6.9 as the sweet spot)
    let bestWinRate = 0;
    score_buckets.forEach(r => { if (r.win_rate > bestWinRate) bestWinRate = r.win_rate; });

    let scoreRows = score_buckets.map(r => {
        const isBest = r.win_rate === bestWinRate;
        const rowClass = isBest ? 'score-highlight' : (r.win_rate < 50 ? 'score-dim' : '');
        const pc = pnlColor(r.total_pnl);
        return `<tr class="${rowClass}">
            <td style="color:#f1f5f9;font-weight:${isBest?'700':'400'}">${r.score_bucket}${isBest ? ' ★' : ''}</td>
            <td style="color:#64748b">${r.total}</td>
            <td style="font-size:11px"><span style="color:#22c55e">${r.wins}W</span><span style="color:#475569">/</span><span style="color:#ef4444">${r.total - r.wins}L</span></td>
            <td>
                <div style="display:flex;align-items:center;min-width:140px">
                    <span style="color:#22c55e;font-weight:700;min-width:40px">${r.win_rate}%</span>
                    ${barHtml(r.win_rate)}
                </div>
            </td>
            <td style="text-align:right;color:${pc};font-weight:600">${r.total_pnl_display}</td>
            <td style="text-align:right;color:#64748b;font-size:11px">${r.avg_pnl_display}</td>
        </tr>`;
    }).join('');

    // DTE bucket table
    let dteRows = dte_buckets.map(r => {
        const pc = pnlColor(r.total_pnl);
        return `<tr>
            <td style="color:#f1f5f9">${r.dte_bucket}</td>
            <td style="color:#64748b">${r.total}</td>
            <td style="font-size:11px"><span style="color:#22c55e">${r.wins}W</span><span style="color:#475569">/</span><span style="color:#ef4444">${r.total - r.wins}L</span></td>
            <td style="color:#22c55e;font-weight:700">${r.win_rate}%</td>
            <td style="text-align:right;color:${pc};font-weight:600">${r.total_pnl_display}</td>
        </tr>`;
    }).join('');

    // Premium tier table
    let premRows = premium_tiers.map(r => {
        const pc = pnlColor(r.total_pnl);
        return `<tr>
            <td style="color:#f1f5f9">${r.premium_tier}</td>
            <td style="color:#64748b">${r.total}</td>
            <td style="color:#22c55e;font-weight:700">${r.win_rate}%</td>
            <td style="text-align:right;color:${pc};font-weight:600">${r.total_pnl_display}</td>
        </tr>`;
    }).join('');

    // Threshold simulation table
    let threshRows = thresholds.map(r => {
        const pc = pnlColor(r.total_pnl);
        const isCurrent = r.threshold === 6.0;
        return `<tr style="${isCurrent ? 'background:#3b82f611' : ''}">
            <td style="color:${isCurrent ? '#3b82f6' : '#f1f5f9'};font-weight:${isCurrent?'700':'400'}">
                ≥ ${r.threshold}${isCurrent ? ' ← current' : ''}</td>
            <td style="color:#64748b">${r.total}</td>
            <td style="color:#22c55e;font-weight:700">${r.win_rate}%</td>
            <td style="text-align:right;color:${pc};font-weight:600">${r.total_pnl_display}</td>
        </tr>`;
    }).join('');

    // Key Insights section
    const insightsHtml = (insights && insights.length) ? `
    <div class="card full-width" style="margin-bottom:16px;border-color:#3b82f644">
        <div class="card-title" style="color:#3b82f6">Key Insights</div>
        <ul style="list-style:none;padding:0;margin:0">
            ${insights.map(txt => `
            <li style="display:flex;align-items:baseline;gap:10px;padding:8px 0;border-bottom:1px solid #334155">
                <span style="color:#3b82f6;font-size:14px;flex-shrink:0">→</span>
                <span style="color:#e2e8f0;font-size:12px;line-height:1.6">${txt}</span>
            </li>`).join('')}
        </ul>
    </div>` : '';

    const html = insightsHtml + `
    <!-- Score Buckets — full width -->
    <div class="card full-width" style="margin-bottom:16px">
        <div class="card-title">Win Rate by Score Bucket</div>
        <table class="data-table">
            <thead><tr>
                <th>Score</th><th>Trades</th><th>W/L</th>
                <th style="min-width:180px">Win Rate</th>
                <th style="text-align:right">Total P&L</th>
                <th style="text-align:right">Avg P&L</th>
            </tr></thead>
            <tbody>${scoreRows}</tbody>
        </table>
    </div>

    <div class="grid-2">
        <!-- DTE Buckets -->
        <div class="card">
            <div class="card-title">Win Rate by DTE at Entry</div>
            <table class="data-table">
                <thead><tr>
                    <th>DTE</th><th>Trades</th><th>W/L</th><th>Win%</th><th style="text-align:right">Total P&L</th>
                </tr></thead>
                <tbody>${dteRows}</tbody>
            </table>
        </div>

        <!-- Premium Tiers -->
        <div class="card">
            <div class="card-title">Win Rate by Premium Tier</div>
            <table class="data-table">
                <thead><tr>
                    <th>Premium</th><th>Trades</th><th>Win%</th><th style="text-align:right">Total P&L</th>
                </tr></thead>
                <tbody>${premRows}</tbody>
            </table>
        </div>
    </div>

    <!-- Score Threshold Simulation -->
    <div class="card full-width">
        <div class="card-title">Score Threshold Simulation
            <span style="color:#475569;font-size:10px;font-weight:400;text-transform:none;letter-spacing:0">
                — if we had only entered signals above each threshold</span>
        </div>
        <table class="data-table">
            <thead><tr>
                <th>Min Score</th><th>Eligible Trades</th><th>Win%</th><th style="text-align:right">Total P&L</th>
            </tr></thead>
            <tbody>${threshRows}</tbody>
        </table>
    </div>`;

    document.getElementById('analytics-content').innerHTML = html;
    document.getElementById('analytics-loading').style.display = 'none';
    document.getElementById('analytics-content').style.display = 'block';
}

// ─── SIGNAL HISTORY CHART ───────────────────────────────────────────
(function renderHistoryChart() {
    const canvas = document.getElementById('historyChart');
    if (!canvas) return;
    if (!SIGNAL_HISTORY || SIGNAL_HISTORY.length === 0) {
        document.getElementById('chartContainer').innerHTML =
            '<div class="empty-msg" style="height:160px;display:flex;align-items:center;justify-content:center">No historical data yet</div>';
        return;
    }
    const PADDING = { top: 12, right: 60, bottom: 32, left: 36 };
    const BAR_GAP = 0.25;
    const COLORS  = { high: '#f97316', inst: '#a855f7', watch: '#3b82f6', premium: '#e2e8f0' };
    const container = canvas.parentElement;
    const dpr = window.devicePixelRatio || 1;
    const W = container.clientWidth || 800, H = 160;
    canvas.width  = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width  = W + 'px';
    canvas.style.height = H + 'px';
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    const innerW = W - PADDING.left - PADDING.right;
    const innerH = H - PADDING.top  - PADDING.bottom;
    const days     = SIGNAL_HISTORY;
    const n        = days.length;
    const maxCount = Math.max(1, ...days.map(d => (d.high_count||0)+(d.inst_count||0)+(d.watch_count||0)));
    const maxPrem  = Math.max(1, ...days.map(d => d.total_premium||0));
    const xSlot = i => PADDING.left + (i / n) * innerW;
    const slotW = () => innerW / n;
    const yCount = v => PADDING.top + innerH - (v / maxCount) * innerH;
    const yPrem  = v => PADDING.top + innerH - (v / maxPrem)  * innerH;
    // grid
    ctx.strokeStyle = '#334155'; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
        const y = PADDING.top + (innerH / 4) * i;
        ctx.beginPath(); ctx.moveTo(PADDING.left, y); ctx.lineTo(W - PADDING.right, y); ctx.stroke();
    }
    // bars
    const bw = slotW() * (1 - BAR_GAP);
    const bx = i => xSlot(i) + slotW() * BAR_GAP / 2;
    days.forEach((d, i) => {
        let base = PADDING.top + innerH;
        [{ key: 'watch_count', color: COLORS.watch },
         { key: 'inst_count',  color: COLORS.inst  },
         { key: 'high_count',  color: COLORS.high  }].forEach(({ key, color }) => {
            const count = d[key] || 0; if (!count) return;
            const h = (count / maxCount) * innerH;
            ctx.fillStyle = color; ctx.fillRect(bx(i), base - h, bw, h); base -= h;
        });
    });
    // premium line
    ctx.beginPath(); ctx.strokeStyle = COLORS.premium; ctx.lineWidth = 1.5; ctx.setLineDash([3, 3]);
    days.forEach((d, i) => {
        const x = bx(i) + bw / 2, y = yPrem(d.total_premium||0);
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke(); ctx.setLineDash([]);
    days.forEach((d, i) => {
        const x = bx(i) + bw / 2, y = yPrem(d.total_premium||0);
        ctx.beginPath(); ctx.arc(x, y, 2.5, 0, Math.PI*2);
        ctx.fillStyle = COLORS.premium; ctx.fill();
    });
    // labels
    ctx.fillStyle = '#475569'; ctx.font = '9px SF Mono, Consolas, monospace'; ctx.textAlign = 'center';
    const step = Math.max(1, Math.ceil(n / 6));
    days.forEach((d, i) => {
        if (i % step !== 0 && i !== n-1) return;
        ctx.fillText(d.scan_date ? d.scan_date.slice(5) : '', bx(i) + bw/2, H - 6);
    });
    ctx.textAlign = 'right'; ctx.fillStyle = '#475569';
    for (let i = 0; i <= 4; i++) {
        const v = Math.round((maxCount / 4) * (4 - i));
        ctx.fillText(v, PADDING.left - 4, PADDING.top + (innerH / 4) * i + 3);
    }
    ctx.textAlign = 'left'; ctx.fillStyle = '#64748b';
    [0, 0.5, 1].forEach(frac => {
        const v = maxPrem * frac;
        const y = yPrem(v);
        const label = v >= 1e6 ? '$'+(v/1e6).toFixed(1)+'M' : v >= 1e3 ? '$'+Math.round(v/1e3)+'K' : '$'+Math.round(v);
        ctx.fillText(label, W - PADDING.right + 4, y + 3);
    });
    canvas.addEventListener('mousemove', e => {
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const idx = Math.floor(((mx - PADDING.left) / innerW) * n);
        if (idx < 0 || idx >= n) { canvas.title = ''; return; }
        const d = days[idx];
        const total = (d.high_count||0)+(d.inst_count||0)+(d.watch_count||0);
        canvas.title = `${d.scan_date}\nSignals: ${total} (H:${d.high_count||0} I:${d.inst_count||0} W:${d.watch_count||0})\nPremium: ${d.premium_display||'$0'}`;
    });
})();

// ─── DTE FILTER (signals tab) ────────────────────────────────────────
let activeDteHigh = 'all', activeDteInst = 'all';

function dteBucket(daysOut) {
    if (daysOut === 0)               return '0dte';
    if (daysOut >= 1 && daysOut <= 2) return '1-2d';
    if (daysOut >= 3 && daysOut <= 7) return 'week';
    if (daysOut > 7)                 return '30d+';
    return 'other';
}
function dteBadgeClass(daysOut) {
    if (daysOut === 0)  return 'dte-urgent';
    if (daysOut <= 2)   return 'dte-soon';
    return 'dte-normal';
}

function setDteFilter(bucket, table) {
    if (table === 'high') activeDteHigh = bucket;
    else                  activeDteInst = bucket;

    // update tab button styles for the right set
    const tableEl = document.getElementById(table + '-table');
    if (tableEl) {
        tableEl.closest('.card').querySelectorAll('.dte-tab').forEach(btn => {
            const b = btn.getAttribute('onclick').match(/'([^']+)'/)[1];
            btn.classList.toggle('active', b === bucket);
        });
    }
    if (table === 'high') renderHighTable();
    else renderInstTable();
}

function renderHighTable() {
    const filtered = HIGH_SIGNALS.filter(s => activeDteHigh === 'all' || dteBucket(s.days_out) === activeDteHigh);
    const tbody = document.getElementById('high-tbody');
    const empty = document.getElementById('high-empty');
    const count = document.getElementById('high-count');
    count.textContent = filtered.length;
    if (!filtered.length) { tbody.innerHTML = ''; empty.style.display = 'block'; return; }
    empty.style.display = 'none';
    tbody.innerHTML = filtered.map(s => `
        <tr>
            <td style="color:#f1f5f9;font-weight:700;font-size:12px">${s.ticker}</td>
            <td style="color:#cbd5e1;font-size:12px;font-weight:600">${s.strike_display}</td>
            <td><span class="${s.contract_type === 'CALL' ? 'type-call' : 'type-put'}">${s.contract_type_display}</span></td>
            <td style="color:#64748b;font-size:11px;white-space:nowrap">
                ${s.expiry_display}
                <span class="dte-badge ${dteBadgeClass(s.days_out)}" style="margin-left:4px">${s.dte_display}</span>
            </td>
            <td style="color:#64748b;font-size:11px">${s.share_price || '<span style="color:#334155">—</span>'}</td>
            <td style="color:#f1f5f9;font-weight:600">${s.premium_display}</td>
            <td>${s.has_greeks
                ? `<span style="font-size:12px;font-weight:600;color:#e2e8f0">${s.delta}</span>
                   <span style="font-size:10px;margin-left:4px;color:${s.moneyness_color}">${s.moneyness}</span>`
                : '<span style="color:#475569">—</span>'}</td>
            <td>${s.has_greeks
                ? `<span style="font-size:12px;color:#94a3b8">${s.iv}</span>`
                : '<span style="color:#475569">—</span>'}</td>
            <td style="color:#94a3b8">${s.composite_score}</td>
        </tr>`).join('');
}

function renderInstTable() {
    const filtered = INST_SIGNALS.filter(s => activeDteInst === 'all' || dteBucket(s.days_out) === activeDteInst);
    const tbody = document.getElementById('inst-tbody');
    const empty = document.getElementById('inst-empty');
    const count = document.getElementById('inst-count');
    count.textContent = filtered.length;
    if (!filtered.length) { tbody.innerHTML = ''; empty.style.display = 'block'; return; }
    empty.style.display = 'none';
    tbody.innerHTML = filtered.map(s => `
        <tr>
            <td style="color:#f1f5f9;font-weight:700;font-size:12px">${s.ticker}</td>
            <td style="color:#cbd5e1;font-size:12px;font-weight:600">${s.strike_display}</td>
            <td><span class="${s.contract_type === 'CALL' ? 'type-call' : 'type-put'}">${s.contract_type_display}</span></td>
            <td style="color:#64748b;font-size:11px;white-space:nowrap">
                ${s.expiry_display}
                <span class="dte-badge ${dteBadgeClass(s.days_out)}" style="margin-left:4px">${s.dte_display}</span>
            </td>
            <td style="color:#64748b;font-size:11px">${s.share_price || '<span style="color:#334155">—</span>'}</td>
            <td style="color:#f1f5f9;font-weight:600">${s.premium_display}</td>
            <td style="color:#94a3b8">${s.composite_score}</td>
        </tr>`).join('');
}

// ─── OUTCOME RECORDING ───────────────────────────────────────────────
async function recordOutcome(contract, outcome, rowIndex) {
    try {
        const res = await fetch('/api/record-outcome', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ contract, outcome, notes: '' })
        });
        const data = await res.json();
        if (data.success) {
            const td = document.querySelector(`#exprow-${rowIndex} td:last-child`);
            if (td) td.innerHTML = `<span class="outcome-recorded outcome-${outcome}">${outcome}</span>`;
        }
    } catch (e) { console.error('Outcome error:', e); }
}

// ─── AUTO-REFRESH (scan-based) ───────────────────────────────────────
let lastKnownScan = null;
async function checkForNewScan() {
    try {
        const res = await fetch('/api/last-scan');
        const data = await res.json();
        if (lastKnownScan === null) lastKnownScan = data.last_scan;
        else if (data.last_scan !== lastKnownScan) location.reload();
    } catch (e) {}
}
setInterval(checkForNewScan, 60000);
checkForNewScan();

// ─── INIT ─────────────────────────────────────────────────────────────
renderHighTable();
renderInstTable();
switchTab(getInitialTab());

{% endraw %}
</script>
</body>
</html>"""


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
