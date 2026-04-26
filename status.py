import os
import sys
import sqlite3
import pytz
import psutil
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY      = os.getenv("PUBLIC_SECRET_KEY")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY")
BASE_URL        = "https://api.public.com"
MARKET_TIMEZONE = "US/Eastern"

# =============================================================================
# PROCESS CHECKS
# =============================================================================

def find_python_process(script_name):
    """
    Search running processes for a Python script by name.
    Returns the process if found, None if not running.
    """
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                # Check if any argument in the command line matches script name
                if any(script_name in arg for arg in cmdline):
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass
    return None


def check_processes():
    """
    Check if scheduler.py and position_monitor.py are running.
    Returns dict with status for each script.
    """
    scripts = {
        "scheduler.py":         "Options Scanner",
        "position_monitor.py":  "Position Monitor",
    }

    results = {}
    for script, label in scripts.items():
        proc = find_python_process(script)
        if proc:
            try:
                # Get process start time and memory usage
                start_time = datetime.fromtimestamp(proc.create_time())
                runtime    = datetime.now() - start_time
                hours      = int(runtime.total_seconds() // 3600)
                mins       = int((runtime.total_seconds() % 3600) // 60)
                memory_mb  = proc.memory_info().rss / 1024 / 1024

                results[script] = {
                    "running":   True,
                    "label":     label,
                    "pid":       proc.pid,
                    "runtime":   f"{hours}h {mins}m",
                    "memory_mb": round(memory_mb, 1),
                }
            except Exception:
                results[script] = {
                    "running": True,
                    "label":   label,
                    "pid":     proc.pid,
                    "runtime": "unknown",
                    "memory_mb": 0,
                }
        else:
            results[script] = {
                "running": False,
                "label":   label,
            }

    return results


# =============================================================================
# API CHECKS
# =============================================================================

def check_public_api():
    """
    Verify Public API is reachable and credentials are valid.
    Returns dict with status and latency.
    """
    try:
        start    = datetime.now()
        response = requests.post(
            f"{BASE_URL}/userapiauthservice/personal/access-tokens",
            json={"secret": SECRET_KEY, "validityInMinutes": 60},
            timeout=10
        )
        latency_ms = int((datetime.now() - start).total_seconds() * 1000)

        if response.status_code == 200:
            return {
                "ok":         True,
                "latency_ms": latency_ms,
                "message":    f"Connected ({latency_ms}ms)",
            }
        else:
            return {
                "ok":      False,
                "message": f"Auth failed — HTTP {response.status_code}",
            }
    except requests.Timeout:
        return {"ok": False, "message": "Timeout — API unreachable"}
    except Exception as e:
        return {"ok": False, "message": f"Error: {e}"}


def check_anthropic_api():
    """
    Verify Anthropic API key is present and the client initializes.
    Does a lightweight check without making a full API call.
    """
    if not ANTHROPIC_KEY:
        return {"ok": False, "message": "ANTHROPIC_API_KEY not found in .env"}

    if not ANTHROPIC_KEY.startswith("sk-ant-"):
        return {"ok": False, "message": "API key format looks incorrect"}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        # Verify the client initialized without error
        # We don't make an actual API call to avoid cost
        if client:
            return {
                "ok":      True,
                "message": "Key present and client initialized",
            }
    except ImportError:
        return {"ok": False, "message": "anthropic package not installed"}
    except Exception as e:
        return {"ok": False, "message": f"Client error: {e}"}

    return {"ok": False, "message": "Unknown error"}


# =============================================================================
# DATABASE CHECKS
# =============================================================================

def check_database():
    """
    Verify DB is accessible and return key stats.
    """
    try:
        from journal import DB_PATH

        if not os.path.exists(DB_PATH):
            return {"ok": False, "message": f"DB file not found: {DB_PATH}"}

        db_size_mb = os.path.getsize(DB_PATH) / 1024 / 1024

        conn   = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Basic integrity check
        cursor.execute("PRAGMA integrity_check")
        integrity = cursor.fetchone()[0]

        if integrity != "ok":
            conn.close()
            return {"ok": False, "message": f"DB integrity check failed: {integrity}"}

        # Key stats
        cursor.execute("SELECT COUNT(*) as n FROM signals")
        signal_count = cursor.fetchone()["n"]

        cursor.execute("""
            SELECT COUNT(*) as n FROM signals
            WHERE outcome IS NULL
            AND expiration >= date('now')
        """)
        pending_count = cursor.fetchone()["n"]

        cursor.execute("""
            SELECT COUNT(*) as n FROM paper_trades
            WHERE status = 'OPEN'
        """)
        open_trades = cursor.fetchone()["n"]

        conn.close()

        return {
            "ok":           True,
            "message":      "Healthy",
            "size_mb":      round(db_size_mb, 2),
            "signals":      signal_count,
            "pending":      pending_count,
            "open_trades":  open_trades,
        }

    except Exception as e:
        return {"ok": False, "message": f"DB error: {e}"}


# =============================================================================
# SCAN AND ACTIVITY CHECKS
# =============================================================================

def check_last_scan():
    """
    Check when the last scan completed and how long ago it was.
    """
    try:
        from journal import DB_PATH

        conn   = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT MAX(scan_time) as last_scan
            FROM scan_log
        """)
        row = cursor.fetchone()
        conn.close()

        if not row or not row["last_scan"]:
            return {"ok": False, "message": "No scans recorded yet"}

        last_scan_str = row["last_scan"]
        last_scan_dt  = datetime.strptime(
            last_scan_str, "%Y-%m-%d %H:%M:%S"
        )

        eastern   = pytz.timezone(MARKET_TIMEZONE)
        now       = datetime.now(eastern).replace(tzinfo=None)
        age       = now - last_scan_dt
        mins_ago  = int(age.total_seconds() / 60)

        if mins_ago < 60:
            age_str = f"{mins_ago} minutes ago"
        elif mins_ago < 1440:
            age_str = f"{mins_ago // 60}h {mins_ago % 60}m ago"
        else:
            age_str = f"{mins_ago // 1440}d ago"

        # Flag if last scan was more than 45 minutes ago during market hours
        market_open = is_market_hours()
        stale       = market_open and mins_ago > 45

        return {
            "ok":       not stale,
            "last":     last_scan_str,
            "age":      age_str,
            "mins_ago": mins_ago,
            "stale":    stale,
            "message":  f"Last scan: {age_str}",
        }

    except Exception as e:
        return {"ok": False, "message": f"Error reading scan log: {e}"}


def check_open_positions():
    """
    Check open paper positions and flag any approaching stop.
    """
    try:
        from journal import DB_PATH

        conn   = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, signal_contract, entry_price,
                   target_price, stop_price, total_cost
            FROM paper_trades
            WHERE status = 'OPEN'
        """)
        positions = [dict(row) for row in cursor.fetchall()]
        conn.close()

        if not positions:
            return {
                "ok":      True,
                "count":   0,
                "message": "No open positions",
                "alerts":  [],
            }

        alerts = []
        for p in positions:
            entry  = p["entry_price"]
            stop   = p["stop_price"]
            target = p["target_price"]

            # We can't check current price without an API call here
            # Just flag DTE if contract is expiring soon
            try:
                contract = p["signal_contract"]
                date_str = contract[-15:-9]
                year  = int("20" + date_str[0:2])
                month = int(date_str[2:4])
                day   = int(date_str[4:6])
                expiry = datetime(year, month, day).date()

                eastern = pytz.timezone(MARKET_TIMEZONE)
                today   = datetime.now(eastern).date()
                dte     = (expiry - today).days

                if dte == 0:
                    alerts.append(
                        f"⚠️  #{p['id']} {contract} expires TODAY"
                    )
                elif dte == 1:
                    alerts.append(
                        f"⚠️  #{p['id']} {contract} expires TOMORROW"
                    )
            except Exception:
                pass

        return {
            "ok":      True,
            "count":   len(positions),
            "message": f"{len(positions)} open position(s)",
            "alerts":  alerts,
            "positions": positions,
        }

    except Exception as e:
        return {"ok": False, "message": f"Error: {e}", "alerts": []}


# =============================================================================
# LOG FILE CHECKS
# =============================================================================

def check_log_files():
    """
    Scan today's log files for errors and warnings.
    Returns counts and recent error lines.
    """
    results = {}

    log_scripts = ["scanner", "monitor", "outcome", "summary"]

    eastern    = pytz.timezone(MARKET_TIMEZONE)
    today_str  = datetime.now(eastern).strftime("%Y-%m-%d")

    from logger_setup import get_log_path_for_date

    for script in log_scripts:
        log_path = get_log_path_for_date(script, today_str)

        if not log_path:
            results[script] = {
                "exists":    False,
                "errors":    0,
                "warnings":  0,
                "error_lines": [],
            }
            continue

        try:
            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            error_lines   = [l.strip() for l in lines
                             if "✗" in l or "ERROR" in l.upper()
                             or "FAILED" in l.upper()]
            warning_lines = [l.strip() for l in lines
                             if "⚠" in l or "WARNING" in l.upper()]

            results[script] = {
                "exists":      True,
                "line_count":  len(lines),
                "errors":      len(error_lines),
                "warnings":    len(warning_lines),
                "error_lines": error_lines[-3:],  # last 3 errors only
            }
        except Exception as e:
            results[script] = {
                "exists": True,
                "errors": 0,
                "error_lines": [f"Could not read log: {e}"],
            }

    return results


# =============================================================================
# MARKET HOURS HELPER
# =============================================================================

def is_market_hours():
    """Check if market is currently open."""
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)
    if now.weekday() > 4:
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0)
    market_close = now.replace(hour=16, minute=0,  second=0)
    return market_open <= now < market_close


