"""Order and orderbook data models."""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from models.market import Market


class OrderSide(Enum):
    """Order side (buy/sell)."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    """Order type."""
    GTC = "GTC"  # Good Till Cancelled
    FOK = "FOK"  # Fill Or Kill
    GTD = "GTD"  # Good Till Date


class OrderStatus(Enum):
    """Order execution status."""
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class OrderBookLevel:
    """
    A single level in the order book.
    """
    price: float
    size: float

    @property
    def value(self) -> float:
        """Total value at this level."""
        return self.price * self.size


@dataclass
class OrderBook:
    """
    Order book for a token.
    """
    token_id: str
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)
    timestamp: float = 0.0

    @property
    def best_bid(self) -> Optional[float]:
        """Best (highest) bid price."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        """Best (lowest) ask price."""
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        """Mid price between best bid and ask."""
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Optional[float]:
        """Bid-ask spread."""
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None

    def get_available_depth(self, side: str = "ask", max_levels: int = 5) -> float:
        """
        Calculate total available liquidity (in USDC) on one side of the book.
        
        Args:
            side: "ask" for buy side depth, "bid" for sell side depth
            max_levels: Maximum number of levels to consider
            
        Returns:
            Total USDC value available
        """
        levels = self.asks if side == "ask" else self.bids
        total = 0.0
        for i, level in enumerate(levels):
            if i >= max_levels:
                break
            total += level.price * level.size
        return total

    def calculate_average_buy_price(self, amount_usdc: float) -> Optional[float]:
        """
        Calculate weighted average price to buy a given USDC amount.
        
        This is the "depth penetration" calculation.
        
        Args:
            amount_usdc: Amount of USDC to spend
            
        Returns:
            Weighted average price, or None if insufficient liquidity
        """
        if not self.asks:
            return None
        
        remaining = amount_usdc
        total_tokens = 0.0
        total_cost = 0.0
        
        for level in self.asks:
            level_value = level.price * level.size
            
            if remaining >= level_value:
                # Take entire level
                total_tokens += level.size
                total_cost += level_value
                remaining -= level_value
            else:
                # Partial fill at this level
                tokens_at_level = remaining / level.price
                total_tokens += tokens_at_level
                total_cost += remaining
                remaining = 0
                break
        
        if remaining > 0:
            # Insufficient liquidity
            return None
        
        return total_cost / total_tokens if total_tokens > 0 else None

    def calculate_depth_penetration(
        self,
        amount_usdc: float,
        side: str = "buy",
        max_slippage: Optional[float] = None,
    ) -> dict:
        """
        Enhanced depth penetration calculation with detailed info.
        
        Args:
            amount_usdc: Amount to trade in USDC
            side: "buy" for asks, "sell" for bids
            max_slippage: Optional maximum allowed slippage (e.g., 0.02 = 2%)
            
        Returns:
            Dict with:
                - avg_price: Weighted average execution price
                - slippage: Slippage vs best price
                - fillable_amount: How much can be filled
                - levels_used: Number of order book levels consumed
                - is_complete: Whether full amount can be filled
        """
        levels = self.asks if side == "buy" else self.bids
        if not levels:
            return {
                "avg_price": None,
                "slippage": None,
                "fillable_amount": 0.0,
                "levels_used": 0,
                "is_complete": False,
            }
        
        best_price = levels[0].price
        remaining = amount_usdc
        total_tokens = 0.0
        total_cost = 0.0
        levels_used = 0
        
        for level in levels:
            # Check slippage constraint
            if max_slippage is not None:
                current_slippage = abs(level.price - best_price) / best_price
                if current_slippage > max_slippage:
                    break  # Stop at this level due to slippage limit
            
            level_value = level.price * level.size
            levels_used += 1
            
            if remaining >= level_value:
                total_tokens += level.size
                total_cost += level_value
                remaining -= level_value
            else:
                tokens_at_level = remaining / level.price
                total_tokens += tokens_at_level
                total_cost += remaining
                remaining = 0
                break
        
        if total_tokens == 0:
            return {
                "avg_price": None,
                "slippage": None,
                "fillable_amount": 0.0,
                "levels_used": 0,
                "is_complete": False,
            }
        
        avg_price = total_cost / total_tokens
        slippage = (avg_price - best_price) / best_price if side == "buy" else (best_price - avg_price) / best_price
        fillable_amount = amount_usdc - remaining
        
        return {
            "avg_price": avg_price,
            "slippage": slippage,
            "fillable_amount": fillable_amount,
            "levels_used": levels_used,
            "is_complete": remaining == 0,
        }


    def calculate_average_sell_price(self, token_amount: float) -> Optional[float]:
        """
        Calculate weighted average price to sell a given token amount.
        
        Args:
            token_amount: Number of tokens to sell
            
        Returns:
            Weighted average price, or None if insufficient liquidity
        """
        if not self.bids:
            return None
        
        remaining = token_amount
        total_value = 0.0
        
        for level in self.bids:
            if remaining >= level.size:
                # Take entire level
                total_value += level.price * level.size
                remaining -= level.size
            else:
                # Partial fill at this level
                total_value += level.price * remaining
                remaining = 0
                break
        
        if remaining > 0:
            # Insufficient liquidity
            return None
        
        return total_value / token_amount if token_amount > 0 else None


@dataclass
class Order:
    """
    Represents an order to be placed.
    """
    token_id: str
    side: OrderSide
    price: float
    size: float
    order_type: OrderType = OrderType.FOK
    
    # After submission
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    filled_avg_price: float = 0.0

    @property
    def is_filled(self) -> bool:
        """Check if order is fully filled."""
        return self.status == OrderStatus.FILLED

    @property
    def is_failed(self) -> bool:
        """Check if order failed."""
        return self.status in (OrderStatus.CANCELLED, OrderStatus.FAILED)


@dataclass
class ArbitrageOpportunity:
    """
    Represents a detected arbitrage opportunity.
    """
    market: Market
    
    # Calculated prices (from depth penetration)
    avg_price_yes: float
    avg_price_no: float
    
    # Trade parameters
    trade_size_usdc: float
    
    # Profit calculation
    total_cost: float  # avg_price_yes + avg_price_no (should be < 1.0)
    estimated_fee: float  # Total fees for both sides
    
    timestamp: float = 0.0
    
    @property
    def gross_profit_pct(self) -> float:
        """Gross profit percentage (before fees)."""
        return 1.0 - self.total_cost
    
    @property
    def net_profit_pct(self) -> float:
        """Net profit percentage (after fees)."""
        return self.gross_profit_pct - self.estimated_fee
    
    @property
    def net_profit_usdc(self) -> float:
        """Net profit in USDC."""
        return self.net_profit_pct * self.trade_size_usdc
    
    def is_profitable(self, threshold: float = 0.008) -> bool:
        """
        Check if this opportunity exceeds the profit threshold.
        
        Args:
            threshold: Minimum profit threshold (default 0.8%)
            
        Returns:
            True if opportunity is profitable after threshold
        """
        return self.net_profit_pct > threshold


@dataclass
class ShortArbitrageOpportunity:
    """
    Represents a SHORT arbitrage opportunity (Mint + Sell).
    
    This occurs when Bid(Yes) + Bid(No) > 1.0.
    Strategy: Mint Yes+No tokens (cost $1), then sell both for total > $1.
    """
    market: Market
    
    # Best bid prices (what we can sell at)
    bid_price_yes: float
    bid_price_no: float
    
    # Trade parameters
    trade_size_usdc: float
    
    # Profit calculation
    total_revenue: float  # bid_yes + bid_no (should be > 1.0)
    mint_cost: float = 1.0  # Minting always costs $1 per pair
    estimated_gas_cost: float = 0.05  # Gas for Mint transaction
    estimated_fee: float = 0.0  # Trading fees for selling
    
    timestamp: float = 0.0
    
    @property
    def gross_profit_pct(self) -> float:
        """Gross profit percentage (before gas and fees)."""
        return self.total_revenue - self.mint_cost
    
    @property
    def net_profit_pct(self) -> float:
        """Net profit percentage (after gas and fees)."""
        # Gas cost as percentage of trade size
        gas_pct = self.estimated_gas_cost / self.trade_size_usdc if self.trade_size_usdc > 0 else 0
        return self.gross_profit_pct - self.estimated_fee - gas_pct
    
    @property
    def net_profit_usdc(self) -> float:
        """Net profit in USDC."""
        gross = (self.total_revenue - self.mint_cost) * self.trade_size_usdc
        return gross - self.estimated_gas_cost - (self.estimated_fee * self.trade_size_usdc)
    
    def is_profitable(self, threshold: float = 0.015) -> bool:
        """
        Check if this short opportunity exceeds the profit threshold.
        
        Args:
            threshold: Minimum profit threshold (default 1.5% for shorts)
            
        Returns:
            True if opportunity is profitable after threshold
        """
        return self.net_profit_pct > threshold

