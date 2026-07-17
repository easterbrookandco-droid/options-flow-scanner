# OPTIONS FLOW SCANNER — ENHANCEMENT BACKLOG
*Reviewed and prioritized periodically. Last updated: 2026-05-08*

---

## HOW TO USE THIS DOCUMENT

Each item has a **priority tier**, **category**, and **effort estimate**.

**Priority tiers:**
- 🔴 HIGH — blocks data quality or live trading readiness
- 🟡 MEDIUM — meaningful improvement, not urgent
- 🟢 LOW — nice to have, do when time allows

**Effort estimates:**
- S — Small (< 1 session)
- M — Medium (1–2 sessions)
- L — Large (2+ sessions, may need to break into phases)

---
## DATA-DRIVEN MODEL IMPROVEMENT PLAN
*Established 2026-05-15 after week 1 analysis*

### The sequence (do not skip ahead):

**Week 2 (now) — Build analytics, run unchanged**
- Build entry quality analyzer query (score/premium/DTE/delta by outcome)
- Do NOT change agent entry logic
- Let data accumulate across different market conditions

**Week 3 — Second data review**
- Run pnl_report.py + entry quality analyzer
- Look for score/premium thresholds where win rate jumps
- Compare week 1 vs week 2 patterns — do they hold?

**Week 4 — First model change if data supports it**
- ONE change only — likely minimum score threshold increase
- Measure for two weeks before any second change

**Later — OI confirmation integration**
- Check overnight OI growth before agent enters next-morning signals
- Highest conviction improvement, needs clean baseline first

### Key principles to not violate:
- One change at a time — can't diagnose what worked otherwise
- Don't filter market context yet — need cross-condition data first
- Concentration in paper trading is a feature not a bug
- Losses are data — let them accumulate, analyze them properly
- TSLA call vs put performance by market context is the pattern to watch


## PHASE 2 SCANNER IMPROVEMENTS

### 🔴 Intraday score escalation detection
**Category:** Scanner / journal.py
**Effort:** M

Current duplicate check skips same-contract same-day signals entirely.
Need to detect intraday score escalation (e.g., Vol/OI jumping from 5x → 50x
mid-day) and update the existing DB record rather than skip it.

This ensures volume explosions mid-session aren't missed.

**Approach:** In `check_duplicate()`, if contract already logged today, compare
current score against stored score. If escalation ≥ threshold, UPDATE the
existing record rather than INSERT a new one or skip.

---

### 🔴 OI opening vs closing detection
**Category:** Scanner / evaluator
**Effort:** M

Compare today's volume against overnight OI change to detect whether flow
was opening new positions or closing existing ones.

- High volume + OI jumps significantly next morning = conviction confirmed,
  position held overnight = strong signal
- High volume + OI barely moves next morning = likely day trade, weaker signal

**Approach:** Store previous-day OI at scan time. Next morning, fetch current OI
and compare. Add `flow_type` field to signals table: OPENING / CLOSING / UNKNOWN.
Surface this in evaluator output and use as a scoring modifier.

**Key insight:** Check OI morning after a signal fires before entering. If OI
grew, the bet is still on. If flat, consider skipping.

---

### 🟡 Scoring model refinement
**Category:** Scanner / fetch_trades.py
**Effort:** L

Current scoring has a ceiling effect — many signals cluster at 8.0, making
agent tiebreaking frequent. Need to spread scores more meaningfully.

**Observations so far:**
- Vol/OI ratio score capped at 5pts is too coarse
- Premium tier breakpoints may need adjustment
- Consider adding IV context, DTE, and delta as scoring inputs

**Approach:** After collecting 4–6 weeks of lifecycle data, analyze which signal
characteristics actually predicted winning trades. Rebuild scoring model
empirically rather than theoretically.

---

### 🟡 Ask ceiling for META/MSFT deep ITM contracts
**Category:** Agent / fetch_trades.py
**Effort:** S

META and MSFT deep ITM puts regularly showing asks of $108–$185, consistently
blocked by $100 ceiling. Need to evaluate whether these are legitimate signals
worth capturing or adjusted contract artifacts.

