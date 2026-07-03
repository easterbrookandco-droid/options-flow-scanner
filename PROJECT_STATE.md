# OPTIONS FLOW SCANNER — PROJECT STATE
*Last updated: 2026-07-03*
*This session superseded the stale 2026-05-24 state. Prior detail lives in SESSION_HISTORY.md.*

---

## 🔴 SYSTEM STATUS
- VM: AWS EC2, 3.144.128.166, Ubuntu. Services running: scheduler, agent, monitor (dashboard/optimizing-agent status to re-verify).
- Data: ~1,565 trades logged, 1,449 closed. signals.db is authoritative on VM.
- Untouched since ~May 29 (user was heads-down at work). System ran autonomously through a June downturn and recovery.
- Known infra note: NordVPN was killing SSH/VS Code sessions via idle-timeout. Fix = split-tunnel exclusion of ssh.exe AND VS Code; plain-terminal SSH holds, VS Code still flaky. VPN off = stable.
- `*** System restart required ***` pending on VM (kernel update). Deferred — reboot restarts all services, babysit when done.

## 📊 CURRENT PERFORMANCE (as of 2026-07-03)
- Realized P&L: +$18,734 (DOWN from +$31,808 on May 23 — the system LOST ground over the month on ~1,160 more trades).
- Mark-to-market: +$37,020. Win rate: 43.5% (down from 54.5%).
- Exit-reason breakdown: TARGET +$65,790 (73 trades, all wins) | MANUAL +$30,286 (50, all wins) | STOP +$5,180 (1,128 trades, ~breakeven) | **EXPIRED −$82,522 (198 trades, −42.5% avg)**.
- Carried by AMD (+$44K) and QQQ (+$31K). Bled by NVDA (−$28.8K), MSFT (−$22K), SPY (−$19K).

---

## 🔬 THE JULY 3 DIAGNOSTIC — WHAT WE LEARNED (most important section)

**Core reframe:** "Outcome" = OUR realizable exit given the path, NOT how the contract settled. A contract that expired worthless but spent a day up 80% was a *good entry we exited wrong*. This is the lens for all analysis.

**Key mechanism discovered — the "never-green" bucket:**
The trailing stop only ARMS after price crosses the +1% hurdle. A position that never goes green never arms it, so its ONLY safety net is the backstop (60/70/80% loss by DTE). Positions that slow-bleed to −42% and expire sit in the dead zone between "never profitable" (no trailing stop) and "catastrophic" (no backstop trip). That gap has no net.

**THE decisive number:** Splitting closed CONTROL trades by whether they EVER went green (snapshot `hurdle_crossed=1`):
- never_green: 170 trades, **−$109,612**
- went_green: 1,258 trades, **+$128,209**
→ The entire loss is the never-green bucket. The went-green book is net +$128K. **The money is lost at ENTRY, not exit.** No exit tweak can touch never-green positions (trailing stop literally never fires on them).

**Four hypotheses tested and KILLED:**
1. Exit logic is the problem → NO. Went-green book is profitable; exits work in aggregate.
2. It's a June-regime event → NO. Never-green loss is ~even across months: May −$55,663 (96 trades), June −$53,746 (73). Persistent structural leak, not a downturn accident.
3. Simple entry filter on score → NO. never-green rate rises only mildly with score (8% under-5 → 18% at 8.0+). No clean cut. NOTE: score is mildly INVERTED — higher score = slightly MORE likely to never go green. Scoring model is suspect.
4. Direction alignment (call into up-tape etc.) → NO. never-green ~proportional across aligned/against. BUT "flat tape" (SPY ~unchanged at entry) is a bloodbath: 45 never-green vs 32 went-green, the only bucket where losers outnumber winners. Flag for later (may be time-of-day proxy).

**Ticker cut nuance:** Don't ban high-loss tickers — TSLA (−$38K never-green) is also the BIGGEST winner (+$54K went-green); the loss is volume-scaled, not toxicity. BUT NVDA and GOOGL are net-negative even on the WENT-GREEN side (NVDA went-green −$5,735; GOOGL −$543). Those two can be dropped with ~zero opportunity cost — the only "free" action found.

