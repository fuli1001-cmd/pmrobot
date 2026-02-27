"""Risk management module."""

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, Dict, Optional

from config.settings import get_settings
from models.order import ArbitrageOpportunity
from utils.logger import get_logger
from utils.notifier import create_notifier

logger = get_logger(__name__)


@dataclass
class TradeStats:
    """Statistics for tracking trade performance."""
    total_trades: int = 0
    successful_trades: int = 0
    simulated_trades: int = 0  # Dry-run mode simulated trades
    partial_fills: int = 0
    failed_trades: int = 0
    total_profit_usdc: float = 0.0
    simulated_profit_usdc: float = 0.0  # Dry-run mode simulated profit
    total_loss_usdc: float = 0.0
    last_trade_time: Optional[datetime] = None

    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        if self.total_trades == 0:
            return 0.0
        return self.successful_trades / self.total_trades

    @property
    def net_profit(self) -> float:
        """Calculate net profit."""
        return self.total_profit_usdc - self.total_loss_usdc


@dataclass
class RiskConfig:
    """Risk management configuration."""
    # Maximum consecutive failures before pausing
    max_consecutive_failures: int = 5
    # Pause duration after max failures (seconds)
    pause_duration: int = 300
    # Maximum daily losses before stopping
    max_daily_loss: float = 100.0
    # Slippage threshold for opportunity invalidation
    max_slippage: float = 0.02
    # Time window for opportunity validity (seconds)
    opportunity_ttl: float = 1.0
    # Minimum time between trades on same market (seconds)
    market_cooldown: float = 5.0


