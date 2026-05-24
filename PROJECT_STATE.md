# OPTIONS FLOW SCANNER — PROJECT STATE
*Auto-updated after every session. Source of truth for all tools.*
*Last updated: 2026-05-24*

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

1. 1. Tuesday morning - verify scheduler woke up at 9:30 ET
2. 2. Review first trailing stop fires in live data
3. 3. Run entry_analyzer for week 3 data
4. 4. OI confirmation filter - build and backtest
5. 5. Auto git pull cron on VM
6. 6. DB backup to S3
7. 7. Mobile responsive dashboard (low priority)

---

## 📝 SESSION LOG

### 2026-05-24 | claude.ai + Claude Code | 15:15 EDT
**What changed:**
- Set up knowledge infrastructure with PROJECT_STATE.md, session_log.py, and CLAUDE.md
- Implemented GitHub Action for auto raw URL generation, switched to jsDelivr CDN
- Rebuilt dashboard with 4-tab layout: Portfolio, Positions, Signals, Analytics
- Added flask-httpauth basic authentication for secure access
- Opened port 5000 on AWS for public access from any device
- Added Key Insights section to Analytics tab
- Fixed signal history chart blur issue with canvas DPI adjustments
- Updated requirements.txt with new dependencies

**Key decisions:**
- Chose jsDelivr CDN over direct GitHub raw URLs for better reliability
- Implemented basic auth instead of more complex authentication for simplicity
- Opened public port 5000 to enable multi-device access during development
- Structured dashboard with clear tab separation for better UX organization

**What we learned / what didn't work:**
- Claude Code integration works effectively for this project structure
- Canvas DPI fixes resolved chart rendering blur issues
- Basic authentication provides sufficient security for development phase
- Mobile responsiveness needs attention but wasn't prioritized this session

**Open questions:**
- Should implement more robust authentication before production deployment?
- How to prioritize mobile responsiveness improvements in next sessions?
- Need to monitor CDN performance and reliability over time
- Consider whether current tab structure serves all user workflows effectively

---

### 2026-05-24 | claude.ai + Claude Code | 12:25 EDT
**What changed:**
- Set up project infrastructure: PROJECT_STATE.md, session_log.py, CLAUDE.md for knowledge management
- Implemented GitHub Action for auto raw URL generation, switched to jsDelivr CDN
- Rebuilt dashboard with 4-tab layout: Portfolio, Positions, Signals, Analytics
- Added flask-httpauth basic authentication to secure dashboard access
- Opened port 5000 on AWS EC2 instance for public web access
- Fixed signal history chart blur issue with canvas DPI settings
- Added Key Insights section to Analytics tab for data summaries
- Made VM publicly accessible at 3.144.128.166:5000

**Key decisions:**
- Used jsDelivr CDN over raw GitHub URLs for better reliability and caching
- Chose flask-httpauth for simple basic authentication implementation
- Opened public port 5000 to enable multi-device access to dashboard
- Structured dashboard into logical tabs for better user experience organization

**What we learned / what didn't work:**
- GitHub raw URLs can be unreliable, CDN approach provides better stability
- Canvas DPI fixes resolved chart rendering blur issues in web browsers
- Basic auth provides adequate security layer for development/testing phase
- Public port access enables testing across different devices and networks

**Open questions:**
- Monitor CDN performance and reliability compared to direct GitHub access
- Evaluate if basic auth security level is sufficient for production use
- Track dashboard performance with public access and multiple concurrent users
- Assess if 4-tab layout meets all user workflow requirements

---

### 2026-05-24 | claude.ai | Morning
**Focus:** Knowledge infrastructure, GitHub Action, Claude Code planning

**What changed:**
- GitHub Action created (.github/workflows/update-raw-urls.yml)
- Auto-generates GitHub_file_URLs.txt with raw URLs on every push
- PROJECT_STATE.md created (this file)
- session_log.py designed (not yet built)
- Cross-tool knowledge strategy defined

**Key decisions:**
- PROJECT_STATE.md as single source of truth across all tools
- session_log.py uses Claude API to auto-summarize — no manual writing required
- Claude Code .claude/ config reads PROJECT_STATE.md at session start
- Defer neural net / vector DB approach — markdown is 80% of benefit at 20% cost

---

### 2026-05-23 | claude.ai | ~8 hours (major session)
**Focus:** Accounting fixes, VM migration, trailing stop implementation

**What changed:**
- Backfilled 134 STOP_TRIGGERED positions with real exit prices
- Added DB indexes — queries now fast
- Scheduler double-sleep bug fixed (global _close_saved_date)
- AWS EC2 VM fully configured — 4 systemd services running 24/7
- Database migrated to VM with full history
- Previous close prices saving at 4pm daily
- Two-stage trailing stop implemented (1% hurdle + 20% trail)
- ITM safety exit at 3:45pm on DTE=0 profitable positions
- Monitor polling reduced to 2 minutes
- Delta/IV now storing correctly on new agent entries
- export_db.py, pnl_report.py, entry_analyzer.py operational
- fix_bad_exits.py — corrected 57 positions incorrectly closed at $0.01

**Key decisions:**
- Trailing stop replaces both TARGET and DTE-based stop (backtested +172% improvement)
- 1% hurdle chosen empirically to filter noise peaks
- VM is now production system — local Windows machine is dev/backup
- GitHub Action setup for auto URL generation

**Open questions:**
- Will scheduler wake up cleanly Tuesday at 9:30?
- How will trailing stop perform on live data vs backtest?

---

### 2026-05-15 | claude.ai | ~6 hours
**Focus:** Week 1 analytics, auto take-profit, accounting cleanup

**What changed:**
- close_stuck_positions.py — closed 22 expired STOP_TRIGGERED positions
- DB indexes added to position_snapshots, paper_trades, signals
- Auto take-profit logic added to monitor (superseded by trailing stop May 23)
- Previous close fix designed (market_closes table)
- pnl_report.py built — on-demand P&L reporter
- entry_analyzer.py built — signal quality analysis
- Week 1 analysis run: score 6.0-6.9 = 90.9% win rate best bucket

**Key findings:**
- Score ≥ 6.0 produces 74% win rate, highest of any threshold
- Score 8.0+ = worst performer at 50%
- Calls vs puts market bias all NEUTRAL (API bug confirmed)
- 1-2 DTE + Score 7+ = 88.9% win rate best cross-section

---

### Prior sessions (summarized)
- Core scanner, agent, monitor, dashboard built iteratively
- Paper trading workflow established
- Signal tiers and scoring model implemented
- DTE-adjusted thresholds, deep ITM filter, adjusted contract filter
- GitHub repo public with README and briefing docs
- agent.py: autonomous entries, AI thesis + rule-based fallback
- position_monitor.py: dynamic DTE-aware stops, STOP_TRIGGERED tracking
