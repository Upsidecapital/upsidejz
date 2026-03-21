"""
GreymatterAI — Walk-Forward Optimiser
Runs every 4 hours. Re-fits strategy parameters on a rolling 30-day window
of the latest M15 bars and updates STRATEGY_CONFIGS in-place.
Grid search with Sharpe as the objective — no future data ever used.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from itertools import product
from typing import Optional

import numpy as np
import pandas as pd

import config
from config import OPTIMIZATION_LOOKBACK_DAYS, STRATEGY_CONFIGS

logger = logging.getLogger(__name__)

# Grid definitions — extend as needed
_ORB_GRID = {
    "ib_minutes": [15, 30],
    "atr_compression_ratio": [0.5, 0.6, 0.7],
    "volume_breakout_mult": [1.3, 1.5, 1.8],
}
_LS_GRID = {
    "volume_spike_multiplier": [1.5, 1.8, 2.2],
    "absorption_rejection_pct": [0.30, 0.40, 0.50],
}


async def run_walk_forward() -> None:
    """Fetch latest data, optimise all strategies, update live config."""
    logger.info("Walk-forward optimisation starting…")
    from data_fetcher import DataFetcher
    fetcher = DataFetcher()
    try:
        data = await fetcher.refresh()
    finally:
        await fetcher.close()

    m15 = data.get("15min")
    h1 = data.get("1h")
    h4 = data.get("4h")
    d1 = data.get("1day")
    if m15 is None or d1 is None:
        logger.warning("Optimisation skipped: missing data")
        return

    # Limit to rolling lookback window
    lookback_bars = OPTIMIZATION_LOOKBACK_DAYS * 96  # 96 M15 bars per day
    m15 = m15.iloc[-lookback_bars:].reset_index(drop=True)
    h1 = h1.iloc[-OPTIMIZATION_LOOKBACK_DAYS * 24:].reset_index(drop=True) if h1 is not None else None
    h4 = h4.iloc[-OPTIMIZATION_LOOKBACK_DAYS * 6:].reset_index(drop=True) if h4 is not None else None
    d1 = d1.iloc[-OPTIMIZATION_LOOKBACK_DAYS:].reset_index(drop=True)

    # --- ORB optimisation ---
    best_orb_sharpe = -np.inf
    best_orb_cfg = STRATEGY_CONFIGS.orb_breakout
    for ib, comp, vol in product(
        _ORB_GRID["ib_minutes"],
        _ORB_GRID["atr_compression_ratio"],
        _ORB_GRID["volume_breakout_mult"],
    ):
        trial = replace(
            STRATEGY_CONFIGS.orb_breakout,
            ib_minutes=ib,
            atr_compression_ratio=comp,
            volume_breakout_mult=vol,
        )
        sharpe = _quick_backtest_orb(m15, trial)
        if sharpe > best_orb_sharpe:
            best_orb_sharpe = sharpe
            best_orb_cfg = trial

    config.STRATEGY_CONFIGS.orb_breakout = best_orb_cfg
    logger.info("ORB optimised: sharpe=%.2f ib=%d comp=%.1f vol=%.1f",
                best_orb_sharpe, best_orb_cfg.ib_minutes,
                best_orb_cfg.atr_compression_ratio, best_orb_cfg.volume_breakout_mult)

    # --- Liquidity Sweep optimisation ---
    best_ls_sharpe = -np.inf
    best_ls_cfg = STRATEGY_CONFIGS.liquidity_sweep
    for vol_mult, rej_pct in product(
        _LS_GRID["volume_spike_multiplier"],
        _LS_GRID["absorption_rejection_pct"],
    ):
        trial = replace(
            STRATEGY_CONFIGS.liquidity_sweep,
            volume_spike_multiplier=vol_mult,
            absorption_rejection_pct=rej_pct,
        )
        sharpe = _quick_backtest_ls(m15, d1, trial)
        if sharpe > best_ls_sharpe:
            best_ls_sharpe = sharpe
            best_ls_cfg = trial

    config.STRATEGY_CONFIGS.liquidity_sweep = best_ls_cfg
    logger.info("LS optimised: sharpe=%.2f vol_mult=%.1f rej_pct=%.2f",
                best_ls_sharpe, best_ls_cfg.volume_spike_multiplier,
                best_ls_cfg.absorption_rejection_pct)

    # Trigger Sharpe kill if overall performance has degraded
    from risk_manager import RiskManager
    # (RiskManager instance is owned by scheduler; log only here)
    if max(best_orb_sharpe, best_ls_sharpe) < config.MIN_SHARPE_THRESHOLD:
        logger.warning("Sharpe below threshold after optimisation — consider halting")


def _quick_backtest_orb(m15: pd.DataFrame, cfg) -> float:
    """Mini walk-forward on ORB only, returns Sharpe."""
    import orb_breakout
    r_list = []
    for i in range(60, len(m15) - 1):
        try:
            slice_ = m15.iloc[:i + 1].reset_index(drop=True)
            sig = orb_breakout.detect(slice_, cfg)
            if sig:
                r_list.append(_sim_r(m15, i + 1, sig))
        except Exception:
            pass
    return _sharpe(r_list)


def _quick_backtest_ls(m15: pd.DataFrame, d1: pd.DataFrame, cfg) -> float:
    """Mini walk-forward on LiquiditySweeps only, returns Sharpe."""
    import liquidity_sweeps
    r_list = []
    for i in range(60, len(m15) - 1):
        try:
            m_sl = m15.iloc[:i + 1].reset_index(drop=True)
            d_sl = d1.iloc[:min(len(d1), i // 96 + 1)].reset_index(drop=True)
            sig = liquidity_sweeps.detect(m_sl, d_sl, cfg)
            if sig:
                r_list.append(_sim_r(m15, i + 1, sig))
        except Exception:
            pass
    return _sharpe(r_list)


def _sim_r(bars: pd.DataFrame, entry_bar: int, sig) -> float:
    """Simplified 1-bar resolution SL/TP simulation. Returns R."""
    if entry_bar >= len(bars):
        return 0.0
    entry = float(bars.iloc[entry_bar]["open"])
    direction = sig.direction.value if hasattr(sig.direction, "value") else str(sig.direction)
    risk = abs(entry - sig.stop_loss)
    if risk < 1e-6:
        return 0.0
    for i in range(entry_bar, min(entry_bar + 200, len(bars))):
        bar = bars.iloc[i]
        if direction == "long":
            if float(bar["low"]) <= sig.stop_loss:
                return -1.0
            if float(bar["high"]) >= sig.take_profit:
                return abs(sig.take_profit - entry) / risk
        else:
            if float(bar["high"]) >= sig.stop_loss:
                return -1.0
            if float(bar["low"]) <= sig.take_profit:
                return abs(entry - sig.take_profit) / risk
    return 0.0


def _sharpe(r_list: list) -> float:
    if len(r_list) < 5:
        return -99.0
    arr = np.array(r_list, dtype=float)
    std = arr.std()
    if std == 0:
        return 0.0
    return float(arr.mean() / std * np.sqrt(252))
