"""Real-time market monitoring using WebSocket."""

import asyncio
import json
import time
from typing import Callable, Dict, List, Optional, Set

import websockets
from websockets.exceptions import ConnectionClosed

from config.constants import CLOB_WS_URL
from models.market import Market, NegativeRiskEvent, NegativeRiskStrategy, NegativeRiskArbitrageOpportunity
from models.order import ArbitrageOpportunity, OrderBook, OrderBookLevel
from utils.logger import get_logger

logger = get_logger(__name__)


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
        cooldown_seconds: float = 60.0,
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

        # Dynamic position sizing based on available depth
        # Use 30% of minimum available depth as safe position size
        depth_yes = book_yes.get_available_depth("ask", max_levels=5)
        depth_no = book_no.get_available_depth("ask", max_levels=5)
        min_depth = min(depth_yes, depth_no)
        effective_trade_size = min(self.trade_size, min_depth * 0.3)
        
        # Skip if insufficient depth
        if effective_trade_size < 10.0:  # Minimum $10 trade
            return None

        # Calculate average buy prices using depth penetration
        avg_price_yes = book_yes.calculate_average_buy_price(effective_trade_size / 2)
        avg_price_no = book_no.calculate_average_buy_price(effective_trade_size / 2)

        if avg_price_yes is None or avg_price_no is None:
            logger.debug(
                "Insufficient liquidity",
                market=market.slug,
                trade_size=effective_trade_size,
            )
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
        cooldown_seconds: float = 60.0,
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

        # Try Buy-All-Yes first (often more common)
        opportunity = self._detect_buy_all_yes(event, order_books)
        if opportunity and opportunity.is_profitable(self.profit_threshold):
            # Record opportunity time for cooldown
            self._last_opportunity[event_id] = now
            return opportunity

        # Try Buy-All-No
        opportunity = self._detect_buy_all_no(event, order_books)
        if opportunity and opportunity.is_profitable(self.profit_threshold):
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
        outcome_prices = {}
        total_yes_cost = 0.0

        for outcome in event.outcomes:
            book_yes = order_books.get(outcome.token_id_yes)
            if not book_yes or not book_yes.asks:
                return None

            # Get best ask price for Yes
            avg_price = book_yes.calculate_average_buy_price(self.trade_size / len(event.outcomes))
            if avg_price is None:
                return None

            outcome_prices[outcome.condition_id] = avg_price
            total_yes_cost += avg_price

        # Calculate profit
        expected_payout = 1.0  # Only one Yes will be worth 1
        gross_profit = expected_payout - total_yes_cost

        # Periodic debug log
        self._check_count += 1
        if self._check_count % 500 == 1:
            logger.info(
                "NegRisk Price sample (Buy-All-Yes)",
                event=event.title[:40],
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
            trade_size_usdc=self.trade_size,
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
        outcome_prices = {}
        total_no_cost = 0.0
        n = event.outcome_count

        for outcome in event.outcomes:
            book_no = order_books.get(outcome.token_id_no)
            if not book_no or not book_no.asks:
                return None

            # Get best ask price for No
            avg_price = book_no.calculate_average_buy_price(self.trade_size / n)
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
                event=event.title[:40],
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
            trade_size_usdc=self.trade_size,
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
    ):
        """
        Initialize the market monitor.

        Args:
            markets: List of markets to monitor
            profit_threshold: Minimum profit threshold
            trade_size: Trade size in USDC
            max_slippage: Maximum allowed slippage
            on_opportunity: Callback when opportunity is detected
        """
        self.markets = {m.condition_id: m for m in markets}
        self.token_to_market: Dict[str, Market] = {}
        for m in markets:
            self.token_to_market[m.token_id_yes] = m
            self.token_to_market[m.token_id_no] = m

        self.order_books = OrderBookManager()
        self.detector = ArbitrageDetector(
            profit_threshold=profit_threshold,
            trade_size=trade_size,
            max_slippage=max_slippage,
        )
        self.on_opportunity = on_opportunity

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
                logger.warning(
                    "WebSocket connection closed",
                    code=e.code,
                    reason=e.reason,
                )
            except Exception as e:
                logger.error("WebSocket error", error=str(e))

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

    async def _connect_and_subscribe(self) -> None:
        """Connect to WebSocket and subscribe to markets."""
        async with websockets.connect(CLOB_WS_URL) as ws:
            self._ws = ws
            self._reconnect_delay = 1.0  # Reset on successful connection

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
            logger.error("Failed to parse WebSocket message", error=str(e))
        except Exception as e:
            logger.debug("Error handling message", error=str(e))

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
            # Price change events - extract book data from price_changes
            await self._handle_price_change(data)
        elif "asset_id" in data and ("bids" in data or "asks" in data):
            # Initial snapshot format (no type field, just raw book data)
            self._book_update_count += 1
            await self._handle_book_update(data)

    async def _handle_price_change(self, data: dict) -> None:
        """Handle price_change event and update order books."""
        market_id = data.get("market")
        price_changes = data.get("price_changes", [])
        
        for change in price_changes:
            if not isinstance(change, dict):
                continue
            
            asset_id = change.get("asset_id")
            if not asset_id:
                continue
            
            # Extract price from price_change
            price = change.get("price")
            if price is None:
                continue
            
            # Get existing book or create minimal one
            book = self.order_books.get(asset_id)
            if book and book.asks:
                # Update best ask with new price (simplified)
                # This is a rough approximation since price_change doesn't give full book
                pass
            
            # Find market and check for arbitrage
            market = self.token_to_market.get(asset_id)
            if market:
                book_yes = self.order_books.get(market.token_id_yes)
                book_no = self.order_books.get(market.token_id_no)
                
                if book_yes and book_no:
                    opportunity = self.detector.detect(market, book_yes, book_no)
                    if opportunity and self.on_opportunity:
                        self.on_opportunity(opportunity)

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

        # Check for arbitrage
        opportunity = self.detector.detect(market, book_yes, book_no)

        if opportunity:
            # Log with distinct marker for easy searching
            logger.info(
                "🚀 [OPPORTUNITY] Binary Market Arbitrage Detected",
                market=market.ticker,
                net_profit=f"{opportunity.net_profit_pct:.2%}",
                profit_usdc=f"${opportunity.net_profit_usdc:.2f}",
                threshold=f"{self.profit_threshold:.2%}",
            )
            
            if self.on_opportunity:
                self.on_opportunity(opportunity)


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
            except Exception as e:
                logger.error("Negative Risk monitor error", error=str(e))
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
                "asset_ids": batch,
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
                logger.debug("NegRisk: Non-JSON message received", msg_preview=message[:100])
        except Exception as e:
            logger.debug("Error handling NegRisk message", error=str(e))

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
            return

        # Rate limit: avoid checking same event too frequently
        now = time.time()
        last_check = self._last_check_time.get(event.event_id, 0)
        if now - last_check < self._min_check_interval:
            return
        
        self._last_check_time[event.event_id] = now

        # Check for arbitrage
        opportunity = self.detector.detect(event, self.order_books)

        if opportunity:
            # Log with distinct marker for easy searching
            logger.info(
                "🚀 [OPPORTUNITY] Negative Risk Arbitrage Detected",
                event=opportunity.event.title[:60],
                strategy=opportunity.strategy.value,
                net_profit=f"{opportunity.net_profit_pct:.2%}",
                profit_usdc=f"${opportunity.net_profit_usdc:.2f}",
                threshold=f"{self.profit_threshold:.2%}",
            )
            
            if self.on_opportunity:
                self.on_opportunity(opportunity)

