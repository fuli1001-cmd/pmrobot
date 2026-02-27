"""Settings configuration using Pydantic."""

from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Polymarket API Credentials
    # Polymarket API Credentials (Optional if deriving from Private Key)
    polymarket_api_key: Optional[str] = Field(None, description="Polymarket CLOB API Key")
    polymarket_api_secret: Optional[str] = Field(None, description="Polymarket CLOB API Secret")
    polymarket_passphrase: Optional[str] = Field(None, description="Polymarket CLOB Passphrase")

    # Wallet Configuration
    private_key: Optional[str] = Field(None, description="Wallet private key for signing (optional)")
    proxy_wallet_address: Optional[str] = Field(
        None, description="Gnosis Safe proxy wallet address"
    )

    # Builder / Relayer Credentials (for gasless merge & mint)
    builder_api_key: Optional[str] = Field(None, description="Polymarket Builder API Key")
    builder_secret: Optional[str] = Field(None, description="Polymarket Builder API Secret")
    builder_passphrase: Optional[str] = Field(None, description="Polymarket Builder Passphrase")
    relayer_tx_type: str = Field(
        "PROXY", description="Relayer wallet type: SAFE or PROXY"
    )

    # Polygon RPC
    polygon_rpc_url: str = Field(
        "https://polygon-rpc.com", description="Polygon mainnet RPC URL"
    )
    polygon_testnet_rpc_url: str = Field(
        "https://rpc-amoy.polygon.technology", description="Polygon testnet RPC URL"
    )

    # Trading Parameters
    profit_threshold: float = Field(
        0.008, ge=0.001, le=0.1, description="Minimum profit threshold (0.8%)"
    )
    single_trade_size: float = Field(
        100.0, ge=1.0, le=10000.0, description="Single trade size in USDC"
    )
    max_slippage: float = Field(
        0.002, ge=0.0001, le=0.05, description="Maximum allowed slippage (0.2%)"
    )
    merge_interval: int = Field(
        600, ge=60, le=3600, description="Merge interval in seconds"
    )

    # Rate Limiting
    api_rate_limit: int = Field(10, ge=1, le=100, description="API requests per second")

    # Telegram Notifications
    telegram_bot_token: Optional[str] = Field(None, description="Telegram bot token")
    telegram_chat_id: Optional[str] = Field(None, description="Telegram chat ID")
    
    # WeChat Notifications
    wechat_webhook_url: Optional[str] = Field(None, description="Enterprise WeChat Webhook URL")

    # Market Refresh
    market_refresh_interval: int = Field(
        1800, ge=0, le=86400, description="Market refresh interval in seconds (0 to disable)"
    )

    # Environment
    env: str = Field("production", description="Environment: production or testnet")
    dry_run: bool = Field(False, description="Dry run mode (no real trades)")
    log_level: str = Field("INFO", description="Logging level")

    # Removed validate_private_key as private_key is now optional

    @field_validator("env")
    @classmethod
    def validate_env(cls, v: str) -> str:
        """Validate environment value."""
        allowed = ["production", "testnet", "development"]
        if v.lower() not in allowed:
            raise ValueError(f"env must be one of {allowed}")
        return v.lower()

    @property
    def is_testnet(self) -> bool:
        """Check if running on testnet."""
        return self.env == "testnet"

    @property
    def rpc_url(self) -> str:
        """Get the appropriate RPC URL based on environment."""
        return self.polygon_testnet_rpc_url if self.is_testnet else self.polygon_rpc_url

    @property
    def notifications_enabled(self) -> bool:
        """Check if any notifications are configured."""
        return bool((self.telegram_bot_token and self.telegram_chat_id) or self.wechat_webhook_url)


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
