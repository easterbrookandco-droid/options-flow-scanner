# OPTIONS FLOW SCANNER — CLAUDE CODE CONTEXT

This file is read automatically by Claude Code at the start of every session.
It provides essential context so Claude Code always knows the current state.

## FIRST ACTIONS EVERY SESSION

1. Read PROJECT_STATE.md for current state and last session summary
2. Check BACKLOG.md for prioritized work items
3. Confirm which files were recently changed via `git log --oneline -5`
4. Ask Nolan what he wants to work on today

## PROJECT OVERVIEW

Autonomous options flow scanner and paper trading system.
- Scans for unusual institutional options flow every 30 minutes
- Agent enters paper trades automatically based on signal quality
- Position monitor tracks and exits via two-stage trailing stop
- Running 24/7 on AWS EC2 VM (3.144.128.166)

**Goal:** Replace W2 income through systematic options trading.
**Current phase:** Paper trading data collection (week 3)
**Next milestone:** Raise MIN_COMPOSITE_SCORE to 6.0 if week 3 confirms pattern

## KEY FILES TO KNOW

```
PROJECT_STATE.md      — Current system state, session log, decisions
BACKLOG.md            — Prioritized enhancement backlog
fetch_trades.py       — Core scanner (~1335 lines)
journal.py            — All SQLite operations
scheduler.py          — 30-min polling loop
agent.py              — Autonomous trading agent
position_monitor.py   — Position tracking and exits (986 lines)
dashboard.py          — Flask dashboard (needs rebuild — signals only currently)
pnl_report.py         — On-demand P&L reporter
entry_analyzer.py     — Signal quality analysis
```

## DEVELOPER CONTEXT

**Nolan** is the developer. He is newer to development but learning fast.
- Explain the "why" before the "how"
- Go at an educational pace
- Connect new concepts to the actual scanner or a recent trade
- He prefers step-by-step with reasoning explained at each stage

## ENVIRONMENT

- **Local:** Windows, VS Code, Python 3.12, venv at C:\Users\neast\options-flow-scanner\
- **VM:** Ubuntu 26.04, AWS EC2, same repo cloned at ~/options-flow-scanner
- **DB:** SQLite signals.db (migrated to VM, also on local machine)
- **APIs:** Public.com (options data), Anthropic (thesis generation)
- **Key in .env:** PUBLIC_SECRET_KEY, ANTHROPIC_API_KEY

## GIT WORKFLOW

```bash
# After making changes locally:
git add <files>
git commit -m "description"
git push

# If push rejected (GitHub has newer commit):
git pull origin master
git push

# On VM after pushing from local:
git pull
sudo systemctl restart scanner-<service>

# End of session — always run:
python session_log.py
```

## VM COMMANDS

```bash
# Connect
ssh -i C:\Users\neast\.ssh\scanner-key.pem ubuntu@3.144.128.166

# Dashboard tunnel (run in separate terminal)
ssh -i C:\Users\neast\.ssh\scanner-key.pem -L 5000:localhost:5000 -N ubuntu@3.144.128.166

# Service management (aliases set up on VM)
scanner-status        # check all 4 services
scanner-logs          # follow all logs
scanner-restart       # restart all services
scanner-scheduler-log # follow scheduler only
scanner-monitor-log   # follow monitor only
scanner-agent-log     # follow agent only
```

## PUBLIC API QUIRKS (important)

- Two-step auth: POST to get accessToken → use as Bearer token
- Must select accountType == "BROKERAGE" from accounts array (not first result)
- Expirations endpoint returns "expirations" key (docs say "expirationDates" — wrong)
- Greeks: GET with osiSymbols as repeated query params, data under "greeks" key
- previousClose returns null from API — use market_closes table instead
- Free tier = 403 on everything; paid plan required

## END OF SESSION

Always run `python session_log.py` before ending any Claude Code session.
This updates PROJECT_STATE.md and keeps claude.ai sessions in sync.