class RiskManager:
    """
    Manages trading risk and prevents excessive losses.
    """

    def __init__(self, config: Optional[RiskConfig] = None):
        """
        Initialize the risk manager.

        Args:
            config: Risk configuration
        """
        self.config = config or RiskConfig()
        self.stats = TradeStats()
        self._consecutive_failures = 0
        self._paused_until: Optional[float] = None
        self._daily_start = datetime.now().date()
        self._daily_loss = 0.0
        self._market_last_trade: Dict[str, float] = {}
        self._recent_opportunities: Deque[float] = deque(maxlen=100)

        # Initialize notifier
        settings = get_settings()
        self.notifier = create_notifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        )

        logger.info(
            "Risk manager initialized",
            max_failures=self.config.max_consecutive_failures,
            max_daily_loss=self.config.max_daily_loss,
        )

    def can_trade(self) -> bool:
        """
        Check if trading is currently allowed.

        Returns:
            True if trading is allowed
        """
        # Check if paused
        if self._paused_until and time.time() < self._paused_until:
            remaining = self._paused_until - time.time()
            logger.debug("Trading paused", remaining_seconds=remaining)
            return False

        # Reset daily stats if new day
        today = datetime.now().date()
        if today != self._daily_start:
            self._daily_start = today
            self._daily_loss = 0.0
            logger.info("Daily stats reset")

        # Check daily loss limit
        if self._daily_loss >= self.config.max_daily_loss:
            logger.warning(
                "Daily loss limit reached",
                daily_loss=self._daily_loss,
                limit=self.config.max_daily_loss,
            )
            return False

        return True

    def can_trade_market(self, condition_id: str) -> bool:
        """
        Check if a specific market can be traded (cooldown check).

        Args:
            condition_id: Market condition ID

        Returns:
            True if market can be traded
        """
        if not self.can_trade():
            return False

        last_trade = self._market_last_trade.get(condition_id, 0)
        if time.time() - last_trade < self.config.market_cooldown:
            logger.debug(
                "Market on cooldown",
                condition_id=condition_id[:8],
                cooldown=self.config.market_cooldown,
            )
            return False

        return True

    def validate_opportunity(
        self,
        opportunity: ArbitrageOpportunity,
        current_price_yes: float,
        current_price_no: float,
    ) -> bool:
        """
        Validate opportunity is still valid given current prices.

        Args:
            opportunity: Original opportunity
            current_price_yes: Current Yes ask price
            current_price_no: Current No ask price

        Returns:
            True if opportunity is still valid
        """
        # Check TTL
        age = time.time() - opportunity.timestamp
        if age > self.config.opportunity_ttl:
            logger.debug("Opportunity expired", age=age, ttl=self.config.opportunity_ttl)
            return False

        # Check price deviation (slippage)
        deviation_yes = abs(current_price_yes - opportunity.avg_price_yes) / opportunity.avg_price_yes
        deviation_no = abs(current_price_no - opportunity.avg_price_no) / opportunity.avg_price_no

        if deviation_yes > self.config.max_slippage or deviation_no > self.config.max_slippage:
            logger.debug(
                "Price deviation too high",
                deviation_yes=f"{deviation_yes:.4f}",
                deviation_no=f"{deviation_no:.4f}",
            )
            return False

        return True

    async def record_success(
        self,
        opportunity: ArbitrageOpportunity,
        profit_usdc: float,
        is_simulated: bool = False,
    ) -> None:
        """
        Record a successful trade.

        Args:
            opportunity: Executed opportunity
            profit_usdc: Actual profit in USDC
            is_simulated: True if this is a dry-run simulated trade
        """
        self.stats.total_trades += 1
        self.stats.last_trade_time = datetime.now()

        # Compatible with both ArbitrageOpportunity (.market) and NegativeRiskArbitrageOpportunity (.event)
        if hasattr(opportunity, 'market'):
            market_id = opportunity.market.condition_id
        elif hasattr(opportunity, 'event'):
            market_id = opportunity.event.event_id
        else:
            market_id = "unknown"
        self._market_last_trade[market_id] = time.time()

        if is_simulated:
            self.stats.simulated_trades += 1
            self.stats.simulated_profit_usdc += profit_usdc
            logger.info(
                "Simulated trade recorded",
                profit=f"${profit_usdc:.2f}",
                total_simulated=self.stats.simulated_trades,
                simulated_profit=f"${self.stats.simulated_profit_usdc:.2f}",
            )
        else:
            self.stats.successful_trades += 1
            self.stats.total_profit_usdc += profit_usdc
            self._consecutive_failures = 0
            logger.info(
                "Trade successful",
                profit=f"${profit_usdc:.2f}",
                total_profit=f"${self.stats.total_profit_usdc:.2f}",
                success_rate=f"{self.stats.success_rate:.2%}",
            )

    async def record_failure(
        self,
        opportunity: ArbitrageOpportunity,
        is_partial: bool = False,
        loss_usdc: float = 0.0,
    ) -> None:
        """
        Record a failed or partial trade.

        Args:
            opportunity: Failed opportunity
            is_partial: True if partial fill (one side filled)
            loss_usdc: Loss amount in USDC (for partial fills)
        """
        self.stats.total_trades += 1
        self.stats.last_trade_time = datetime.now()

        if is_partial:
            self.stats.partial_fills += 1
            self.stats.total_loss_usdc += loss_usdc
            self._daily_loss += loss_usdc
            self._consecutive_failures += 1

            # Compatible with both ArbitrageOpportunity (.market) and NegativeRiskArbitrageOpportunity (.event)
            if hasattr(opportunity, 'market'):
                market_label = opportunity.market.slug[:30]
            elif hasattr(opportunity, 'event'):
                market_label = opportunity.event.title[:30]
            else:
                market_label = "unknown"

            await self.notifier.send_alert(
                "Partial Fill Loss",
                f"Lost ${loss_usdc:.2f} on {market_label}",
            )
        else:
            self.stats.failed_trades += 1
            self._consecutive_failures += 1

        # Check if should pause
        if self._consecutive_failures >= self.config.max_consecutive_failures:
            self._paused_until = time.time() + self.config.pause_duration
            self._consecutive_failures = 0

            logger.warning(
                "Trading paused due to consecutive failures",
                pause_duration=self.config.pause_duration,
            )

            await self.notifier.send_alert(
                "Trading Paused",
                f"Paused for {self.config.pause_duration}s after {self.config.max_consecutive_failures} failures",
            )

    async def check_relayer_quota(
        self,
        remaining: int,
        total: int,
        threshold_pct: float = 0.1,
    ) -> None:
        """
        Check Relayer quota and alert if low.

        Args:
            remaining: Remaining quota
            total: Total daily quota
            threshold_pct: Alert threshold (e.g., 0.1 = 10%)
        """
        usage_pct = 1 - (remaining / total)

        if remaining <= total * threshold_pct:
            await self.notifier.send_alert(
                "Relayer Quota Low",
                f"Only {remaining}/{total} transactions remaining ({usage_pct:.0%} used)",
            )

    def get_stats_summary(self) -> Dict:
        """Get summary of trading statistics."""
        return {
            "total_trades": self.stats.total_trades,
            "real_trades": self.stats.successful_trades,
            "simulated_trades": self.stats.simulated_trades,
            "success_rate": f"{self.stats.success_rate:.2%}",
            "net_profit": f"${self.stats.net_profit:.2f}",
            "simulated_profit": f"${self.stats.simulated_profit_usdc:.2f}",
            "partial_fills": self.stats.partial_fills,
            "daily_loss": f"${self._daily_loss:.2f}",
            "is_paused": self._paused_until is not None and time.time() < self._paused_until,
        }
