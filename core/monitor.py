"""Real-time market monitoring using WebSocket."""

import asyncio
import json
import time
from typing import Callable, Dict, List, Optional, Set

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from config.constants import (
    CLOB_API_BASE_URL,
    CLOB_WS_URL, 
    get_profit_threshold,
    ESTIMATED_MINT_GAS_COST_USD,
    SHORT_ARBITRAGE_THRESHOLD,
)
from models.market import Market, NegativeRiskEvent, NegativeRiskStrategy, NegativeRiskArbitrageOpportunity
from models.order import ArbitrageOpportunity, ShortArbitrageOpportunity, OrderBook, OrderBookLevel
from utils.logger import get_logger

logger = get_logger(__name__)

WS_SUBSCRIPTION_SHARD_SIZE = 500
WS_SNAPSHOT_BACKFILL_DELAY_SECONDS = 3.0
WS_MIN_SNAPSHOT_COVERAGE = 0.95


def _market_subscription(token_ids: List[str], initial: bool) -> dict:
    """Build a current Polymarket market-channel subscription message."""
    subscription = {"assets_ids": token_ids, "initial_dump": True}
    if initial:
        subscription.update(type="market", custom_feature_enabled=True)
    else:
        subscription.update(operation="subscribe", custom_feature_enabled=True)
    return subscription


def _token_shards(token_ids: List[str], shard_size: int = WS_SUBSCRIPTION_SHARD_SIZE) -> List[List[str]]:
    """Split de-duplicated token IDs into stable WebSocket shards."""
    unique_tokens = list(dict.fromkeys(token_ids))
    return [unique_tokens[i:i + shard_size] for i in range(0, len(unique_tokens), shard_size)]


