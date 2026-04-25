import logging
import os
import sys
from datetime import datetime

def setup_logger(script_name):
    """
    Configure logging to write to both terminal and a dated log file.
    
    Creates a logs/ directory if it doesn't exist.
    Log files are named: {script_name}_{YYYYMMDD}.log
    Each run appends to the daily file rather than overwriting.
    
    Parameters:
        script_name (str): Short name for the log file
                           e.g. 'scanner', 'monitor', 'outcome'
    
    Returns:
        str: Path to the log file being written
    """

    # Create logs directory
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)

    # Daily log file name
    today        = datetime.now().strftime("%Y%m%d")
    log_filename = os.path.join(log_dir, f"{script_name}_{today}.log")

    # Get the root logger
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    # Clear any existing handlers — prevents duplicate output
    # if setup_logger is called more than once
    if logger.handlers:
        logger.handlers.clear()

    # File handler — appends to daily log file
    file_handler = logging.FileHandler(log_filename, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(message)s"))

    # Stream handler — mirrors to terminal exactly as before
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.DEBUG)
    stream_handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    # Redirect print() to logging so existing print statements
    # automatically go to both terminal and file without any changes
    # to the rest of the codebase
    class PrintToLogger:
        def __init__(self, level=logging.INFO):
            self.level = level
            self.buffer = ""

        def write(self, message):
            if message != "\n":
                # Buffer partial lines until newline received
                self.buffer += message
                if "\n" in self.buffer:
                    lines = self.buffer.split("\n")
                    for line in lines[:-1]:
                        if line:  # skip empty lines from double newlines
                            logging.log(self.level, line)
                    self.buffer = lines[-1]
            elif self.buffer:
                logging.log(self.level, self.buffer)
                self.buffer = ""

        def flush(self):
            if self.buffer:
                logging.log(self.level, self.buffer)
                self.buffer = ""

    sys.stdout = PrintToLogger()

    return log_filename


def get_todays_log_path(script_name):
    """
    Return the path to today's log file for a given script.
    Used by daily_summary.py to read log content.
    
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