# Upside - Polytracker · Quick Start

Polymarket latency arbitrage bot with a native desktop dashboard.

## For End Users (clients)

### Windows
1. Make sure Python 3.11+ is installed from https://www.python.org/downloads/
   (check **"Add python.exe to PATH"** during install)
2. Double-click **`Polytracker.bat`**
3. The bot installs itself the first time, then opens a native desktop
   window with the live dashboard

That's it. The window looks like a real application — not a browser tab.
To stop the bot, close the window or press Ctrl+C in the terminal.

### macOS / Linux
1. Make sure Python 3.11+ is installed
2. Double-click **`Polytracker.command`**
   (on macOS, right-click → Open the first time to bypass Gatekeeper)
3. The bot installs itself, then opens the dashboard in a native window

## For Developers

### Manual run from the command line
```bash
python launcher.py                     # native desktop window
python launcher.py --no-browser        # headless (dashboard at :8787)
python launcher.py --portfolio 1000
python launcher.py --live --confirm-live --accept-risk   # LIVE mode
```

### Run the terminal version (no native window)
```bash
python -m polytracker                  # just the bot + browser dashboard
```

## Shipping a Single `.exe` to Clients

Build a standalone Windows executable with **no Python required** on the
target machine:

```bash
build_exe.bat
```

This produces **`dist/Polytracker.exe`** — a single file you can email,
zip, or drop on a USB drive. Clients double-click it and the dashboard
opens. They don't need Python, pip, or anything installed.

First launch on a fresh Windows machine may show a SmartScreen warning
(the binary isn't code-signed). Click "More info" → "Run anyway".

To sign it for a production release, buy a code signing certificate
(e.g. DigiCert) and sign with `signtool`.

## File Structure

```
upsidejz/
├── Polytracker.bat          ← Windows double-click launcher
├── Polytracker.command      ← macOS/Linux double-click launcher
├── launcher.py              ← Python entry (native desktop window)
├── build_exe.bat            ← Build standalone .exe
├── polytracker.spec         ← PyInstaller config
├── polytracker/             ← The bot package
│   ├── bot.py               ← Main orchestrator
│   ├── binance_feed.py      ← Binance WebSocket feed
│   ├── polymarket_client.py ← Polymarket CLOB API
│   ├── strategy.py          ← Arbitrage edge detection
│   ├── kelly.py             ← Half-Kelly sizing
│   ├── risk_manager.py      ← Kill switch + drawdown
│   ├── trade_logger.py      ← SQLite trade history
│   ├── telegram_alerts.py   ← Telegram notifications
│   └── web/                 ← FastAPI dashboard
│       ├── server.py        ← FastAPI + WebSocket
│       ├── state.py         ← Pub/sub state store
│       └── templates/ + static/
└── .env                     ← Credentials (auto-created)
```

## Configuration

On first run a `.env` file is created from the template. Edit it to add:

- `POLYMARKET_*` — API credentials + wallet (for LIVE mode only)
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — for alerts

PAPER mode (default) works without any credentials.

## Safety

- **Paper mode is the default.** Three explicit flags required to enable
  live trading: `--live --confirm-live --accept-risk`
- Kill switch halts at -20% daily drawdown
- Total drawdown kill switch at -40%
- Max position size: 8% of portfolio
- Half-Kelly sizing
- Per the guide: **run paper mode for a week before going live**
