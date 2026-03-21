# GreymatterAI — XAUUSD Autonomous Prop Desk

Fully autonomous trading system for XAUUSD. Four institutional-grade strategies, walk-forward optimisation, Monte Carlo stress testing, Telegram alerts, and a live dashboard.

## Architecture

```
greymatter_ai/
├── main.py                 # FastAPI + dashboard
├── config.py               # All settings & strategy params
├── database.py             # SQLAlchemy models (PostgreSQL)
├── data_fetcher.py         # Async Twelve Data client (M15/H1/H4/D1)
├── scheduler.py            # APScheduler (30-min heartbeat)
├── signal_orchestrator.py  # Central brain — collects, ranks, fires trades
├── risk_manager.py         # Kill-switches (8 losers, 2% DD, Sharpe)
├── alert_manager.py        # Telegram alerts
├── backtester.py           # Walk-forward backtest + Monte Carlo
├── optimiser.py            # 4-hour rolling parameter optimisation
├── liquidity_sweeps.py     # Strategy 1: Sweep + absorption
├── ema_pullbacks.py        # Strategy 2: EMA zone pullbacks
├── orb_breakout.py         # Strategy 3: ORB flagship
├── ema_momentum.py         # Strategy 4: HTF-aligned momentum
├── run_live.py             # Start the live system
├── run_backtest.py         # Backtest + Monte Carlo report
├── Dockerfile
└── railway.json
```

## Quick Start

### 1. Prerequisites

- Python 3.12+
- PostgreSQL database
- [Twelve Data API key](https://twelvedata.com) (free tier works)
- (Optional) Telegram bot token + chat ID

### 2. Install

```bash
cd greymatter_ai
pip install -r requirements.txt
cp .env.example .env
# Fill in .env with your keys
```

### 3. Run backtest first

```bash
python run_backtest.py
```

### 4. Go live

```bash
python run_live.py
```

Dashboard: http://localhost:8000

## Risk Rules

| Rule | Value |
|------|-------|
| Max daily drawdown | 2% of equity |
| Max risk per trade | 1% of equity (1R) |
| Max consecutive losers | 8 → auto-halt |
| Sharpe threshold | 0.5 → auto-halt |
| Max open positions | 1 (XAUUSD only) |

## Deploy to Railway

1. Push repo to GitHub
2. Create new Railway project → Deploy from GitHub
3. Add a PostgreSQL plugin
4. Set all env vars from `.env.example`
5. Railway auto-builds via `Dockerfile`

## No Lookahead Guarantee

Every strategy receives only fully-closed bars. The current incomplete bar is always dropped in `data_fetcher.py`. Entry signals fire on bar close; execution uses the **next bar open**. The backtester enforces this bar-by-bar.

## Disclaimer

This software is for educational and research purposes. Past backtest performance does not guarantee live results. Always paper-trade before deploying real capital.
