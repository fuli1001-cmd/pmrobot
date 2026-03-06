"""Event-driven cross-platform arbitrage monitor.

Replaces the old polling-based ``_cross_platform_scan_cycle`` in
``main.py`` with a dual-WebSocket architecture:

  * **Polymarket** — CLOB WebSocket (``wss://ws-subscriptions-clob.polymarket.com``)
  * **SX Bet** — Ably WebSocket (``order_book_v2`` channel)

On any price update from either side, the affected aligned pair is
immediately re-evaluated for arbitrage.  Profitable opportunities are
pushed onto an ``asyncio.Queue`` for the executor to consume.

Market discovery (alignment) is still performed periodically via HTTP
polling on a much longer interval — only pair *evaluation* is real-time.
"""

import asyncio
import json
import time
from typing import Callable, Dict, List, Optional, Set

import websockets
from websockets.exceptions import ConnectionClosed

from config.constants import CLOB_WS_URL
from core.alignment import AlignedMarketPair
from core.cross_platform import (
    CrossPlatformDetector,
    _ALT_SLIPPAGE_BUFFER,
    _MIN_PM_CLOB_DEPTH_USD,
    _SXBET_ORACLE_FEE_PCT,
    _ALT_EXECUTION_COST_USD,
)
from exchanges.base import Platform, UnifiedOdds
from exchanges.sxbet_ws import SxBetOrderBook, SxBetWebSocket
from models.cross_models import (
    CrossPlatformOpportunity,
    CrossPlatformStrategy,
)
from utils.logger import get_logger

logger = get_logger(__name__)


class _PMBookCache:
    """Lightweight Polymarket order-book cache.

    Stores best-ask prices and depth (from WS ``book`` channel updates)
    for PM tokens used in cross-platform pairs.  This is separate from
    the PM-only ``OrderBookManager`` in ``core/monitor.py`` so that the
    cross-platform monitor is self-contained.
    """

    __slots__ = ("_data",)

    def __init__(self):
        # token_id -> (best_ask, total_ask_depth_usdc, timestamp)
        self._data: Dict[str, tuple] = {}

    def update(self, token_id: str, bids: list, asks: list) -> None:
        best_ask = float(asks[0]["price"]) if asks else 0.0
        depth = sum(float(a["size"]) * float(a["price"]) for a in asks) if asks else 0.0
        best_bid = float(bids[0]["price"]) if bids else 0.0
        bid_depth = sum(float(b["size"]) * float(b["price"]) for b in bids) if bids else 0.0
        self._data[token_id] = (best_ask, depth, best_bid, bid_depth, time.time())

    def get_ask(self, token_id: str) -> tuple:
        """Returns (best_ask_price, depth_usdc) or (0.0, 0.0)."""
        entry = self._data.get(token_id)
        if entry:
            return (entry[0], entry[1])
        return (0.0, 0.0)

    def get_bid(self, token_id: str) -> tuple:
        """Returns (best_bid_price, depth_usdc) or (0.0, 0.0)."""
        entry = self._data.get(token_id)
        if entry:
            return (entry[2], entry[3])
        return (0.0, 0.0)


