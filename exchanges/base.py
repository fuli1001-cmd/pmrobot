"""Abstract base exchange adapter for multi-platform arbitrage.

Defines the unified interface that all exchange adapters must implement,
plus shared data models used across adapters.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


# ---------------------------------------------------------------------------
# Unified data models
# ---------------------------------------------------------------------------


class Platform(Enum):
    """Supported trading platforms."""
    POLYMARKET = "polymarket"
    AZURO = "azuro"
    SXBET = "sxbet"


class OutcomeSide(Enum):
    """Normalised outcome side."""
    YES = "yes"
    NO = "no"


@dataclass
class UnifiedMarket:
    """Platform-agnostic market representation.

    Each market describes a single binary question (e.g. "Man City to win?")
    with exactly two mutually exclusive outcomes.
    """

    platform: Platform
    market_id: str          # Platform-specific unique ID
    question: str           # Human-readable question
    sport: str = ""         # Sport category (e.g. "football", "basketball")
    league: str = ""        # League / competition name
    event_name: str = ""    # Underlying event (e.g. "Man City vs Liverpool")
    start_time: float = 0.0  # Event start Unix timestamp

    # Participant identifiers for structural matching
    team_a: str = ""
    team_b: str = ""

    # Extra metadata
    active: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass
class UnifiedOdds:
    """Normalised odds for a market on a single platform.

    Prices are expressed as probabilities in [0, 1].
    For AMMs, *effective_price* already includes price impact for the
    configured trade size.
    """

    platform: Platform
    market_id: str
    price_yes: float = 0.0   # Cost to buy YES (ask for CLOB, effective for AMM)
    price_no: float = 0.0    # Cost to buy NO
    max_size_yes: float = 0.0  # Max available size (USDC) for YES side
    max_size_no: float = 0.0   # Max available size (USDC) for NO side
    timestamp: float = 0.0


@dataclass
class BetResult:
    """Result of a bet placement attempt."""

    class Status(Enum):
        SUCCESS = "success"
        FAILED = "failed"
        SKIPPED = "skipped"  # dry-run

    status: Status
    platform: Platform
    market_id: str
    outcome: OutcomeSide
    amount: float = 0.0
    effective_odds: float = 0.0
    tx_hash: Optional[str] = None
    error_message: Optional[str] = None
    execution_time_ms: float = 0.0


# ---------------------------------------------------------------------------
# Abstract adapter
# ---------------------------------------------------------------------------


class BaseExchange(ABC):
    """Unified exchange adapter interface.

    Each concrete adapter wraps a single platform (Polymarket, Azuro, …)
    and exposes a common API for market discovery, pricing and execution.
    """

    platform: Platform

    @abstractmethod
    async def connect(self) -> None:
        """Initialise connections (HTTP clients, subscriptions, etc.)."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean up connections."""

    @abstractmethod
    async def get_markets(self, sport: Optional[str] = None) -> List[UnifiedMarket]:
        """Fetch active markets, optionally filtered by sport.

        Args:
            sport: Sport slug to filter by (e.g. "football").

        Returns:
            List of markets available on this platform.
        """

    @abstractmethod
    async def get_odds(
        self, market_id: str, trade_size: float = 50.0, *, live: bool = False,
    ) -> Optional[UnifiedOdds]:
        """Get current odds for a market.

        For AMM platforms the returned prices should already account for
        price impact at the given *trade_size*.

        Args:
            market_id: Platform-specific market identifier.
            trade_size: Intended bet size in USDC (for slippage calc).
            live: If *True*, fetch fresh prices from the exchange API
                  instead of relying on cached / snapshot data.

        Returns:
            UnifiedOdds, or None if market not found / inactive.
        """

    @abstractmethod
    async def place_bet(
        self,
        market_id: str,
        outcome: OutcomeSide,
        amount: float,
        min_odds: float,
    ) -> BetResult:
        """Place a bet with slippage protection.

        Args:
            market_id: Platform-specific market identifier.
            outcome: Which side to bet on.
            amount: Bet size in USDC.
            min_odds: Minimum acceptable odds (price ≤ 1/min_odds).

        Returns:
            BetResult with execution details.
        """

    @abstractmethod
    async def get_balance(self) -> float:
        """Get available USDC balance on this platform.

        Returns:
            Balance in USDC.
        """

    # ---- optional methods with default impls ----

    async def sell_position(
        self,
        market_id: str,
        outcome: OutcomeSide,
        token_amount: float,
        bought_price: float,
    ) -> BetResult:
        """Attempt to sell (unwind) a filled position at a discount.

        Used for emergency exit when one leg of a cross-platform trade
        fails.  Not all platforms support instant selling — the default
        implementation returns FAILED.

        Args:
            market_id: Market to sell in.
            outcome: Which side to sell (YES or NO).
            token_amount: Number of tokens (shares) to sell.
            bought_price: Price at which the position was acquired
                          (used to compute discount sell price).

        Returns:
            BetResult with execution details.
        """
        return BetResult(
            status=BetResult.Status.FAILED,
            platform=self.platform,
            market_id=market_id,
            outcome=outcome,
            amount=0.0,
            error_message="sell_position not supported on this platform",
        )

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()
