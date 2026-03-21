"""
GreymatterAI — Backtest + Monte Carlo Report
Usage:
    python run_backtest.py

Fetches latest 30 days of M15/H1/H4/D1 data, runs the walk-forward
backtest across all 4 strategies, then runs 10,000 Monte Carlo simulations.
Prints a full performance report to stdout.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

logging.basicConfig(level=logging.WARNING)  # silence strategy logs during BT

from backtester import monte_carlo, run_backtest
from config import ACCOUNT_SIZE_USD, MIN_BARS
from data_fetcher import DataFetcher


async def main() -> None:
    print("\n=== GreymatterAI — Backtest Report ===\n")
    fetcher = DataFetcher()
    print("Fetching market data…")
    try:
        data = await fetcher.refresh()
    finally:
        await fetcher.close()

    m15 = data.get("15min")
    h1 = data.get("1h")
    h4 = data.get("4h")
    d1 = data.get("1day")

    if any(df is None for df in (m15, h1, h4, d1)):
        print("ERROR: Could not fetch all timeframes. Check your TWELVE_DATA_API_KEY.")
        sys.exit(1)

    print(f"Data loaded: M15={len(m15)} bars | H1={len(h1)} | H4={len(h4)} | D1={len(d1)}")
    print("Running walk-forward backtest (no lookahead)…\n")

    result = run_backtest(m15, h1, h4, d1, initial_equity=ACCOUNT_SIZE_USD)
    summary = result.summary()

    print("--- Performance Summary ---")
    print(f"  Total Trades  : {summary['total_trades']}")
    print(f"  Win Rate      : {summary['win_rate']}%")
    print(f"  Avg R         : {summary['avg_r']}")
    print(f"  Sharpe Ratio  : {summary['sharpe']}")
    print(f"  Max Drawdown  : {summary['max_drawdown_pct']}%")
    print(f"  Final Equity  : ${summary['final_equity']:,.0f}")

    if result.trades:
        print("\n--- Last 10 Trades ---")
        for t in result.trades[-10:]:
            sign = "+" if t.pnl_r >= 0 else ""
            print(f"  {t.strategy:<20} {t.direction:<6} {sign}{t.pnl_r:+.2f}R  ${t.pnl_usd:+,.0f}")

    if result.total_trades >= 20:
        print("\nRunning Monte Carlo (10,000 simulations)…")
        mc = monte_carlo(result, n_simulations=10_000, initial_equity=ACCOUNT_SIZE_USD)
        print("\n--- Monte Carlo Results ---")
        fe = mc["final_equity"]
        mdd = mc["max_drawdown_pct"]
        print(f"  Final Equity (p5/median/p95): ${fe['p5']:,.0f} / ${fe['median']:,.0f} / ${fe['p95']:,.0f}")
        print(f"  Max Drawdown (p5/median/p95): {mdd['p5']}% / {mdd['median']}% / {mdd['p95']}%")
        print(f"  Ruin Probability (equity < 50%): {mc['ruin_probability']}%")
    else:
        print("\n(Monte Carlo requires ≥ 20 trades — run with more data)")

    print("\n=== Report Complete ===\n")


if __name__ == "__main__":
    asyncio.run(main())
