Options Flow Scanner
A Python-based options flow detection system that monitors unusual institutional activity across a watchlist of high-liquidity tickers. Built on the Public.com API, it scans options chains every 30 minutes during market hours, scores signals by conviction level, logs them to a SQLite database, and surfaces them through a live Flask dashboard.

Goal: Detect smart money positioning before the move, paper trade signals to validate the system, then scale into real micro positions ($200–500 range).


Architecture
fetch_trades.py       — Core scanner: auth, chain fetch, scoring, signal logging

journal.py            — SQLite interface: signals + paper_trades tables in signals.db

scheduler.py          — Market-hours-aware loop, runs fetch_trades every 30 min

paper_trade.py        — Paper trading CLI: enter, exit, list, summary commands

dashboard.py          — Flask dashboard at localhost:5000

evaluate.py           — On-demand contract evaluator: python evaluate.py AAPL260516C00200000

daily_summary.py      — End-of-day signal review

thesis_generator.py   — AI-powered trade thesis generator

position_monitor.py   — Live position monitor with auto-close logic

analytics.py          — Performance analytics across closed trades

db_utility.py         — One-off database fixes and maintenance

backup_db.py          — Database backup utility

status.py             — Script status checker


Signal Tiers
Signals are classified into three tiers using a composite score combining Vol/OI ratio and premium dollar value:

Tier
Criteria
HIGH
Composite score ≥ 6 AND premium ≥ $1M (0DTE thresholds)
INST
Premium ≥ $5M regardless of score
WATCH
Score ≥ 3 AND premium ≥ $100K


Thresholds are DTE-adjusted — longer-dated options naturally accumulate more OI, so criteria loosen as DTE increases to avoid filtering out genuine conviction flow on weekly/monthly contracts.
Scoring Model
Vol/OI ratio score: min(ratio / 10, 5) — capped at 5 points
Premium score: 0–4 points based on dollar value tiers ($100K / $500K / $1M / $5M breakpoints)
Composite: sum of both, max 9 points


Watchlist (19 tickers)
AAPL, NVDA, MSFT, AMZN, META, GOOGL, TSLA   — Mega-cap tech

SPY, QQQ, IWM                                 — Broad market ETFs

JPM, GS, BAC                                  — Financials

AMD, NFLX, CRM, UBER                          — High-conviction names

XLF, XLE                                      — Sector ETFs

Scans 4 nearest expirations per ticker each run.


Trade Assessment Framework
Every HIGH/INST signal runs through evaluate_trade_quality() in fetch_trades.py:

Deep ITM filter — skips contracts with |delta| ≥ 0.95 (behaves like stock, no leverage)
Adjusted contract filter — skips post-split artifacts (delta ~1.0 AND IV = 0%)
DTE check — 0DTE auto-skip; 5–14 days preferred window
Score check — flags marginal vs. strong signals
Premium check — flags institutional vs. moderate size
IV vs. baseline — compares to per-ticker historical IV ranges
Directional lean — signal type must align with ticker flow AND market bias (SPY/QQQ)

Verdict outputs: QUALIFIED / REVIEW / CAUTION / SKIP


Paper Trading
# Enter a trade

python paper_trade.py enter SPY260516P00560000

# Exit a trade

python paper_trade.py exit <trade_id> <exit_price> <reason>

# reason options: TARGET | STOP | MANUAL | EXPIRED

# List open positions

python paper_trade.py list

# Summary of all trades

python paper_trade.py summary

Paper trade records live in the paper_trades table in signals.db. Entry/exit prices should use the option mid-price (bid + ask / 2) for realistic paper fills. At expiration, use intrinsic value: for puts = max(strike - stock_price, 0).


Running the System
# Start the scheduler (runs scanner every 30 min during market hours)

python scheduler.py

# or use the batch file:

run_scheduler.bat

# Start the dashboard

python dashboard.py

# → http://localhost:5000

# Run a single on-demand scan

python fetch_trades.py

# Evaluate a specific contract

python evaluate.py AAPL260516C00200000

# Run daily summary

python daily_summary.py

# Monitor open positions

python position_monitor.py

# or:

run_monitor.bat


API Setup
Requires a paid Public.com account with LEVEL_2 options access.

Brokerage account: 5LT39200

Auth flow: POST /userapiauthservice/personal/access-tokens → accessToken (Bearer)

Store your key in a .env file (never committed):

PUBLIC_SECRET_KEY=your_key_here
Known Public.com API Quirks
Two-step auth: POST for token, then Bearer header on all subsequent calls
Must select accountType == "BROKERAGE" from accounts array — don't assume first result
Expirations endpoint returns "expirations" key (not "expirationDates" as documented)
Greeks endpoint: GET with osiSymbols as repeated query params; data nested under "greeks" key
Free tier returns 403 on all endpoints — paid plan required


Database
Single SQLite file: signals.db

Tables:

signals — all logged flow signals with score, tier, Greeks, outcome
paper_trades — paper trade entries/exits with full P&L tracking
scan_log — scan run history

# Open database directly

sqlite3 signals.db

# Useful queries

SELECT * FROM paper_trades WHERE status = 'OPEN';

SELECT ticker, signal_tier, COUNT(*) FROM signals GROUP BY ticker, signal_tier;


Environment
Python 3.12.1
Windows local dev (VS Code)
Virtual environment with dependencies in requirements.txt
Logs written to /logs/ directory

pip install -r requirements.txt

