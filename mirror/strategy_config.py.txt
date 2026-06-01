"""
strategy_config.py — single source of truth for the exit strategy knobs.

Imported by:
  - position_monitor.py  (two-stage trailing stop evaluated each scan)
  - journal.py           (hurdle_price stamped onto each trade at entry)
  - migrate_and_backfill_trailing.py (historical backfill)

Change a value here and it flows through to the live monitor (on its next
restart) and to every new entry. Note: hurdle_price is *stamped at entry*
onto paper_trades, but the monitor currently still recomputes the hurdle
from the live HURDLE_PCT each cycle — so changing HURDLE_PCT today affects
all open positions that have not yet crossed their hurdle. (Decision on
whether the monitor should instead read the stored per-trade hurdle is
deferred — see PROJECT_STATE notes.)
"""

# ── Stage 1: Hurdle ──────────────────────────────────────────────────────
# The contract price must rise at least this fraction above entry before the
# trailing stop activates. 0.01 = +1%  (entry $10.00 → hurdle $10.10).
HURDLE_PCT = 0.01


def hurdle_price(entry_price):
    """Price the contract must reach to activate the trailing stop.

    Rounded to 4 dp so the +1% survives even for low-priced contracts
    (a 2 dp round would collapse the hurdle to entry for sub-$0.50 options).
    """
    if entry_price is None:
        return None
    return round(entry_price * (1 + HURDLE_PCT), 4)


# ── Stage 2: Trailing stop (DTE-aware) ───────────────────────────────────
# Once the hurdle is crossed, exit if price falls this fraction below the
# running peak PRICE (not peak gain). Tighter as expiration approaches.
def trailing_stop_pct(dte):
    if dte is None or dte <= 0:
        return 0.10
    if dte <= 2:
        return 0.15
    if dte <= 5:
        return 0.20
    if dte <= 14:
        return 0.25
    return 0.25


# ── Downside backstop (DTE-aware) ────────────────────────────────────────
# Last-resort floor for positions that never cross the hurdle (so the
# trailing stop never engages). Exit if the loss exceeds this fraction of
# total cost.
def backstop_pct(dte):
    if dte is not None and dte <= 2:
        return 0.60
    if dte is not None and dte <= 14:
        return 0.70
    return 0.80


# ── Optimizing agent: per-contract ask ceiling ───────────────────────────
# Maximum ask price per contract — budget ceiling per position, same logic as
# the MAX_ASK_PER_CONTRACT cap. SCOPED TO optimizing_agent.py ONLY — it
# deliberately does not affect agent.py, the scanner, or the position monitor.
# Set tight ($7.00) so the optimizing run only enters cheap contracts while
# the parameter sweep collects data on low-priced entries.
OPTIMIZING_MAX_ASK_PER_CONTRACT = 7.00
