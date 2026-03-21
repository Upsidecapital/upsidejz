"""
GreymatterAI — Walk-Forward Backtester + Monte Carlo
Strict no-lookahead: signals are generated bar-by-bar on closed bars,
executed at the OPEN of the NEXT bar.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    entry_bar: int
    direction: str          # "long" | "short"
    entry_price: float
    stop_loss: float
    take_profit: float
    exit_bar: Optional[int] = None
    exit_price: Optional[float] = None
    pnl_r: float = 0.0
    pnl_usd: float = 0.0
    strategy: str = ""


@dataclass
class BacktestResult:
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl_r > 0)
        return wins / len(self.trades)

    @property
    def avg_r(self) -> float:
        if not self.trades:
            return 0.0
        return float(np.mean([t.pnl_r for t in self.trades]))

    @property
    def max_drawdown_pct(self) -> float:
        curve = np.array(self.equity_curve)
        if len(curve) < 2:
            return 0.0
        peak = np.maximum.accumulate(curve)
        dd = (curve - peak) / peak
        return float(dd.min())

    @property
    def sharpe(self) -> float:
        if len(self.equity_curve) < 2:
            return 0.0
        returns = np.diff(self.equity_curve) / self.equity_curve[:-1]
        if returns.std() == 0:
            return 0.0
        return float(returns.mean() / returns.std() * np.sqrt(252))

    def summary(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate * 100, 1),
            "avg_r": round(self.avg_r, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct * 100, 2),
            "sharpe": round(self.sharpe, 2),
            "final_equity": round(self.equity_curve[-1], 2) if self.equity_curve else 0,
        }


def _simulate_trade(
    bars: pd.DataFrame,
    entry_bar: int,
    direction: str,
    stop_loss: float,
    take_profit: float,
    strategy: str,
    risk_usd: float,
) -> BacktestTrade:
    """Simulate trade execution bar-by-bar after entry bar (no lookahead)."""
    entry_price = float(bars.iloc[entry_bar]["open"])  # next bar open
    risk_dist = abs(entry_price - stop_loss)
    if risk_dist < 1e-6:
        risk_dist = 1.0
    lot = risk_usd / risk_dist

    for i in range(entry_bar, len(bars)):
        bar = bars.iloc[i]
        if direction == "long":
            if float(bar["low"]) <= stop_loss:
                pnl = (stop_loss - entry_price) * lot
                return BacktestTrade(entry_bar, direction, entry_price, stop_loss, take_profit,
                                     exit_bar=i, exit_price=stop_loss,
                                     pnl_r=-1.0, pnl_usd=pnl, strategy=strategy)
            if float(bar["high"]) >= take_profit:
                pnl = (take_profit - entry_price) * lot
                r = pnl / risk_usd
                return BacktestTrade(entry_bar, direction, entry_price, stop_loss, take_profit,
                                     exit_bar=i, exit_price=take_profit,
                                     pnl_r=r, pnl_usd=pnl, strategy=strategy)
        else:  # short
            if float(bar["high"]) >= stop_loss:
                pnl = (entry_price - stop_loss) * lot
                return BacktestTrade(entry_bar, direction, entry_price, stop_loss, take_profit,
                                     exit_bar=i, exit_price=stop_loss,
                                     pnl_r=-1.0, pnl_usd=pnl, strategy=strategy)
            if float(bar["low"]) <= take_profit:
                pnl = (entry_price - take_profit) * lot
                r = pnl / risk_usd
                return BacktestTrade(entry_bar, direction, entry_price, stop_loss, take_profit,
                                     exit_bar=i, exit_price=take_profit,
                                     pnl_r=r, pnl_usd=pnl, strategy=strategy)

    # Timed out — close at last bar
    last = bars.iloc[-1]
    close_price = float(last["close"])
    pnl = (close_price - entry_price) * lot if direction == "long" else (entry_price - close_price) * lot
    return BacktestTrade(entry_bar, direction, entry_price, stop_loss, take_profit,
                         exit_bar=len(bars) - 1, exit_price=close_price,
                         pnl_r=pnl / risk_usd, pnl_usd=pnl, strategy=strategy)


def run_backtest(
    m15: pd.DataFrame,
    h1: pd.DataFrame,
    h4: pd.DataFrame,
    d1: pd.DataFrame,
    initial_equity: float = 200_000,
    risk_pct: float = 0.01,
    max_bars: Optional[int] = None,
) -> BacktestResult:
    """
    Walk-forward backtest across all 4 strategies.
    Each bar: generate signal on bar close, enter on next bar open.
    """
    import ema_momentum as em
    import ema_pullbacks as ep
    import liquidity_sweeps as ls
    import orb_breakout as orb

    result = BacktestResult()
    equity = initial_equity
    result.equity_curve.append(equity)

    n = min(len(m15), max_bars or len(m15))
    active_trade: Optional[BacktestTrade] = None
    active_exit_bar: int = -1

    for i in range(100, n - 1):
        # Close active trade if exit bar reached
        if active_trade and active_trade.exit_bar is not None and i >= active_trade.exit_bar:
            equity += active_trade.pnl_usd
            result.trades.append(active_trade)
            result.equity_curve.append(equity)
            active_trade = None

        if active_trade is not None:
            continue  # one trade at a time

        # Slice data up to and including bar i (no lookahead)
        m15_slice = m15.iloc[:i + 1].reset_index(drop=True)
        h1_slice = h1.iloc[:min(len(h1), (i // 4) + 1)].reset_index(drop=True)
        h4_slice = h4.iloc[:min(len(h4), (i // 16) + 1)].reset_index(drop=True)
        d1_slice = d1.iloc[:min(len(d1), (i // 96) + 1)].reset_index(drop=True)

        risk_usd = equity * risk_pct

        # Try strategies in conviction order (ORB first as flagship)
        signal = None
        strat = None
        for strategy_fn, args, name in [
            (orb.detect, (m15_slice,), "orb_breakout"),
            (ls.detect, (m15_slice, d1_slice), "liquidity_sweep"),
            (ep.detect, (m15_slice, h1_slice), "ema_pullback"),
            (em.detect, (m15_slice, h4_slice, d1_slice), "ema_momentum"),
        ]:
            try:
                sig = strategy_fn(*args)
                if sig:
                    signal = sig
                    strat = name
                    break
            except Exception:
                pass

        if signal is None:
            continue

        # Execute at next bar open (i+1)
        entry_bar = i + 1
        active_trade = _simulate_trade(
            bars=m15.iloc[entry_bar:].reset_index(drop=True),
            entry_bar=0,
            direction=signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction),
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            strategy=strat or "",
            risk_usd=risk_usd,
        )
        if active_trade.exit_bar is not None:
            active_trade.exit_bar += entry_bar  # convert to absolute bar index

    # Close any remaining open trade
    if active_trade:
        equity += active_trade.pnl_usd
        result.trades.append(active_trade)
        result.equity_curve.append(equity)

    logger.info("Backtest complete: %d trades | WR=%.1f%% | Sharpe=%.2f | MaxDD=%.1f%%",
                result.total_trades, result.win_rate * 100, result.sharpe,
                result.max_drawdown_pct * 100)
    return result


def monte_carlo(
    result: BacktestResult,
    n_simulations: int = 10_000,
    initial_equity: float = 200_000,
) -> dict:
    """
    Monte Carlo simulation: randomly reshuffle trade outcomes to stress-test robustness.
    Returns percentile statistics of final equity and max drawdown.
    """
    if not result.trades:
        return {}

    r_series = np.array([t.pnl_r for t in result.trades])
    final_equities = []
    max_dds = []

    risk_per_trade = initial_equity * 0.01  # 1% per trade

    rng = np.random.default_rng(42)
    for _ in range(n_simulations):
        shuffled = rng.choice(r_series, size=len(r_series), replace=True)
        equity = initial_equity
        peak = equity
        max_dd = 0.0
        for r in shuffled:
            equity += r * risk_per_trade
            peak = max(peak, equity)
            dd = (equity - peak) / peak
            max_dd = min(max_dd, dd)
        final_equities.append(equity)
        max_dds.append(max_dd)

    fe = np.array(final_equities)
    mdd = np.array(max_dds)

    return {
        "final_equity": {
            "p5": round(float(np.percentile(fe, 5)), 0),
            "p25": round(float(np.percentile(fe, 25)), 0),
            "median": round(float(np.median(fe)), 0),
            "p75": round(float(np.percentile(fe, 75)), 0),
            "p95": round(float(np.percentile(fe, 95)), 0),
        },
        "max_drawdown_pct": {
            "p5": round(float(np.percentile(mdd, 5)) * 100, 2),
            "median": round(float(np.median(mdd)) * 100, 2),
            "p95": round(float(np.percentile(mdd, 95)) * 100, 2),
        },
        "ruin_probability": round(float(np.mean(fe < initial_equity * 0.5)) * 100, 2),
    }
