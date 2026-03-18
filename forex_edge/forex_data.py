"""
Forex rates, currency strength, and economic indicator data.

Free data sources used:
  • exchangerate.host  – live forex rates (no key needed)
  • Fallback:           hardcoded approximate rates for offline mode
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

from database import (
    get_interest_rates,
    get_latest_forex_rates,
    store_forex_rates,
    update_interest_rate,
)

logger = logging.getLogger(__name__)

MAJOR_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"]

MAJOR_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "USDCAD", "AUDUSD", "NZDUSD",
    "EURJPY", "GBPJPY", "EURGBP",
    "AUDJPY", "CADJPY", "CHFJPY",
    "EURAUD", "EURCHF", "EURCAD",
    "GBPAUD", "GBPCAD", "GBPCHF",
    "AUDCAD", "AUDCHF", "AUDNZD",
    "NZDCAD", "NZDCHF", "NZDJPY",
]

# Fallback rates (USD base) used when API is unavailable
FALLBACK_RATES: dict[str, float] = {
    "EUR": 0.918, "GBP": 0.786, "JPY": 149.5, "CHF": 0.895,
    "CAD": 1.358, "AUD": 1.538, "NZD": 1.660, "MXN": 17.15,
    "SGD": 1.342, "HKD": 7.820, "NOK": 10.52, "SEK": 10.35,
}


# ── Rate fetching ─────────────────────────────────────────────────────────────

def fetch_forex_rates() -> dict[str, float]:
    """
    Fetch latest USD-base forex rates from exchangerate.host.
    Falls back to cached DB data, then hardcoded fallback.
    """
    try:
        resp = requests.get(
            "https://api.exchangerate.host/live",
            params={"access_key": "", "source": "USD"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            quotes = data.get("quotes", {})
            if quotes:
                # quotes are like {"USDEUR": 0.918, ...}
                rates = {k[3:]: v for k, v in quotes.items() if len(k) == 6}
                store_forex_rates(rates)
                return rates
    except Exception as e:
        logger.warning(f"exchangerate.host failed: {e}")

    # Try open.er-api.com (no key needed)
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            rates = data.get("rates", {})
            if rates:
                store_forex_rates(rates)
                return rates
    except Exception as e:
        logger.warning(f"open.er-api.com failed: {e}")

    # Try frankfurter.app (free ECB rates)
    try:
        resp = requests.get("https://api.frankfurter.app/latest?from=USD", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            rates = data.get("rates", {})
            rates["USD"] = 1.0
            if rates:
                store_forex_rates(rates)
                return rates
    except Exception as e:
        logger.warning(f"frankfurter.app failed: {e}")

    # Use DB cache
    cached = get_latest_forex_rates()
    if cached:
        logger.info("Using cached forex rates from DB")
        return cached["rates"]

    # Final fallback
    logger.warning("Using hardcoded fallback forex rates")
    return {**FALLBACK_RATES, "USD": 1.0}


def get_rates_usd_base() -> dict[str, float]:
    """Get forex rates with USD as base, with caching logic."""
    cached = get_latest_forex_rates()
    if cached:
        fetched_at = datetime.fromisoformat(cached["fetched_at"])
        age = datetime.utcnow() - fetched_at
        if age < timedelta(minutes=30):
            return cached["rates"]
    return fetch_forex_rates()


def get_pair_rate(base: str, quote: str, rates: Optional[dict] = None) -> Optional[float]:
    """Convert base/quote pair rate from USD-base rates."""
    if rates is None:
        rates = get_rates_usd_base()
    if base == "USD":
        return rates.get(quote)
    if quote == "USD":
        base_rate = rates.get(base)
        return 1.0 / base_rate if base_rate else None
    # Cross rate
    base_rate = rates.get(base)
    quote_rate = rates.get(quote)
    if base_rate and quote_rate:
        return quote_rate / base_rate
    return None


# ── Currency strength ─────────────────────────────────────────────────────────

def calculate_currency_strength(rates: Optional[dict] = None) -> dict[str, float]:
    """
    Calculate relative currency strength (0–100 scale) based on performance
    of each currency against all others.

    Method: For each currency, sum its gain/loss against all other majors,
    then normalize to 0–100.
    """
    if rates is None:
        rates = get_rates_usd_base()

    scores: dict[str, float] = {}

    for base in MAJOR_CURRENCIES:
        total = 0.0
        count = 0
        for quote in MAJOR_CURRENCIES:
            if base == quote:
                continue
            rate = get_pair_rate(base, quote, rates)
            if rate is not None:
                # Normalised as % above/below 1.0 (rough proxy for strength)
                total += rate
                count += 1
        scores[base] = total / count if count else 1.0

    # Normalise to 0–100
    mn = min(scores.values())
    mx = max(scores.values())
    rng = mx - mn
    if rng == 0:
        return {c: 50.0 for c in scores}

    return {
        c: round((v - mn) / rng * 100, 1)
        for c, v in scores.items()
    }


# ── Interest rates & fundamentals ─────────────────────────────────────────────

def get_rate_differential(base: str, quote: str) -> float:
    """Return interest rate differential (base rate - quote rate)."""
    ir = get_interest_rates()
    base_rate = ir.get(base, {}).get("rate", 0.0)
    quote_rate = ir.get(quote, {}).get("rate", 0.0)
    return round(base_rate - quote_rate, 2)


def get_all_rate_differentials() -> dict[str, float]:
    """Return rate differentials vs USD for all major currencies."""
    ir = get_interest_rates()
    usd_rate = ir.get("USD", {}).get("rate", 4.50)
    return {
        c: round(data.get("rate", 0.0) - usd_rate, 2)
        for c, data in ir.items()
        if c != "USD"
    }


def get_fundamental_score(currency: str) -> float:
    """
    Fundamental score (0–100) based on interest rate vs peers.

    Higher rate relative to the average → higher score.
    """
    ir = get_interest_rates()
    rates_list = [d["rate"] for d in ir.values()]
    if not rates_list:
        return 50.0
    currency_rate = ir.get(currency, {}).get("rate", 0.0)
    mn, mx = min(rates_list), max(rates_list)
    if mx == mn:
        return 50.0
    return round((currency_rate - mn) / (mx - mn) * 100, 1)


# ── Economic Calendar (lightweight built-in events) ──────────────────────────

def get_default_economic_calendar() -> list[dict]:
    """
    Return a set of upcoming high-impact events for the current week.
    In production these would come from a real calendar API.
    """
    today = datetime.utcnow().date()

    def next_weekday(weekday: int) -> str:
        """0=Mon … 6=Sun"""
        days_ahead = weekday - today.weekday()
        if days_ahead < 0:
            days_ahead += 7
        return (today + timedelta(days=days_ahead)).isoformat()

    events = [
        # USD events
        {"event_date": next_weekday(1), "currency": "USD", "impact": "HIGH",
         "event_name": "US CPI (m/m)", "forecast": "0.3%", "previous": "0.4%"},
        {"event_date": next_weekday(2), "currency": "USD", "impact": "HIGH",
         "event_name": "FOMC Meeting Minutes", "forecast": None, "previous": None},
        {"event_date": next_weekday(4), "currency": "USD", "impact": "HIGH",
         "event_name": "Non-Farm Payrolls", "forecast": "185K", "previous": "275K"},
        # EUR events
        {"event_date": next_weekday(1), "currency": "EUR", "impact": "HIGH",
         "event_name": "ECB Interest Rate Decision", "forecast": "2.65%", "previous": "2.90%"},
        {"event_date": next_weekday(2), "currency": "EUR", "impact": "MEDIUM",
         "event_name": "Germany ZEW Economic Sentiment", "forecast": "8.5", "previous": "6.3"},
        # GBP events
        {"event_date": next_weekday(3), "currency": "GBP", "impact": "HIGH",
         "event_name": "Bank of England Rate Decision", "forecast": "4.50%", "previous": "4.75%"},
        {"event_date": next_weekday(2), "currency": "GBP", "impact": "HIGH",
         "event_name": "UK CPI (y/y)", "forecast": "2.9%", "previous": "3.0%"},
        # JPY events
        {"event_date": next_weekday(0), "currency": "JPY", "impact": "HIGH",
         "event_name": "BOJ Policy Rate Statement", "forecast": "0.50%", "previous": "0.25%"},
        # CAD events
        {"event_date": next_weekday(3), "currency": "CAD", "impact": "HIGH",
         "event_name": "Bank of Canada Rate Decision", "forecast": "3.00%", "previous": "3.25%"},
        # AUD events
        {"event_date": next_weekday(1), "currency": "AUD", "impact": "HIGH",
         "event_name": "RBA Meeting Minutes", "forecast": None, "previous": None},
        # NZD events
        {"event_date": next_weekday(2), "currency": "NZD", "impact": "HIGH",
         "event_name": "RBNZ Rate Decision", "forecast": "3.75%", "previous": "4.25%"},
        # CHF events
        {"event_date": next_weekday(4), "currency": "CHF", "impact": "MEDIUM",
         "event_name": "SNB Quarterly Bulletin", "forecast": None, "previous": None},
    ]

    # Add source
    for ev in events:
        ev["source"] = "builtin"
        ev.setdefault("actual", None)

    return sorted(events, key=lambda x: x["event_date"])


def update_central_bank_rate(currency: str, rate: float) -> None:
    """Allow manual update of a central bank rate."""
    update_interest_rate(currency, rate)
    logger.info(f"Updated {currency} rate to {rate}%")
