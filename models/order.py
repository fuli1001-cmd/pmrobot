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

    def calculate_average_buy_price_for_tokens(
        self,
        token_amount: float,
    ) -> Optional[float]:
        """Calculate the weighted average ask for an exact token quantity."""
        if token_amount <= 0 or not self.asks:
            return None

        remaining = token_amount
        total_cost = 0.0
        for level in self.asks:
            take = min(level.size, remaining)
            if take <= 0:
                continue
            total_cost += take * level.price
            remaining -= take
            if remaining <= 1e-9:
                break

        if remaining > 1e-9:
            return None
        return total_cost / token_amount

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

    def calculate_greedy_fill(
        self,
        other_book: "OrderBook",
        profit_threshold: float,
        max_size: float,
        min_size: float = 1.0,
        depth_safety_multiplier: float = 1.5,
    ) -> dict:
        """
        Greedy fill algorithm: expand position size while profit threshold is met.
        
        For binary arbitrage where we buy both Yes and No:
        - Start with first level of both books
        - Incrementally add deeper levels
        - Stop when combined cost exceeds profit threshold or max_size
        
        Args:
            other_book: The other side's order book (e.g., No book if self is Yes)
            profit_threshold: Minimum profit percentage required (e.g., 0.01 = 1%)
            max_size: Maximum position size in USDC
            min_size: Minimum position size in USDC
            depth_safety_multiplier: Reserve multiple required on each leg
            
        Returns:
            Dict with:
                - optimal_size: Best size that meets profit threshold
                - avg_price_self: Weighted avg price for this book
                - avg_price_other: Weighted avg price for other book
                - combined_cost: Total cost (should be < 1.0 for profit)
                - profit_pct: Actual profit percentage
                - levels_self: Levels consumed from this book
                - levels_other: Levels consumed from other book
        """
        if not self.asks or not other_book.asks:
            return {
                "optimal_size": 0.0,
                "avg_price_self": None,
                "avg_price_other": None,
                "combined_cost": None,
                "profit_pct": 0.0,
                "levels_self": 0,
                "levels_other": 0,
                "safe_max_size": 0.0,
            }
        
        # Binary search for optimal size
        best_result = None
        test_sizes = [min_size]
        
        # Generate test sizes: min, min*2, min*4, ..., max
        size = min_size * 2
        while size <= max_size:
            test_sizes.append(size)
            size *= 2
        test_sizes.append(max_size)
        
        # Use L1 prices for proportional USDC split (not 50/50).
        # Binary arb buys equal token counts, so USDC is split by price ratio.
        best_self = self.asks[0].price
        best_other = other_book.asks[0].price
        total_l1 = best_self + best_other

        # Convert per-leg available depth into a safe max total budget.
        # If one leg gets x USDC of the total budget, require reserve multiple m:
        #   x * m <= available_depth_leg
        # Rearranged to total budget cap for each leg:
        #   total <= available_depth_leg * total_l1 / (best_leg * m)
        available_depth_self = self.get_available_depth(side="ask")
        available_depth_other = other_book.get_available_depth(side="ask")
        safe_max_self = available_depth_self * total_l1 / (best_self * depth_safety_multiplier)
        safe_max_other = available_depth_other * total_l1 / (best_other * depth_safety_multiplier)
        safe_max_size = min(max_size, safe_max_self, safe_max_other)

        if safe_max_size < min_size:
            return {
                "optimal_size": 0.0,
                "avg_price_self": None,
                "avg_price_other": None,
                "combined_cost": None,
                "profit_pct": 0.0,
                "levels_self": 0,
                "levels_other": 0,
                "safe_max_size": safe_max_size,
            }

        # Polymarket enforces min $1 per order (size * price >= 1.0).
        # Equal token sizing means the cheap side is the binding constraint:
        #   num_tokens * min(price_self, price_other) >= 1.0
        #   total_usdc = num_tokens * total_l1
        # So: min_total_usdc = total_l1 / min(price_self, price_other)
        min_price = min(best_self, best_other)
        min_total_for_order = total_l1 / min_price if min_price > 0 else float('inf')
        # Bump up to next dollar to be safe after GCD rounding
        import math as _math
        min_total_for_order = _math.ceil(min_total_for_order)

        test_sizes.append(safe_max_size)
        test_sizes = sorted({round(size, 6) for size in test_sizes})

        for test_size in test_sizes:
            if test_size > safe_max_size:
                continue
            # Skip sizes where the cheap side would produce < $1 order
            if test_size < min_total_for_order:
                continue
            # Proportional split: cheap side needs less USDC depth
            usdc_self = test_size * best_self / total_l1
            usdc_other = test_size * best_other / total_l1
            
            info_self = self.calculate_depth_penetration(usdc_self, "buy")
            info_other = other_book.calculate_depth_penetration(usdc_other, "buy")
            
            if not info_self["is_complete"] or not info_other["is_complete"]:
                break  # Not enough liquidity at this size
            
            combined_cost = info_self["avg_price"] + info_other["avg_price"]
            profit_pct = 1.0 - combined_cost
            
            if profit_pct >= profit_threshold:
                best_result = {
                    "optimal_size": test_size,
                    "avg_price_self": info_self["avg_price"],
                    "avg_price_other": info_other["avg_price"],
                    "combined_cost": combined_cost,
                    "profit_pct": profit_pct,
                    "levels_self": info_self["levels_used"],
                    "levels_other": info_other["levels_used"],
                    "safe_max_size": safe_max_size,
                }
            else:
                # Profit dropped below threshold, stop expanding
                break
        
        if best_result is None:
            return {
                "optimal_size": 0.0,
                "avg_price_self": None,
                "avg_price_other": None,
                "combined_cost": None,
                "profit_pct": 0.0,
                "levels_self": 0,
                "levels_other": 0,
                "safe_max_size": safe_max_size,
            }
        
        return best_result


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
    safe_max_trade_size_usdc: float = 0.0
    configured_max_trade_size_usdc: float = 0.0
    depth_safety_multiplier: float = 1.0
    levels_yes: int = 0
    levels_no: int = 0
    
    timestamp: float = 0.0
    
    @property
    def gross_profit_pct(self) -> float:
        """Gross profit percentage (before fees).

        For Binary markets total_cost ≈ 1.0, so this is effectively
        the same as ROI = (payout - cost) / cost.  The NegRisk model
        uses explicit ROI because total_cost can differ significantly
        from the expected payout.
        """
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
