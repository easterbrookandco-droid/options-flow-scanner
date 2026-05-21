import time
import subprocess
import sys
from datetime import datetime, timedelta
import pytz
from fetch_trades import get_access_token, get_account_id, get_market_overview


# =============================================================================
# CONFIGURATION
# =============================================================================

SCAN_INTERVAL_MINUTES = 30      # How often to scan during market hours
MARKET_OPEN_HOUR = 9            # 9:30 AM Eastern
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 16          # 4:00 PM Eastern
MARKET_CLOSE_MINUTE = 0
MARKET_TIMEZONE = "US/Eastern"

# Path to the scanner script
# Since scheduler.py lives in the same folder, this works as-is
SCANNER_SCRIPT = "fetch_trades.py"

# Track whether we've saved today's market close prices
_close_saved_date = None



# =============================================================================
# MARKET HOURS HELPERS
# =============================================================================

def is_market_open():
    """
    Check if the US stock market is currently open.
    
    Covers Monday-Friday, 9:30 AM - 4:00 PM Eastern.
    Does not account for market holidays — sufficient for POC.
    
    Returns:
        bool: True if market is open right now
    """
    
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now = datetime.now(eastern)
    
    # Weekend check (Monday=0, Sunday=6)
    if now.weekday() > 4:
        return False
    
    market_open = now.replace(
        hour=MARKET_OPEN_HOUR,
        minute=MARKET_OPEN_MINUTE,
        second=0,
        microsecond=0
    )
    market_close = now.replace(
        hour=MARKET_CLOSE_HOUR,
        minute=MARKET_CLOSE_MINUTE,
        second=0,
        microsecond=0
    )
    
    return market_open <= now < market_close


def seconds_until_market_open():
    """
    Calculate how many seconds until the next market open.
    Accounts for weekends by skipping to Monday if needed.
    
    Returns:
        int: Seconds until next market open
    """
    
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now = datetime.now(eastern)
    
    # Start with today's open time
    next_open = now.replace(
        hour=MARKET_OPEN_HOUR,
        minute=MARKET_OPEN_MINUTE,
        second=0,
        microsecond=0
    )
    
    # If we're already past today's open, move to tomorrow
    if now >= next_open:
        next_open = next_open + timedelta(days=1)
    
    # Skip Saturday and Sunday
    while next_open.weekday() > 4:
        next_open = next_open + timedelta(days=1)
    
    return max(0, int((next_open - now).total_seconds()))


# =============================================================================
# SCANNER RUNNER
# =============================================================================

def run_scanner():
    """
    Execute fetch_trades.py as a subprocess.
    
    Using subprocess means the scanner runs in its own process —
    clean, isolated, and identical to running it manually.
    Any crash in the scanner won't take down the scheduler.
    
    Returns:
        bool: True if scan completed successfully, False if it errored
    """
    
    print(f"  Launching scanner...")
    
    try:
        # sys.executable uses the same Python interpreter
        # that's running this script — ensures venv is respected
        result = subprocess.run(
            [sys.executable, SCANNER_SCRIPT],
            capture_output=False,   # Let output print to terminal in real time
            text=True
        )
        
        if result.returncode == 0:
            return True
        else:
            print(f"  ✗ Scanner exited with error code {result.returncode}")
            return False
    
    except FileNotFoundError:
        print(f"  ✗ Could not find {SCANNER_SCRIPT} — check file path")
        return False
    
    except Exception as e:
        print(f"  ✗ Unexpected error running scanner: {e}")
        return False


# =============================================================================
# MAIN POLLING LOOP
# =============================================================================

