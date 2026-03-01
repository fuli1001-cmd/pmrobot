"""Polymarket exchange adapter.

Wraps existing core/scanner.py and core/executor.py behind the
unified BaseExchange interface.  Does NOT modify existing core modules —
acts purely as a delegate adapter.
"""

import time
from typing import List, Optional

from exchanges.base import (
    BaseExchange,
    BetResult,
    OutcomeSide,
    Platform,
    UnifiedMarket,
    UnifiedOdds,
)
from core.scanner import MarketScanner
from core.executor import OrderExecutor, create_executor
from core.monitor import OrderBookManager
from models.market import Market
from models.order import Order, OrderSide, OrderType, OrderStatus
from config.settings import get_settings
from utils.logger import get_logger

logger = get_logger(__name__)


class PolymarketExchange(BaseExchange):
    """Polymarket adapter implementing BaseExchange.

    Delegates market discovery to MarketScanner (Gamma REST API) and
    order execution to OrderExecutor (CLOB API).
    """

    platform = Platform.POLYMARKET

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._scanner: Optional[MarketScanner] = None
        self._executor: Optional[OrderExecutor] = None
        self._order_book_mgr = OrderBookManager()
        # Cache: condition_id -> Market (original model)
        self._markets_cache: dict[str, Market] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Initialise scanner and executor."""
        self._scanner = MarketScanner()
        await self._scanner.__aenter__()
        try:
            self._executor = create_executor(dry_run=self.dry_run)
        except Exception as e:
            logger.warning(
                "Polymarket executor init failed (non-fatal in dry-run)",
                error=str(e),
            )
            if not self.dry_run:
                raise
        logger.info("PolymarketExchange connected", dry_run=self.dry_run)

    async def disconnect(self) -> None:
        """Clean up scanner HTTP client."""
        if self._scanner:
            await self._scanner.__aexit__(None, None, None)
        logger.info("PolymarketExchange disconnected")

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def get_markets(self, sport: Optional[str] = None) -> List[UnifiedMarket]:
        """Fetch active Polymarket markets.

        Args:
            sport: If provided, only return markets tagged with this sport.

        Returns:
            List of UnifiedMarket.
        """
        if not self._scanner:
            return []

        raw_markets = await self._scanner.fetch_all_markets(fee_free_only=True)

        results: List[UnifiedMarket] = []
        for m in raw_markets:
            # Optional sport filter
            if sport and sport.lower() not in [t.lower() for t in m.tags]:
                continue

            um = _to_unified_market(m)
            results.append(um)
            self._markets_cache[m.condition_id] = m

        logger.info(
            "Polymarket markets fetched",
            total=len(raw_markets),
            filtered=len(results),
            sport=sport,
        )
        return results

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------

    async def get_odds(
        self, market_id: str, trade_size: float = 50.0
    ) -> Optional[UnifiedOdds]:
        """Get current best prices from cached order books.

        For Polymarket the ``market_id`` is the ``condition_id``.
        Prices are best-ask (buy side) which is what we pay.

        Note: In the full pipeline, the MarketMonitor continuously updates
        order books via WebSocket.  This adapter reads whatever is currently
        cached in the OrderBookManager.
        """
        market = self._markets_cache.get(market_id)
        if not market:
            return None

        book_yes = self._order_book_mgr.get(market.token_id_yes)
        book_no = self._order_book_mgr.get(market.token_id_no)

        price_yes = book_yes.best_ask if book_yes else 0.0
        price_no = book_no.best_ask if book_no else 0.0
        depth_yes = book_yes.get_available_depth("ask") if book_yes else 0.0
        depth_no = book_no.get_available_depth("ask") if book_no else 0.0

        return UnifiedOdds(
            platform=Platform.POLYMARKET,
            market_id=market_id,
            price_yes=price_yes,
            price_no=price_no,
            max_size_yes=depth_yes,
            max_size_no=depth_no,
            timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def place_bet(
        self,
        market_id: str,
        outcome: OutcomeSide,
        amount: float,
        min_odds: float,
    ) -> BetResult:
        """Place a FOK buy order on Polymarket.

        Args:
            market_id: condition_id of the market.
            outcome: YES or NO.
            amount: USDC to spend.
            min_odds: Minimum acceptable price (worst price we accept).

        Returns:
            BetResult.
        """
        start = time.time()
        market = self._markets_cache.get(market_id)
        if not market:
            return BetResult(
                status=BetResult.Status.FAILED,
                platform=Platform.POLYMARKET,
                market_id=market_id,
                outcome=outcome,
                error_message="Market not in cache",
            )

        token_id = (
            market.token_id_yes
            if outcome == OutcomeSide.YES
            else market.token_id_no
        )

        if self.dry_run or not self._executor:
            logger.info(
                "DRY RUN: Polymarket would place bet",
                market=market.slug,
                outcome=outcome.value,
                amount=amount,
            )
            return BetResult(
                status=BetResult.Status.SKIPPED,
                platform=Platform.POLYMARKET,
                market_id=market_id,
                outcome=outcome,
                amount=amount,
                effective_odds=min_odds,
                execution_time_ms=(time.time() - start) * 1000,
            )

        # Build and submit FOK order
        order = Order(
            token_id=token_id,
            side=OrderSide.BUY,
            price=min_odds,
            size=amount / min_odds,
            order_type=OrderType.FOK,
        )
        filled_order = await self._executor._submit_order(order)
        elapsed = (time.time() - start) * 1000

        if filled_order.status == OrderStatus.FILLED:
            return BetResult(
                status=BetResult.Status.SUCCESS,
                platform=Platform.POLYMARKET,
                market_id=market_id,
                outcome=outcome,
                amount=amount,
                effective_odds=filled_order.filled_avg_price,
                execution_time_ms=elapsed,
            )
        else:
            return BetResult(
                status=BetResult.Status.FAILED,
                platform=Platform.POLYMARKET,
                market_id=market_id,
                outcome=outcome,
                amount=amount,
                error_message="FOK order rejected",
                execution_time_ms=elapsed,
            )

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    async def get_balance(self) -> float:
        """Get USDC balance via executor."""
        if self._executor:
            return await self._executor.get_account_balance()
        return 10000.0 if self.dry_run else 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def update_cache(
        self, raw_markets: List[Market], sport: Optional[str] = None
    ) -> List[UnifiedMarket]:
        """Populate internal cache from externally-fetched Market objects.

        Use this when another component (e.g. market refresher) has already
        fetched the full market list and we want to avoid a redundant API call.

        Args:
            raw_markets: Polymarket ``Market`` objects.
            sport: If provided, only include markets whose *tags* contain
                   this value (case-insensitive).

        Returns:
            Corresponding ``UnifiedMarket`` list.
        """
        results: List[UnifiedMarket] = []
        for m in raw_markets:
            if sport and sport.lower() not in [t.lower() for t in m.tags]:
                continue
            self._markets_cache[m.condition_id] = m
            results.append(_to_unified_market(m))
        return results

    @property
    def order_book_manager(self) -> OrderBookManager:
        """Expose order book manager for Monitor to update."""
        return self._order_book_mgr

    def get_raw_market(self, market_id: str) -> Optional[Market]:
        """Get the original Polymarket Market model by condition_id."""
        return self._markets_cache.get(market_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_unified_market(m: Market) -> UnifiedMarket:
    """Convert a Polymarket Market to UnifiedMarket."""
    sport = ""
    for tag in m.tags:
        if tag.lower() in ("sports", "football", "basketball", "baseball",
                           "hockey", "tennis", "soccer", "mma", "boxing"):
            sport = tag.lower()
            break

    return UnifiedMarket(
        platform=Platform.POLYMARKET,
        market_id=m.condition_id,
        question=m.question,
        sport=sport,
        event_name=m.question,  # Polymarket doesn't separate event name
        active=m.active and not m.closed,
        metadata={
            "slug": m.slug,
            "tags": m.tags,
            "token_id_yes": m.token_id_yes,
            "token_id_no": m.token_id_no,
            "liquidity": m.liquidity,
            "volume_24h": m.volume_24h,
        },
    )
