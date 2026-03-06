"""SX Bet real-time WebSocket client via Ably SDK.

Connects to SX Bet's Ably-powered WebSocket for real-time order book
updates.  Used by the cross-platform monitor to detect arbitrage
opportunities with sub-second latency (replacing the previous HTTP
polling approach).

SX Bet Ably channels:
  - ``order_book_v2:{baseToken}:{marketHash}`` — full order book snapshots
  - ``best_odds:{baseToken}`` — best bid/ask per market (lighter)
  - ``markets`` — new/removed/status-changed markets

Auth flow:
  1. ``GET /user/token`` with ``X-Api-Key`` header → Ably ``tokenRequest``
  2. Pass ``auth_callback`` to ``AblyRealtime`` for automatic token renewal

Reference: https://api.docs.sx.bet (WebSocket / Ably section)
"""

import asyncio
import json
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import httpx
from ably import AblyRealtime
from ably.realtime.connection import ConnectionState

from utils.logger import get_logger

logger = get_logger(__name__)

# Default USDC contract address on SX Network (6 decimals)
_DEFAULT_BASE_TOKEN = "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"

# ODDS_PRECISION matches the value in sxbet.py
_ODDS_PRECISION = 10**20


class SxBetOrderBook:
    """Parsed SX Bet order book for a single market.

    Stores best taker prices and total depth for both outcomes.
    """

    __slots__ = (
        "market_hash", "price_yes", "price_no",
        "depth_yes", "depth_no", "timestamp",
    )

    def __init__(
        self,
        market_hash: str = "",
        price_yes: float = 0.0,
        price_no: float = 0.0,
        depth_yes: float = 0.0,
        depth_no: float = 0.0,
        timestamp: float = 0.0,
    ):
        self.market_hash = market_hash
        self.price_yes = price_yes
        self.price_no = price_no
        self.depth_yes = depth_yes
        self.depth_no = depth_no
        self.timestamp = timestamp or time.time()


# Type alias for the price-change callback.
# Signature: callback(market_hash: str, book: SxBetOrderBook)
PriceCallback = Callable[[str, SxBetOrderBook], Any]


