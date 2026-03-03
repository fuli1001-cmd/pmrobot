"""Market data models."""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class MarketType(Enum):
    """Type of prediction market."""
    BINARY = "binary"
    NEGATIVE_RISK = "negative_risk"


class FeeCategory(Enum):
    """Fee category for the market."""
    FREE = "free"  # 0% fee (politics, sports, etc.)
    CRYPTO_15MIN = "crypto_15min"  # Dynamic fee up to 3.15%
    STANDARD = "standard"  # Standard fee structure


@dataclass
class Market:
    """
    Represents a Polymarket prediction market.
    """
    
    # Core identifiers
    condition_id: str
    token_id_yes: str
    token_id_no: str
    
    # Market metadata
    question: str
    slug: str
    
    # Market type
    market_type: MarketType = MarketType.BINARY
    
    # Price info
    min_tick_size: float = 0.01
    
    # Fee info
    fee_category: FeeCategory = FeeCategory.FREE
    
    # Additional metadata
    tags: List[str] = field(default_factory=list)
    volume_24h: float = 0.0
    liquidity: float = 0.0
    end_date: str = ""  # ISO-8601 event end / resolution time from API
    game_start_time: str = ""  # ISO-8601 actual event start time (from Events API)

    # Cached outcome prices from API (used as fallback when order book is empty)
    outcome_price_yes: float = 0.0
    outcome_price_no: float = 0.0

    # Status
    active: bool = True
    closed: bool = False
    enable_order_book: bool = True

    @property
    def is_fee_free(self) -> bool:
        """Check if this market has zero fees."""
        return self.fee_category == FeeCategory.FREE

    @property
    def is_crypto_15min(self) -> bool:
        """Check if this is a 15-minute crypto market."""
        return self.fee_category == FeeCategory.CRYPTO_15MIN

    def estimate_fee(self, odds: float = 0.5) -> float:
        """
        Estimate the taker fee for this market.

        Args:
            odds: Current market probability (0-1)

        Returns:
            Estimated fee as a decimal (e.g., 0.0315 for 3.15%)
        """
        if self.fee_category == FeeCategory.FREE:
            return 0.0
        elif self.fee_category == FeeCategory.CRYPTO_15MIN:
            # Dynamic fee: highest at 50%, lowest at 0% or 100%
            # Max fee is 3.15% at 50% probability
            deviation = abs(odds - 0.5)
            # Fee decreases linearly as odds move away from 50%
            return 0.0315 * (1 - 2 * deviation)
        else:
            return 0.0  # Standard markets are currently fee-free


@dataclass
class MarketSnapshot:
    """
    A point-in-time snapshot of market prices.
    """
    
    market: Market
    best_bid_yes: float
    best_ask_yes: float
    best_bid_no: float
    best_ask_no: float
    timestamp: float
    
    @property
    def mid_price_yes(self) -> float:
        """Calculate mid price for Yes."""
        return (self.best_bid_yes + self.best_ask_yes) / 2
    
    @property
    def mid_price_no(self) -> float:
        """Calculate mid price for No."""
        return (self.best_bid_no + self.best_ask_no) / 2
    
    @property
    def spread_yes(self) -> float:
        """Calculate bid-ask spread for Yes."""
        return self.best_ask_yes - self.best_bid_yes
    
    @property
    def spread_no(self) -> float:
        """Calculate bid-ask spread for No."""
        return self.best_ask_no - self.best_bid_no
    
    @property
    def total_ask_price(self) -> float:
        """Total cost to buy both Yes and No (at ask)."""
        return self.best_ask_yes + self.best_ask_no
    
    @property
    def naive_profit_opportunity(self) -> float:
        """
        Simple arbitrage profit opportunity (not accounting for depth).
        
        Positive value means potential arbitrage opportunity.
        """
        return 1.0 - self.total_ask_price


@dataclass
class NegativeRiskEvent:
    """
    A Negative Risk event with multiple mutually exclusive outcomes.
    
    In Negative Risk markets, only ONE outcome can be true.
    This enables two arbitrage strategies:
    1. Buy-All-No: If sum(No prices) < N-1, profit = (N-1) - sum(No)
    2. Buy-All-Yes: If sum(Yes prices) < 1, profit = 1 - sum(Yes)
    """
    
    # Event identifiers
    event_id: str
    title: str
    slug: str
    
    # All outcomes (each is a Market with Yes/No tokens)
    outcomes: List["Market"] = field(default_factory=list)
    
    # Metadata
    liquidity: float = 0.0
    volume_24h: float = 0.0
    
    # Status
    active: bool = True
    closed: bool = False
    
    @property
    def outcome_count(self) -> int:
        """Number of outcomes in this event."""
        return len(self.outcomes)
    
    @property
    def is_tradeable(self) -> bool:
        """Check if this event has enough outcomes for arbitrage."""
        return self.outcome_count >= 2 and self.active and not self.closed
    
    def get_all_token_ids(self) -> List[str]:
        """Get all token IDs (Yes and No) for WebSocket subscription."""
        tokens = []
        for outcome in self.outcomes:
            tokens.append(outcome.token_id_yes)
            tokens.append(outcome.token_id_no)
        return tokens


class NegativeRiskStrategy(Enum):
    """Arbitrage strategy for Negative Risk events."""
    BUY_ALL_NO = "buy_all_no"        # Buy No for all outcomes (when sum(No) < N-1)
    BUY_ALL_YES = "buy_all_yes"      # Buy Yes for all outcomes (when sum(Yes) < 1)
    SHORT_REBALANCE = "short_rebalance"  # Buy all No when sum(Yes) > 1 (short the overpriced Yes)



@dataclass
class NegativeRiskArbitrageOpportunity:
    """
    An arbitrage opportunity in a Negative Risk event.
    """
    
    event: NegativeRiskEvent
    strategy: NegativeRiskStrategy
    
    # Prices for each outcome (keyed by condition_id)
    outcome_prices: dict = field(default_factory=dict)  # {condition_id: price}
    
    # Trade parameters
    trade_size_usdc: float = 100.0
    
    # Calculated values
    total_cost: float = 0.0  # Sum of all prices
    expected_payout: float = 0.0  # What we get back
    estimated_fee: float = 0.0
    timestamp: float = 0.0
    
    @property
    def gross_profit(self) -> float:
        """Gross profit before fees."""
        return self.expected_payout - self.total_cost
    
    @property
    def gross_profit_pct(self) -> float:
        """Gross profit as percentage of cost."""
        if self.total_cost <= 0:
            return 0.0
        return self.gross_profit / self.total_cost
    
    @property
    def net_profit(self) -> float:
        """Net profit after fees."""
        return self.gross_profit - self.estimated_fee
    
    @property
    def net_profit_pct(self) -> float:
        """Net profit as percentage of cost."""
        if self.total_cost <= 0:
            return 0.0
        return self.net_profit / self.total_cost
    
    @property
    def net_profit_usdc(self) -> float:
        """Estimated profit in USDC."""
        return self.net_profit * self.trade_size_usdc
    
    def is_profitable(self, threshold: float = 0.008) -> bool:
        """Check if opportunity exceeds profit threshold."""
        return self.net_profit_pct >= threshold

