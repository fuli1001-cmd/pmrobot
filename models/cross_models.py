"""Data models for cross-platform arbitrage."""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from exchanges.base import BetResult, Platform


class CrossPlatformStrategy(Enum):
    """Cross-platform arbitrage strategy type."""
    BINARY_HEDGE = "binary_hedge"       # Buy YES on one, NO on the other
    NEG_RISK_CROSS = "neg_risk_cross"   # Multi-outcome best-price picking
    SHORT_CROSS = "short_cross"         # Mint+sell on PM, hedge on Azuro


@dataclass
class CrossPlatformOpportunity:
    """A detected cross-platform arbitrage opportunity.

    The opportunity pairs a YES buy on one platform with a NO buy on
    the other, such that total cost < 1.0 (guaranteed profit).
    """

    # Aligned market identifiers
    pm_market_id: str
    az_market_id: str
    pm_question: str
    az_question: str

    # Strategy
    strategy: CrossPlatformStrategy = CrossPlatformStrategy.BINARY_HEDGE

    # Which platform to buy YES on, which to buy NO on
    yes_platform: Platform = Platform.POLYMARKET
    no_platform: Platform = Platform.AZURO

    # Prices (probability / price-per-share)
    price_yes: float = 0.0   # Price to buy YES on yes_platform
    price_no: float = 0.0    # Price to buy NO on no_platform
    total_cost: float = 0.0  # price_yes + price_no

    # Profit
    gross_profit_pct: float = 0.0  # (1 - total_cost) / total_cost
    estimated_fees: float = 0.0     # Gas + platform fees
    net_profit_pct: float = 0.0     # After fees

    # Sizing
    trade_size_usdc: float = 0.0

    # Metadata
    timestamp: float = field(default_factory=time.time)

    @property
    def spread(self) -> float:
        """Dollar spread per share: 1.0 - total_cost."""
        return 1.0 - self.total_cost

    def is_profitable(self, threshold: float = 0.03) -> bool:
        """Check if opportunity exceeds the minimum profit threshold."""
        return self.net_profit_pct >= threshold


@dataclass
class CrossExecutionReport:
    """Report of a cross-platform arbitrage execution attempt."""

    class Result(Enum):
        SUCCESS = "success"       # Both legs filled
        PARTIAL = "partial"       # One leg filled, other failed
        FAILED = "failed"         # Both legs failed
        SKIPPED = "skipped"       # Dry-run

    result: Result
    opportunity: CrossPlatformOpportunity
    yes_bet: Optional[BetResult] = None
    no_bet: Optional[BetResult] = None
    execution_time_ms: float = 0.0
    error_message: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.result == self.Result.SUCCESS
