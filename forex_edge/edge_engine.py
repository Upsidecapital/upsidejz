"""
Edge Engine — combines COT + fundamental data to produce trading signals.

Scoring system (all 0–100, 50 = neutral):
  • COT Score      (weight 50%): speculator net positioning vs 52-week range
  • Fundamental    (weight 30%): interest rate relative to peers
  • Momentum       (weight 20%): week-over-week COT position change direction

A currency EDGE SCORE > 60  → bullish bias
A currency EDGE SCORE < 40  → bearish bias

For a PAIR signal:
  pair_score = base_edge - quote_edge
  +20 or more → BUY base/quote   (strong → STRONG_BUY at +35)
  -20 or less → SELL base/quote  (strong → STRONG_SELL at -35)
"""

from __future__ import annotations

import logging
from typing import Optional

from cot_fetcher import get_all_cot_analysis, get_cot_analysis
from database import get_interest_rates, store_signal
from forex_data import (
    MAJOR_CURRENCIES,
    MAJOR_PAIRS,
    calculate_currency_strength,
    get_fundamental_score,
    get_rates_usd_base,
)

logger = logging.getLogger(__name__)

WEIGHTS = {"cot": 0.50, "fundamental": 0.30, "momentum": 0.20}


def _momentum_score(cot_analysis: dict) -> float:
    """
    Convert weekly COT change to a 0–100 momentum score.
    A large positive change → high score; large negative → low score.
    Uses open interest as denominator for normalisation when available.
    """
    wc = cot_analysis.get("weekly_change")
    latest = cot_analysis.get("latest") or {}
    oi = latest.get("open_interest", 0) or 0

    if wc is None:
        return 50.0

    if oi > 0:
        # Normalise change as % of open interest, cap at ±5%
        pct = wc / oi * 100
        pct = max(-5.0, min(5.0, pct))
        score = (pct + 5.0) / 10.0 * 100
    else:
        # Rough absolute normalisation: cap at ±50 000 contracts
        capped = max(-50_000, min(50_000, wc))
        score = (capped + 50_000) / 100_000 * 100

    return round(score, 1)


def compute_currency_edge(currency: str,
                           cot_data: Optional[dict] = None,
                           fundamental_score: Optional[float] = None) -> dict:
    """
    Compute a composite edge score (0–100) for a single currency.
    """
    if cot_data is None:
        cot_data = get_cot_analysis(currency)

    cot_index = cot_data.get("cot_index")
    cot_score = float(cot_index) if cot_index is not None else 50.0
    momentum = _momentum_score(cot_data)

    if fundamental_score is None:
        fundamental_score = get_fundamental_score(currency)

    edge = round(
        cot_score * WEIGHTS["cot"]
        + fundamental_score * WEIGHTS["fundamental"]
        + momentum * WEIGHTS["momentum"],
        1,
    )

    if edge >= 65:
        label, color = "STRONG BULLISH", "#00e676"
    elif edge >= 55:
        label, color = "BULLISH", "#69f0ae"
    elif edge >= 45:
        label, color = "NEUTRAL", "#ffd740"
    elif edge >= 35:
        label, color = "BEARISH", "#ff6d6d"
    else:
        label, color = "STRONG BEARISH", "#ff1744"

    return {
        "currency": currency,
        "edge_score": edge,
        "cot_score": round(cot_score, 1),
        "fundamental_score": round(fundamental_score, 1),
        "momentum_score": round(momentum, 1),
        "bias": cot_data.get("bias", "NEUTRAL"),
        "cot_index": cot_index,
        "weekly_change": cot_data.get("weekly_change"),
        "label": label,
        "color": color,
    }


