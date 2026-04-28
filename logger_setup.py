import os
import sys
from datetime import datetime


class _Tee:
    """
    Writes to both the original stdout and a log file simultaneously.
    Avoids routing through the logging module entirely, which prevents
    the recursion error that occurs when logging tries to write to
    a redirected stdout.
    """

    def __init__(self, original_stdout, log_file):
        self.original_stdout = original_stdout
        self.log_file        = log_file

    def write(self, message):
        self.original_stdout.write(message)
        self.original_stdout.flush()
        try:
            self.log_file.write(message)
            self.log_file.flush()
        except Exception:
            pass  # Never let logging errors crash the main script

    def flush(self):
        self.original_stdout.flush()
        try:
            self.log_file.flush()
        except Exception:
            pass

    def isatty(self):
        return False


# Keep reference to log file so it can be closed on exit
_active_log_file = None


def setup_logger(script_name):
    """
    Configure output to write to both terminal and a dated log file.

    Redirects sys.stdout to a Tee object that writes to both the
    original terminal and a log file simultaneously. Uses no logging
    module involvement to avoid recursion issues.

    Creates a logs/ directory if it doesn't exist.
    Log files are named: {script_name}_{YYYYMMDD}.log
    Each run appends to the daily file.

    Parameters:
        script_name (str): Short name e.g. 'scanner', 'monitor', 'outcome'

    Returns:
        str: Path to the log file being written
    """
    global _active_log_file

    log_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "logs"
    )
    os.makedirs(log_dir, exist_ok=True)

    today        = datetime.now().strftime("%Y%m%d")
    log_filename = os.path.join(log_dir, f"{script_name}_{today}.log")

    # Always write to the real original stdout, not a previously
    # redirected version — prevents chained Tee wrapping on restart
    original_stdout = getattr(sys.stdout, "original_stdout", sys.stdout)

    # Close any previously open log file
    if _active_log_file is not None:
        try:
            _active_log_file.close()
        except Exception:
            pass

    log_file         = open(log_filename, "a", encoding="utf-8")
    _active_log_file = log_file

    sys.stdout = _Tee(original_stdout, log_file)

    # Write a session start marker to the log
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stdout.write(f"\n[Session started: {now}]\n")

    return log_filename


def get_todays_log_path(script_name):
    """
    Return the path to today's log file for a given script.

    Parameters:
        script_name (str): 'scanner', 'monitor', or 'outcome'

    Returns:
        str: Full path to log file, or None if it doesn't exist
    """
    log_dir      = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "logs"
    )
    today        = datetime.now().strftime("%Y%m%d")
    log_filename = os.path.join(log_dir, f"{script_name}_{today}.log")
    return log_filename if os.path.exists(log_filename) else None


def get_log_path_for_date(script_name, date_str):
    """
    Return the path to a log file for a specific date.

    Parameters:
        script_name (str): 'scanner', 'monitor', or 'outcome'
        date_str (str): Date in YYYY-MM-DD format

    Returns:
        str: Full path to log file, or None if it doesn't exist
    """
    log_dir      = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "logs"
    )
    date_compact = date_str.replace("-", "")
    log_filename = os.path.join(log_dir, f"{script_name}_{date_compact}.log")
    return log_filename if os.path.exists(log_filename) else None