def main():
    from logger_setup import setup_logger
    log_path = setup_logger("scanner")
    """
    Main scheduler loop.
    
    Behavior:
    - If market is open: run scanner immediately, then every SCAN_INTERVAL_MINUTES
    - If market is closed: display wait time, sleep until next open
    - Ctrl+C at any time exits cleanly with a summary
    
    Usage:
        python scheduler.py
    """
    
    eastern = pytz.timezone(MARKET_TIMEZONE)
    scan_count = 0
    error_count = 0
    start_time = datetime.now(eastern)
    
    print("\n" + "="*70)
    print("  🔄 OPTIONS FLOW SCANNER — SCHEDULER")
    print(f"  Started: {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Scan interval: every {SCAN_INTERVAL_MINUTES} minutes during market hours")
    print(f"  Market hours: {MARKET_OPEN_HOUR}:{MARKET_OPEN_MINUTE:02d} — "
          f"{MARKET_CLOSE_HOUR}:{MARKET_CLOSE_MINUTE:02d} Eastern")
    print(f"  Scanner: {SCANNER_SCRIPT}")
    print(f"  Press Ctrl+C to stop")
    print("="*70)
    
    try:
        while True:
            
            now = datetime.now(eastern)
            
            if is_market_open():
                
                scan_count += 1
                print(f"\n{'─'*70}")
                print(f"  📡 SCAN #{scan_count}")
                print(f"  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                print(f"{'─'*70}")
                
                success = run_scanner()
                
                if not success:
                    error_count += 1
                    print(f"  ⚠ Scan #{scan_count} encountered an error "
                          f"({error_count} total errors)")
                    
                    # If we get 3 consecutive errors something is seriously wrong
                    # Better to stop and let the user investigate
                    if error_count >= 3:
                        print(f"\n  ✗ 3 consecutive errors — stopping scheduler.")
                        print(f"  Check fetch_trades.py and your API connection.")
                        break
                else:
                    error_count = 0  # Reset error count on success
                
                # Check if next scan would be after market close
                now = datetime.now(eastern)
                next_scan_time = now + timedelta(minutes=SCAN_INTERVAL_MINUTES)
                market_close = now.replace(
                    hour=MARKET_CLOSE_HOUR,
                    minute=MARKET_CLOSE_MINUTE,
                    second=0,
                    microsecond=0
                )
                
                if next_scan_time >= market_close:
                    print(f"\n  Market closing soon — final scan complete for today.")
                    
                    # End of day review — surface expiring signals for manual outcome recording
                    print(f"\n{'='*70}")
                    print(f"  📋 END OF DAY REVIEW")
                    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                    print(f"{'='*70}")

                    # Fire a post-close scan at 4:01 PM for final settlement snapshot
                    now = datetime.now(eastern)
                    eod_close_time = now.replace(hour=16, minute=1, second=0, microsecond=0)
                    secs_to_close = int((eod_close_time - now).total_seconds())
                    if secs_to_close > 0:
                        print(f"\n  Waiting until 4:01 PM for post-close settlement scan...")
                        time.sleep(secs_to_close)
                        scan_count += 1
                        print(f"\n  📡 SCAN #{scan_count} — POST-CLOSE SETTLEMENT")
                        run_scanner()

                    # Record outcomes for today's expiring contracts
                    print(f"\n  Recording outcomes for expiring contracts...")
                    try:
                        from outcome_recorder import main as record_outcomes
                        record_outcomes()
                    except Exception as e:
                        print(f"  ✗ Outcome recording failed: {e}")

                    # Generate daily summary
                    print(f"\n  Generating daily summary...")
                    try:
                        from daily_summary import run_summary
                        run_summary()
                    except Exception as e:
                        print(f"  ✗ Daily summary failed: {e}")

                    # Import and run the expiring signals review
                    sys.path.insert(0, ".")
                    try:
                        from review import review_expiring_today
                        review_expiring_today()
                        print(f"\n  Run 'python review.py record' to log outcomes.")
                    except Exception as e:
                        print(f"  ✗ Could not run end of day review: {e}")
                    
                    # Sleep until next market open
                    secs = seconds_until_market_open()
                    hours = secs // 3600
                    minutes = (secs % 3600) // 60
                    next_open = (now + timedelta(seconds=secs)).strftime('%Y-%m-%d %H:%M %Z')
                    
                    print(f"\n  Next market open: {next_open} ({hours}h {minutes}m)")
                    print(f"  Sleeping until then...")
                    time.sleep(secs)
                    continue  # Loop back to top — recheck is_market_open() fresh
                
                else:
                    # Sleep until next scan
                    sleep_secs = SCAN_INTERVAL_MINUTES * 60
                    next_str = next_scan_time.strftime('%H:%M:%S %Z')
                    print(f"\n  ⏳ Next scan at {next_str}")
                    time.sleep(sleep_secs)
            
            else:
                # ── End of day close price save ────────────────────────
                # Fire once per day right after market closes
                today_str = now.strftime("%Y-%m-%d")

                if _close_saved_date != today_str:
                    try:
                        print(f"\n  📈 Saving end-of-day market close prices...")
                        token      = get_access_token()
                        account_id = get_account_id(token) if token else None

                        if token and account_id:
                            overview = get_market_overview(token, account_id)
                            closes   = {
                                ticker: data.get("price")
                                for ticker, data in overview.items()
                                if data.get("price")
                            }
                            if closes:
                                from journal import save_market_close
                                save_market_close(closes)
                                _close_saved_date = today_str
                                print(f"  ✅ Saved closes for: {list(closes.keys())}")
                            else:
                                print(f"  ⚠️  No close prices available")
                        else:
                            print(f"  ✗ Auth failed — close prices not saved")
                    except Exception as e:
                        print(f"  ✗ Close price save error: {e}")

                # Market closed — wait until next open
                secs = seconds_until_market_open()
                hours = secs // 3600
                minutes_remaining = (secs % 3600) // 60
                next_open = (now + timedelta(seconds=secs)).strftime('%Y-%m-%d %H:%M %Z')

                print(f"\n  Market is currently closed.")
                print(f"  Next open: {next_open} ({hours}h {minutes_remaining}m from now)")
                print(f"  Sleeping until market open...")

                sleep_chunk = min(secs, 3600)
                time.sleep(sleep_chunk)
                continue  # Loop back to top — recheck is_market_open() fresh
    
    except KeyboardInterrupt:
        now = datetime.now(eastern)
        runtime = now - start_time
        hours = int(runtime.total_seconds() // 3600)
        minutes = int((runtime.total_seconds() % 3600) // 60)
        
        print(f"\n\n{'='*70}")
        print(f"  Scheduler stopped by user")
        print(f"  Runtime:     {hours}h {minutes}m")
        print(f"  Total scans: {scan_count}")
        print(f"  Errors:      {error_count}")
        print(f"  Journal contains your complete signal history.")
        print(f"{'='*70}\n")


if __name__ == "__main__":
    main()