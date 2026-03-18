"""
COT (Commitment of Traders) data fetcher and analyzer.

Fetches the CFTC Traders in Financial Futures (TFF) report which contains
the forex currency futures positioning data for large speculators, asset
managers, leveraged funds (hedge funds/CTAs), and dealers.

Source: https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm
"""

import io
import logging
import zipfile
from datetime import datetime
from typing import Optional

import pandas as pd
import requests

from database import (
    get_cot_history,
    get_latest_cot,
    upsert_cot_snapshot,
)

logger = logging.getLogger(__name__)

# Maps currency code → substring to match in CFTC "Market_and_Exchange_Names"
CURRENCY_CONTRACTS = {
    "EUR": "EURO FX",
    "GBP": "BRITISH POUND",
    "JPY": "JAPANESE YEN",
    "CHF": "SWISS FRANC",
    "CAD": "CANADIAN DOLLAR",
    "AUD": "AUSTRALIAN DOLLAR",
    "NZD": "NEW ZEALAND DOLLAR",
    "MXN": "MEXICAN PESO",
}

CFTC_TFF_URL_PATTERN = "https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip"
CFTC_TFF_CURRENT_URL = "https://www.cftc.gov/dea/newcot/FinFutWk.txt"


def _download_cot_df(year: Optional[int] = None) -> Optional[pd.DataFrame]:
    """Download and parse CFTC TFF COT data into a DataFrame."""
    if year is None:
        year = datetime.now().year

    # Try current year, then previous year
    for y in [year, year - 1]:
        url = CFTC_TFF_URL_PATTERN.format(year=y)
        try:
            logger.info(f"Fetching COT data from {url}")
            resp = requests.get(url, timeout=45)
            if resp.status_code == 200:
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    txt_files = [f for f in zf.namelist() if f.lower().endswith(".txt")]
                    if txt_files:
                        with zf.open(txt_files[0]) as f:
                            df = pd.read_csv(f, low_memory=False)
                            logger.info(f"Loaded {len(df)} COT records from {y}")
                            return df
        except Exception as e:
            logger.warning(f"Failed to fetch {url}: {e}")

    # Fallback: current week file
    try:
        logger.info("Trying current-week COT fallback URL")
        resp = requests.get(CFTC_TFF_CURRENT_URL, timeout=30)
        if resp.status_code == 200:
            df = pd.read_csv(io.StringIO(resp.text), low_memory=False)
            return df
    except Exception as e:
        logger.warning(f"Current-week fallback failed: {e}")

    return None


def _find_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    """Return the first column name from candidates that exists in df."""
    cols_lower = {c.lower(): c for c in df.columns}
    for candidate in candidates:
        if candidate.lower() in cols_lower:
            return cols_lower[candidate.lower()]
    return None


def _extract_currency_rows(df: pd.DataFrame, currency: str) -> pd.DataFrame:
    """Filter df rows matching the given currency contract."""
    name_col = _find_col(df, ["Market_and_Exchange_Names", "market_and_exchange_names",
                                "Contract_Market_Name"])
    if name_col is None:
        return pd.DataFrame()

    keyword = CURRENCY_CONTRACTS.get(currency, "")
    mask = df[name_col].str.upper().str.contains(keyword.upper(), na=False)
    return df[mask].copy()