class SxBetWebSocket:
    """Real-time SX Bet order-book listener using Ably SDK.

    Usage::

        ws = SxBetWebSocket(
            api_key="...",
            api_url="https://api.sx.bet",
            base_token="0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B",
            on_book_update=my_callback,
        )
        await ws.connect()
        await ws.subscribe(["0xabc...", "0xdef..."])
        ...
        await ws.close()
    """

    def __init__(
        self,
        api_key: str,
        api_url: str = "https://api.sx.bet",
        base_token: str = _DEFAULT_BASE_TOKEN,
        on_book_update: Optional[PriceCallback] = None,
    ):
        self._api_key = api_key
        self._api_url = api_url.rstrip("/")
        self._base_token = base_token
        self._on_book_update = on_book_update

        self._ably: Optional[AblyRealtime] = None
        self._subscribed: Set[str] = set()          # market hashes
        self._books: Dict[str, SxBetOrderBook] = {} # market_hash -> latest book
        # Running state: market_hash -> {orderHash -> order_dict}
        self._live_orders: Dict[str, Dict[str, dict]] = {}
        self._connected = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Establish the Ably realtime connection.

        Uses ``auth_callback`` so tokens are automatically renewed when
        they expire.
        """
        self._ably = AblyRealtime(
            auth_callback=self._auth_callback,
            auto_connect=True,
        )

        # Wait for connection to be established (with timeout)
        for _ in range(30):
            if self._ably.connection.state == ConnectionState.CONNECTED:
                break
            await asyncio.sleep(0.5)
        else:
            state = self._ably.connection.state if self._ably else "no-client"
            logger.warning(
                "SX Bet WS: connection not established within timeout",
                state=str(state),
            )

        self._connected = True
        logger.info("SX Bet WebSocket connected via Ably")

    async def close(self) -> None:
        """Disconnect and clean up."""
        self._connected = False
        if self._ably:
            await self._ably.close()
            self._ably = None
        self._subscribed.clear()
        self._books.clear()
        self._live_orders.clear()
        logger.info("SX Bet WebSocket closed")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def _auth_callback(self, token_params) -> dict:
        """Fetch an Ably tokenRequest from SX Bet's ``/user/token`` endpoint.

        This is called by the Ably SDK whenever a token is needed or
        needs to be renewed.  Returns a ``tokenRequest`` dict that the
        SDK uses to authenticate with Ably.

        SX Bet response format (top-level tokenRequest)::

            {
                "keyName": "Pb_c6A.2IZKTQ",
                "clientId": "0x...",
                "capability": "{\"*\":[\"subscribe\"]}",
                "ttl": 86400000,
                "timestamp": 1772779201784,
                "nonce": "3522558038605510",
                "mac": "lJpC6VMLV22A6xeGSt5q+..."
            }
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{self._api_url}/user/token",
                headers={"X-Api-Key": self._api_key},
            )
            resp.raise_for_status()
            token_request = resp.json()

        if not token_request.get("keyName"):
            raise ValueError("SX Bet /user/token returned invalid tokenRequest")

        logger.debug("SX Bet Ably tokenRequest obtained", key_name=token_request["keyName"])
        return token_request

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    async def subscribe(self, market_hashes: List[str]) -> None:
        """Subscribe to order-book updates for the given markets.

        Subscribes to the ``order_book_v2:{baseToken}:{marketHash}``
        Ably channel for each market.  Already-subscribed hashes are
        skipped.
        """
        if not self._ably:
            return

        new_hashes = [h for h in market_hashes if h not in self._subscribed]
        if not new_hashes:
            return

        for market_hash in new_hashes:
            channel_name = f"order_book_v2:{self._base_token}:{market_hash}"
            channel = self._ably.channels.get(channel_name)
            # Subscribe to all messages on the channel
            await channel.subscribe(
                lambda msg, mh=market_hash: self._handle_order_book_message(mh, msg)
            )
            self._subscribed.add(market_hash)

        logger.info(
            "SX Bet WS: subscribed to order books",
            new=len(new_hashes),
            total=len(self._subscribed),
        )

    async def unsubscribe(self, market_hashes: List[str]) -> None:
        """Unsubscribe from order-book updates for the given markets."""
        if not self._ably:
            return

        for market_hash in market_hashes:
            if market_hash not in self._subscribed:
                continue
            channel_name = f"order_book_v2:{self._base_token}:{market_hash}"
            channel = self._ably.channels.get(channel_name)
            await channel.detach()
            self._subscribed.discard(market_hash)
            self._live_orders.pop(market_hash, None)
            self._books.pop(market_hash, None)

        logger.debug(
            "SX Bet WS: unsubscribed",
            removed=len(market_hashes),
            remaining=len(self._subscribed),
        )

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    def _handle_order_book_message(self, market_hash: str, message) -> None:
        """Process an incoming Ably message for a specific market.

        SX Bet sends *incremental* updates: each message contains only
        the orders that changed.  Orders with ``status == "ACTIVE"`` are
        added/updated; orders with ``status == "INACTIVE"`` (or other
        non-active status) are removed.

        We maintain a per-market dict of live orders and recompute the
        aggregate book after each update.
        """
        try:
            data = message.data
            if isinstance(data, str):
                data = json.loads(data)

            orders = data.get("orders", []) if isinstance(data, dict) else []
            if not orders and isinstance(data, list):
                orders = data

            if not orders:
                return

            # Ensure per-market live order dict exists
            if market_hash not in self._live_orders:
                self._live_orders[market_hash] = {}
            live = self._live_orders[market_hash]

            # Apply incremental updates
            for order in orders:
                order_hash = order.get("orderHash", "")
                if not order_hash:
                    continue
                status = order.get("status", "")
                if status == "ACTIVE":
                    live[order_hash] = order
                else:
                    # INACTIVE, CANCELLED, FILLED, etc. → remove
                    live.pop(order_hash, None)

            # Recompute aggregate book from all live orders
            book = self._parse_orders(market_hash, list(live.values()))
            self._books[market_hash] = book

            if self._on_book_update:
                self._on_book_update(market_hash, book)

        except Exception as e:
            logger.debug(
                "SX Bet WS: message parse error",
                market_hash=market_hash[:16],
                error=repr(e),
            )

    def _parse_orders(
        self, market_hash: str, orders: list
    ) -> SxBetOrderBook:
        """Parse a list of SX Bet orders into a SxBetOrderBook.

        - ``isMakerBettingOutcomeOne=True`` → taker can buy outcome 2 (NO)
        - ``isMakerBettingOutcomeOne=False`` → taker can buy outcome 1 (YES)

        Returns the best taker price and total depth for each side.
        """
        # YES side: sorted ascending by taker price (cheapest first)
        yes_levels: List[Tuple[float, float]] = []  # (taker_price, taker_usdc)
        # NO side
        no_levels: List[Tuple[float, float]] = []

        for order in orders:
            total_size = int(order.get("totalBetSize", 0))
            fill_amount = int(order.get("fillAmount", 0))
            remaining_maker = total_size - fill_amount
            if remaining_maker <= 0:
                continue

            pct_odds = int(order.get("percentageOdds", 0))
            if pct_odds <= 0 or pct_odds >= _ODDS_PRECISION:
                continue

            taker_price = 1.0 - pct_odds / _ODDS_PRECISION

            # Remaining taker capacity in raw units
            remaining_taker_raw = (
                remaining_maker * _ODDS_PRECISION // pct_odds
                - remaining_maker
            )
            remaining_taker_usdc = remaining_taker_raw / (10 ** 6)  # 6 decimals
            if remaining_taker_usdc < 0.01:
                continue

            is_maker_one = order.get("isMakerBettingOutcomeOne")
            if is_maker_one is True:
                no_levels.append((taker_price, remaining_taker_usdc))
            elif is_maker_one is False:
                yes_levels.append((taker_price, remaining_taker_usdc))

        # Sort by price ascending: best (cheapest) first
        yes_levels.sort(key=lambda x: x[0])
        no_levels.sort(key=lambda x: x[0])

        best_yes = yes_levels[0][0] if yes_levels else 0.0
        best_no = no_levels[0][0] if no_levels else 0.0
        depth_yes = sum(cap for _, cap in yes_levels)
        depth_no = sum(cap for _, cap in no_levels)

        return SxBetOrderBook(
            market_hash=market_hash,
            price_yes=best_yes,
            price_no=best_no,
            depth_yes=depth_yes,
            depth_no=depth_no,
            timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_book(self, market_hash: str) -> Optional[SxBetOrderBook]:
        """Get the latest order book for a market (or None)."""
        return self._books.get(market_hash)

    @property
    def is_connected(self) -> bool:
        if not self._ably:
            return False
        return self._ably.connection.state == ConnectionState.CONNECTED

    @property
    def subscribed_count(self) -> int:
        return len(self._subscribed)
