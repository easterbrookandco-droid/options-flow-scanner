# OPTIONS FLOW SCANNER — PROJECT STATE
*Auto-updated after every session. Source of truth for all tools.*
*Last updated: 2026-05-29*

---

## 🔴 SYSTEM STATUS

| Service | Status | Location |
|---------|--------|----------|
| Scheduler | RUNNING | AWS EC2 VM |
| Agent | RUNNING | AWS EC2 VM |
| Position Monitor | RUNNING | AWS EC2 VM |
| Dashboard | RUNNING | AWS EC2 VM (port 5000) |
| VM | RUNNING | 3.144.128.166 (Ubuntu 26.04, t3.micro) |

**Last confirmed running:** 2026-05-23 evening

---

## 📍 WHERE WE ARE (plain English)

The options flow scanner is fully autonomous and running 24/7 on AWS. 
The system scans for unusual options flow every 30 minutes during market 
hours, enters paper trades automatically via the agent, tracks positions 
via the monitor, and exits via a two-stage trailing stop.

We are in week 3 of paper trading data collection. The system has processed 
290+ closed trades with +$31,808 realized P&L. The trailing stop was just 
deployed (May 23) and will generate its first live data on Tuesday May 26 
(Monday is Memorial Day — market closed).

The next major decision point is week 3-4: if score ≥ 6.0 holds as the 
best performing bucket for a third week, we raise MIN_COMPOSITE_SCORE to 
6.0 in the agent. That's the first live model change.

---

## 🔬 ACTIVE EXPERIMENTS

| Experiment | Status | Decision Point |
|-----------|--------|----------------|
| Score ≥ 6.0 threshold | Week 3 of 3 — confirm Tuesday | Week 4 if pattern holds |
| Trailing stop (1% hurdle + 20% trail) | Deployed May 23, first live data Tue | Review after 2 weeks |
| OI confirmation filter | Not yet built | Next session |
| Market bias data (previous close) | Deployed May 23, first real data Tue | Verify Tuesday |

---

## 🏗️ SYSTEM ARCHITECTURE

### Files (all on GitHub + VM + local Windows)
```
fetch_trades.py       — Core scanner, auth, chain fetch, scoring (~1335 lines)
journal.py            — All SQLite operations
scheduler.py          — 30-min polling loop, saves market closes at 4pm
agent.py              — Autonomous paper trading agent, 5-min loop
position_monitor.py   — Position tracking, trailing stop, exits (986 lines)
dashboard.py          — Flask at localhost:5000 (signals only — needs rebuild)
evaluate.py           — On-demand contract evaluator
pnl_report.py         — On-demand P&L reporter with open pro-forma
entry_analyzer.py     — Signal quality analysis by score/DTE/premium/ticker
export_db.py          — Exports DB to Excel for validation
export_analysis_dataset.py — Flat snapshot export for hurdle analysis
backfill_stop_exits.py — One-time backfill for STOP_TRIGGERED positions
optimize_full_dataset.py — Trailing stop optimization analysis
```

### Database: signals.db (SQLite)
```
signals              — Flow signals logged by scanner
paper_trades         — Paper trade entries and exits
position_snapshots   — Price snapshots every 2 minutes per position
market_closes        — End-of-day close prices (SPY/QQQ/IWM/TLT/GLD/USO)
```

### Infrastructure
- **VM:** AWS EC2 t3.micro, Ubuntu 26.04, 20GB EBS
- **SSH:** ssh -i C:\Users\neast\.ssh\scanner-key.pem ubuntu@3.144.128.166
- **Dashboard tunnel:** ssh -i C:\Users\neast\.ssh\scanner-key.pem -L 5000:localhost:5000 -N ubuntu@3.144.128.166
- **Services:** systemd (scanner-scheduler, scanner-monitor, scanner-agent, scanner-dashboard)
- **VM aliases:** scanner-status, scanner-logs, scanner-restart, scanner-scheduler-log, scanner-monitor-log, scanner-agent-log
- **Git on VM:** credentials cached via credential.helper store

### GitHub
- **Repo:** https://github.com/easterbrookandco-droid/options-flow-scanner (public)
- **Raw URLs:** https://cdn.jsdelivr.net/gh/easterbrookandco-droid/options-flow-scanner@master/GitHub_file_URLs.txt
- **GitHub Action:** Auto-updates GitHub_file_URLs.txt on every push

---

## ⚙️ CURRENT CONFIGURATION

### Agent (agent.py)
```python
CHECK_INTERVAL_SECONDS = 300    # 5 min
STALENESS_THRESHOLD    = 0.25   # 25%
MIN_SCORE_GAP          = 0.0    # DISABLED for data collection
MAX_ASK_PER_CONTRACT   = 100.00
# Ticker dedup DISABLED (commented out) — re-enable for live trading
# No 0DTE entries
```

### Position Monitor (position_monitor.py)
```python
CHECK_INTERVAL_SECONDS = 120    # 2 min (changed from 3 min May 23)
HURDLE_PCT             = 0.01   # 1% gain before trailing stop activates
TRAILING_STOP_PCT      = 0.20   # 20% drop from post-hurdle peak
# ITM safety exit: DTE=0, profitable, after 3:45pm ET
# STOP_TRIGGERED: records exit, keeps tracking to expiration for data
```

### Signal Tiers
```
HIGH:  composite_score ≥ 6 AND premium ≥ $1M
INST:  premium ≥ $5M regardless of score
WATCH: score ≥ 3 AND premium ≥ $100K
```

### Scoring
```
composite_score = Vol/OI ratio score (cap 5pts) + premium tier score (0-4pts)
DTE-adjusted thresholds in analyze_and_display()
```