**Check:** Are these delta ~1.0 with IV ~0? If so, adjusted contract filter
should catch them upstream. If not, consider raising ceiling or adding
per-ticker ceiling overrides.

---

## AGENT IMPROVEMENTS

### 🔴 Live trading mode switch
**Category:** agent.py
**Effort:** S

When transitioning from paper to real capital, need to:
1. Re-enable ticker deduplication filter (already commented, just uncomment)
2. Re-enable bankroll exposure limits
3. Replace `insert_paper_trade()` with real order execution via Public API
4. Add position sizing model based on lifecycle data analysis

**Note:** All the commented-out logic is already in place. This is mostly
a configuration change plus the order execution layer.

---

### 🔴 Risk-based position sizing model
**Category:** agent.py / strategy_config.py
**Effort:** M
**Blocked on:** Live trading mode switch (not needed for paper)

Current bankroll rules are portfolio-level exposure caps (20% max open,
30% drawdown reduction). There is no per-trade sizing model — the agent
enters 1 contract regardless of premium, conviction, or account state.

For long options, max loss is bounded at the premium paid, so no stop
distance is needed to compute risk. Naively: 1% risk of a $10K account
= $100 = exactly 1 contract at the current $100 MAX_ASK ceiling.

But the trailing stop model means full premium loss is not the typical
outcome. A stop-triggered exit at -25% on a $60 contract risks $15, not
$60. True risk-per-trade sits somewhere between (premium × trail %) and
full premium — the gap being never-green trades that die before the
trail ever arms, plus overnight gap risk.

**Approach:** Query the lifecycle data for the realized loss distribution
on closed losers, split by never-green vs. went-green-then-stopped. The
never-green mean/median loss is the effective risk denominator. Size as:
  contracts = (account × risk_pct) / (entry_premium × effective_loss_pct)
Floor at 1 contract. Revisit whenever the entry filter changes materially,
since the never-green rate is what this number is made of.

**Data needed:** Sufficient post-instrumentation closed trades to
characterize the never-green loss distribution with confidence.

---

### 🔴 Daily loss circuit breaker
**Category:** agent.py / optimizing_agent.py
**Effort:** S
**Blocked on:** Live trading mode switch (not needed for paper)

Existing drawdown triggers (30% reduce, 50% halt) are slow — a 30%
drawdown could accumulate over weeks of bad entries before firing. A
daily ceiling is the fast circuit breaker: it catches a regime break,
a broken data feed, or a scoring bug before it compounds across a
session.

On a $10K bankroll, a 2% daily ceiling = $200 ≈ 2-3 contracts going to
zero in one session. Encode alongside the trail tranches in
strategy_config.py.

**Approach:** At the top of each agent loop, sum today's realized P&L
plus mark-to-market on open positions. If the loss exceeds
DAILY_LOSS_CEILING, block new entries for the remainder of the session
(position_monitor continues managing open positions normally — this is
an entry gate, not a liquidation trigger). Reset at session open.

**Open question:** Should the ceiling count unrealized drawdown on open
positions, or realized only? Realized-only is simpler and avoids
grounding the agent on noise, but is slower to react. Lean realized-only
for a first pass.

---

### 🟡 Signal quality score for agent decisions
**Category:** agent.py
**Effort:** M

Currently agent enters any QUALIFIED signal with a clear score gap.
As data accumulates, build a meta-score that incorporates:
- Historical win rate for this ticker/contract_type combination
- OI confirmation (was flow opening or closing?)
- IV percentile (is this expensive or cheap relative to history?)
- DTE sweet spot (which DTE ranges produce best outcomes?)

Agent uses meta-score as primary filter, composite score as tiebreaker.

---

### 🟡 Friday 0DTE noise handling
**Category:** agent.py
**Effort:** S

On expiration Fridays, 0DTE signals dominate the eligible pool and get
filtered, leaving fewer non-0DTE candidates. Consider:
- Adjusting scanner to deprioritize 0DTE on Fridays
- Or adding a Friday-specific mode that focuses only on next-week expirations

