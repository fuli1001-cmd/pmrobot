"""Real-time market monitoring using WebSocket."""

import asyncio
import json
import time
from typing import Callable, Dict, List, Optional, Set

import websockets
from websockets.exceptions import ConnectionClosed

from config.constants import (
    CLOB_WS_URL, 
    get_profit_threshold,
    ESTIMATED_MINT_GAS_COST_USD,
    MIN_SHORT_ARBITRAGE_SIZE,
    SHORT_ARBITRAGE_THRESHOLD,
)
from models.market import Market, NegativeRiskEvent, NegativeRiskStrategy, NegativeRiskArbitrageOpportunity
from models.order import ArbitrageOpportunity, ShortArbitrageOpportunity, OrderBook, OrderBookLevel
from utils.logger import get_logger

logger = get_logger(__name__)


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

    def get(self, token_id: str) -> Optional[OrderBook]:
        """Get order book for a token."""
        return self._books.get(token_id)

    def get_pair(self, yes_token_id: str, no_token_id: str) -> tuple:
        """Get order books for a Yes/No pair."""
        return self.get(yes_token_id), self.get(no_token_id)


class ArbitrageDetector:
    """
    Detects arbitrage opportunities from order books.
    """

    def __init__(
        self,
        profit_threshold: float = 0.008,
        trade_size: float = 100.0,
        max_slippage: float = 0.002,
        cooldown_seconds: float = 0.0,  # 0 for dry-run, use 5-10 for real trading
    ):
        """
        Initialize the arbitrage detector.

        Args:
            profit_threshold: Minimum profit threshold (e.g., 0.008 = 0.8%)
            trade_size: Trade size in USDC
            max_slippage: Maximum allowed slippage
            cooldown_seconds: Seconds to wait before re-detecting same market
        """
        self.profit_threshold = profit_threshold
        self.trade_size = trade_size
        self.max_slippage = max_slippage
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
            min_size=5.0,  # Lowered from 10 to capture more opportunities
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
        
        # Calculate effective trade size based on available depth
        depth_yes = book_yes.get_available_depth("bid", max_levels=5)
        depth_no = book_no.get_available_depth("bid", max_levels=5)
        min_depth = min(depth_yes, depth_no)
        effective_trade_size = min(self.trade_size, min_depth * 0.3)
        
        # Skip if below minimum for short arbitrage
        if effective_trade_size < MIN_SHORT_ARBITRAGE_SIZE:
            return None
        
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
            trade_size: Trade size in USDC per outcome
            max_slippage: Maximum allowed slippage
            cooldown_seconds: Seconds to wait before re-detecting same event
        """
        self.profit_threshold = profit_threshold
        self.trade_size = trade_size
        self.max_slippage = max_slippage
        self.cooldown_seconds = cooldown_seconds
        self._check_count = 0
        self._last_opportunity: dict[str, float] = {}  # event_id -> timestamp
        self._min_trade_size = 10.0  # Floor: don't bother below $10

    def _effective_trade_size(
        self,
        event: NegativeRiskEvent,
        order_books: "OrderBookManager",
        side: str = "yes",
    ) -> float:
        """
        Return the maximum trade size supported by the shallowest leg.

        Clamps self.trade_size down to avoid exceeding available depth.
        """
        n = len(event.outcomes)
        if n == 0:
            return self.trade_size
        per_leg = self.trade_size / n
        min_depth = float("inf")
        for outcome in event.outcomes:
            token_id = outcome.token_id_yes if side == "yes" else outcome.token_id_no
            book = order_books.get(token_id)
            if book:
                depth = book.get_available_depth(side="ask")
                min_depth = min(min_depth, depth)
            else:
                return self._min_trade_size  # Missing book → fall back to min
        # Scale back so the smallest leg still fits; don't go below floor
        capped = min(self.trade_size, min_depth * n * 0.8)  # 80% of shallowest leg
        return max(capped, self._min_trade_size)

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

        # Use dynamic threshold based on outcome count
        effective_threshold = get_profit_threshold(event.outcome_count)

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
        
        # CRITICAL FIX: Skip events with too much missing data
        # If more than 30% of tokens have no liquidity, this is not a real opportunity
        total_tokens = event.outcome_count * 2  # Yes + No for each outcome
        missing_ratio = missing_books / total_tokens
        
        if missing_ratio > 0.3:
            if self._check_count % 500 == 1:
                logger.debug(
                    "Skipping event due to insufficient liquidity",
                    event_title=event.title[:40],
                    missing_ratio=f"{missing_ratio:.1%}",
                    missing_books=missing_books,
                    total_tokens=total_tokens,
                )
            return None  # Not enough liquidity for real arbitrage
        
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
        effective_size = self._effective_trade_size(event, order_books, side="yes")
        outcome_prices = {}
        total_yes_cost = 0.0

        for outcome in event.outcomes:
            book_yes = order_books.get(outcome.token_id_yes)
            if not book_yes or not book_yes.asks:
                return None

            # Get best ask price for Yes
            avg_price = book_yes.calculate_average_buy_price(effective_size / len(event.outcomes))
            if avg_price is None:
                return None

            outcome_prices[outcome.condition_id] = avg_price
            total_yes_cost += avg_price

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
            trade_size_usdc=effective_size,
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
        effective_size = self._effective_trade_size(event, order_books, side="no")
        outcome_prices = {}
        total_no_cost = 0.0
        n = event.outcome_count

        for outcome in event.outcomes:
            book_no = order_books.get(outcome.token_id_no)
            if not book_no or not book_no.asks:
                return None

            # Get best ask price for No
            avg_price = book_no.calculate_average_buy_price(effective_size / n)
            if avg_price is None:
                return None

            outcome_prices[outcome.condition_id] = avg_price
            total_no_cost += avg_price

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
            trade_size_usdc=effective_size,
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
        effective_size = self._effective_trade_size(event, order_books, side="no")

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
            no_avg_price = book_no.calculate_average_buy_price(effective_size / n)
            if no_avg_price is None:
                return None
            
            outcome_no_prices[outcome.condition_id] = no_avg_price
            total_no_cost += no_avg_price

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
            trade_size_usdc=effective_size,
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
            on_opportunity: Callback when long arbitrage opportunity is detected
            on_short_opportunity: Callback when short (Mint+Sell) opportunity is detected
        """
        self.markets = {m.condition_id: m for m in markets}
        self.token_to_market: Dict[str, Market] = {}
        for m in markets:
            self.token_to_market[m.token_id_yes] = m
            self.token_to_market[m.token_id_no] = m

        self.profit_threshold = profit_threshold
        self.order_books = OrderBookManager()
        self.detector = ArbitrageDetector(
            profit_threshold=profit_threshold,
            trade_size=trade_size,
            max_slippage=max_slippage,
        )
        self.on_opportunity = on_opportunity
        self.on_short_opportunity = on_short_opportunity

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_delay = 1.0
        self._heartbeat_interval = 30.0  # Ping every 30 seconds
        
        # Debug counters
        self._message_count = 0
        self._book_update_count = 0
        self._last_log_time = time.time()

    async def start(self) -> None:
        """Start monitoring markets."""
        self._running = True
        logger.info("Starting market monitor", num_markets=len(self.markets))

        while self._running:
            try:
                await self._connect_and_subscribe()
            except ConnectionClosed as e:
                log_fn = logger.info if e.code == 1006 else logger.warning
                log_fn(
                    "WebSocket connection closed",
                    code=e.code,
                    reason=e.reason,
                )
            except Exception as e:
                error_text = repr(e)
                if _is_transient_ws_error(e):
                    logger.info("WebSocket transient disconnect", error=error_text)
                else:
                    logger.error("WebSocket error", error=error_text)

            if self._running:
                logger.info(
                    "Reconnecting in seconds",
                    delay=self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60.0)

    async def stop(self) -> None:
        """Stop monitoring markets."""
        self._running = False
        if self._ws:
            await self._ws.close()

    async def update_markets(self, new_markets: list) -> None:
        """
        Dynamically add new Binary markets to the monitoring set.

        Subscribes the WebSocket to any newly-added tokens so the
        ArbitrageDetector will start evaluating them immediately.
        """
        new_tokens_to_sub = []
        count = 0
        for market in new_markets:
            if market.condition_id not in self.markets:
                self.markets[market.condition_id] = market
                self.token_to_market[market.token_id_yes] = market
                self.token_to_market[market.token_id_no] = market
                new_tokens_to_sub.extend([market.token_id_yes, market.token_id_no])
                count += 1

        if new_tokens_to_sub and self._ws:
            subscribe_msg = {
                "type": "subscribe",
                "channel": "book",
                "assets_ids": new_tokens_to_sub,
            }
            await self._ws.send(json.dumps(subscribe_msg))
            logger.info(
                "Subscribed to new Binary tokens",
                new_markets=count,
                new_tokens=len(new_tokens_to_sub),
            )

    async def _connect_and_subscribe(self) -> None:
        """Connect to WebSocket and subscribe to markets."""
        async with websockets.connect(CLOB_WS_URL) as ws:
            self._ws = ws
            self._reconnect_delay = 1.0  # Reset on successful connection

            # I4: Discard stale order-book state from previous connection
            self.order_books = OrderBookManager()

            # Subscribe to all token order books
            token_ids = []
            for market in self.markets.values():
                token_ids.extend([market.token_id_yes, market.token_id_no])

            subscribe_msg = {
                "type": "subscribe",
                "channel": "book",
                "assets_ids": token_ids,
            }
            await ws.send(json.dumps(subscribe_msg))
            logger.info("Subscribed to order books", num_tokens=len(token_ids))

            # Run message handler and heartbeat concurrently
            await asyncio.gather(
                self._process_messages(ws),
                self._heartbeat(ws),
            )

    async def _heartbeat(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Send periodic ping to keep connection alive."""
        while self._running:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                await ws.ping()
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
                    markets_with_books=len(self.order_books._books),
                )
                self._last_log_time = now

        except json.JSONDecodeError as e:
            # Transient parse errors are normal during reconnections
            if message and not message.isspace():
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
            # price_change events don't carry full order book depth data,
            # so they cannot support depth penetration calculations.
            # Ignore them; we rely on 'book' channel updates instead.
            pass
        elif "asset_id" in data and ("bids" in data or "asks" in data):
            # Initial snapshot format (no type field, just raw book data)
            self._book_update_count += 1
            await self._handle_book_update(data)

    async def _handle_book_update(self, data: dict) -> None:
        """Handle order book update."""
        token_id = data.get("asset_id")
        if not token_id:
            return

        # Update order book
        self.order_books.update(token_id, data)

        # Find market for this token
        market = self.token_to_market.get(token_id)
        if not market:
            return

        # Get both order books
        book_yes = self.order_books.get(market.token_id_yes)
        book_no = self.order_books.get(market.token_id_no)

        if not book_yes or not book_no:
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
        self.order_books = OrderBookManager()
        self.detector = NegativeRiskArbitrageDetector(
            profit_threshold=profit_threshold,
            trade_size=trade_size,
            max_slippage=max_slippage,
        )
        self.on_opportunity = on_opportunity

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_delay = 1.0
        self._heartbeat_interval = 30.0  # Ping every 30 seconds
        
        # Track which events have been checked recently to avoid duplicate checks
        self._last_check_time: Dict[str, float] = {}
        self._min_check_interval = 0.5  # Minimum 0.5 seconds between checks for same event
        
        # Debug counters
        self._message_count = 0
        self._book_update_count = 0
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
            try:
                await self._connect_and_monitor()
            except ConnectionClosed as e:
                log_fn = logger.info if e.code == 1006 else logger.warning
                log_fn(
                    "NegRisk WebSocket connection closed",
                    code=e.code,
                    reason=e.reason,
                )
            except Exception as e:
                error_text = repr(e)
                if _is_transient_ws_error(e):
                    logger.info("Negative Risk monitor transient disconnect", error=error_text)
                else:
                    logger.error("Negative Risk monitor error", error=error_text)

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def stop(self) -> None:
        """Stop monitoring."""
        logger.info("Stopping Negative Risk monitor")
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _connect_and_monitor(self) -> None:
        """Connect to WebSocket and start monitoring."""
        logger.info("NegRisk: Connecting to WebSocket", url=CLOB_WS_URL)
        async with websockets.connect(CLOB_WS_URL) as ws:
            self._ws = ws
            self._reconnect_delay = 1.0
            logger.info("NegRisk: WebSocket connected successfully")

            # I4: Discard stale order-book state from previous connection
            self.order_books = OrderBookManager()

            # Subscribe to all tokens
            all_tokens = list(self.token_to_event.keys())
            logger.info("NegRisk: About to subscribe", num_tokens=len(all_tokens))
            await self._subscribe(all_tokens)
            logger.info("NegRisk: Waiting for messages...")

            # Run message handler and heartbeat concurrently
            await asyncio.gather(
                self._process_messages(ws),
                self._heartbeat(ws),
            )

    async def _heartbeat(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Send periodic ping to keep connection alive."""
        while self._running:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                await ws.ping()
                logger.debug("NegRisk: Heartbeat ping sent")
            except Exception:
                break  # Connection closed

    async def _process_messages(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Process incoming WebSocket messages."""
        async for message in ws:
            await self._handle_message(message)

    async def update_events(self, new_events: List[NegativeRiskEvent]) -> None:
        """
        Dynamically update the monitored events list.
        Adds new events to the monitoring set and subscribes to them.
        """
        new_tokens_to_sub = []
        count = 0
        
        for event in new_events:
            if event.event_id not in self.events:
                self.events[event.event_id] = event
                # Add tokens
                for token_id in event.get_all_token_ids():
                    self.token_to_event[token_id] = event
                    new_tokens_to_sub.append(token_id)
                count += 1
        
        if new_tokens_to_sub:
            logger.info("Monitor found new events", new_events=count, new_tokens=len(new_tokens_to_sub))
            # If connected, subscribe immediately
            # websockets >=14: ClientConnection has no .closed; use .close_code
            if self._ws and getattr(self._ws, 'close_code', None) is None:
                await self._subscribe(new_tokens_to_sub)
        else:
            logger.debug("Monitor update called but no new events found")

    async def _subscribe(self, token_ids: List[str]) -> None:
        """Subscribe to order books for given tokens in batches."""
        # Polymarket WebSocket has limits on subscription message size
        # Split into batches of 2000 tokens max
        BATCH_SIZE = 2000
        total_batches = (len(token_ids) + BATCH_SIZE - 1) // BATCH_SIZE
        
        logger.info(
            "NegRisk: Subscribing in batches",
            total_tokens=len(token_ids),
            batch_size=BATCH_SIZE,
            num_batches=total_batches,
        )
        
        for i in range(0, len(token_ids), BATCH_SIZE):
            batch = token_ids[i:i + BATCH_SIZE]
            batch_num = (i // BATCH_SIZE) + 1
            
            subscription = {
                "type": "subscribe",
                "channel": "book",
                "assets_ids": batch,  # Fixed: was "asset_ids", Polymarket expects "assets_ids"
            }
            subscription_json = json.dumps(subscription)
            
            logger.info(
                "NegRisk: Sending subscription batch",
                batch=f"{batch_num}/{total_batches}",
                num_tokens=len(batch),
                msg_size_bytes=len(subscription_json),
            )
            
            await self._ws.send(subscription_json)
            
            # Small delay between batches to avoid overwhelming the server
            if i + BATCH_SIZE < len(token_ids):
                await asyncio.sleep(0.1)
        
        logger.info("NegRisk: All subscription batches sent successfully", total_tokens=len(token_ids))

    async def _handle_message(self, message: str) -> None:
        """Handle incoming WebSocket message."""
        self._message_count += 1
        
        try:
            # Skip empty messages
            if not message or message.isspace():
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
                    tokens_with_books=len([t for t in self.token_to_event.keys() if self.order_books.get(t)]),
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
        """Handle price_change event."""
        price_changes = data.get("price_changes", [])
        for change in price_changes:
            if "asset_id" in change:
                await self._handle_book_update(change)

    async def _handle_book_update(self, data: dict) -> None:
        """Handle order book update."""
        token_id = data.get("asset_id")
        if not token_id:
            return

        # Update order book
        self.order_books.update(token_id, data)

        # Find event for this token
        event = self.token_to_event.get(token_id)
        if not event:
            if self._book_update_count <= 5:
                logger.debug("NegRisk: Token not mapped to event", token_id=token_id)
            return

        # Rate limit: avoid checking same event too frequently
        now = time.time()
        last_check = self._last_check_time.get(event.event_id, 0)
        
        # Debug log for first few updates to check flow
        self._handle_update_count = getattr(self, '_handle_update_count', 0) + 1
        if self._handle_update_count <= 5:
            logger.debug(
                "NegRisk: Handling update", 
                token=token_id, 
                event_id=event.event_id,
                time_diff=now-last_check
            )

        if now - last_check < self._min_check_interval:
            return
        
        self._last_check_time[event.event_id] = now

        # Check for arbitrage
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