def _parse_date_col(df: pd.DataFrame) -> Optional[str]:
    """Return the date column name after adding a normalized 'parsed_date' column."""
    date_col = _find_col(df, [
        "As_of_Date_In_Form_YYMMDD",
        "Report_Date_as_MM_DD_YYYY",
        "As_of_Date_In_Form_YY-MM-DD",
    ])
    if date_col is None:
        return None

    def _parse(val):
        val = str(val).strip()
        for fmt in ("%y%m%d", "%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
            try:
                return datetime.strptime(val, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        return None

    df["parsed_date"] = df[date_col].apply(_parse)
    return "parsed_date"


def _to_int(val) -> int:
    try:
        return int(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0


def _process_currency_df(currency: str, rows: pd.DataFrame) -> list[dict]:
    """Convert filtered rows into a list of COT snapshot dicts."""
    date_col = _parse_date_col(rows)
    if date_col is None or rows.empty:
        return []

    # Column name candidates for leveraged funds (hedge funds/CTAs = smart money)
    lev_long_col = _find_col(rows, [
        "Lev_Money_Positions_Long_All", "lev_money_positions_long_all",
        "NonComm_Positions_Long_All", "noncomm_positions_long_all",
    ])
    lev_short_col = _find_col(rows, [
        "Lev_Money_Positions_Short_All", "lev_money_positions_short_all",
        "NonComm_Positions_Short_All", "noncomm_positions_short_all",
    ])
    asset_long_col = _find_col(rows, [
        "Asset_Mgr_Positions_Long_All", "asset_mgr_positions_long_all",
    ])
    asset_short_col = _find_col(rows, [
        "Asset_Mgr_Positions_Short_All", "asset_mgr_positions_short_all",
    ])
    oi_col = _find_col(rows, ["Open_Interest_All", "open_interest_all"])

    snapshots = []
    for _, row in rows.iterrows():
        date = row.get(date_col)
        if not date:
            continue

        lev_long = _to_int(row.get(lev_long_col, 0)) if lev_long_col else 0
        lev_short = _to_int(row.get(lev_short_col, 0)) if lev_short_col else 0
        asset_long = _to_int(row.get(asset_long_col, 0)) if asset_long_col else 0
        asset_short = _to_int(row.get(asset_short_col, 0)) if asset_short_col else 0
        open_interest = _to_int(row.get(oi_col, 0)) if oi_col else 0

        lev_net = lev_long - lev_short
        asset_net = asset_long - asset_short
        combined_net = lev_net + asset_net

        snapshots.append({
            "currency": currency,
            "lev_long": lev_long,
            "lev_short": lev_short,
            "lev_net": lev_net,
            "asset_long": asset_long,
            "asset_short": asset_short,
            "asset_net": asset_net,
            "combined_net": combined_net,
            "open_interest": open_interest,
        })

    # Sort ascending by date (date is in the "date" local var per iteration)
    # We need date alongside snapshots — zip approach:
    dates = rows[date_col].tolist()
    for i, snap in enumerate(snapshots):
        snap["report_date"] = dates[i] if i < len(dates) else None

    return sorted(
        [s for s in snapshots if s.get("report_date")],
        key=lambda x: x["report_date"]
    )


def _compute_cot_index(history: list[dict], lookback: int = 52) -> Optional[float]:
    """
    COT Index = (latest_net - min_net) / (max_net - min_net) * 100
    over a rolling lookback period of weekly snapshots.
    """
    if len(history) < 3:
        return None
    nets = [h["lev_net"] for h in history[-lookback:]]
    current = nets[-1]
    mn, mx = min(nets), max(nets)
    if mx == mn:
        return 50.0
    return round((current - mn) / (mx - mn) * 100, 1)


def _compute_weekly_change(history: list[dict]) -> Optional[int]:
    if len(history) < 2:
        return None
    return history[-1]["lev_net"] - history[-2]["lev_net"]


def refresh_cot_data() -> dict[str, bool]:
    """
    Download latest CFTC data and persist all currency snapshots to DB.
    Returns {currency: success_bool} mapping.
    """
    df = _download_cot_df()
    results: dict[str, bool] = {}

    if df is None:
        logger.error("Could not download COT data from CFTC")
        return {c: False for c in CURRENCY_CONTRACTS}

    for currency in CURRENCY_CONTRACTS:
        try:
            rows = _extract_currency_rows(df, currency)
            snapshots = _process_currency_df(currency, rows)
            for snap in snapshots:
                upsert_cot_snapshot(currency, snap["report_date"], snap)
            logger.info(f"Stored {len(snapshots)} COT records for {currency}")
            results[currency] = len(snapshots) > 0
        except Exception as e:
            logger.error(f"Error processing COT for {currency}: {e}")
            results[currency] = False

    return results


def get_cot_analysis(currency: str) -> dict:
    """
    Return a full COT analysis dict for a currency, enriched with
    COT index, weekly change, and bias signal.
    """
    history = get_cot_history(currency, limit=52)

    if not history:
        return {
            "currency": currency,
            "available": False,
            "history": [],
            "latest": None,
            "cot_index": None,
            "weekly_change": None,
            "bias": "NEUTRAL",
            "bias_strength": 0,
        }

    latest = history[-1]
    cot_index = _compute_cot_index(history)
    weekly_change = _compute_weekly_change(history)

    # Bias based on COT index
    if cot_index is not None:
        if cot_index >= 70:
            bias = "BULLISH"
            bias_strength = min(100, int(cot_index))
        elif cot_index <= 30:
            bias = "BEARISH"
            bias_strength = max(0, int(100 - cot_index))
        else:
            bias = "NEUTRAL"
            bias_strength = abs(int(cot_index - 50)) * 2
    else:
        bias = "NEUTRAL"
        bias_strength = 0

    return {
        "currency": currency,
        "available": True,
        "history": history,
        "latest": latest,
        "cot_index": cot_index,
        "weekly_change": weekly_change,
        "bias": bias,
        "bias_strength": bias_strength,
    }


def get_all_cot_analysis() -> dict[str, dict]:
    """Return COT analysis for all tracked currencies."""
    return {c: get_cot_analysis(c) for c in CURRENCY_CONTRACTS}