### Watchlist (19 tickers)
```
AAPL, NVDA, MSFT, AMZN, META, GOOGL, TSLA, SPY, QQQ, IWM,
JPM, GS, BAC, AMD, NFLX, CRM, UBER, XLF, XLE
EXPIRATIONS_TO_SCAN = 4  (nearest 4 per ticker)
```

---

## 📊 CURRENT PERFORMANCE (as of 2026-05-23)

| Metric | Value |
|--------|-------|
| Total closed trades | 290 |
| Win rate | 54.5% (158W/132L) |
| Realized P&L | +$31,808 |
| Mark-to-market | +$21,241 |
| Starting bankroll | $10,000 |
| Current value (MTM) | +$31,241 (+212%) |
| Best ticker | AMD +$51,682 (77% win rate) |
| Worst ticker | SPY -$20,325 |
| Avg hold time | 2.5 days |

### Key findings from entry_analyzer.py (weeks 1-2)
- Score 6.0-6.9 = best bucket (86-91% win rate both weeks) — CONSISTENT
- Score 8.0+ = worst performer (50-60%) — counterintuitive, likely late-stage flow
- Score threshold simulation peaks at 6.0 — plan to set MIN_COMPOSITE_SCORE=6.0 in week 4
- $5M+ premium tier = underperforming (46% win rate week 2)
- 3-5 DTE = weakest DTE bucket (45-48% win rate)
- Market bias all showing NEUTRAL (previousClose API bug — fixed May 23, real data Tuesday)

---

## 🔑 KEY TECHNICAL DECISIONS & LEARNINGS

### Public.com API Quirks
- Two-step auth: POST → accessToken → Bearer token
- Must select accountType == "BROKERAGE" (account 5LT39200, LEVEL_2 options)
- Expirations endpoint returns "expirations" key (docs say "expirationDates" — wrong)
- Greeks: GET with osiSymbols as repeated params, data under "greeks" key
- previousClose returns null — fixed by storing closes ourselves in market_closes table
- Free tier = 403 on everything; paid plan required

### Flask/Jinja2
- Template literal {{ }} conflicts with JavaScript
- Always wrap JS blocks in {% raw %}...{% endraw %}

### Git Workflow
- Always commit from whichever machine made the change
- If push rejected: git pull origin master first
- VM uses PAT stored via credential.helper store
- GitHub Action auto-updates GitHub_file_URLs.txt on every push

### Trailing Stop Analysis (empirical, May 23)
- 95 positions expired at loss but were profitable at some point
- $21,665 available P&L never captured by current system
- 84/95 peaked BEFORE 0DTE — time-based exits largely wrong approach
- 75% of cliff dives happen within 5 min — slippage unavoidable but acceptable
- Optimal: 1% hurdle + 20% trailing stop → +$54,854 improvement backtested
- False exit rate: 67/82 saves = 82% false exits BUT still net positive vs holding

### Model Improvement Philosophy
- One change at a time — can't diagnose what worked otherwise
- Don't filter market context yet — need cross-condition data first  
- Concentration in paper trading is a feature not a bug
- Losses are data — let them accumulate, analyze properly
- Score ≥ 6.0 confirmed as sweet spot across 2 weeks — week 3 is confirmation

---

## 📋 NEXT SESSION AGENDA

1. Decide whether the monitor should read the stored per-trade hurdle_price vs recomputing from live HURDLE_PCT
2. Review trailing-stop behavior Monday once the monitor resumes and trails live price peaks
3. Tune trailing_stop_pct / hurdle tiers if needed based on the new validation columns

---


## 📝 SESSION LOG (Last 3 Sessions)

### 2026-05-29 | Claude Code | 16:47 EDT
- Fixed duplicate contract rows by joining on signal_id instead of contract strings
- Switched trailing stops from gain-based to price-based to prevent premature exits
- Added Strike, Cost, Mode columns to Portfolio tables per specification
- Centralized strategy parameters into new strategy_config.py module for consistency
- Added database fields for hurdle_price and trailing stop tracking
*→ Full details in SESSION_HISTORY.md*

---

### 2026-05-28 | Claude Code | 23:48 EDT
- Expanded Today's Entries to 10 columns with Strike, Score, Premium
- Rebuilt Positions tab with 18-column table and 4-row summary header
- Added Cost and ROI columns to Position Summary analytics
- Implemented reversed Post-Exit coloring: red for money left behind
- Updated all summary stats to include STOP_TRIGGERED trades
*→ Full details in SESSION_HISTORY.md*

---

### 2026-05-28 | claude.ai | 11:19 EDT
- Fixed scheduler.py NameError and 0h0m boundary stall with 60-second minimum
- Added market close condition to roll-forward logic for consistent boundaries
- Deployed fixes to VM and successfully restarted all four services
- Kept 6.0 score threshold unchanged as selectivity remains design objective
- Learned tmux displays can freeze while services run normally
*→ Full details in SESSION_HISTORY.md*

---

### 2026-05-27 | claude.ai + Claude Code | 16:48 EDT
- Fixed Python buffering on systemd services, resolving silent agent operation
- Built optimizing_agent.py with score 6.0-7.9, premium $100K-$2M, DTE 1-2d/6-14d filters
- Added mode column to paper_trades, deployed scanner-optimizing-agent systemd service
- Established three-tier architecture: CONTROL data collection, OPTIMIZING refined criteria, LIVE capital
- Set live trading model: 10% max deployment, 80% reinvestable profits
*→ Full details in SESSION_HISTORY.md*

---

*Older sessions in SESSION_HISTORY.md*
