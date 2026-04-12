"""Configuration management for Upside - Polytracker."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TradingConfig:
    """Core trading parameters."""

    # Edge detection
    min_edge_pct: float = 10.0  # Minimum edge percentage to execute
    lag_threshold_pct: float = 10.0  # Polymarket odds lag vs CEX threshold
    confidence_threshold: float = 0.70  # Minimum confidence score

    # Position sizing
    max_position_pct: float = 4.0  # Max position as % of portfolio (conservative)
    kelly_fraction: float = 0.25  # Quarter-Kelly for conservative sizing
    min_market_liquidity: float = 50_000.0  # Min market liquidity in USDC

    # Risk management
    daily_drawdown_limit_pct: float = 20.0  # Daily loss limit - halt trading
    total_drawdown_kill_pct: float = 40.0  # Total drawdown - kill switch
    max_open_positions: int = 5

    # Paper trading (default ON - three flags required for live)
    paper_mode: bool = True
    live_flag_1: bool = False  # --live
    live_flag_2: bool = False  # --confirm-live
    live_flag_3: bool = False  # --accept-risk

    @property
    def is_live(self) -> bool:
        return (
            not self.paper_mode
            and self.live_flag_1
            and self.live_flag_2
            and self.live_flag_3
        )


@dataclass
class APIConfig:
    """API credentials and endpoints - loaded from environment."""

    # Polymarket
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_passphrase: str = ""
    polymarket_wallet_address: str = ""
    polymarket_private_key: str = ""
    polymarket_base_url: str = "https://clob.polymarket.com"

    # Binance WebSocket
    binance_ws_url: str = "wss://stream.binance.com:9443"

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Polygon RPC
    polygon_rpc_url: str = "https://polygon-rpc.com"

    @classmethod
    def from_env(cls) -> "APIConfig":
        return cls(
            polymarket_api_key=os.getenv("POLYMARKET_API_KEY", ""),
            polymarket_api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
            polymarket_passphrase=os.getenv("POLYMARKET_PASSPHRASE", ""),
            polymarket_wallet_address=os.getenv("POLYMARKET_WALLET_ADDRESS", ""),
            polymarket_private_key=os.getenv("POLYMARKET_PRIVATE_KEY", ""),
            polymarket_base_url=os.getenv(
                "POLYMARKET_BASE_URL", "https://clob.polymarket.com"
            ),
            binance_ws_url=os.getenv(
                "BINANCE_WS_URL", "wss://stream.binance.com:9443"
            ),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            polygon_rpc_url=os.getenv(
                "POLYGON_RPC_URL", "https://polygon-rpc.com"
            ),
        )


@dataclass
class BotConfig:
    """Top-level bot configuration."""

    trading: TradingConfig = field(default_factory=TradingConfig)
    api: APIConfig = field(default_factory=APIConfig)

    # Database
    db_path: str = str(Path(__file__).parent.parent / "polytracker_trades.db")

    # Dashboard refresh rate (seconds)
    dashboard_refresh: float = 2.0

    # Monitored contract timeframes
    timeframes: list[str] = field(
        default_factory=lambda: ["5m", "15m"]
    )

    # Monitored assets
    assets: list[str] = field(
        default_factory=lambda: ["BTC", "ETH"]
    )

    # Rate limiting
    api_rate_limit: float = 0.1  # Min seconds between API calls
    ws_reconnect_delay: float = 5.0  # Seconds before WS reconnect
    max_retries: int = 3

    @classmethod
    def load(cls) -> "BotConfig":
        return cls(
            trading=TradingConfig(),
            api=APIConfig.from_env(),
        )