def compute_all_currency_edges() -> dict[str, dict]:
    """Compute edge scores for all tracked currencies."""
    all_cot = get_all_cot_analysis()
    ir = get_interest_rates()
    rates_list = [d["rate"] for d in ir.values()] if ir else []
    mn_rate = min(rates_list) if rates_list else 0
    mx_rate = max(rates_list) if rates_list else 1
    rate_range = mx_rate - mn_rate or 1

    results = {}
    for currency in ["EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "USD"]:
        cot = all_cot.get(currency, {})
        ir_rate = ir.get(currency, {}).get("rate", 0.0)
        fund_score = round((ir_rate - mn_rate) / rate_range * 100, 1)
        results[currency] = compute_currency_edge(currency, cot, fund_score)

    return results


def _parse_pair(pair: str) -> tuple[str, str]:
    """Split 'EURUSD' → ('EUR', 'USD')."""
    return pair[:3].upper(), pair[3:].upper()


def generate_pair_signal(pair: str,
                          edges: Optional[dict[str, dict]] = None) -> dict:
    """
    Generate a trading signal for a given pair string (e.g. 'EURUSD').
    """
    if edges is None:
        edges = compute_all_currency_edges()

    base, quote = _parse_pair(pair)
    base_edge = edges.get(base, {})
    quote_edge = edges.get(quote, {})

    base_score = base_edge.get("edge_score", 50.0)
    quote_score = quote_edge.get("edge_score", 50.0)
    diff = base_score - quote_score

    base_cot = base_edge.get("cot_score", 50.0)
    quote_cot = quote_edge.get("cot_score", 50.0)
    cot_diff = base_cot - quote_cot

    base_fund = base_edge.get("fundamental_score", 50.0)
    quote_fund = quote_edge.get("fundamental_score", 50.0)
    fund_diff = base_fund - quote_fund

    if diff >= 35:
        direction, strength = "STRONG_BUY", "Strong Buy"
        color = "#00e676"
        signal_score = min(100, 50 + diff)
    elif diff >= 18:
        direction, strength = "BUY", "Buy"
        color = "#69f0ae"
        signal_score = min(100, 50 + diff)
    elif diff <= -35:
        direction, strength = "STRONG_SELL", "Strong Sell"
        color = "#ff1744"
        signal_score = max(0, 50 + diff)
    elif diff <= -18:
        direction, strength = "SELL", "Sell"
        color = "#ff6d6d"
        signal_score = max(0, 50 + diff)
    else:
        direction, strength = "NEUTRAL", "Neutral"
        color = "#ffd740"
        signal_score = 50.0

    rationale = _build_rationale(pair, base, quote, base_edge, quote_edge, diff, cot_diff, fund_diff)

    return {
        "pair": pair,
        "direction": direction,
        "strength": strength,
        "color": color,
        "signal_score": round(signal_score, 1),
        "edge_diff": round(diff, 1),
        "base": {"currency": base, **base_edge},
        "quote": {"currency": quote, **quote_edge},
        "cot_diff": round(cot_diff, 1),
        "fundamental_diff": round(fund_diff, 1),
        "rationale": rationale,
    }


def _build_rationale(pair: str, base: str, quote: str,
                     base_edge: dict, quote_edge: dict,
                     diff: float, cot_diff: float, fund_diff: float) -> str:
    parts = []

    cot_b = base_edge.get("cot_index")
    cot_q = quote_edge.get("cot_index")
    if cot_b is not None and cot_q is not None:
        parts.append(
            f"COT: {base} speculators at {cot_b:.0f}/100 vs "
            f"{quote} at {cot_q:.0f}/100 (diff {cot_diff:+.1f})."
        )

    fund_b = base_edge.get("fundamental_score", 50)
    fund_q = quote_edge.get("fundamental_score", 50)
    parts.append(
        f"Fundamentals: {base} score {fund_b:.0f} vs {quote} {fund_q:.0f} "
        f"(rate differential {fund_diff:+.1f} pts)."
    )

    if diff >= 18:
        parts.append(f"Combined edge favors {base} over {quote} by {diff:+.1f} pts → BUY signal.")
    elif diff <= -18:
        parts.append(f"Combined edge favors {quote} over {base} by {abs(diff):.1f} pts → SELL signal.")
    else:
        parts.append(f"No significant edge between {base} and {quote} (diff {diff:+.1f} pts).")

    return " ".join(parts)


def generate_all_signals(persist: bool = True) -> list[dict]:
    """Generate and optionally store signals for all major pairs."""
    edges = compute_all_currency_edges()
    signals = []

    for pair in MAJOR_PAIRS:
        try:
            sig = generate_pair_signal(pair, edges)
            signals.append(sig)
            if persist and sig["direction"] != "NEUTRAL":
                store_signal(
                    pair=pair,
                    direction=sig["direction"],
                    cot_score=sig["cot_diff"],
                    fundamental_score=sig["fundamental_diff"],
                    combined_score=sig["signal_score"],
                    rationale=sig["rationale"],
                )
        except Exception as e:
            logger.warning(f"Signal generation failed for {pair}: {e}")

    # Sort: strong signals first, then by abs(edge_diff)
    order = {"STRONG_BUY": 0, "STRONG_SELL": 1, "BUY": 2, "SELL": 3, "NEUTRAL": 4}
    signals.sort(key=lambda s: (order.get(s["direction"], 5), -abs(s["edge_diff"])))
    return signals


def get_top_signals(n: int = 5) -> list[dict]:
    """Return the top n actionable signals (non-neutral)."""
    all_sigs = generate_all_signals(persist=False)
    actionable = [s for s in all_sigs if s["direction"] != "NEUTRAL"]
    return actionable[:n]