# =============================================================================
# DISPLAY
# =============================================================================

def print_status():
    """
    Run all checks and print a comprehensive status report.
    """
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now     = datetime.now(eastern)

    print(f"\n{'='*60}")
    print(f"  🔧 SYSTEM STATUS")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Market: {'OPEN' if is_market_hours() else 'CLOSED'}")
    print(f"{'='*60}")

    all_ok = True

    # ── Processes ─────────────────────────────────────────────────────────
    print(f"\n  PROCESSES")
    print(f"  {'─'*50}")

    processes = check_processes()
    for script, info in processes.items():
        if info["running"]:
            print(f"  ✅ {info['label']:<22} RUNNING  "
                  f"PID {info['pid']}  "
                  f"up {info['runtime']}  "
                  f"{info['memory_mb']}MB")
        else:
            print(f"  ❌ {info['label']:<22} NOT RUNNING")
            all_ok = False

    # ── APIs ──────────────────────────────────────────────────────────────
    print(f"\n  APIs")
    print(f"  {'─'*50}")

    public_api    = check_public_api()
    anthropic_api = check_anthropic_api()

    icon = "✅" if public_api["ok"] else "❌"
    print(f"  {icon} Public API             {public_api['message']}")
    if not public_api["ok"]:
        all_ok = False

    icon = "✅" if anthropic_api["ok"] else "❌"
    print(f"  {icon} Anthropic API          {anthropic_api['message']}")
    if not anthropic_api["ok"]:
        all_ok = False

    # ── Database ──────────────────────────────────────────────────────────
    print(f"\n  DATABASE")
    print(f"  {'─'*50}")

    db = check_database()
    if db["ok"]:
        print(f"  ✅ signals.db              {db['message']}  "
              f"{db['size_mb']}MB")
        print(f"     Signals: {db['signals']:,}  "
              f"Pending: {db['pending']:,}  "
              f"Open trades: {db['open_trades']}")
    else:
        print(f"  ❌ signals.db              {db['message']}")
        all_ok = False

    # ── Last scan ─────────────────────────────────────────────────────────
    print(f"\n  SCANNER ACTIVITY")
    print(f"  {'─'*50}")

    scan = check_last_scan()
    icon = "✅" if scan["ok"] else "⚠️ "
    print(f"  {icon} Last scan              {scan.get('age', scan['message'])}")
    if scan.get("stale"):
        print(f"     ⚠️  Scanner may be stuck — last scan over 45 min ago "
              f"during market hours")
        all_ok = False

    # ── Open positions ────────────────────────────────────────────────────
    print(f"\n  POSITIONS")
    print(f"  {'─'*50}")

    positions = check_open_positions()
    icon = "✅" if positions["ok"] else "❌"
    print(f"  {icon} Paper positions        {positions['message']}")

    if positions.get("alerts"):
        for alert in positions["alerts"]:
            print(f"     {alert}")

    # ── Log files ─────────────────────────────────────────────────────────
    print(f"\n  LOG FILES (today)")
    print(f"  {'─'*50}")

    logs = check_log_files()
    for script, info in logs.items():
        if not info["exists"]:
            print(f"  ⬜ {script:<12} No log file yet")
            continue

        if info["errors"] > 0:
            print(f"  ⚠️  {script:<12} "
                  f"{info['line_count']} lines  "
                  f"{info['errors']} error(s)  "
                  f"{info.get('warnings', 0)} warning(s)")
            for err in info["error_lines"]:
                print(f"       → {err[:70]}")
        else:
            print(f"  ✅ {script:<12} "
                  f"{info['line_count']} lines  "
                  f"No errors  "
                  f"{info.get('warnings', 0)} warning(s)")

    # ── Overall verdict ───────────────────────────────────────────────────
    print(f"\n{'='*60}")
    if all_ok:
        print(f"  ✅ ALL SYSTEMS OPERATIONAL")
    else:
        print(f"  ⚠️  ONE OR MORE ISSUES DETECTED — review above")
    print(f"{'='*60}\n")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    # Check if psutil is installed
    try:
        import psutil
    except ImportError:
        print("\n  psutil not installed. Run: pip install psutil")
        sys.exit(1)

    print_status()