async def _fetch_order_book_snapshots(token_ids: List[str]) -> List[dict]:
    """Fetch full CLOB books in batches for WebSocket snapshot backfill."""
    snapshots: List[dict] = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for batch in _token_shards(token_ids):
            response = await client.post(
                f"{CLOB_API_BASE_URL}/books",
                json=[{"token_id": token_id} for token_id in batch],
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                snapshots.extend(item for item in payload if isinstance(item, dict))
    return snapshots


def _is_transient_ws_error(error: Exception) -> bool:
    """Return True for reconnectable websocket/network errors."""
    error_text = repr(error)
    transient_markers = (
        "timed out during opening handshake",
        "ConnectionResetError",
        "WinError 64",
        "指定的网络名不再可用",
    )
    return any(marker in error_text for marker in transient_markers)


class OrderBookManager:
    """
    Manages order books for multiple markets.
    """

    def __init__(self):
        """Initialize the order book manager."""
        self._books: Dict[str, OrderBook] = {}  # token_id -> OrderBook

    def update(self, token_id: str, data: dict) -> OrderBook:
        """
        Update order book from WebSocket message.

        Args:
            token_id: Token ID
            data: Order book data from WebSocket

        Returns:
            Updated order book
        """
        bids = [
            OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
            for b in data.get("bids", [])
        ]
        asks = [
            OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
            for a in data.get("asks", [])
        ]

        # Sort bids descending, asks ascending
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        book = OrderBook(
            token_id=token_id,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )
        self._books[token_id] = book
        return book

    def apply_price_change(
        self,
        token_id: str,
        data: dict,
        timestamp: Optional[float] = None,
    ) -> Optional[OrderBook]:
        """Apply one incremental CLOB price-level update to a full snapshot."""
        book = self._books.get(token_id)
        if book is None:
            return None

        side = str(data.get("side", "")).upper()
        if side not in {"BUY", "SELL"}:
            return None

        try:
            price = float(data["price"])
            size = float(data["size"])
        except (KeyError, TypeError, ValueError):
            return None

        levels = book.bids if side == "BUY" else book.asks
        levels[:] = [level for level in levels if abs(level.price - price) > 1e-12]
        if size > 0:
            levels.append(OrderBookLevel(price=price, size=size))

        levels.sort(key=lambda level: level.price, reverse=side == "BUY")
        book.timestamp = timestamp if timestamp is not None else time.time()
        return book

    def get(self, token_id: str) -> Optional[OrderBook]:
        """Get order book for a token."""
        return self._books.get(token_id)

    def get_pair(self, yes_token_id: str, no_token_id: str) -> tuple:
        """Get order books for a Yes/No pair."""
        return self.get(yes_token_id), self.get(no_token_id)

    def remove_many(self, token_ids: Set[str]) -> None:
        """Remove books whose subscriptions are no longer active."""
        for token_id in token_ids:
            self._books.pop(token_id, None)

    def coverage(self, token_ids: Set[str]) -> float:
        """Return the fraction of expected tokens with a full snapshot."""
        if not token_ids:
            return 1.0
        available = sum(token_id in self._books for token_id in token_ids)
        return available / len(token_ids)


class ArbitrageDetector:
    """
    Detects arbitrage opportunities from order books.
    """

    def __init__(
        self,
        profit_threshold: float = 0.008,
        trade_size: float = 100.0,
        max_slippage: float = 0.002,
        depth_safety_multiplier: float = 1.5,
        cooldown_seconds: float = 0.0,  # 0 for dry-run, use 5-10 for real trading
    ):
        """
        Initialize the arbitrage detector.

        Args:
            profit_threshold: Minimum profit threshold (e.g., 0.008 = 0.8%)
            trade_size: Maximum trade size in USDC
            max_slippage: Maximum allowed slippage
            depth_safety_multiplier: Required order book reserve multiple
            cooldown_seconds: Seconds to wait before re-detecting same market
        """
        self.profit_threshold = profit_threshold
        self.trade_size = trade_size
        self.max_slippage = max_slippage
        self.depth_safety_multiplier = depth_safety_multiplier
        self.cooldown_seconds = cooldown_seconds
        self._last_opportunity: dict[str, float] = {}  # market_id -> timestamp

    def detect(
        self,
        market: Market,
        book_yes: OrderBook,
        book_no: OrderBook,
    ) -> Optional[ArbitrageOpportunity]:
        """
        Detect arbitrage opportunity for a market.

        Args:
            market: Market to check
            book_yes: Order book for Yes token
            book_no: Order book for No token

        Returns:
            ArbitrageOpportunity if profitable, None otherwise
        """
        # Check cooldown period
        market_id = market.condition_id or market.question_id or market.slug
        now = time.time()
        if market_id in self._last_opportunity:
            elapsed = now - self._last_opportunity[market_id]
            if elapsed < self.cooldown_seconds:
                return None  # Still in cooldown period

        # Use greedy fill algorithm to find optimal position size
        greedy_result = book_yes.calculate_greedy_fill(
            other_book=book_no,
            profit_threshold=self.profit_threshold,
            max_size=self.trade_size,
            min_size=1.0,
            depth_safety_multiplier=self.depth_safety_multiplier,
        )
        
        # Skip if no profitable size found
        if greedy_result["optimal_size"] == 0.0:
            return None
        
        effective_trade_size = greedy_result["optimal_size"]
        avg_price_yes = greedy_result["avg_price_self"]
        avg_price_no = greedy_result["avg_price_other"]

        if avg_price_yes is None or avg_price_no is None:
            return None

        # Check slippage against best ask
        if book_yes.best_ask and book_no.best_ask:
            slippage_yes = (avg_price_yes - book_yes.best_ask) / book_yes.best_ask
            slippage_no = (avg_price_no - book_no.best_ask) / book_no.best_ask

            if slippage_yes > self.max_slippage or slippage_no > self.max_slippage:
                logger.debug(
                    "Slippage too high",
                    market=market.slug,
                    slippage_yes=f"{slippage_yes:.4f}",
                    slippage_no=f"{slippage_no:.4f}",
                )
                return None

        total_cost = avg_price_yes + avg_price_no

        # Estimate fees
        estimated_fee = market.estimate_fee(avg_price_yes)

        opportunity = ArbitrageOpportunity(
            market=market,
            avg_price_yes=avg_price_yes,
            avg_price_no=avg_price_no,
            trade_size_usdc=effective_trade_size,
            safe_max_trade_size_usdc=greedy_result["safe_max_size"],
            configured_max_trade_size_usdc=self.trade_size,
            depth_safety_multiplier=self.depth_safety_multiplier,
            total_cost=total_cost,
            estimated_fee=estimated_fee,
            levels_yes=greedy_result["levels_self"],
            levels_no=greedy_result["levels_other"],
            timestamp=time.time(),
        )

        # Periodic debug log (every ~1000 checks) to show typical prices
        self._check_count = getattr(self, '_check_count', 0) + 1
        if self._check_count % 1000 == 1:
            logger.info(
                "Price sample",
                market=market.slug[:30],
                yes=f"{avg_price_yes:.4f}",
                no=f"{avg_price_no:.4f}",
                total=f"{total_cost:.4f}",
                profit=f"{opportunity.gross_profit_pct:.2%}",
                threshold=f"{self.profit_threshold:.2%}",
            )

        if opportunity.is_profitable(self.profit_threshold):
            # Record opportunity time for cooldown
            self._last_opportunity[market_id] = now
            logger.info(
                "Arbitrage opportunity detected!",
                market=market.slug,
                gross_profit=f"{opportunity.gross_profit_pct:.4f}",
                net_profit=f"{opportunity.net_profit_pct:.4f}",
                profit_usdc=f"${opportunity.net_profit_usdc:.2f}",
                trade_size=f"${opportunity.trade_size_usdc:.2f}",
                safe_max_trade_size=f"${opportunity.safe_max_trade_size_usdc:.2f}",
                configured_max_trade_size=f"${opportunity.configured_max_trade_size_usdc:.2f}",
                depth_safety_multiplier=opportunity.depth_safety_multiplier,
            )
            return opportunity

        return None

    def detect_short(
        self,
        market: Market,
        book_yes: OrderBook,
        book_no: OrderBook,
    ) -> Optional[ShortArbitrageOpportunity]:
        """
        Detect SHORT arbitrage opportunity (Mint + Sell).
        
        This occurs when Bid(Yes) + Bid(No) > 1.0.
        Strategy: Mint tokens at $1, sell both for > $1.
        
        Args:
            market: Market to check
            book_yes: Order book for Yes token
            book_no: Order book for No token
            
        Returns:
            ShortArbitrageOpportunity if profitable, None otherwise
        """
        # Check cooldown period (use same cooldown as long)
        market_id = market.condition_id or market.question_id or market.slug
        now = time.time()
        # Use different cooldown key for short opportunities
        short_key = f"short_{market_id}"
        if short_key in self._last_opportunity:
            elapsed = now - self._last_opportunity[short_key]
            if elapsed < self.cooldown_seconds:
                return None
        
        # Check if we have bid prices
        if not book_yes.best_bid or not book_no.best_bid:
            return None
        
        bid_yes = book_yes.best_bid
        bid_no = book_no.best_bid
        total_revenue = bid_yes + bid_no
        
        # Skip if not a short opportunity
        if total_revenue <= 1.0:
            return None
        
        # Short mint+sell pays a fixed gas cost, so do not silently shrink
        # below the configured MAX_TRADE_SIZE. If depth cannot support that
        # size, skip the opportunity.
        depth_yes = book_yes.get_available_depth("bid", max_levels=5)
        depth_no = book_no.get_available_depth("bid", max_levels=5)
        min_depth = min(depth_yes, depth_no)
        safe_depth_size = min_depth * 0.3
        
        if safe_depth_size < self.trade_size:
            logger.debug(
                "Short arbitrage skipped: insufficient depth for configured size",
                market=market.slug,
                configured_trade_size=f"${self.trade_size:.2f}",
                safe_depth_size=f"${safe_depth_size:.2f}",
            )
            return None

        effective_trade_size = self.trade_size
        
        opportunity = ShortArbitrageOpportunity(
            market=market,
            bid_price_yes=bid_yes,
            bid_price_no=bid_no,
            trade_size_usdc=effective_trade_size,
            total_revenue=total_revenue,
            mint_cost=1.0,
            estimated_gas_cost=ESTIMATED_MINT_GAS_COST_USD,
            estimated_fee=market.estimate_fee(bid_yes),
            timestamp=now,
        )
        
        # Periodic debug log
        self._check_count = getattr(self, '_check_count', 0)
        if self._check_count % 1000 == 500:
            logger.info(
                "Short price sample",
                market=market.slug[:30],
                bid_yes=f"{bid_yes:.4f}",
                bid_no=f"{bid_no:.4f}",
                total=f"{total_revenue:.4f}",
                profit=f"{opportunity.gross_profit_pct:.2%}",
            )
        
        if opportunity.is_profitable(SHORT_ARBITRAGE_THRESHOLD):
            self._last_opportunity[short_key] = now
            logger.info(
                "[SHORT OPPORTUNITY] Mint+Sell detected!",
                market=market.slug,
                gross_profit=f"{opportunity.gross_profit_pct:.4f}",
                net_profit=f"{opportunity.net_profit_pct:.4f}",
                profit_usdc=f"${opportunity.net_profit_usdc:.2f}",
            )
            return opportunity
        
        return None


class NegativeRiskArbitrageDetector:
    """
    Detects arbitrage opportunities in Negative Risk events.
    
    Strategies:
    1. Buy-All-No: If sum(No prices) < N-1, profit = (N-1) - sum(No)
    2. Buy-All-Yes: If sum(Yes prices) < 1, profit = 1 - sum(Yes)
    """

    def __init__(
        self,
        profit_threshold: float = 0.008,
        trade_size: float = 100.0,
        max_slippage: float = 0.002,
        cooldown_seconds: float = 0.0,  # 0 for dry-run, use 5-10 for real trading
    ):
        """
        Initialize the Negative Risk arbitrage detector.

        Args:
            profit_threshold: Minimum profit threshold (e.g., 0.008 = 0.8%)
            trade_size: Maximum total USDC budget across all outcome legs
            max_slippage: Maximum allowed slippage
            cooldown_seconds: Seconds to wait before re-detecting same event
        """
        self.profit_threshold = profit_threshold
        self.trade_size = trade_size
        self.max_slippage = max_slippage
        self.cooldown_seconds = cooldown_seconds
        self._check_count = 0
        self._last_opportunity: dict[str, float] = {}  # event_id -> timestamp

    def _effective_token_size(
        self,
        event: NegativeRiskEvent,
        order_books: "OrderBookManager",
        side: str = "yes",
    ) -> float:
        """Return an equal token quantity within the configured total budget."""
        if not event.outcomes:
            return 0.0

        books: List[OrderBook] = []
        best_price_sum = 0.0
        for outcome in event.outcomes:
            token_id = outcome.token_id_yes if side == "yes" else outcome.token_id_no
            book = order_books.get(token_id)
            if not book or not book.asks or not book.best_ask:
                return 0.0
            books.append(book)
            best_price_sum += book.best_ask

        if best_price_sum <= 0:
            return 0.0

        budget_limited_size = self.trade_size / best_price_sum
        depth_limited_size = min(
            sum(level.size for level in book.asks[:5]) * 0.8
            for book in books
        )
        return max(min(budget_limited_size, depth_limited_size), 0.0)

    def detect(
        self,
        event: NegativeRiskEvent,
        order_books: "OrderBookManager",
    ) -> Optional[NegativeRiskArbitrageOpportunity]:
        """
        Detect arbitrage opportunity in a Negative Risk event.

        Checks both Buy-All-No and Buy-All-Yes strategies.

        Args:
            event: The NegativeRiskEvent to check
            order_books: OrderBookManager with current prices

        Returns:
            NegativeRiskArbitrageOpportunity if profitable, None otherwise
        """
        # Check cooldown period
        event_id = event.event_id
        now = time.time()
        if event_id in self._last_opportunity:
            elapsed = now - self._last_opportunity[event_id]
            if elapsed < self.cooldown_seconds:
                return None  # Still in cooldown period

        # Increment check counter for periodic logging
        self._check_count += 1

        # Early entry-point logging to confirm detect() is being called
        if self._check_count % 500 == 1:
            logger.info(
                "NegRisk detect() called",
                event_title=event.title[:30],
                outcomes=event.outcome_count,
                check_num=self._check_count,
            )

        # Dynamic thresholds may tighten the configured floor, never lower it.
        effective_threshold = max(
            self.profit_threshold,
            get_profit_threshold(event.outcome_count),
        )

        # Quick sum calculation for debug logging (before full detection)
        sum_yes = 0.0
        sum_no = 0.0
        missing_books = 0
        
        for outcome in event.outcomes:
            book_yes = order_books.get(outcome.token_id_yes)
            book_no = order_books.get(outcome.token_id_no)
            if book_yes and book_yes.best_ask:
                sum_yes += book_yes.best_ask
            else:
                missing_books += 1
            if book_no and book_no.best_ask:
                sum_no += book_no.best_ask
            else:
                missing_books += 1
        
        total_tokens = event.outcome_count * 2  # Yes + No for each outcome
        missing_ratio = missing_books / total_tokens
        
        if missing_books:
            if self._check_count % 500 == 1:
                logger.debug(
                    "Skipping event due to insufficient liquidity",
                    event_title=event.title[:40],
                    missing_ratio=f"{missing_ratio:.1%}",
                    missing_books=missing_books,
                    total_tokens=total_tokens,
                )
            return None
        
        # Log sample for visibility (only for events with sufficient data)
        if self._check_count % 500 == 1:
            n = event.outcome_count
            profit_yes = 1.0 - sum_yes  # Profit if buy all Yes
            profit_no = (n - 1) - sum_no  # Profit if buy all No
            logger.info(
                "NegRisk Price sample",
                event_title=event.title[:40],  # Fixed: avoid conflict with logger 'event' arg
                outcomes=n,
                sum_yes=f"{sum_yes:.4f}",
                sum_no=f"{sum_no:.4f}",
                profit_yes=f"{profit_yes:.2%}",
                profit_no=f"{profit_no:.2%}",
                threshold=f"{effective_threshold:.2%}",
                missing=missing_books,
            )

        # Try Buy-All-Yes first (often more common)
        opportunity = self._detect_buy_all_yes(event, order_books)
        if opportunity and opportunity.is_profitable(effective_threshold):
            # Record opportunity time for cooldown
            self._last_opportunity[event_id] = now
            return opportunity

        # Try Buy-All-No
        opportunity = self._detect_buy_all_no(event, order_books)
        if opportunity and opportunity.is_profitable(effective_threshold):
            # Record opportunity time for cooldown
            self._last_opportunity[event_id] = now
            return opportunity

        # Try Short Rebalance (when sum(Yes) > 1)
        opportunity = self._detect_short_rebalance(event, order_books)
        if opportunity and opportunity.is_profitable(effective_threshold):
            # Record opportunity time for cooldown
            self._last_opportunity[event_id] = now
            return opportunity

        return None

    def _detect_buy_all_yes(
        self,
        event: NegativeRiskEvent,
        order_books: "OrderBookManager",
    ) -> Optional[NegativeRiskArbitrageOpportunity]:
        """
        Check if buying all Yes tokens is profitable.

        Condition: sum(Yes prices) < 1 - threshold - fees
        Profit: 1 - sum(Yes prices)
        """
        token_size = self._effective_token_size(event, order_books, side="yes")
        if token_size <= 0:
            return None
        outcome_prices = {}
        total_yes_cost = 0.0

        for outcome in event.outcomes:
            book_yes = order_books.get(outcome.token_id_yes)
            if not book_yes or not book_yes.asks:
                return None

            # Get best ask price for Yes
            avg_price = book_yes.calculate_average_buy_price_for_tokens(token_size)
            if avg_price is None:
                return None
            if avg_price * token_size < 1.0:
                return None

            outcome_prices[outcome.condition_id] = avg_price
            total_yes_cost += avg_price

        if total_yes_cost * token_size > self.trade_size + 1e-9:
            return None

        # Calculate profit
        expected_payout = 1.0  # Only one Yes will be worth 1
        gross_profit = expected_payout - total_yes_cost

        # Periodic debug log (use parent detect()'s _check_count, no duplicate increment)
        if self._check_count % 500 == 1:
            logger.info(
                "NegRisk Price sample (Buy-All-Yes)",
                event_title=event.title[:40],
                outcomes=event.outcome_count,
                total_yes=f"{total_yes_cost:.4f}",
                profit=f"{gross_profit:.2%}",
                threshold=f"{self.profit_threshold:.2%}",
            )

        if gross_profit <= 0:
            return None

        return NegativeRiskArbitrageOpportunity(
            event=event,
            strategy=NegativeRiskStrategy.BUY_ALL_YES,
            outcome_prices=outcome_prices,
            trade_size_usdc=total_yes_cost * token_size,
            token_size=token_size,
            total_cost=total_yes_cost,
            expected_payout=expected_payout,
            estimated_fee=0.0,  # Assume fee-free for now
            timestamp=time.time(),
        )

    def _detect_buy_all_no(
        self,
        event: NegativeRiskEvent,
        order_books: "OrderBookManager",
    ) -> Optional[NegativeRiskArbitrageOpportunity]:
        """
        Check if buying all No tokens is profitable.

        Condition: sum(No prices) < N-1 - threshold - fees
        Profit: (N-1) - sum(No prices)
        """
        token_size = self._effective_token_size(event, order_books, side="no")
        if token_size <= 0:
            return None
        outcome_prices = {}
        total_no_cost = 0.0
        n = event.outcome_count

        for outcome in event.outcomes:
            book_no = order_books.get(outcome.token_id_no)
            if not book_no or not book_no.asks:
                return None

            # Get best ask price for No
            avg_price = book_no.calculate_average_buy_price_for_tokens(token_size)
            if avg_price is None:
                return None
            if avg_price * token_size < 1.0:
                return None

            outcome_prices[outcome.condition_id] = avg_price
            total_no_cost += avg_price

        if total_no_cost * token_size > self.trade_size + 1e-9:
            return None

        # Calculate profit
        # When one outcome wins, that No = 0, all other (N-1) No = 1
        expected_payout = n - 1
        gross_profit = expected_payout - total_no_cost

        # Periodic debug log
        if self._check_count % 500 == 250:  # Offset from Yes checks
            logger.info(
                "NegRisk Price sample (Buy-All-No)",
                event_title=event.title[:40],
                outcomes=n,
                total_no=f"{total_no_cost:.4f}",
                expected=f"{expected_payout:.1f}",
                profit=f"{gross_profit / total_no_cost if total_no_cost > 0 else 0:.2%}",
            )

        if gross_profit <= 0:
            return None

        return NegativeRiskArbitrageOpportunity(
            event=event,
            strategy=NegativeRiskStrategy.BUY_ALL_NO,
            outcome_prices=outcome_prices,
            trade_size_usdc=total_no_cost * token_size,
            token_size=token_size,
            total_cost=total_no_cost,
            expected_payout=expected_payout,
            estimated_fee=0.0,
            timestamp=time.time(),
        )

    def _detect_short_rebalance(
        self,
        event: NegativeRiskEvent,
        order_books: "OrderBookManager",
    ) -> Optional[NegativeRiskArbitrageOpportunity]:
        """
        Detect short rebalancing opportunity when sum(Yes prices) > 1.
        
        When the market overprices the total probability (sum(Yes) > 1),
        we can profit by buying all No tokens (equivalent to shorting the overpriced Yes).
        
        Condition: sum(Yes prices) > 1 + threshold
        Strategy: Buy all No tokens
        Profit: sum(Yes) - 1 (which equals N-1 - sum(No))
        """
        outcome_no_prices = {}
        total_yes_cost = 0.0
        total_no_cost = 0.0
        n = event.outcome_count
        token_size = self._effective_token_size(event, order_books, side="no")
        if token_size <= 0:
            return None

        for outcome in event.outcomes:
            book_yes = order_books.get(outcome.token_id_yes)
            book_no = order_books.get(outcome.token_id_no)
            
            if not book_yes or not book_yes.bids:
                return None  # Need Yes bid prices to calculate sum(Yes)
            if not book_no or not book_no.asks:
                return None  # Need No ask prices to buy

            # Get Yes price (best bid = what we'd get if selling Yes)
            yes_price = book_yes.best_bid
            total_yes_cost += yes_price
            
            # Get No price (for buying)
            no_avg_price = book_no.calculate_average_buy_price_for_tokens(token_size)
            if no_avg_price is None:
                return None
            if no_avg_price * token_size < 1.0:
                return None
            
            outcome_no_prices[outcome.condition_id] = no_avg_price
            total_no_cost += no_avg_price

        if total_no_cost * token_size > self.trade_size + 1e-9:
            return None

        # Check if sum(Yes) > 1 (market is overpriced)
        if total_yes_cost <= 1.0:
            return None  # No short opportunity

        # Calculate profit
        # Buying all No costs total_no_cost, payout is (N-1)
        expected_payout = n - 1
        gross_profit = expected_payout - total_no_cost
        
        # Only profitable if gross_profit > 0
        if gross_profit <= 0:
            return None

        # Periodic debug log
        if self._check_count % 500 == 375:  # Offset from other checks
            logger.info(
                "NegRisk Price sample (Short-Rebalance)",
                event_title=event.title[:40],
                outcomes=n,
                sum_yes=f"{total_yes_cost:.4f}",
                sum_no=f"{total_no_cost:.4f}",
                profit=f"{gross_profit / total_no_cost if total_no_cost > 0 else 0:.2%}",
            )

        return NegativeRiskArbitrageOpportunity(
            event=event,
            strategy=NegativeRiskStrategy.SHORT_REBALANCE,
            outcome_prices=outcome_no_prices,
            trade_size_usdc=total_no_cost * token_size,
            token_size=token_size,
            total_cost=total_no_cost,
            expected_payout=expected_payout,
            estimated_fee=0.0,
            timestamp=time.time(),
        )


class MarketMonitor:
    """
    Monitors markets for arbitrage opportunities using WebSocket.
    """

    def __init__(
        self,
        markets: List[Market],
        profit_threshold: float = 0.008,
        trade_size: float = 100.0,
        max_slippage: float = 0.002,
        depth_safety_multiplier: float = 1.5,
        book_max_age_seconds: float = 2.0,
        book_max_skew_seconds: float = 0.5,
        on_opportunity: Optional[Callable[[ArbitrageOpportunity], None]] = None,
        on_short_opportunity: Optional[Callable[[ShortArbitrageOpportunity], None]] = None,
    ):
        """
        Initialize the market monitor.

        Args:
            markets: List of markets to monitor
            profit_threshold: Minimum profit threshold
            trade_size: Trade size in USDC
            max_slippage: Maximum allowed slippage
            depth_safety_multiplier: Required order book reserve multiple
            book_max_age_seconds: Maximum age of either cached order book
            book_max_skew_seconds: Maximum timestamp difference between paired books
            on_opportunity: Callback when long arbitrage opportunity is detected
            on_short_opportunity: Callback when short (Mint+Sell) opportunity is detected
        """
        self.markets = {m.condition_id: m for m in markets}
        self.token_to_market: Dict[str, Market] = {}
        for m in markets:
            self.token_to_market[m.token_id_yes] = m
            self.token_to_market[m.token_id_no] = m

        self.profit_threshold = profit_threshold
        self.book_max_age_seconds = book_max_age_seconds
        self.book_max_skew_seconds = book_max_skew_seconds
        self.order_books = OrderBookManager()
        self.detector = ArbitrageDetector(
            profit_threshold=profit_threshold,
            trade_size=trade_size,
            max_slippage=max_slippage,
            depth_safety_multiplier=depth_safety_multiplier,
        )
        self.on_opportunity = on_opportunity
        self.on_short_opportunity = on_short_opportunity

        self._connections: Set[websockets.WebSocketClientProtocol] = set()
        self._subscriptions_changed = asyncio.Event()
        self._subscription_revision = 0
        self._running = False
        self._heartbeat_interval = 10.0
        
        # Debug counters
        self._message_count = 0
        self._book_update_count = 0
        self._price_change_count = 0
        self._price_change_ignored_count = 0
        self._tokens_awaiting_snapshot: Set[str] = set()
        self._snapshot_gate_open = False
        self._stale_pair_skip_count = 0
        self._last_stale_log: Dict[str, float] = {}
        self._last_log_time = time.time()

    async def start(self) -> None:
        """Start monitoring markets."""
        self._running = True
        logger.info(
            "Starting market monitor",
            num_markets=len(self.markets),
            book_max_age_seconds=self.book_max_age_seconds,
            book_max_skew_seconds=self.book_max_skew_seconds,
        )

        while self._running:
            revision = self._subscription_revision
            shards = _token_shards(list(self.token_to_market))
            self._subscriptions_changed.clear()
            logger.info(
                "Starting Binary WebSocket shards",
                shards=len(shards),
                tokens=len(self.token_to_market),
                shard_size=WS_SUBSCRIPTION_SHARD_SIZE,
            )
            workers = [
                asyncio.create_task(self._run_shard(index, token_ids, revision))
                for index, token_ids in enumerate(shards, start=1)
            ]
            changed = asyncio.create_task(self._subscriptions_changed.wait())
            try:
                await asyncio.wait([*workers, changed], return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in [*workers, changed]:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*workers, changed, return_exceptions=True)

    async def stop(self) -> None:
        """Stop monitoring markets."""
        self._running = False
        self._subscriptions_changed.set()
        connections = list(self._connections)
        if connections:
            await asyncio.gather(
                *(connection.close() for connection in connections),
                return_exceptions=True,
            )

    async def update_markets(self, new_markets: list) -> None:
        """
        Replace the active Binary universe and restart changed shards.
        """
        old_market_ids = set(self.markets)
        old_tokens = set(self.token_to_market)
        self.markets = {market.condition_id: market for market in new_markets}
        self.token_to_market = {}
        for market in new_markets:
            self.token_to_market[market.token_id_yes] = market
            self.token_to_market[market.token_id_no] = market

        new_tokens = set(self.token_to_market)
        removed_tokens = old_tokens - new_tokens
        self.order_books.remove_many(removed_tokens)
        self._tokens_awaiting_snapshot.intersection_update(new_tokens)

        if old_tokens != new_tokens:
            self._snapshot_gate_open = False
            self._subscription_revision += 1
            self._subscriptions_changed.set()
        logger.info(
            "Binary market universe refreshed",
            added=len(set(self.markets) - old_market_ids),
            removed=len(old_market_ids - set(self.markets)),
            markets=len(self.markets),
            tokens=len(new_tokens),
        )

    async def _run_shard(
        self,
        shard_id: int,
        token_ids: List[str],
        revision: int,
    ) -> None:
        reconnect_delay = 1.0
        while self._running and revision == self._subscription_revision:
            try:
                await self._connect_shard(shard_id, token_ids)
            except ConnectionClosed as e:
                log_fn = logger.info if e.code == 1006 else logger.warning
                log_fn(
                    "Binary WebSocket shard closed",
                    shard=shard_id,
                    code=e.code,
                    reason=e.reason,
                )
            except Exception as e:
                error_text = repr(e)
                if _is_transient_ws_error(e):
                    logger.info(
                        "Binary WebSocket shard transient disconnect",
                        shard=shard_id,
                        error=error_text,
                    )
                else:
                    logger.error(
                        "Binary WebSocket shard error",
                        shard=shard_id,
                        error=error_text,
                    )

            if self._running and revision == self._subscription_revision:
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60.0)

    async def _connect_shard(self, shard_id: int, token_ids: List[str]) -> None:
        """Connect one bounded market-channel shard."""
        async with websockets.connect(CLOB_WS_URL) as ws:
            self._connections.add(ws)
            token_set = set(token_ids)
            self.order_books.remove_many(token_set)
            self._tokens_awaiting_snapshot.update(token_set)
            self._snapshot_gate_open = False
            try:
                await ws.send(json.dumps(_market_subscription(token_ids, initial=True)))
                logger.info(
                    "Subscribed Binary WebSocket shard",
                    shard=shard_id,
                    num_tokens=len(token_ids),
                )
                tasks = [
                    asyncio.create_task(self._process_messages(ws)),
                    asyncio.create_task(self._heartbeat(ws)),
                    asyncio.create_task(
                        self._backfill_missing_snapshots(token_ids, "Binary", shard_id)
                    ),
                ]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self._connections.discard(ws)

    async def _backfill_missing_snapshots(
        self,
        token_ids: List[str],
        monitor_name: str,
        shard_id: int,
    ) -> None:
        """Backfill snapshots that did not arrive promptly over WebSocket."""
        await asyncio.sleep(WS_SNAPSHOT_BACKFILL_DELAY_SECONDS)
        missing = [token_id for token_id in token_ids if not self.order_books.get(token_id)]
        if not missing:
            return
        try:
            snapshots = await _fetch_order_book_snapshots(missing)
        except Exception as e:
            logger.warning(
                "Order book snapshot backfill failed",
                monitor=monitor_name,
                shard=shard_id,
                missing=len(missing),
                error=repr(e),
            )
            return

        restored_markets: Dict[str, Market] = {}
        expected = set(token_ids)
        restored = 0
        for snapshot in snapshots:
            token_id = str(snapshot.get("asset_id", ""))
            if not token_id or token_id not in expected:
                continue
            if self.order_books.get(token_id):
                continue
            self.order_books.update(token_id, snapshot)
            restored += 1
            self._tokens_awaiting_snapshot.discard(token_id)
            market = self.token_to_market.get(token_id)
            if market:
                restored_markets[market.condition_id or market.slug] = market

        logger.info(
            "Order book snapshot backfill complete",
            monitor=monitor_name,
            shard=shard_id,
            requested=len(missing),
            restored=restored,
            coverage=f"{self._snapshot_coverage():.2%}",
        )
        self._refresh_snapshot_gate()
        for market in restored_markets.values():
            await self._detect_market(market)

    def _snapshot_coverage(self) -> float:
        return self.order_books.coverage(set(self.token_to_market))

    def _refresh_snapshot_gate(self) -> None:
        coverage = self._snapshot_coverage()
        was_open = self._snapshot_gate_open
        self._snapshot_gate_open = coverage >= WS_MIN_SNAPSHOT_COVERAGE
        if self._snapshot_gate_open and not was_open:
            logger.info(
                "Binary snapshot health gate opened",
                coverage=f"{coverage:.2%}",
                required=f"{WS_MIN_SNAPSHOT_COVERAGE:.2%}",
            )

    async def _heartbeat(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Send periodic ping to keep connection alive."""
        while self._running:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                await ws.send("PING")
                logger.debug("Heartbeat ping sent")
            except Exception:
                break  # Connection closed

    async def _process_messages(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Process incoming WebSocket messages."""
        async for message in ws:
            await self._handle_message(message)

    async def _handle_message(self, message: str) -> None:
        """Handle incoming WebSocket message."""
        self._message_count += 1
        
        try:
            # Skip empty messages (e.g. during reconnection)
            if not message or message.isspace():
                return
            if message.strip().upper() == "PONG":
                return

            data = json.loads(message)
            
            # Log first few messages for debugging
            if self._message_count <= 3:
                logger.info(
                    "WebSocket message sample",
                    msg_num=self._message_count,
                    msg_type=type(data).__name__,
                    keys=list(data.keys()) if isinstance(data, dict) else f"list[{len(data)}]",
                )
            
            # Handle if data is a list (batch of updates)
            if isinstance(data, list):
                for item in data:
                    await self._process_single_message(item)
            else:
                await self._process_single_message(data)
            
            # Periodic debug log every 60 seconds
            now = time.time()
            if now - self._last_log_time >= 60:
                logger.info(
                    "WebSocket stats",
                    total_messages=self._message_count,
                    book_updates=self._book_update_count,
                    price_changes=self._price_change_count,
                    price_changes_ignored_before_snapshot=self._price_change_ignored_count,
                    tokens_awaiting_snapshot=len(self._tokens_awaiting_snapshot),
                    stale_pair_skips=self._stale_pair_skip_count,
                    markets_with_books=len(self.order_books._books),
                    expected_tokens=len(self.token_to_market),
                    snapshot_coverage=f"{self._snapshot_coverage():.2%}",
                    snapshot_gate_open=self._snapshot_gate_open,
                )
                self._last_log_time = now

        except json.JSONDecodeError as e:
            # Transient parse errors are normal during reconnections
            if message and not message.isspace():
                if "INVALID OPERATION" in message or "404" in message:
                    logger.debug("Ignored invalid op (likely 404/No Orderbook)", msg_preview=message[:100])
                else:
                    logger.debug("Failed to parse WebSocket message", error=repr(e), msg_preview=message[:100])
        except Exception as e:
            logger.debug("Error handling message", error=repr(e))

    async def _process_single_message(self, data: dict) -> None:
        """Process a single message dict."""
        if not isinstance(data, dict):
            return
            
        msg_type = data.get("type") or data.get("event_type")
        
        if msg_type == "book":
            self._book_update_count += 1
            await self._handle_book_update(data)
        elif msg_type == "error":
            logger.error("WebSocket error message", data=data)
        elif msg_type == "subscribed":
            logger.debug("Subscription confirmed")
        elif msg_type == "price_change":
            await self._handle_price_change(data)
        elif "asset_id" in data and ("bids" in data or "asks" in data):
            # Initial snapshot format (no type field, just raw book data)
            self._book_update_count += 1
            await self._handle_book_update(data)

    async def _handle_book_update(self, data: dict) -> None:
        """Handle order book update."""
        token_id = data.get("asset_id")
        if not token_id or token_id not in self.token_to_market:
            return

        # Update order book
        self.order_books.update(token_id, data)
        self._tokens_awaiting_snapshot.discard(token_id)
        if not self._snapshot_gate_open:
            self._refresh_snapshot_gate()

        # Find market for this token
        market = self.token_to_market.get(token_id)
        if not market:
            return

        await self._detect_market(market)

    async def _handle_price_change(self, data: dict) -> None:
        """Apply a complete incremental update batch before running detection."""
        received_at = time.time()
        affected_markets: Dict[str, Market] = {}

        for change in data.get("price_changes", []):
            if not isinstance(change, dict):
                continue

            token_id = change.get("asset_id")
            if not token_id:
                continue

            book = self.order_books.apply_price_change(
                token_id,
                change,
                timestamp=received_at,
            )
            if book is None:
                self._price_change_ignored_count += 1
                if token_id in self.token_to_market:
                    self._tokens_awaiting_snapshot.add(token_id)
                continue

            self._price_change_count += 1
            market = self.token_to_market.get(token_id)
            if market:
                affected_markets[market.condition_id or market.slug] = market

        for market in affected_markets.values():
            await self._detect_market(market)

    async def _detect_market(self, market: Market) -> None:
        """Detect opportunities only from a fresh, synchronized book pair."""
        if self._running and not self._snapshot_gate_open:
            return
        book_yes = self.order_books.get(market.token_id_yes)
        book_no = self.order_books.get(market.token_id_no)

        if not book_yes or not book_no:
            return

        now = time.time()
        yes_age = max(now - book_yes.timestamp, 0.0)
        no_age = max(now - book_no.timestamp, 0.0)
        pair_skew = abs(book_yes.timestamp - book_no.timestamp)
        if (
            yes_age > self.book_max_age_seconds
            or no_age > self.book_max_age_seconds
            or pair_skew > self.book_max_skew_seconds
        ):
            self._stale_pair_skip_count += 1
            market_id = market.condition_id or market.slug
            last_log = self._last_stale_log.get(market_id, 0.0)
            if now - last_log >= 60.0:
                logger.debug(
                    "Binary detection skipped: stale or unsynchronized books",
                    market=market.slug[:50],
                    yes_age_ms=f"{yes_age * 1000:.0f}",
                    no_age_ms=f"{no_age * 1000:.0f}",
                    pair_skew_ms=f"{pair_skew * 1000:.0f}",
                    max_age_ms=f"{self.book_max_age_seconds * 1000:.0f}",
                    max_skew_ms=f"{self.book_max_skew_seconds * 1000:.0f}",
                )
                self._last_stale_log[market_id] = now
            return

        # Check for long arbitrage (Buy-Yes + Buy-No < $1)
        opportunity = self.detector.detect(market, book_yes, book_no)

        if opportunity:
            # Log with distinct marker for easy searching
            logger.info(
                "🚀 [OPPORTUNITY] Binary Market Arbitrage Detected",
                market=market.slug,
                net_profit=f"{opportunity.net_profit_pct:.2%}",
                profit_usdc=f"${opportunity.net_profit_usdc:.2f}",
                threshold=f"{self.profit_threshold:.2%}",
                yes_avg=f"{opportunity.avg_price_yes:.4f}",
                no_avg=f"{opportunity.avg_price_no:.4f}",
                yes_book_age_ms=f"{yes_age * 1000:.0f}",
                no_book_age_ms=f"{no_age * 1000:.0f}",
                pair_skew_ms=f"{pair_skew * 1000:.0f}",
            )
            
            if self.on_opportunity:
                self.on_opportunity(opportunity)

        # Check for short arbitrage (Mint + Sell: Bid-Yes + Bid-No > $1)
        short_opp = self.detector.detect_short(market, book_yes, book_no)

        if short_opp:
            logger.info(
                "🩳 [SHORT OPPORTUNITY] Mint+Sell Arbitrage Detected",
                market=market.slug,
                net_profit=f"{short_opp.net_profit_pct:.2%}",
                profit_usdc=f"${short_opp.net_profit_usdc:.2f}",
            )

            if self.on_short_opportunity:
                self.on_short_opportunity(short_opp)


class NegativeRiskMarketMonitor:
    """
    Monitors Negative Risk events for arbitrage opportunities using WebSocket.
    
    Similar to MarketMonitor but handles events with multiple mutually exclusive outcomes.
    """

    def __init__(
        self,
        events: List[NegativeRiskEvent],
        profit_threshold: float = 0.008,
        trade_size: float = 100.0,
        max_slippage: float = 0.002,
        book_max_age_seconds: float = 30.0,
        book_max_skew_seconds: float = 5.0,
        on_opportunity: Optional[Callable[[NegativeRiskArbitrageOpportunity], None]] = None,
    ):
        """
        Initialize the Negative Risk market monitor.

        Args:
            events: List of Negative Risk events to monitor
            profit_threshold: Minimum profit threshold
            trade_size: Trade size in USDC
            max_slippage: Maximum allowed slippage
            on_opportunity: Callback when opportunity is detected
        """
        self.events = {e.event_id: e for e in events}
        self.token_to_event: Dict[str, NegativeRiskEvent] = {}
        
        # Map each token to its parent event
        for event in events:
            for token_id in event.get_all_token_ids():
                self.token_to_event[token_id] = event

        self.profit_threshold = profit_threshold
        self.book_max_age_seconds = book_max_age_seconds
        self.book_max_skew_seconds = book_max_skew_seconds
        self.order_books = OrderBookManager()
        self.detector = NegativeRiskArbitrageDetector(
            profit_threshold=profit_threshold,
            trade_size=trade_size,
            max_slippage=max_slippage,
        )
        self.on_opportunity = on_opportunity

        self._connections: Set[websockets.WebSocketClientProtocol] = set()
        self._subscriptions_changed = asyncio.Event()
        self._subscription_revision = 0
        self._running = False
        self._heartbeat_interval = 10.0
        
        # Track which events have been checked recently to avoid duplicate checks
        self._last_check_time: Dict[str, float] = {}
        self._min_check_interval = 0.5  # Minimum 0.5 seconds between checks for same event
        
        # Debug counters
        self._message_count = 0
        self._book_update_count = 0
        self._price_change_count = 0
        self._price_change_ignored_count = 0
        self._tokens_awaiting_snapshot: Set[str] = set()
        self._snapshot_gate_open = False
        self._stale_event_skip_count = 0
        self._last_log_time = time.time()

    async def start(self) -> None:
        """Start monitoring Negative Risk events."""
        self._running = True
        logger.info(
            "Starting Negative Risk monitor",
            num_events=len(self.events),
            total_tokens=len(self.token_to_event),
        )

        while self._running:
            revision = self._subscription_revision
            shards = _token_shards(list(self.token_to_event))
            self._subscriptions_changed.clear()
            logger.info(
                "Starting NegRisk WebSocket shards",
                shards=len(shards),
                tokens=len(self.token_to_event),
                shard_size=WS_SUBSCRIPTION_SHARD_SIZE,
            )
            workers = [
                asyncio.create_task(self._run_shard(index, token_ids, revision))
                for index, token_ids in enumerate(shards, start=1)
            ]
            changed = asyncio.create_task(self._subscriptions_changed.wait())
            try:
                await asyncio.wait([*workers, changed], return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in [*workers, changed]:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*workers, changed, return_exceptions=True)

    async def stop(self) -> None:
        """Stop monitoring."""
        logger.info("Stopping Negative Risk monitor")
        self._running = False
        self._subscriptions_changed.set()
        connections = list(self._connections)
        if connections:
            await asyncio.gather(
                *(connection.close() for connection in connections),
                return_exceptions=True,
            )

    async def _heartbeat(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Send periodic ping to keep connection alive."""
        while self._running:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                await ws.send("PING")
                logger.debug("NegRisk: Heartbeat ping sent")
            except Exception:
                break  # Connection closed

    async def _process_messages(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Process incoming WebSocket messages."""
        async for message in ws:
            await self._handle_message(message)

    async def update_events(self, new_events: List[NegativeRiskEvent]) -> None:
        """
        Replace the active event universe and restart changed shards.
        """
        old_event_ids = set(self.events)
        old_tokens = set(self.token_to_event)
        self.events = {event.event_id: event for event in new_events}
        self.token_to_event = {}
        for event in new_events:
            for token_id in event.get_all_token_ids():
                self.token_to_event[token_id] = event

        new_tokens = set(self.token_to_event)
        removed_tokens = old_tokens - new_tokens
        self.order_books.remove_many(removed_tokens)
        self._tokens_awaiting_snapshot.intersection_update(new_tokens)
        if old_tokens != new_tokens:
            self._snapshot_gate_open = False
            self._subscription_revision += 1
            self._subscriptions_changed.set()
        logger.info(
            "NegRisk event universe refreshed",
            added=len(set(self.events) - old_event_ids),
            removed=len(old_event_ids - set(self.events)),
            events=len(self.events),
            tokens=len(new_tokens),
        )

    async def _run_shard(
        self,
        shard_id: int,
        token_ids: List[str],
        revision: int,
    ) -> None:
        reconnect_delay = 1.0
        while self._running and revision == self._subscription_revision:
            try:
                await self._connect_shard(shard_id, token_ids)
            except ConnectionClosed as e:
                log_fn = logger.info if e.code == 1006 else logger.warning
                log_fn(
                    "NegRisk WebSocket shard closed",
                    shard=shard_id,
                    code=e.code,
                    reason=e.reason,
                )
            except Exception as e:
                logger.warning(
                    "NegRisk WebSocket shard error",
                    shard=shard_id,
                    error=repr(e),
                )
            if self._running and revision == self._subscription_revision:
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60.0)

    async def _connect_shard(self, shard_id: int, token_ids: List[str]) -> None:
        async with websockets.connect(CLOB_WS_URL) as ws:
            self._connections.add(ws)
            token_set = set(token_ids)
            self.order_books.remove_many(token_set)
            self._tokens_awaiting_snapshot.update(token_set)
            self._snapshot_gate_open = False
            try:
                await ws.send(json.dumps(_market_subscription(token_ids, initial=True)))
                logger.info(
                    "Subscribed NegRisk WebSocket shard",
                    shard=shard_id,
                    num_tokens=len(token_ids),
                )
                tasks = [
                    asyncio.create_task(self._process_messages(ws)),
                    asyncio.create_task(self._heartbeat(ws)),
                    asyncio.create_task(self._refresh_shard_snapshots(token_ids, shard_id)),
                ]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self._connections.discard(ws)

    async def _refresh_shard_snapshots(
        self,
        token_ids: List[str],
        shard_id: int,
    ) -> None:
        await asyncio.sleep(WS_SNAPSHOT_BACKFILL_DELAY_SECONDS)
        first_refresh = True
        while self._running:
            requested = (
                [token_id for token_id in token_ids if not self.order_books.get(token_id)]
                if first_refresh
                else token_ids
            )
            if requested:
                try:
                    snapshots = await _fetch_order_book_snapshots(requested)
                except Exception as e:
                    logger.warning(
                        "NegRisk snapshot refresh failed",
                        shard=shard_id,
                        requested=len(requested),
                        error=repr(e),
                    )
                else:
                    restored_events: Dict[str, NegativeRiskEvent] = {}
                    expected = set(token_ids)
                    restored = 0
                    for snapshot in snapshots:
                        token_id = str(snapshot.get("asset_id", ""))
                        if not token_id or token_id not in expected:
                            continue
                        if first_refresh and self.order_books.get(token_id):
                            continue
                        self.order_books.update(token_id, snapshot)
                        restored += 1
                        self._tokens_awaiting_snapshot.discard(token_id)
                        event = self.token_to_event.get(token_id)
                        if event:
                            restored_events[event.event_id] = event

                    self._refresh_snapshot_gate()
                    log_fn = logger.info if first_refresh else logger.debug
                    log_fn(
                        "NegRisk snapshot refresh complete",
                        shard=shard_id,
                        requested=len(requested),
                        restored=restored,
                        coverage=f"{self._snapshot_coverage():.2%}",
                    )
                    for event in restored_events.values():
                        await self._detect_event(event)

            first_refresh = False
            await asyncio.sleep(self.book_max_age_seconds / 2)

    def _snapshot_coverage(self) -> float:
        return self.order_books.coverage(set(self.token_to_event))

    def _refresh_snapshot_gate(self) -> None:
        coverage = self._snapshot_coverage()
        was_open = self._snapshot_gate_open
        self._snapshot_gate_open = coverage >= WS_MIN_SNAPSHOT_COVERAGE
        if self._snapshot_gate_open and not was_open:
            logger.info(
                "NegRisk snapshot health gate opened",
                coverage=f"{coverage:.2%}",
                required=f"{WS_MIN_SNAPSHOT_COVERAGE:.2%}",
            )

    async def _handle_message(self, message: str) -> None:
        """Handle incoming WebSocket message."""
        self._message_count += 1
        
        try:
            # Skip empty messages
            if not message or message.isspace():
                return
            if message.strip().upper() == "PONG":
                return
                
            data = json.loads(message)
            
            # Log first few messages for debugging
            if self._message_count <= 3:
                logger.info(
                    "NegRisk WebSocket message sample",
                    msg_num=self._message_count,
                    msg_type=type(data).__name__,
                    keys=list(data.keys()) if isinstance(data, dict) else f"list[{len(data)}]",
                )
            
            # Handle if data is a list (batch of updates)
            if isinstance(data, list):
                for item in data:
                    await self._process_single_message(item)
            else:
                await self._process_single_message(data)
            
            # Periodic debug log every 60 seconds
            now = time.time()
            if now - self._last_log_time >= 60:
                logger.info(
                    "NegRisk WebSocket stats",
                    total_messages=self._message_count,
                    book_updates=self._book_update_count,
                    price_changes=self._price_change_count,
                    price_changes_ignored_before_snapshot=self._price_change_ignored_count,
                    tokens_with_books=len(
                        [token for token in self.token_to_event if self.order_books.get(token)]
                    ),
                    expected_tokens=len(self.token_to_event),
                    tokens_awaiting_snapshot=len(self._tokens_awaiting_snapshot),
                    snapshot_coverage=f"{self._snapshot_coverage():.2%}",
                    snapshot_gate_open=self._snapshot_gate_open,
                    stale_event_skips=self._stale_event_skip_count,
                )
                self._last_log_time = now

        except json.JSONDecodeError as e:
            # Only log non-empty parsing errors at debug level
            if message and not message.isspace():
                if "INVALID OPERATION" in message or "404" in message:
                    logger.debug("NegRisk: Ignored invalid op (likely 404/No Orderbook)", msg_preview=message[:100])
                else:
                    logger.warning("NegRisk: Non-JSON message received", msg_preview=message[:100])
        except Exception as e:
            logger.error("Error handling NegRisk message", error=repr(e))

    async def _process_single_message(self, data: dict) -> None:
        """Process a single WebSocket message."""
        if not isinstance(data, dict):
            return
            
        msg_type = data.get("type") or data.get("event_type")
        
        if msg_type == "book":
            self._book_update_count += 1
            await self._handle_book_update(data)
        elif msg_type == "price_change":
            # Price change events - extract book data from price_changes
            await self._handle_price_change(data)
        elif "asset_id" in data and ("bids" in data or "asks" in data):
            # Initial snapshot format (no type field, just raw book data)
            self._book_update_count += 1
            await self._handle_book_update(data)

    async def _handle_price_change(self, data: dict) -> None:
        """Apply a complete incremental batch before checking affected events."""
        received_at = time.time()
        affected_events: Dict[str, NegativeRiskEvent] = {}
        for change in data.get("price_changes", []):
            if not isinstance(change, dict):
                continue
            token_id = change.get("asset_id")
            if not token_id:
                continue
            book = self.order_books.apply_price_change(
                token_id,
                change,
                timestamp=received_at,
            )
            if book is None:
                self._price_change_ignored_count += 1
                if token_id in self.token_to_event:
                    self._tokens_awaiting_snapshot.add(token_id)
                continue
            self._price_change_count += 1
            event = self.token_to_event.get(token_id)
            if event:
                affected_events[event.event_id] = event

        for event in affected_events.values():
            await self._detect_event(event)

    async def _handle_book_update(self, data: dict) -> None:
        """Handle order book update."""
        token_id = data.get("asset_id")
        if not token_id or token_id not in self.token_to_event:
            return

        # Update order book
        self.order_books.update(token_id, data)
        self._tokens_awaiting_snapshot.discard(token_id)
        if not self._snapshot_gate_open:
            self._refresh_snapshot_gate()

        # Find event for this token
        event = self.token_to_event.get(token_id)
        if not event:
            if self._book_update_count <= 5:
                logger.debug("NegRisk: Token not mapped to event", token_id=token_id)
            return

        await self._detect_event(event)

    async def _detect_event(self, event: NegativeRiskEvent) -> None:
        """Detect only when every event leg is complete, fresh, and synchronized."""
        if self._running and not self._snapshot_gate_open:
            return

        books = [self.order_books.get(token_id) for token_id in event.get_all_token_ids()]
        if not books or any(book is None for book in books):
            return

        now = time.time()
        last_check = self._last_check_time.get(event.event_id, 0)
        if now - last_check < self._min_check_interval:
            return

        timestamps = [book.timestamp for book in books if book is not None]
        max_age = max(now - timestamp for timestamp in timestamps)
        max_skew = max(timestamps) - min(timestamps)
        if (
            max_age > self.book_max_age_seconds
            or max_skew > self.book_max_skew_seconds
        ):
            self._stale_event_skip_count += 1
            return

        self._last_check_time[event.event_id] = now
        opportunity = self.detector.detect(event, self.order_books)

        if opportunity:
            # Log with distinct marker for easy searching
            logger.info(
                "🚀 [OPPORTUNITY] Negative Risk Arbitrage Detected",
                event_title=opportunity.event.title[:60],
                strategy=opportunity.strategy.value,
                net_profit=f"{opportunity.net_profit_pct:.2%}",
                profit_usdc=f"${opportunity.net_profit_usdc:.2f}",
                threshold=f"{self.profit_threshold:.2%}",
            )
            
            if self.on_opportunity:
                self.on_opportunity(opportunity)