---

## POSITION MONITOR IMPROVEMENTS

### 🔴 Empirical stop curve
**Category:** position_monitor.py
**Effort:** L

Current DTE-aware stops (50% / 30% / 20%) are theoretical. After collecting
sufficient lifecycle data (position_snapshots with full price history), fit a
polynomial curve to actual option price behavior:
- At what % drawdown do winning trades typically bottom before recovering?
- At what % drawdown are losing trades effectively dead?

Replace flat tier thresholds with a continuous curve fitted to real data.

**Data needed:** ~200+ complete option lifecycles across multiple tickers,
DTEs, and market conditions. Probably 4–8 weeks of agent running.

---

### 🟡 Market context quality
**Category:** position_monitor.py
**Effort:** S

SPY/QQQ showing 0.00% change right at market open — previousClose field
may not populate until a few minutes after open. Add a grace period or
fallback for the first 5 minutes of the session.

---

## DASHBOARD IMPROVEMENTS

### 🟡 Signal history chart
**Category:** dashboard.py
**Effort:** M

Visual chart showing signal count and tier breakdown over time.
Helps identify whether scanner activity correlates with market volatility.

---

### 🟡 Expiration filter tabs
**Category:** dashboard.py
**Effort:** S

Filter dashboard signals by expiration date — today / this week / next week / all.

---

### 🟡 Ticker filter buttons
**Category:** dashboard.py
**Effort:** S

Quick-filter buttons to show signals for specific tickers.

---

## INFRASTRUCTURE

### 🟡 Cloud VM migration
**Category:** Infrastructure
**Effort:** M

Currently running on local Windows machine — scanner stops when computer
sleeps or restarts. Migrate to AWS or Azure small VM (~$15-20/month) for:
- 24/7 uptime
- Remote dashboard access
- No missed scans during off-hours
- Persistent data collection

**Sequence:** Stabilize agent + monitor locally first (2–4 weeks),
then migrate once system is proven.

---

### 🟢 Dev environment deep-dive session
**Category:** Education
**Effort:** S

Dedicated session covering: virtual environments, pip, .env files,
.gitignore, Git/version control, VS Code. Nolan understands these at
surface level and wants to go deeper. Schedule when there's a natural
pause in feature development.

---

## FUTURE PROJECTS

### 🟢 Wealth Hub
**Category:** New project
**Effort:** L

Unified portfolio dashboard covering all income sources, debts, trading
activity, tax considerations (including tax lot awareness on entries/exits),
and overall net worth. To be built after scanner is stable and proven.

---

### 🟢 Kalshi Tennis Agent
**Category:** New project
**Effort:** M

React dashboard with live Kalshi API integration, Sports API layer,
Claude AI streaming analysis, and paper trading with P&L tracking.
Pending: GitHub setup + api-sports.io Sports API key.

---

## COMPLETED ✅

- [x] Core scanner operational (fetch_trades.py, scheduler.py)
- [x] SQLite journal with signals + paper_trades tables
- [x] Flask dashboard at localhost:5000
- [x] DTE-adjusted signal tier thresholds
- [x] Deep ITM filter (delta ≥ 0.95)
- [x] Adjusted contract filter (delta ≥ 0.95 AND IV = 0%)
- [x] Paper trading CLI (paper_trade.py)
- [x] Position monitor with auto-close (position_monitor.py)
- [x] Dynamic DTE-aware stops (50% / 30% / 20%)
- [x] STOP_TRIGGERED status for continued tracking
- [x] Market context snapshots (SPY/QQQ/IWM/TLT/VIX per snapshot)
- [x] Autonomous paper trading agent (agent.py)
- [x] AI thesis generation with rule-based fallback
- [x] Score gap tiebreaker (premium → DTE hierarchy)
- [x] GitHub repo public with README and briefing docs
- [x] Print market overview duplicate block fix
- [x] GOOGL adjusted contract DB cleanup
