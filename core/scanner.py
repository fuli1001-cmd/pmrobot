"""Market scanner using Gamma API."""

from typing import List, Optional, Set

import httpx

from config.constants import (
    GAMMA_API_BASE_URL,
    FEE_FREE_TAGS,
    CRYPTO_15MIN_TAGS,
)
from models.market import Market, MarketType, FeeCategory, NegativeRiskEvent
from utils.logger import get_logger
from utils.rate_limiter import RateLimiter

logger = get_logger(__name__)


class MarketScanner:
    """
    Scans Polymarket for active markets using Gamma API.
    """

    def __init__(self, rate_limit: float = 10.0):
        """
        Initialize the market scanner.

        Args:
            rate_limit: Maximum API requests per second
        """
        self.base_url = GAMMA_API_BASE_URL
        self.rate_limiter = RateLimiter(rate_limit)
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        """Async context manager entry."""
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()

    async def fetch_active_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        fee_free_only: bool = True,
    ) -> List[Market]:
        """
        Fetch active markets from Gamma API.

        Args:
            limit: Maximum number of markets to fetch per request
            offset: Pagination offset
            fee_free_only: If True, only return zero-fee markets

        Returns:
            List of active markets
        """
        await self.rate_limiter.acquire()

        params = {
            "active": "true",
            "closed": "false",
            "enable_order_book": "true",
            "limit": str(limit),
            "offset": str(offset),
        }

        try:
            response = await self._client.get(
                f"{self.base_url}/markets",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

            markets = []
            for item in data:
                market = self._parse_market(item)
                if market and (not fee_free_only or market.is_fee_free):
                    markets.append(market)

            logger.info(
                "Fetched markets",
                total=len(data),
                filtered=len(markets),
                fee_free_only=fee_free_only,
            )
            return markets

        except httpx.HTTPError as e:
            logger.error("Failed to fetch markets", error=repr(e))
            return []

    async def fetch_all_markets(
        self,
        fee_free_only: bool = True,
        max_markets: int = 2000,  # Increased from 1000 for better coverage
    ) -> List[Market]:
        """
        Fetch all active markets with pagination.

        Args:
            fee_free_only: If True, only return zero-fee markets
            max_markets: Maximum total markets to fetch

        Returns:
            List of all active markets
        """
        all_markets = []
        offset = 0
        limit = 100

        while len(all_markets) < max_markets:
            markets = await self.fetch_active_markets(
                limit=limit,
                offset=offset,
                fee_free_only=fee_free_only,
            )

            if not markets:
                break

            all_markets.extend(markets)
            offset += limit

            if len(markets) < limit:
                # No more markets
                break

        logger.info("Fetched all markets", total=len(all_markets))
        return all_markets[:max_markets]

    async def fetch_market_by_slug(self, slug: str) -> Optional[Market]:
        """
        Fetch a specific market by its slug.

        Args:
            slug: Market slug (from URL)

        Returns:
            Market if found, None otherwise
        """
        await self.rate_limiter.acquire()

        try:
            response = await self._client.get(
                f"{self.base_url}/markets",
                params={"slug": slug},
            )
            response.raise_for_status()
            data = response.json()

            if data:
                return self._parse_market(data[0])
            return None

        except httpx.HTTPError as e:
            logger.error("Failed to fetch market by slug", slug=slug, error=repr(e))
            return None

    def _parse_market(self, data: dict) -> Optional[Market]:
        """
        Parse market data from API response.

        Args:
            data: Raw market data from API

        Returns:
            Parsed Market object, or None if invalid
        """
        import json as json_module
        
        try:
            # API uses camelCase: conditionId, clobTokenIds, enableOrderBook
            condition_id = data.get("conditionId", "")
            if not condition_id:
                logger.debug("Market missing conditionId", slug=data.get("slug"))
                return None

            # Parse clobTokenIds - it's a JSON string like '["token1", "token2"]'
            clob_token_ids_raw = data.get("clobTokenIds", "[]")
            try:
                clob_token_ids = json_module.loads(clob_token_ids_raw) if isinstance(clob_token_ids_raw, str) else clob_token_ids_raw
            except json_module.JSONDecodeError:
                clob_token_ids = []

            if len(clob_token_ids) < 2:
                logger.debug("Market has insufficient tokens", slug=data.get("slug"), tokens=len(clob_token_ids))
                return None

            # For binary markets: first token is Yes, second is No
            token_id_yes = clob_token_ids[0]
            token_id_no = clob_token_ids[1]

            # Check if order book is enabled
            enable_order_book = data.get("enableOrderBook", False)
            if not enable_order_book:
                logger.debug("Order book not enabled", slug=data.get("slug"))
                return None

            # Determine fee category based on tags
            tags = data.get("tags", []) or []
            if isinstance(tags, str):
                try:
                    tags = json_module.loads(tags)
                except:
                    tags = []
            tags = [t.lower() if isinstance(t, str) else str(t).lower() for t in tags]
            fee_category = self._determine_fee_category(tags)

            # Determine market type
            neg_risk = data.get("negRisk", False)
            market_type = MarketType.NEGATIVE_RISK if neg_risk else MarketType.BINARY

            # Parse liquidity
            liquidity = data.get("liquidity", 0)
            if isinstance(liquidity, str):
                liquidity = float(liquidity) if liquidity else 0.0
            else:
                liquidity = float(liquidity) if liquidity else 0.0

            # Parse outcome prices (from Gamma / Events API)
            outcome_price_yes = 0.0
            outcome_price_no = 0.0
            raw_prices = data.get("outcomePrices", "")
            if raw_prices:
                try:
                    prices_list = json_module.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                    if isinstance(prices_list, list) and len(prices_list) >= 2:
                        outcome_price_yes = float(prices_list[0])
                        outcome_price_no = float(prices_list[1])
                except (json_module.JSONDecodeError, ValueError, TypeError):
                    pass

            return Market(
                condition_id=condition_id,
                token_id_yes=token_id_yes,
                token_id_no=token_id_no,
                question=data.get("question", ""),
                slug=data.get("slug", ""),
                market_type=market_type,
                min_tick_size=float(data.get("orderPriceMinTickSize", 0.01) or 0.01),
                fee_category=fee_category,
                tags=tags,
                volume_24h=float(data.get("volume24hr", 0) or 0),
                liquidity=liquidity,
                end_date=data.get("endDate", "") or "",
                game_start_time=data.get("_gameStartTime", "") or "",
                outcome_price_yes=outcome_price_yes,
                outcome_price_no=outcome_price_no,
                active=data.get("active", True),
                closed=data.get("closed", False),
                enable_order_book=enable_order_book,
            )

        except (KeyError, ValueError, TypeError) as e:
            logger.debug("Failed to parse market", error=repr(e), slug=data.get("slug"))
            return None

    def _determine_fee_category(self, tags: List[str]) -> FeeCategory:
        """
        Determine fee category based on market tags.

        Args:
            tags: List of market tags (lowercase)

        Returns:
            Fee category
        """
        # Check for 15-minute crypto markets
        for tag in tags:
            if any(crypto_tag in tag for crypto_tag in CRYPTO_15MIN_TAGS):
                return FeeCategory.CRYPTO_15MIN

        # Check for fee-free categories
        for tag in tags:
            if any(free_tag in tag for free_tag in FEE_FREE_TAGS):
                return FeeCategory.FREE

        # Default to free (most markets are fee-free)
        return FeeCategory.FREE

    # ------------------------------------------------------------------
    # Sports-specific fetch (Events API with tag_slug)
    # ------------------------------------------------------------------

    async def fetch_sports_markets(
        self,
        tag_slug: str = "sports",
        max_events: int = 100,
    ) -> List[Market]:
        """Fetch active sports markets via the **Events** API.

        The Gamma *markets* endpoint does not expose tags.  Tags exist only
        at the event level, so we query ``/events?tag_slug=<tag>`` and
        extract the markets embedded in each event.

        Args:
            tag_slug: Tag slug to filter events (default ``"sports"``).
            max_events: Maximum events to page through.

        Returns:
            De-duplicated list of ``Market`` objects for sports events.
        """
        seen: Set[str] = set()
        all_markets: List[Market] = []
        offset = 0
        limit = 50

        while len(all_markets) < 2000 and offset < max_events:
            await self.rate_limiter.acquire()
            try:
                response = await self._client.get(
                    f"{self.base_url}/events",
                    params={
                        "active": "true",
                        "closed": "false",
                        "tag_slug": tag_slug,
                        "limit": str(limit),
                        "offset": str(offset),
                    },
                )
                response.raise_for_status()
                data = response.json()
                if not data:
                    break

                for event_data in data:
                    # Extract event-level start time and inject into each
                    # market dict so _parse_market can store it.
                    event_start = (
                        event_data.get("startDate")
                        or event_data.get("startTime")
                        or ""
                    )
                    for mkt_data in event_data.get("markets", []):
                        cid = mkt_data.get("conditionId", "")
                        if cid and cid not in seen:
                            if event_start:
                                mkt_data["_gameStartTime"] = event_start
                            m = self._parse_market(mkt_data)
                            if m:
                                seen.add(cid)
                                all_markets.append(m)

                offset += limit
                if len(data) < limit:
                    break

            except httpx.HTTPError as e:
                logger.error("Failed to fetch sports events", error=repr(e))
                break

        logger.info(
            "Fetched sports markets via Events API",
            tag_slug=tag_slug,
            total=len(all_markets),
        )
        return all_markets

    async def fetch_negative_risk_events(
        self,
        min_outcomes: int = 2,
        max_events: int = 200,
    ) -> List[NegativeRiskEvent]:
        """
        Fetch Negative Risk events from Events API.
        
        Each event contains multiple mutually exclusive outcomes.
        
        Args:
            min_outcomes: Minimum number of outcomes for an event
            max_events: Maximum events to fetch
            
        Returns:
            List of NegativeRiskEvent objects
        """
        await self.rate_limiter.acquire()
        
        events = []
        offset = 0
        limit = 50
        
        while len(events) < max_events:
            try:
                response = await self._client.get(
                    f"{self.base_url}/events",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": str(limit),
                        "offset": str(offset),
                    },
                )
                response.raise_for_status()
                data = response.json()
                
                if not data:
                    break
                
                for event_data in data:
                    parsed_event = self._parse_negative_risk_event(event_data)
                    if parsed_event and parsed_event.outcome_count >= min_outcomes:
                        events.append(parsed_event)
                
                offset += limit
                await self.rate_limiter.acquire()
                
                if len(data) < limit:
                    break
                    
            except httpx.HTTPError as e:
                logger.error("Failed to fetch events", error=repr(e))
                break
        
        logger.info(
            "Fetched Negative Risk events",
            total=len(events),
            min_outcomes=min_outcomes,
        )
        return events[:max_events]
    
    def _parse_negative_risk_event(self, data: dict) -> Optional[NegativeRiskEvent]:
        """
        Parse event data from Events API response.
        
        Only returns events with negRisk=True markets.
        
        Args:
            data: Raw event data from API
            
        Returns:
            NegativeRiskEvent if valid, None otherwise
        """
        try:
            event_markets = data.get("markets", [])
            
            # Filter for negRisk markets only
            neg_risk_markets = [m for m in event_markets if m.get("negRisk", False)]
            
            if not neg_risk_markets:
                return None
            
            # Parse each outcome market
            outcomes = []
            for market_data in neg_risk_markets:
                market = self._parse_market(market_data)
                if market:
                    outcomes.append(market)
            
            if len(outcomes) < 2:
                return None
            
            # Calculate total liquidity
            total_liquidity = sum(m.liquidity for m in outcomes)
            total_volume = sum(m.volume_24h for m in outcomes)
            
            return NegativeRiskEvent(
                event_id=str(data.get("id", "")),
                title=data.get("title", ""),
                slug=data.get("slug", ""),
                outcomes=outcomes,
                liquidity=total_liquidity,
                volume_24h=total_volume,
                active=data.get("active", True),
                closed=data.get("closed", False),
            )
            
        except (KeyError, ValueError, TypeError) as e:
            logger.debug(
                "Failed to parse negative risk event",
                error=repr(e),
                slug=data.get("slug"),
            )
            return None


async def get_tradeable_markets(
    fee_free_only: bool = True,
    min_liquidity: float = 1000.0,
) -> List[Market]:
    """
    Convenience function to get tradeable markets.

    Args:
        fee_free_only: Only return zero-fee markets
        min_liquidity: Minimum liquidity threshold

    Returns:
        List of tradeable markets
    """
    async with MarketScanner() as scanner:
        markets = await scanner.fetch_all_markets(fee_free_only=fee_free_only)

        # Filter by liquidity
        tradeable = [m for m in markets if m.liquidity >= min_liquidity]

        logger.info(
            "Filtered tradeable markets",
            total=len(markets),
            above_liquidity=len(tradeable),
            min_liquidity=min_liquidity,
        )
        return tradeable
