"""
AI analysis module using the Claude API.

Provides narrative trade analysis for forex pairs by combining:
  - COT positioning data
  - Interest rate fundamentals
  - Edge scores
  - Recent economic events
"""

import logging
import os
from datetime import datetime

import anthropic

from database import get_ai_analysis, get_interest_rates, store_ai_analysis

logger = logging.getLogger(__name__)


def _build_analysis_prompt(
    pair: str,
    signal: dict,
    cot_data: dict,
    upcoming_events: list[dict],
) -> str:
    base = signal.get("base", {})
    quote = signal.get("quote", {})
    ir = get_interest_rates()

    base_currency = pair[:3].upper()
    quote_currency = pair[3:].upper()

    base_ir = ir.get(base_currency, {}).get("rate", "N/A")
    quote_ir = ir.get(quote_currency, {}).get("rate", "N/A")
    base_bank = ir.get(base_currency, {}).get("central_bank", "")
    quote_bank = ir.get(quote_currency, {}).get("central_bank", "")

    cot_base = cot_data.get(base_currency, {})
    cot_quote = cot_data.get(quote_currency, {})

    relevant_events = [
        e for e in upcoming_events
        if e.get("currency") in (base_currency, quote_currency)
    ][:5]

    events_text = ""
    if relevant_events:
        events_text = "\n**Upcoming Economic Events:**\n"
        for ev in relevant_events:
            events_text += (
                f"  - {ev['event_date']} [{ev['currency']}] {ev['event_name']}"
                f" | Impact: {ev['impact']}"
                f" | Forecast: {ev.get('forecast', 'N/A')}"
                f" | Previous: {ev.get('previous', 'N/A')}\n"
            )

    return f"""You are an expert forex market analyst providing a COT-based trade analysis.

**Pair:** {pair}
**Date:** {datetime.utcnow().strftime('%A, %d %B %Y')} UTC

**Edge Scores:**
- {base_currency} Edge Score: {base.get('edge_score', 'N/A')}/100 ({base.get('label', '')})
- {quote_currency} Edge Score: {quote.get('edge_score', 'N/A')}/100 ({quote.get('label', '')})
- Combined Signal: {signal.get('strength', 'Neutral')} (diff: {signal.get('edge_diff', 0):+.1f})

**COT Positioning:**
- {base_currency} COT Index: {cot_base.get('cot_index', 'N/A')}/100 | Bias: {cot_base.get('bias', 'N/A')} | Weekly Change: {cot_base.get('weekly_change', 'N/A')} contracts
- {quote_currency} COT Index: {cot_quote.get('cot_index', 'N/A')}/100 | Bias: {cot_quote.get('bias', 'N/A')} | Weekly Change: {cot_quote.get('weekly_change', 'N/A')} contracts

**Central Bank Interest Rates:**
- {base_currency} ({base_bank}): {base_ir}%
- {quote_currency} ({quote_bank}): {quote_ir}%
- Rate Differential: {signal.get('fundamental_diff', 0):+.1f} pts in favor of {base_currency if signal.get('fundamental_diff', 0) > 0 else quote_currency}
{events_text}

Please provide a concise, professional analysis (3–4 paragraphs) covering:
1. **COT Analysis**: What the positioning tells us about institutional sentiment
2. **Fundamental Outlook**: Interest rate differential and macro context
3. **Trade Recommendation**: Clear direction with key levels to watch (support/resistance)
4. **Risk Factors**: What could invalidate this setup

Be direct and actionable. Use professional trading language."""


async def analyze_pair_streaming(
    pair: str,
    signal: dict,
    cot_data: dict,
    upcoming_events: list[dict],
):
    """
    Async generator that streams Claude's analysis as text chunks.
    Yields str chunks, then stores the full analysis when done.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        yield "⚠️ ANTHROPIC_API_KEY not configured. Please set it in your .env file.\n"
        return

    prompt = _build_analysis_prompt(pair, signal, cot_data, upcoming_events)
    client = anthropic.Anthropic(api_key=api_key)
    full_text = []

    try:
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                full_text.append(text)
                yield text

        store_ai_analysis(pair, "".join(full_text))

    except anthropic.AuthenticationError:
        yield "⚠️ Invalid API key. Please check your ANTHROPIC_API_KEY in .env.\n"
    except Exception as e:
        logger.error(f"AI analysis failed for {pair}: {e}")
        yield f"⚠️ Analysis failed: {e}\n"


def get_cached_analysis(pair: str) -> dict | None:
    """Return the most recent cached analysis for a pair."""
    return get_ai_analysis(pair)


async def get_market_overview_stream(edges: dict, top_signals: list[dict]):
    """
    Stream a brief market overview (COT + macro summary) for the dashboard.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        yield "⚠️ ANTHROPIC_API_KEY not configured.\n"
        return

    ir = get_interest_rates()

    # Build a compact summary
    currency_summary = ""
    for currency in ["USD", "EUR", "GBP", "JPY", "CAD", "AUD", "NZD", "CHF"]:
        edge = edges.get(currency, {})
        rate = ir.get(currency, {}).get("rate", "N/A")
        currency_summary += (
            f"  {currency}: Edge={edge.get('edge_score', 'N/A'):.0f}/100 | "
            f"COT={edge.get('cot_score', 'N/A'):.0f} | "
            f"Rate={rate}% | Bias={edge.get('bias', 'N/A')}\n"
        )

    top_sig_text = ""
    for sig in top_signals[:5]:
        top_sig_text += f"  {sig['pair']}: {sig['strength']} (score {sig['signal_score']:.0f})\n"

    prompt = f"""You are a professional forex market analyst. Provide a brief market overview (2–3 paragraphs)
based on the following COT and fundamental data for {datetime.utcnow().strftime('%d %B %Y')}:

**Currency Edge Scores:**
{currency_summary}

**Top Trading Signals:**
{top_sig_text}

Cover: (1) dominant market themes this week, (2) key COT-driven opportunities,
(3) pairs to watch. Keep it concise and actionable. Professional tone."""

    client = anthropic.Anthropic(api_key=api_key)
    try:
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                yield text
    except Exception as e:
        logger.error(f"Market overview AI failed: {e}")
        yield f"⚠️ Overview generation failed: {e}\n"