**THE VALIDATED FINDING — vol/OI inversion:**
Bucketing CONTROL trades by scan-time vol_oi_ratio, never-green rate and P&L:
- 5.0+ : 1,165 trades, 13.3% never-green, **−$8,244** (only losing band; 82% of all entries live here)
- 3.0–5.0 : 6.2% ng, +$15,652
- 2.0–3.0 : 4.0% ng, +$8,913
- 1.0–2.0 : 5.9% ng, +$4,599
- under 1.0 : 2.5% ng, −$1,814 (thin)
The LOUDEST prints (extreme vol/OI, which the score REWARDS MOST) are the WORST. Sweet spot is MODERATE vol/OI (~2–5).
**Survives out-of-sample regime check** — 5.0+ has ~3x the never-green rate of under-5 in BOTH May (18.9% vs 6.3%) and June (10.0% vs 3.6%). And under-5 was regime-ROBUST: profitable in both the up month (+$13.7K) and the down month (+$12.4K), while 5.0+ made +$8.8K in May but −$16.8K in June (edge evaporates exactly when needed).

**Interpretation (footprint thesis, refined):** The original premise — tail informed institutional footprints — is VALID but we've been trading its crowded shadow. Extreme vol/OI = frenzy = move already in progress / picked-over / low-conviction lottery flow. Moderate vol/OI = real fresh positioning not yet chased. "Not size, but confirmed, fresh, directional positioning."

**Open caveat:** vol/OI may not be independent of score (score is built partly from vol/OI) — the "high score inverts" and "high vol/OI inverts" findings may be the same effect seen twice. Need to test vol/OI predictive power AFTER controlling for other features before treating as separate levers.

---

## 📋 NEXT STEPS (in leverage order)

1. **OI-confirmation capture (STARTING NOW):** Store next-morning open interest so we can distinguish OPENING vs CLOSING flow directly, instead of inferring from same-day vol/OI. Cannot be tested retroactively — we never stored next-day OI — so every day uninstrumented is lost data. This is the real thesis test.
2. **vol/OI ceiling / score inversion:** Stop over-weighting extreme vol/OI. Cap entries or invert that score component so moderate ranks highest. DO NOT hand-code from chat — run the full simulation treatment (test ceiling vs whole population, confirm it doesn't gut went-green winners, check capture efficiency) like the May exit-model work.
3. **Drop NVDA + GOOGL from watchlist** — net-negative even on went-green side, ~zero opportunity cost.
4. **Entry-evaluation rebuild** — the deep one. Rebuild scoring around footprint QUALITY (opening-vs-closing, delta-as-conviction, moderate-not-extreme size), not the current size-weighted score that has now inverted in 3 separate cuts.
5. **Loser-side exit (demoted):** A mid-tier exit for never-green bleeders would claw back part of −$109K, but it treats the symptom — entry fix prevents the disease. Must be tested against went-green winners simultaneously (recovery_matrix proved deep-drawdown winners exist) or it guillotines them.

**Stopping rule agreed:** The rebuilt/confirmed entry model must MATERIALLY cut the never-green rate AND survive out-of-sample on a period it wasn't tuned on — or we conclude the uncompeted edge is too thin for a retail account and say so. Sound premise ≠ harvestable edge.

## ⚙️ LIVE EXIT LOGIC (current, from strategy_config.py + position_monitor.py)
- Trailing stop trails peak PRICE (not peak P&L — the May "% basis" bug was fixed). Arms only after +1% hurdle.
- Trailing width by DTE: 0DTE 10%, 1–2d 15%, 3–5d 20%, 6–14d+ 25%.
- Backstop: 60% loss ≤2 DTE, 70% 3–14d, 80% 15+d.
- Hurdle recomputed live every cycle (intentional — open positions benefit from rule changes; do NOT trust a stamped hurdle field in analysis, monitor ignores it. Apply +1% uniformly when reconstructing history).
- ITM safety exit near expiration.

## 🧪 AGENT ARCHITECTURE
- agent.py = CONTROL (unconstrained, max data, up to ~$40 entries). optimizing_agent.py = OPTIMIZING (mode flag; capped $7 ask INTENTIONALLY — mirrors the real-capital price range user will actually trade; not a confound, a deliberate second cohort). Only 21 OPTIMIZING closed trades so far — too few to conclude anything yet.
- live/0_record_trade.py exists for manual real-capital logging → live_trades.db.

## 🔧 INFRA / WORKFLOW NOTES
- GitHub fetch for Claude: paste `raw.githubusercontent.com/.../master/FILENAME` links — jsDelivr serves emoji-containing .py files as BINARY (unreadable) and its index is stale. raw.githubusercontent works clean. Permission quirk: Claude can only fetch URLs the user has pasted in-chat.
- session_log.py habit LAPSED (that's why state was stale May→July). Re-establish: run at end of every session.
- signals.db schema: paper_trades has entry/exit features + market context strip + mode + hurdle_price. position_snapshots has current_price, pnl, current_dte, hurdle_crossed, running_max_price/pnl, market strip — dense ~2-min cadence, paths are trustworthy. signals table has volume, open_interest, vol_oi_ratio (single scan-time snapshot only — no next-day OI, hence step 1).
