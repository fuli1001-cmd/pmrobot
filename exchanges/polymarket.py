"""Polymarket exchange adapter.

Wraps existing core/scanner.py and core/executor.py behind the
unified BaseExchange interface.  Does NOT modify existing core modules —
acts purely as a delegate adapter.
"""

import asyncio
import time
from typing import List, Optional

import httpx

from config.constants import CLOB_API_BASE_URL, GAMMA_API_BASE_URL
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
        self._http: Optional[httpx.AsyncClient] = None
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
        self._http = httpx.AsyncClient(timeout=15.0)
        try:
            self._executor = create_executor(dry_run=self.dry_run)
        except Exception as e:
            logger.warning(
                "Polymarket executor init failed (non-fatal in dry-run)",
                error=repr(e),
            )
            if not self.dry_run:
                raise
        logger.info("PolymarketExchange connected", dry_run=self.dry_run)

    async def disconnect(self) -> None:
        """Clean up scanner HTTP client."""
        if self._http:
            await self._http.aclose()
        if self._scanner:
            await self._scanner.__aexit__(None, None, None)
        logger.info("PolymarketExchange disconnected")

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def get_markets(self, sport: Optional[str] = None) -> List[UnifiedMarket]:
        """Fetch active Polymarket markets.

        When *sport* is provided the Events API (``tag_slug=<sport>``) is
        used because the markets endpoint does not expose tags.

        Args:
            sport: If provided, queries the Events API with this value as
                   ``tag_slug`` (e.g. ``"sports"``, ``"soccer"``, ``"nba"``).
                   If *None*, falls back to the general markets endpoint.

        Returns:
            List of UnifiedMarket.
        """
        if not self._scanner:
            return []

        # Use the dedicated Events-API path for sport-tagged markets
        if sport:
            raw_markets = await self._scanner.fetch_sports_markets(tag_slug=sport)
        else:
            raw_markets = await self._scanner.fetch_all_markets(fee_free_only=True)

        results: List[UnifiedMarket] = []
        for m in raw_markets:
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
        self, market_id: str, trade_size: float = 50.0, *, live: bool = False,
    ) -> Optional[UnifiedOdds]:
        """Get current best prices.

        For Polymarket the ``market_id`` is the ``condition_id``.
        Prices are best-ask (buy side) which is what we pay.

        Args:
            market_id: Polymarket condition ID.
            trade_size: Intended trade size (for depth check).
            live: If *True*, re-fetch prices from Gamma API baseline
                  **and** CLOB order-book depth.  When the CLOB book has
                  liquidity, the ``best_ask`` is used as the price signal
                  (it is the actual FOK fill price).  Falls back to Gamma
                  ``outcomePrices`` when CLOB is empty.
        """
        market = self._markets_cache.get(market_id)
        if not market:
            return None

        if live:
            return await self._fetch_live_odds(market)

        book_yes = self._order_book_mgr.get(market.token_id_yes)
        book_no = self._order_book_mgr.get(market.token_id_no)

        price_yes = book_yes.best_ask if book_yes else 0.0
        price_no = book_no.best_ask if book_no else 0.0
        depth_yes = book_yes.get_available_depth("ask") if book_yes else 0.0
        depth_no = book_no.get_available_depth("ask") if book_no else 0.0

        # Fallback: if order book is empty (no WebSocket feed), use cached
        # outcome prices from the Gamma / Events API last scan.
        if price_yes <= 0 and market.outcome_price_yes > 0:
            price_yes = market.outcome_price_yes
        if price_no <= 0 and market.outcome_price_no > 0:
            price_no = market.outcome_price_no

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
    # CLOB order-book helpers
    # ------------------------------------------------------------------

    async def _fetch_clob_book(self, token_id: str) -> dict:
        """Fetch the CLOB order book for a single token.

        Returns a dict with keys:
          - ``best_ask``: float (lowest ask price, 0.0 if empty)
          - ``best_bid``: float (highest bid price, 0.0 if empty)
          - ``ask_count``: int (number of ask levels)
          - ``bid_count``: int (number of bid levels)
          - ``ask_depth``: float (total ask size in shares)
          - ``bid_depth``: float (total bid size in shares)
        """
        empty = dict(best_ask=0.0, best_bid=0.0, ask_count=0,
                     bid_count=0, ask_depth=0.0, bid_depth=0.0)
        if not self._http:
            return empty
        try:
            resp = await self._http.get(
                f"{CLOB_API_BASE_URL}/book",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            data = resp.json()
            asks = data.get("asks", [])
            bids = data.get("bids", [])
            # CLOB /book sorts asks DESCENDING (worst first) and bids
            # ASCENDING (worst first).  Best ask = asks[-1] (lowest),
            # best bid = bids[-1] (highest).
            best_ask = float(asks[-1]["price"]) if asks else 0.0
            best_bid = float(bids[-1]["price"]) if bids else 0.0
            # Depth in USDC: sum(size * price) per level
            ask_depth_usd = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks)
            bid_depth_usd = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids)
            return dict(
                best_ask=best_ask, best_bid=best_bid,
                ask_count=len(asks), bid_count=len(bids),
                ask_depth=ask_depth_usd, bid_depth=bid_depth_usd,
            )
        except Exception as e:
            logger.debug("CLOB book fetch failed", token_id=token_id[:20], error=repr(e))
            return empty

    async def _fetch_clob_midpoint(self, token_id: str) -> float:
        """Fetch the CLOB midpoint price for a token.

        Returns the midpoint (average of best bid and best ask) or 0.0
        if unavailable.
        """
        if not self._http:
            return 0.0
        try:
            resp = await self._http.get(
                f"{CLOB_API_BASE_URL}/midpoint",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("mid", 0.0))
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Live price fetch (Gamma API + CLOB depth)
    # ------------------------------------------------------------------

    async def _fetch_live_odds(self, market: "Market") -> Optional[UnifiedOdds]:
        """Re-fetch prices using Gamma API + CLOB order-book depth.

        Strategy:
        1. Fetch Gamma ``outcomePrices`` as baseline price signal.
        2. Concurrently fetch CLOB order books for both YES and NO tokens.
        3. If a CLOB book has depth, prefer its ``best_ask`` as the price
           (this is the actual fill price for a FOK order, more accurate
           than the Gamma AMM snapshot).
        4. Return ``max_size_yes`` / ``max_size_no`` from CLOB depth so
           downstream code can skip markets with empty order books.
        """
        if not self._http:
            return self._cached_odds(market)

        try:
            # ── Step 1: Gamma baseline prices ──
            gamma_resp = await self._http.get(
                f"{GAMMA_API_BASE_URL}/markets",
                params={"condition_ids": market.condition_id},
            )
            gamma_resp.raise_for_status()
            data = gamma_resp.json()

            if not data or not isinstance(data, list) or len(data) == 0:
                logger.debug(
                    "Gamma /markets returned empty for condition_id",
                    condition_id=market.condition_id[:20],
                )
                return self._cached_odds(market)

            mk = data[0]
            raw_prices = mk.get("outcomePrices", "")

            import json
            try:
                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            except (json.JSONDecodeError, TypeError):
                prices = []

            if len(prices) >= 2:
                gamma_yes = float(prices[0])
                gamma_no = float(prices[1])
            else:
                return self._cached_odds(market)

            # ── Step 2: CLOB order-book depth (concurrent) ──
            book_yes, book_no = await asyncio.gather(
                self._fetch_clob_book(market.token_id_yes),
                self._fetch_clob_book(market.token_id_no),
            )

            # ── Step 3: Price selection ──
            # Prefer CLOB best_ask when available — it is the actual price
            # the FOK order would fill at.  Fall back to Gamma outcomePrices.
            price_yes = book_yes["best_ask"] if book_yes["best_ask"] > 0 else gamma_yes
            price_no = book_no["best_ask"] if book_no["best_ask"] > 0 else gamma_no

            # ── Step 4: Depth reporting ──
            # ask_depth is already in USDC (sum of size * price per level)
            max_size_yes = book_yes["ask_depth"]
            max_size_no = book_no["ask_depth"]

            logger.debug(
                "Live odds fetched",
                cid=market.condition_id[:16],
                gamma_y=f"{gamma_yes:.3f}", gamma_n=f"{gamma_no:.3f}",
                clob_y=f"{book_yes['best_ask']:.3f}" if book_yes["best_ask"] else "none",
                clob_n=f"{book_no['best_ask']:.3f}" if book_no["best_ask"] else "none",
                depth_y=f"${max_size_yes:.0f}", depth_n=f"${max_size_no:.0f}",
                asks_y=book_yes["ask_count"], asks_n=book_no["ask_count"],
            )

            return UnifiedOdds(
                platform=Platform.POLYMARKET,
                market_id=market.condition_id,
                price_yes=price_yes,
                price_no=price_no,
                max_size_yes=max_size_yes,
                max_size_no=max_size_no,
                timestamp=time.time(),
            )

        except Exception as e:
            logger.debug(
                "Gamma live price fetch failed, using cache",
                condition_id=market.condition_id[:20],
                error=repr(e),
            )
            return self._cached_odds(market)

    def _cached_odds(self, market: "Market") -> UnifiedOdds:
        """Build UnifiedOdds from the cached ``outcome_price_*`` fields."""
        return UnifiedOdds(
            platform=Platform.POLYMARKET,
            market_id=market.condition_id,
            price_yes=market.outcome_price_yes,
            price_no=market.outcome_price_no,
            max_size_yes=0.0,
            max_size_no=0.0,
            timestamp=0.0,  # sentinel: data is stale
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
        fetched the full market list and we want to warm the pricing cache.

        Args:
            raw_markets: Polymarket ``Market`` objects.
            sport: Ignored (kept for API compat).  Sport filtering is now
                   handled at the Events-API level.

        Returns:
            Corresponding ``UnifiedMarket`` list.
        """
        results: List[UnifiedMarket] = []
        for m in raw_markets:
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


# All tags that indicate a sports market.  When the caller passes
# sport="sports" we match against *any* of these, not just the literal.
SPORTS_TAGS = frozenset([
    "sports", "football", "basketball", "baseball",
    "hockey", "tennis", "soccer", "mma", "boxing",
    "cricket", "rugby", "golf", "motorsport", "nfl",
    "nba", "mlb", "nhl", "epl", "formula-1",
    # Esports
    "esports", "counter-strike", "league-of-legends", "valorant",
])


def _matches_sport(tags: list, sport: str) -> bool:
    """Return True if *tags* indicate a sports market.

    When ``sport`` is ``"sports"`` we match against the full SPORTS_TAGS set.
    Otherwise we match the single requested sport exactly.
    """
    lower_tags = {t.lower() for t in tags}
    if sport.lower() == "sports":
        return bool(lower_tags & SPORTS_TAGS)
    return sport.lower() in lower_tags


def _parse_end_date(iso: str) -> float:
    """Convert an ISO-8601 date string to a Unix timestamp.

    Returns 0.0 on failure or empty input.
    """
    if not iso:
        return 0.0
    try:
        from datetime import datetime, timezone
        # Handle trailing 'Z' and optional fractional seconds
        cleaned = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def _extract_teams_from_question(question: str) -> tuple[str, str]:
    """Best-effort extraction of team/player names from a PM question.

    Handles common patterns like:
      - "Tournament: Team A vs Team B"
      - "Team A vs Team B"
      - "Will Team A beat Team B?"

    Returns (team_a, team_b) or ("", "") if no match.
    """
    import re
    q = question.strip()

    # Strip leading tournament/venue prefix before colon
    # e.g. "Lugano: Stricker vs Grenier" → "Stricker vs Grenier"
    if ":" in q:
        q = q.split(":", 1)[1].strip()

    # Strip trailing '?' and common wrappers
    q = q.rstrip("?").strip()

    # Pattern: "X vs Y" or "X vs. Y"
    m = re.search(r"^(.+?)\s+vs\.?\s+(.+)$", q, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Pattern: "Will X beat Y"
    m = re.search(r"^will\s+(.+?)\s+beat\s+(.+)$", q, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Pattern: "X to win against Y"
    m = re.search(r"^(.+?)\s+to\s+win\s+(?:against|vs)\s+(.+)$", q, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    return "", ""


def _to_unified_market(m: Market) -> UnifiedMarket:
    """Convert a Polymarket Market to UnifiedMarket."""
    sport = ""
    for tag in m.tags:
        if tag.lower() in SPORTS_TAGS:
            sport = tag.lower()
            break

    team_a, team_b = _extract_teams_from_question(m.question)

    # Fallback: if the market question didn't yield team names
    # (e.g. rugby "Will X win?") try the parent event title
    # which often has the "X vs Y" pattern.
    if (not team_a or not team_b) and m.event_title:
        team_a, team_b = _extract_teams_from_question(m.event_title)

    return UnifiedMarket(
        platform=Platform.POLYMARKET,
        market_id=m.condition_id,
        question=m.question,
        sport=sport,
        event_name=m.event_title or m.question,
        team_a=team_a,
        team_b=team_b,
        start_time=(
            _parse_end_date(m.game_start_time)
            if m.game_start_time
            else _parse_end_date(m.end_date)
        ),
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
