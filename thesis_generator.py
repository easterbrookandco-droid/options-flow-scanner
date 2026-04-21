import anthropic
import os
from dotenv import load_dotenv

load_dotenv()

# ── Toggle ────────────────────────────────────────────────────────────────────
# Set to False to disable thesis generation without removing the integration
THESIS_ENABLED = True

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def generate_thesis(signal, greeks, market_overview, flow_bias):
    """
    Generate a one-sentence trade thesis for a QUALIFIED signal
    using Claude. Called once per QUALIFIED signal per scan run.

    Parameters:
        signal (dict): Signal fields — ticker, contract, contract_type,
                       composite_score, premium, decoded (days_out,
                       strike_display, expiry_display)
        greeks (dict): Delta, IV, moneyness for this contract
        market_overview (dict): SPY/QQQ/IWM price and day change
        flow_bias (dict): Overall call/put bias from today's scan
                          keys: call_pct, put_pct, bias_label

    Returns:
        str: One sentence thesis, or fallback string if call fails
    """

    if not THESIS_ENABLED:
        return "(Thesis generation disabled)"

    try:
        # ── Build context block ───────────────────────────────────────────
        ticker    = signal.get("ticker", "")
        contract  = signal.get("contract", "")
        c_type    = signal.get("contract_type", "")
        score     = signal.get("composite_score", 0)
        premium   = signal.get("premium", 0)
        decoded   = signal.get("decoded", {})
        dte       = decoded.get("days_out", 0)
        strike    = decoded.get("strike_display", "")
        expiry    = decoded.get("expiry_display", "")

        delta     = greeks.get("delta", "unavailable")
        iv        = greeks.get("iv", "unavailable")
        moneyness = greeks.get("moneyness", "unavailable")

        spy  = market_overview.get("SPY", {})
        qqq  = market_overview.get("QQQ", {})
        spy_chg = f"{spy.get('sign','')}{spy.get('chg_pct', 0):.2f}%" \
                  if spy.get("has_change") else "unavailable"
        qqq_chg = f"{qqq.get('sign','')}{qqq.get('chg_pct', 0):.2f}%" \
                  if qqq.get("has_change") else "unavailable"

        call_pct   = flow_bias.get("call_pct", 0)
        put_pct    = flow_bias.get("put_pct", 0)
        bias_label = flow_bias.get("bias_label", "NEUTRAL")

        premium_display = (f"${premium/1_000_000:.1f}M" if premium >= 1_000_000
                           else f"${premium/1_000:.0f}K")

        context = f"""
You are a concise options flow analyst. Write exactly one sentence 
(under 30 words) summarizing why this signal is worth paper trading.
Be specific — use the actual numbers. Do not use filler phrases like 
"suggesting" or "indicating". State the case directly.

Signal:
  Ticker:        {ticker}
  Contract:      {contract} ({c_type})
  Strike:        {strike}  Expiry: {expiry}  DTE: {dte}
  Score:         {score}   Premium: {premium_display}
  Delta:         {delta}   IV: {iv}   Moneyness: {moneyness}

Market context:
  SPY day change: {spy_chg}
  QQQ day change: {qqq_chg}
  Overall flow bias: {call_pct:.0f}% calls / {put_pct:.0f}% puts → {bias_label}

Write one sentence only. No preamble, no label, just the sentence.
""".strip()

        # ── API call ──────────────────────────────────────────────────────
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": context}]
        )

        thesis = message.content[0].text.strip()

        # Safety trim removed — prompt constraints handle length
        return thesis

    except Exception as e:
        return f"(Thesis unavailable: {e})"