class CrossPlatformMonitor:
    """Dual-WebSocket cross-platform arbitrage monitor.

    Manages:
    1. PM WebSocket connection (subscribes to ``book`` channel for PM tokens)
    2. SX Bet Ably WebSocket (subscribes to ``order_book_v2`` channels)
    3. Aligned pairs index (updated by periodic alignment refresh)
    4. On price change from either side → re-evaluate affected pairs

    Detected opportunities are pushed onto the provided asyncio.Queue.
    """

    def __init__(
        self,
        sx_ws: SxBetWebSocket,
        opportunity_queue: asyncio.Queue,
        profit_threshold: float = 0.03,
        trade_size: float = 50.0,
    ):
        self._sx_ws = sx_ws
        self._queue = opportunity_queue
        self._profit_threshold = profit_threshold
        self._trade_size = trade_size

        # ── Aligned-pair indices ──
        # pm_token_id -> list of AlignedMarketPair  (YES and NO tokens)
        self._pm_token_to_pairs: Dict[str, List[AlignedMarketPair]] = {}
        # sx_market_hash -> list of AlignedMarketPair
        self._sx_hash_to_pairs: Dict[str, List[AlignedMarketPair]] = {}
        # All current pairs (for replacement on refresh)
        self._pairs: List[AlignedMarketPair] = []

        # PM book cache
        self._pm_books = _PMBookCache()

        # PM WS state
        self._pm_ws = None
        self._pm_running = False
        self._pm_reconnect_delay = 1.0

        # Stats
        self._eval_count = 0
        self._opp_count = 0
        self._last_stats_time = time.time()

    # ------------------------------------------------------------------
    # Pair management (called from market refresher)
    # ------------------------------------------------------------------

    async def update_pairs(self, pairs: List[AlignedMarketPair]) -> None:
        """Replace the current set of aligned pairs and update subscriptions.

        Called periodically by the market refresher when new alignment
        results are available.
        """
        old_pm_tokens = set(self._pm_token_to_pairs.keys())
        old_sx_hashes = set(self._sx_hash_to_pairs.keys())

        # Rebuild indices
        self._pairs = pairs
        self._pm_token_to_pairs.clear()
        self._sx_hash_to_pairs.clear()

        new_pm_tokens: Set[str] = set()
        new_sx_hashes: Set[str] = set()

        for pair in pairs:
            pm = pair.polymarket
            sx = pair.azuro  # field named azuro, holds SX Bet market

            # PM tokens: we need both YES and NO token IDs.
            # UnifiedMarket.metadata contains these (set by polymarket.py).
            yes_token = pm.metadata.get("token_id_yes", "")
            no_token = pm.metadata.get("token_id_no", "")
            if not yes_token and not no_token:
                # Fallback: market_id might be a condition_id
                # (PM CLOB uses token_ids, not condition_ids for WS)
                continue

            for tok in (yes_token, no_token):
                if tok:
                    self._pm_token_to_pairs.setdefault(tok, []).append(pair)
                    new_pm_tokens.add(tok)

            # SX Bet market hash
            sx_hash = sx.market_id
            self._sx_hash_to_pairs.setdefault(sx_hash, []).append(pair)
            new_sx_hashes.add(sx_hash)

        # ── Update PM WS subscriptions ──
        tokens_to_add = new_pm_tokens - old_pm_tokens
        if tokens_to_add and self._pm_ws:
            subscribe_msg = {
                "type": "subscribe",
                "channel": "book",
                "assets_ids": list(tokens_to_add),
            }
            try:
                await self._pm_ws.send(json.dumps(subscribe_msg))
                logger.info("Cross-PM WS: subscribed new tokens", count=len(tokens_to_add))
            except Exception as e:
                logger.warning("Cross-PM WS: subscribe failed", error=repr(e))

        # ── Update SX Bet WS subscriptions ──
        hashes_to_add = list(new_sx_hashes - old_sx_hashes)
        hashes_to_remove = list(old_sx_hashes - new_sx_hashes)
        if hashes_to_add:
            await self._sx_ws.subscribe(hashes_to_add)
        if hashes_to_remove:
            await self._sx_ws.unsubscribe(hashes_to_remove)

        logger.info(
            "Cross-platform pairs updated",
            pairs=len(pairs),
            pm_tokens=len(new_pm_tokens),
            sx_markets=len(new_sx_hashes),
        )

    # ------------------------------------------------------------------
    # PM WebSocket
    # ------------------------------------------------------------------

    async def run_pm_ws(self) -> None:
        """Run the Polymarket WebSocket loop (reconnecting on errors).

        This is started as an asyncio task by the main bot.
        """
        self._pm_running = True
        logger.info("Cross-platform PM WebSocket starting")

        while self._pm_running:
            try:
                await self._pm_connect_and_subscribe()
            except ConnectionClosed as e:
                logger.warning("Cross-PM WS closed", code=e.code, reason=e.reason)
            except Exception as e:
                logger.error("Cross-PM WS error", error=repr(e))

            if self._pm_running:
                logger.info("Cross-PM WS reconnecting", delay=self._pm_reconnect_delay)
                await asyncio.sleep(self._pm_reconnect_delay)
                self._pm_reconnect_delay = min(self._pm_reconnect_delay * 2, 60.0)

    async def stop(self) -> None:
        """Stop the PM WebSocket loop."""
        self._pm_running = False
        if self._pm_ws:
            await self._pm_ws.close()
            self._pm_ws = None

    async def _pm_connect_and_subscribe(self) -> None:
        """Connect to PM WS and subscribe to cross-platform tokens."""
        async with websockets.connect(CLOB_WS_URL) as ws:
            self._pm_ws = ws
            self._pm_reconnect_delay = 1.0

            # Subscribe to all current PM tokens
            token_ids = list(self._pm_token_to_pairs.keys())
            if token_ids:
                subscribe_msg = {
                    "type": "subscribe",
                    "channel": "book",
                    "assets_ids": token_ids,
                }
                await ws.send(json.dumps(subscribe_msg))
                logger.info("Cross-PM WS subscribed", tokens=len(token_ids))

            # Process messages + heartbeat
            await asyncio.gather(
                self._pm_process_messages(ws),
                self._pm_heartbeat(ws),
            )

    async def _pm_heartbeat(self, ws) -> None:
        while self._pm_running:
            try:
                await asyncio.sleep(30.0)
                await ws.ping()
            except Exception:
                break

    async def _pm_process_messages(self, ws) -> None:
        async for message in ws:
            try:
                if not message or message.isspace():
                    continue
                data = json.loads(message)
                if isinstance(data, list):
                    for item in data:
                        self._handle_pm_update(item)
                else:
                    self._handle_pm_update(data)
            except json.JSONDecodeError:
                pass
            except Exception as e:
                logger.debug("Cross-PM WS message error", error=repr(e))

    def _handle_pm_update(self, data: dict) -> None:
        """Handle a PM book update and trigger evaluation."""
        if not isinstance(data, dict):
            return

        msg_type = data.get("type") or data.get("event_type")

        # Accept explicit "book" messages or raw snapshots with asset_id + bids/asks
        is_book = msg_type == "book" or (
            "asset_id" in data and ("bids" in data or "asks" in data)
        )
        if not is_book:
            return

        token_id = data.get("asset_id")
        if not token_id:
            return

        bids = data.get("bids", [])
        asks = data.get("asks", [])
        self._pm_books.update(token_id, bids, asks)

        # Find affected pairs
        pairs = self._pm_token_to_pairs.get(token_id, [])
        for pair in pairs:
            self._evaluate_pair(pair)

    # ------------------------------------------------------------------
    # SX Bet callback (set by connect)
    # ------------------------------------------------------------------

    def on_sx_book_update(self, market_hash: str, book: SxBetOrderBook) -> None:
        """Called by SxBetWebSocket when an order book update arrives."""
        pairs = self._sx_hash_to_pairs.get(market_hash, [])
        for pair in pairs:
            self._evaluate_pair(pair)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def _evaluate_pair(self, pair: AlignedMarketPair) -> None:
        """Evaluate a pair for arbitrage using cached prices.

        This is the hot path — called on every book update.  Must be
        fast (no I/O, no awaits).

        If profitable, pushes a CrossPlatformOpportunity onto the queue.
        """
        self._eval_count += 1

        # Periodic stats (at top, so they fire even when no arb found)
        now = time.time()
        if now - self._last_stats_time >= 60:
            logger.info(
                "Cross-platform monitor stats",
                evaluations=self._eval_count,
                opportunities=self._opp_count,
                pairs=len(self._pairs),
                pm_tokens=len(self._pm_token_to_pairs),
                sx_markets=len(self._sx_hash_to_pairs),
            )
            self._last_stats_time = now

        pm = pair.polymarket
        sx = pair.azuro  # field named azuro, holds SX Bet market

        # ── Gather PM prices from cache ──
        yes_token = pm.metadata.get("token_id_yes", "")
        no_token = pm.metadata.get("token_id_no", "")

        pm_ask_yes, pm_depth_yes = self._pm_books.get_ask(yes_token) if yes_token else (0.0, 0.0)
        pm_ask_no, pm_depth_no = self._pm_books.get_ask(no_token) if no_token else (0.0, 0.0)
        pm_bid_yes, pm_bid_depth_yes = self._pm_books.get_bid(yes_token) if yes_token else (0.0, 0.0)
        pm_bid_no, pm_bid_depth_no = self._pm_books.get_bid(no_token) if no_token else (0.0, 0.0)

        # ── Gather SX prices from WS cache ──
        sx_book = self._sx_ws.get_book(sx.market_id)
        if not sx_book:
            return

        sx_price_yes = sx_book.price_yes
        sx_price_no = sx_book.price_no
        sx_depth_yes = sx_book.depth_yes
        sx_depth_no = sx_book.depth_no

        # When teams are reversed, swap SX sides
        if pair.teams_reversed:
            sx_price_yes, sx_price_no = sx_price_no, sx_price_yes
            sx_depth_yes, sx_depth_no = sx_depth_no, sx_depth_yes

        # ── Check both directions ──
        # Direction 1: YES on PM + NO on SX
        opp1 = self._compute_combo(
            pm_price=pm_ask_yes,
            pm_depth=pm_depth_yes,
            sx_price=sx_price_no,
            sx_depth=sx_depth_no,
            yes_platform=Platform.POLYMARKET,
        )

        # Direction 2: YES on SX + NO on PM
        opp2 = self._compute_combo(
            pm_price=pm_ask_no,
            pm_depth=pm_depth_no,
            sx_price=sx_price_yes,
            sx_depth=sx_depth_yes,
            yes_platform=Platform.SXBET,
        )

        best = None
        if opp1 and opp2:
            best = opp1 if opp1[0] >= opp2[0] else opp2
        elif opp1:
            best = opp1
        elif opp2:
            best = opp2

        if not best:
            return

        net_profit_pct, price_yes, price_no, total_cost, yes_plat = best

        if net_profit_pct < self._profit_threshold:
            return

        # Build opportunity
        opportunity = CrossPlatformOpportunity(
            pm_market_id=pm.market_id,
            az_market_id=sx.market_id,
            pm_question=pm.question,
            az_question=sx.question,
            strategy=CrossPlatformStrategy.BINARY_HEDGE,
            yes_platform=yes_plat,
            no_platform=(Platform.SXBET if yes_plat == Platform.POLYMARKET else Platform.POLYMARKET),
            price_yes=price_yes,
            price_no=price_no,
            total_cost=total_cost,
            gross_profit_pct=(1.0 - total_cost) / total_cost,
            estimated_fees=(
                _ALT_EXECUTION_COST_USD / self._trade_size
                + ((1.0 - total_cost) / total_cost) * _SXBET_ORACLE_FEE_PCT
            ),
            net_profit_pct=net_profit_pct,
            trade_size_usdc=self._trade_size,
        )

        self._opp_count += 1
        logger.info(
            "Cross-platform WS opportunity detected",
            pm_q=pm.question[:50],
            sx_q=sx.question[:50],
            yes_on=yes_plat.value,
            net_profit=f"{net_profit_pct:.2%}",
            total_cost=f"{total_cost:.4f}",
        )

        try:
            self._queue.put_nowait(opportunity)
        except asyncio.QueueFull:
            logger.warning("Cross-platform queue full — opportunity dropped")

    def _compute_combo(
        self,
        pm_price: float,
        pm_depth: float,
        sx_price: float,
        sx_depth: float,
        yes_platform: Platform,
    ) -> Optional[tuple]:
        """Compute arbitrage P/L for a specific direction.

        Returns (net_profit_pct, price_yes, price_no, total_cost, yes_platform)
        or None if not viable.
        """
        if pm_price <= 0 or sx_price <= 0:
            return None

        # Depth gate: PM side must have sufficient depth
        if pm_depth < max(_MIN_PM_CLOB_DEPTH_USD, self._trade_size):
            return None

        # SX depth gate
        if sx_depth < self._trade_size:
            return None

        # Assign prices based on direction
        if yes_platform == Platform.POLYMARKET:
            price_yes = pm_price
            price_no = sx_price * (1.0 + _ALT_SLIPPAGE_BUFFER)
        else:
            price_yes = sx_price * (1.0 + _ALT_SLIPPAGE_BUFFER)
            price_no = pm_price

        total_cost = price_yes + price_no
        if total_cost >= 1.0:
            return None

        gross_profit_pct = (1.0 - total_cost) / total_cost
        execution_fee = _ALT_EXECUTION_COST_USD / self._trade_size
        oracle_fee = gross_profit_pct * _SXBET_ORACLE_FEE_PCT
        net_profit_pct = gross_profit_pct - execution_fee - oracle_fee

        if net_profit_pct <= 0:
            return None

        return (net_profit_pct, price_yes, price_no, total_cost, yes_platform)
