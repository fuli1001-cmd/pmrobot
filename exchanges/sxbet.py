"""SX Bet exchange adapter.

Integrates with SX Bet via its REST API (https://api.sx.bet) for
market discovery, order-book pricing, and order execution (taker fills).

SX Bet is a peer-to-peer CLOB prediction market on the SX Network
(Arbitrum Orbit L2, chain ID 4162).  Key properties:

  * 0% taker fee (5% oracle fee on winning profit only)
  * USDC stablecoin (6 decimals) as base token
  * ``desiredOdds`` + ``oddsSlippage`` (0-100) execution protection
  * Pregame betting delay: 0.5 s
  * Taker minimum: 5 USDC
  * Maker minimum: 10 USDC

Odds format:
  - ``percentageOdds`` is stored as ``impliedOdds * 10^20``
  - Maker odds = ``percentageOdds / 10^20``
  - Taker odds = ``1 - makerOdds``
  - Odds ladder step size = 25 (0.25% increments)

API reference: https://api.docs.sx.bet
"""

import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from exchanges.base import (
    BaseExchange,
    BetResult,
    OutcomeSide,
    Platform,
    UnifiedMarket,
    UnifiedOdds,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# SX Bet sport IDs relevant to cross-platform arbitrage with Polymarket.
# Maps Polymarket tag_slug -> SX Bet sportId.
SXBET_SPORT_IDS: Dict[str, int] = {
    "basketball": 1,
    "hockey": 2,
    "baseball": 3,
    "soccer": 5,
    "tennis": 6,
    "mma": 7,
    "nfl": 8,
    "boxing": 11,
    "crypto": 14,
    "politics": 17,
    "entertainment": 18,
    "cricket": 21,
    "rugby": 22,
    "counter-strike": 24,   # Esports - CS
    "league-of-legends": 25,  # Esports - LoL
}

# USDC has 6 decimals on SX Network (same as Polygon/Polymarket)
USDC_DECIMALS = 6

# Moneyline market types on SX Bet (only these are suitable for arb)
# 1  = generic moneyline, 52 = H2H moneyline, 226 = W moneyline (team sports)
# All other types (28/29 = totals, 165 = sets, 202/203 = period,
#                  342/866 = spreads) are excluded.
SXBET_MONEYLINE_TYPES: set = {1, 52, 226}

# Minimum taker bet size on SX Bet (1 USDC = 1_000_000 raw)
TAKER_MIN_USDC = 1.0

# SX Bet odds precision: percentageOdds = impliedOdds * 10^20
ODDS_PRECISION = 10**20


class SxBetExchange(BaseExchange):
    """SX Bet adapter implementing BaseExchange.

    Uses the SX Bet REST API for all operations:
      - ``GET /markets/active`` for market discovery
      - ``GET /orders/odds/best`` for best bid/ask pricing
      - ``GET /orders`` for full order-book depth
      - ``POST /orders/fill/v2`` for taker fills
      - ``GET /metadata`` for contract addresses
    """

    platform = Platform.SXBET

    def __init__(
        self,
        api_key: str = "",
        api_url: str = "https://api.sx.bet",
        rpc_url: str = "https://rpc-rollup.sx.technology",
        chain_id: int = 4162,
        usdc_address: str = "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B",
        private_key: str = "",
        dry_run: bool = False,
    ):
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.usdc_address = Web3.to_checksum_address(usdc_address)
        self.private_key = private_key
        self.dry_run = dry_run

        self._http: Optional[httpx.AsyncClient] = None
        self._w3: Optional[Web3] = None
        self._wallet_address: Optional[str] = None

        # Metadata fetched from /metadata on connect
        self._executor_address: Optional[str] = None
        self._token_transfer_proxy: Optional[str] = None
        self._eip712_fill_hasher: Optional[str] = None
        self._domain_version: str = "6.0"

        # Sport lookup cache: sportId -> sport name
        self._sports: Dict[int, str] = {}
        # League cache: leagueId -> league info
        self._leagues: Dict[int, Dict] = {}

        # Market cache: marketHash -> market data (raw API response)
        self._markets_cache: Dict[str, Dict] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Initialise HTTP client, fetch metadata and sports."""
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["X-Api-Key"] = self.api_key

        self._http = httpx.AsyncClient(
            base_url=self.api_url,
            headers=headers,
            timeout=20.0,
        )

        # Derive wallet address from private key
        if self.private_key:
            acct = Account.from_key(self.private_key)
            self._wallet_address = acct.address
        
        # Connect to SX Network RPC
        self._w3 = Web3(Web3.HTTPProvider(self.rpc_url))

        # Fetch contract metadata
        try:
            resp = await self._http.get("/metadata")
            resp.raise_for_status()
            meta = resp.json()
            # Response: {"status": "success", "data": {"executorAddress": ..., "EIP712FillHasher": ..., ...}}
            meta_data = meta.get("data", meta) if isinstance(meta, dict) else meta
            self._executor_address = meta_data.get("executorAddress")
            self._token_transfer_proxy = meta_data.get("TokenTransferProxy")
            self._eip712_fill_hasher = meta_data.get("EIP712FillHasher")
            self._domain_version = meta_data.get("domainVersion", "6.0")
            logger.info(
                "SX Bet metadata loaded",
                executor=self._executor_address,
                proxy=self._token_transfer_proxy,
                fill_hasher=self._eip712_fill_hasher,
                domain_version=self._domain_version,
            )
        except Exception as e:
            logger.warning("Failed to fetch SX Bet metadata", error=repr(e))

        # Fetch sport list
        # Response: {"status": "success", "data": [{"sportId": 1, "label": "Basketball"}, ...]}
        try:
            resp = await self._http.get("/sports")
            resp.raise_for_status()
            sports_resp = resp.json()
            sports_data = sports_resp.get("data", []) if isinstance(sports_resp, dict) else sports_resp
            if not isinstance(sports_data, list):
                sports_data = []
            for s in sports_data:
                sport_id = s.get("sportId")
                label = s.get("label", "")
                if sport_id is not None:
                    self._sports[sport_id] = label
            logger.info("SX Bet sports loaded", count=len(self._sports))
        except Exception as e:
            logger.warning("Failed to fetch SX Bet sports", error=repr(e))

        logger.info(
            "SxBetExchange connected",
            api_url=self.api_url,
            wallet=self._wallet_address or "(no key)",
            dry_run=self.dry_run,
        )

    async def disconnect(self) -> None:
        """Close HTTP client."""
        if self._http:
            await self._http.aclose()
            self._http = None
        logger.info("SxBetExchange disconnected")

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def get_markets(self, sport: Optional[str] = None) -> List[UnifiedMarket]:
        """Fetch active markets from SX Bet.

        Args:
            sport: Polymarket-style sport slug (e.g. "mma", "tennis").
                   Mapped to SX Bet sportId via SXBET_SPORT_IDS.

        Returns:
            List of UnifiedMarket for two-outcome markets only.
        """
        if not self._http:
            return []

        params: Dict[str, Any] = {
            "pageSize": 50,   # API max is 50
        }

        # Map sport slug to SX Bet sportId
        if sport:
            sport_id = SXBET_SPORT_IDS.get(sport.lower())
            if sport_id is None:
                logger.debug("No SX Bet sportId mapping for sport", sport=sport)
                return []
            params["sportIds"] = sport_id

        all_markets: List[Dict] = []
        pages_fetched = 0
        max_pages = 10  # Safety limit

        try:
            while pages_fetched < max_pages:
                resp = await self._http.get("/markets/active", params=params)
                resp.raise_for_status()
                body = resp.json()

                # Response: {"status": "success", "data": {"markets": [...], "nextKey": "..."}}
                data = body.get("data", {}) if isinstance(body, dict) else {}
                if isinstance(data, dict):
                    markets_page = data.get("markets", [])
                    next_key = data.get("nextKey")
                else:
                    markets_page = []
                    next_key = None

                if not isinstance(markets_page, list):
                    break

                all_markets.extend(markets_page)
                pages_fetched += 1

                # Paginate if there's a next key
                if not next_key or len(markets_page) < params.get("pageSize", 50):
                    break
                params["paginationKey"] = next_key

        except Exception as e:
            logger.error("SX Bet market fetch failed", error=repr(e), sport=sport)
            return []

        results: List[UnifiedMarket] = []
        for m in all_markets:
            um = self._to_unified_market(m)
            if um:
                results.append(um)
                self._markets_cache[m.get("marketHash", "")] = m

        logger.info(
            "SX Bet markets fetched",
            total_raw=len(all_markets),
            two_outcome=len(results),
            sport=sport,
        )
        return results

    def _to_unified_market(self, m: Dict) -> Optional[UnifiedMarket]:
        """Convert a raw SX Bet market dict to UnifiedMarket.

        Only two-outcome **moneyline** markets (type ∈ SXBET_MONEYLINE_TYPES)
        are supported for binary hedge arbitrage.
        """
        market_hash = m.get("marketHash", "")
        if not market_hash:
            return None

        # Filter: only moneyline types
        market_type = m.get("type", 0)
        if market_type not in SXBET_MONEYLINE_TYPES:
            return None

        # SX Bet markets have outcomeOneName / outcomeTwoName
        outcome_one = m.get("outcomeOneName", "")
        outcome_two = m.get("outcomeTwoName", "")

        # Build question from team names or market title
        team_a = outcome_one
        team_b = outcome_two
        event_name = m.get("eventName", "") or m.get("leagueLabel", "")
        # Construct a question similar to "Team A vs Team B"
        question = f"{team_a} vs {team_b}" if team_a and team_b else event_name

        # Sport mapping (reverse lookup)
        sport_id = m.get("sportId", 0)
        sport_label = self._sports.get(sport_id, "").lower()

        # Start time
        game_time = m.get("gameTime", 0)
        if isinstance(game_time, str):
            try:
                game_time = int(game_time)
            except (ValueError, TypeError):
                game_time = 0

        # Market status — SX Bet uses "ACTIVE" string
        status = m.get("status", "")
        active = str(status).upper() == "ACTIVE"

        return UnifiedMarket(
            platform=Platform.SXBET,
            market_id=market_hash,
            question=question,
            sport=sport_label,
            league=m.get("leagueLabel", "") or str(m.get("leagueId", "")),
            event_name=event_name,
            start_time=float(game_time),
            team_a=team_a,
            team_b=team_b,
            active=active,
            metadata={
                "sportId": sport_id,
                "leagueId": m.get("leagueId"),
                "marketHash": market_hash,
                "outcomeOneName": outcome_one,
                "outcomeTwoName": outcome_two,
                "teamOneName": m.get("teamOneName", ""),
                "teamTwoName": m.get("teamTwoName", ""),
                "type": m.get("type", ""),
                "group1": m.get("group1", ""),
            },
        )

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------

    async def get_odds(
        self,
        market_id: str,
        trade_size: float = 50.0,
        *,
        live: bool = False,
    ) -> Optional[UnifiedOdds]:
        """Get current best odds for a market.

        Uses ``GET /orders/odds/best`` for quick best-price lookup, and
        optionally ``GET /orders`` for full depth when *live* is True.

        SX Bet odds encoding:
          - API returns ``percentageOdds = impliedOdds * 10^20``
          - Maker odds (probability) = percentageOdds / 10^20
          - Taker odds = 1 - makerOdds
          - Price we pay to BUY outcome = taker odds for that outcome

        For a two-outcome market with outcomes 1 and 2:
          - To buy outcome 1: fill a maker who is selling outcome 1
            → we pay ``1 - makerOdds`` (taker price)
          - best_ask for outcome 1 = lowest taker price among sell orders

        Args:
            market_id: SX Bet marketHash.
            trade_size: Intended bet size in USDC.
            live: If True, also fetch full order-book depth.

        Returns:
            UnifiedOdds or None.
        """
        if not self._http:
            return None

        try:
            # Fetch best odds (both outcomes at once)
            resp = await self._http.get(
                "/orders/odds/best",
                params={
                    "marketHashes": market_id,
                    "baseToken": self.usdc_address,
                },
            )
            resp.raise_for_status()
            body = resp.json()

            # Response: {"data": {"bestOdds": [{"marketHash": ...,
            #   "outcomeOne": {"percentageOdds": "X"}, "outcomeTwo": {"percentageOdds": "Y"}}]}}
            best_odds_list = body.get("data", {}).get("bestOdds", [])
            odds_entry = None
            for entry in best_odds_list:
                if entry.get("marketHash") == market_id:
                    odds_entry = entry
                    break
            if not odds_entry and best_odds_list:
                odds_entry = best_odds_list[0]

            price_yes = 0.0
            price_no = 0.0
            if odds_entry:
                price_yes = self._extract_best_price(odds_entry, outcome="1")
                price_no = self._extract_best_price(odds_entry, outcome="2")

            # Fetch depth + VWAP if live mode requested
            depth_yes = 0.0
            depth_no = 0.0
            if live and (price_yes > 0 or price_no > 0):
                depth_yes, depth_no, vwap_yes, vwap_no = (
                    await self._fetch_order_depth_and_vwap(market_id, trade_size)
                )
                # Use VWAP as price when available (more realistic than
                # best-ask since it accounts for full trade_size fill).
                if vwap_yes > 0:
                    price_yes = vwap_yes
                if vwap_no > 0:
                    price_no = vwap_no

            return UnifiedOdds(
                platform=Platform.SXBET,
                market_id=market_id,
                price_yes=price_yes,
                price_no=price_no,
                max_size_yes=depth_yes,
                max_size_no=depth_no,
                timestamp=time.time(),
            )

        except Exception as e:
            logger.debug(
                "SX Bet odds fetch failed",
                market_id=market_id[:20],
                error=repr(e),
            )
            return None

    def _extract_best_price(self, odds_entry: Dict, outcome: str = "1") -> float:
        """Extract the best taker price for a given outcome.

        SX Bet best odds entry contains:
          {"outcomeOne": {"percentageOdds": "X"}, "outcomeTwo": {"percentageOdds": "Y"}}

        The percentageOdds is the *maker's* implied odds * 10^20.
        The taker (buyer) pays ``1 - (percentageOdds / 10^20)``.
        """
        try:
            # Primary format: outcomeOne / outcomeTwo nested objects
            outcome_key = "outcomeOne" if outcome == "1" else "outcomeTwo"
            outcome_data = odds_entry.get(outcome_key, {})
            if isinstance(outcome_data, dict):
                pct_odds = outcome_data.get("percentageOdds", 0)
                if pct_odds:
                    maker_implied = int(pct_odds) / ODDS_PRECISION
                    taker_price = 1.0 - maker_implied
                    return taker_price

        except (ValueError, TypeError, KeyError) as e:
            logger.debug("SX Bet price parse error", outcome=outcome, error=repr(e))

        return 0.0

    async def _fetch_order_depth_and_vwap(
        self, market_id: str, trade_size: float
    ) -> Tuple[float, float, float, float]:
        """Fetch order-book depth and VWAP for both outcomes.

        Walks the order book to compute the volume-weighted average taker
        price to fill ``trade_size`` USDC.  This is more realistic than
        best-ask because it accounts for depth at each price level.

        Taker capacity formula (from SX Bet docs):
            remainingTakerSpace = (totalBetSize - fillAmount)
                                  * 10^20 / percentageOdds
                                  - (totalBetSize - fillAmount)

        Returns:
            (depth_yes_usdc, depth_no_usdc, vwap_yes, vwap_no)
            depth = total taker capacity in USDC
            vwap  = volume-weighted avg taker price to fill trade_size
                    (0.0 if depth < trade_size → unfillable)
        """
        if not self._http:
            return (0.0, 0.0, 0.0, 0.0)

        try:
            resp = await self._http.get(
                "/orders",
                params={
                    "marketHashes": market_id,
                    "baseToken": self.usdc_address,
                },
            )
            resp.raise_for_status()
            body = resp.json()

            # Response is a flat list of order objects
            orders = body.get("data", body) if isinstance(body, dict) else body
            if not isinstance(orders, list):
                orders = []

            # Separate orders by which outcome the TAKER can buy.
            # isMakerBettingOutcomeOne=True  → taker buys outcome 2 (NO)
            # isMakerBettingOutcomeOne=False → taker buys outcome 1 (YES)
            yes_levels: List[Tuple[float, float]] = []  # (taker_price, taker_usdc)
            no_levels: List[Tuple[float, float]] = []

            for order in orders:
                total_size = int(order.get("totalBetSize", 0))
                fill_amount = int(order.get("fillAmount", 0))
                remaining_maker = total_size - fill_amount
                if remaining_maker <= 0:
                    continue

                pct_odds = int(order.get("percentageOdds", 0))
                if pct_odds <= 0 or pct_odds >= ODDS_PRECISION:
                    continue

                # Taker price = 1 - makerOdds
                taker_price = 1.0 - pct_odds / ODDS_PRECISION

                # Remaining taker capacity in raw USDC units
                remaining_taker_raw = (
                    remaining_maker * ODDS_PRECISION // pct_odds
                    - remaining_maker
                )
                remaining_taker_usdc = remaining_taker_raw / (10 ** USDC_DECIMALS)
                if remaining_taker_usdc < 0.01:
                    continue

                maker_betting_one = order.get("isMakerBettingOutcomeOne")
                if maker_betting_one is True:
                    # Maker bets 1 → taker can buy 2 (NO)
                    no_levels.append((taker_price, remaining_taker_usdc))
                elif maker_betting_one is False:
                    # Maker bets 2 → taker can buy 1 (YES)
                    yes_levels.append((taker_price, remaining_taker_usdc))

            def _depth_and_vwap(
                levels: List[Tuple[float, float]], size: float
            ) -> Tuple[float, float]:
                """Compute total depth and VWAP for price levels."""
                # Sort by taker price ascending (cheapest first)
                levels.sort(key=lambda x: x[0])
                total_depth = sum(cap for _, cap in levels)
                if total_depth < 0.01:
                    return (0.0, 0.0)
                # Only compute VWAP if depth >= size (fully fillable)
                if total_depth < size:
                    return (total_depth, 0.0)
                remaining = size
                weighted_sum = 0.0
                filled = 0.0
                for price, cap in levels:
                    take = min(remaining, cap)
                    weighted_sum += price * take
                    filled += take
                    remaining -= take
                    if remaining <= 0:
                        break
                vwap = weighted_sum / filled if filled > 0 else 0.0
                return (total_depth, vwap)

            depth_yes, vwap_yes = _depth_and_vwap(yes_levels, trade_size)
            depth_no, vwap_no = _depth_and_vwap(no_levels, trade_size)
            return (depth_yes, depth_no, vwap_yes, vwap_no)

        except Exception as e:
            logger.debug("SX Bet depth/VWAP fetch failed", error=repr(e))
            return (0.0, 0.0, 0.0, 0.0)

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
        """Fill existing maker orders on SX Bet.

        Uses ``POST /orders/fill/v2`` with ``desiredOdds`` and
        ``oddsSlippage`` for slippage protection.

        Args:
            market_id: SX Bet marketHash.
            outcome: YES (outcome 1) or NO (outcome 2).
            amount: Bet size in USDC.
            min_odds: Maximum acceptable price (probability).
                      Converted to SX Bet ``desiredOdds`` format.

        Returns:
            BetResult with execution details.
        """
        start = time.time()

        if amount < TAKER_MIN_USDC:
            return BetResult(
                status=BetResult.Status.FAILED,
                platform=Platform.SXBET,
                market_id=market_id,
                outcome=outcome,
                amount=amount,
                error_message=f"Below taker minimum ({TAKER_MIN_USDC} USDC)",
            )

        if self.dry_run or not self.private_key:
            logger.info(
                "DRY RUN: SX Bet would place bet",
                market=market_id[:20],
                outcome=outcome.value,
                amount=f"${amount:.2f}",
                min_odds=f"{min_odds:.4f}",
            )
            return BetResult(
                status=BetResult.Status.SKIPPED,
                platform=Platform.SXBET,
                market_id=market_id,
                outcome=outcome,
                amount=amount,
                effective_odds=min_odds,
                execution_time_ms=(time.time() - start) * 1000,
            )

        try:
            # Convert outcome to SX Bet format
            betting_outcome_one = outcome == OutcomeSide.YES

            # Convert min_odds (probability we pay) to SX Bet desiredOdds
            # We pay taker_price = 1 - maker_implied
            # So desired maker_implied = 1 - taker_price = 1 - min_odds
            # desiredOdds = maker_implied * 10^20
            desired_maker_implied = 1.0 - min_odds
            desired_odds = str(int(desired_maker_implied * ODDS_PRECISION))

            # Convert amount to raw USDC (6 decimals)
            stake_wei = str(int(amount * (10 ** USDC_DECIMALS)))

            # Random fill salt (32 bytes as uint256)
            fill_salt = int.from_bytes(os.urandom(32), "big")

            # Odds slippage: 0-100 integer (percentage points)
            odds_slippage = 5  # 5% slippage tolerance

            # Sign the fill (EIP-712 signature required by SX Bet)
            signature = self._sign_fill_order(
                market_hash=market_id,
                stake_wei=stake_wei,
                desired_odds=desired_odds,
                odds_slippage=odds_slippage,
                betting_outcome_one=betting_outcome_one,
                fill_salt=fill_salt,
            )
            if not signature:
                return BetResult(
                    status=BetResult.Status.FAILED,
                    platform=Platform.SXBET,
                    market_id=market_id,
                    outcome=outcome,
                    amount=amount,
                    error_message="EIP-712 signing failed",
                )

            # Build fill payload matching SX Bet fill/v2 API spec
            payload = {
                "market": market_id,
                "baseToken": self.usdc_address,
                "isTakerBettingOutcomeOne": betting_outcome_one,
                "stakeWei": stake_wei,
                "desiredOdds": desired_odds,
                "oddsSlippage": odds_slippage,
                "fillSalt": str(fill_salt),
                "taker": self._wallet_address,
                "takerSig": signature,
                "message": "N/A",
            }

            resp = await self._http.post("/orders/fill/v2", json=payload)
            elapsed = (time.time() - start) * 1000

            if resp.status_code == 200:
                result = resp.json()
                fill_hash = result.get("fillHash", "")
                is_partial = result.get("isPartialFill", False)
                total_filled = result.get("totalFilled", "0")
                avg_odds = result.get("averageOdds", "0")

                # Convert filled amount back to USDC
                filled_usdc = int(total_filled) / (10 ** USDC_DECIMALS) if total_filled else 0

                # Convert averageOdds to taker price
                if avg_odds and int(avg_odds) > 0:
                    effective_price = 1.0 - (int(avg_odds) / ODDS_PRECISION)
                else:
                    effective_price = min_odds

                logger.info(
                    "SX Bet fill executed",
                    fill_hash=fill_hash[:20] if fill_hash else "none",
                    filled=f"${filled_usdc:.2f}",
                    partial=is_partial,
                    effective_price=f"{effective_price:.4f}",
                    latency_ms=f"{elapsed:.0f}",
                )

                return BetResult(
                    status=BetResult.Status.SUCCESS,
                    platform=Platform.SXBET,
                    market_id=market_id,
                    outcome=outcome,
                    amount=filled_usdc,
                    effective_odds=effective_price,
                    tx_hash=fill_hash,
                    execution_time_ms=elapsed,
                )
            else:
                error_body = resp.text
                logger.error(
                    "SX Bet fill rejected",
                    status=resp.status_code,
                    body=error_body[:200],
                )
                return BetResult(
                    status=BetResult.Status.FAILED,
                    platform=Platform.SXBET,
                    market_id=market_id,
                    outcome=outcome,
                    amount=amount,
                    error_message=f"HTTP {resp.status_code}: {error_body[:100]}",
                    execution_time_ms=elapsed,
                )

        except Exception as e:
            elapsed = (time.time() - start) * 1000
            logger.error("SX Bet bet placement failed", error=repr(e))
            return BetResult(
                status=BetResult.Status.FAILED,
                platform=Platform.SXBET,
                market_id=market_id,
                outcome=outcome,
                amount=amount,
                error_message=repr(e),
                execution_time_ms=elapsed,
            )

    def _sign_fill_order(
        self,
        market_hash: str,
        stake_wei: str,
        desired_odds: str,
        odds_slippage: int,
        betting_outcome_one: bool,
        fill_salt: int,
    ) -> str:
        """Sign a fill order using EIP-712 typed data.

        Constructs the exact typed-data payload required by SX Bet's
        ``POST /orders/fill/v2`` endpoint, including the nested
        ``FillObject`` struct.

        Domain:
            name="SX Bet", version=domainVersion (from /metadata),
            chainId=4162, verifyingContract=EIP712FillHasher

        Primary type: ``Details`` containing human-readable metadata
        and a nested ``FillObject`` with the actual fill parameters.
        """
        if not self.private_key:
            return ""

        try:
            verifying_contract = (
                self._eip712_fill_hasher
                or "0x845a2Da2D70fEDe8474b1C8518200798c60aC364"
            )
            ADDRESS_ZERO = "0x0000000000000000000000000000000000000000"
            HASH_ZERO = (
                "0x0000000000000000000000000000000000000000"
                "000000000000000000000000"
            )

            full_message = {
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                        {"name": "verifyingContract", "type": "address"},
                    ],
                    "Details": [
                        {"name": "action", "type": "string"},
                        {"name": "market", "type": "string"},
                        {"name": "betting", "type": "string"},
                        {"name": "stake", "type": "string"},
                        {"name": "worstOdds", "type": "string"},
                        {"name": "worstReturning", "type": "string"},
                        {"name": "fills", "type": "FillObject"},
                    ],
                    "FillObject": [
                        {"name": "stakeWei", "type": "string"},
                        {"name": "marketHash", "type": "string"},
                        {"name": "baseToken", "type": "string"},
                        {"name": "desiredOdds", "type": "string"},
                        {"name": "oddsSlippage", "type": "uint256"},
                        {"name": "isTakerBettingOutcomeOne", "type": "bool"},
                        {"name": "fillSalt", "type": "uint256"},
                        {"name": "beneficiary", "type": "address"},
                        {"name": "beneficiaryType", "type": "uint8"},
                        {"name": "cashOutTarget", "type": "bytes32"},
                    ],
                },
                "primaryType": "Details",
                "domain": {
                    "name": "SX Bet",
                    "version": self._domain_version,
                    "chainId": self.chain_id,
                    "verifyingContract": verifying_contract,
                },
                "message": {
                    "action": "N/A",
                    "betting": "N/A",
                    "stake": "N/A",
                    "worstOdds": "N/A",
                    "worstReturning": "N/A",
                    "market": market_hash,
                    "fills": {
                        "stakeWei": stake_wei,
                        "marketHash": market_hash,
                        "baseToken": self.usdc_address,
                        "desiredOdds": desired_odds,
                        "oddsSlippage": odds_slippage,
                        "isTakerBettingOutcomeOne": betting_outcome_one,
                        "fillSalt": fill_salt,
                        "beneficiary": ADDRESS_ZERO,
                        "beneficiaryType": 0,
                        "cashOutTarget": HASH_ZERO,
                    },
                },
            }

            signable = encode_typed_data(full_message=full_message)
            signed = Account.sign_message(
                signable, private_key=self.private_key
            )
            return "0x" + signed.signature.hex()

        except Exception as e:
            logger.error("EIP-712 signing failed", error=repr(e))
            return ""

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    async def get_balance(self) -> float:
        """Get USDC balance on SX Network.

        Reads the USDC ERC-20 balance via RPC call to the SX Network.
        """
        if not self._w3 or not self._wallet_address:
            return 10000.0 if self.dry_run else 0.0

        try:
            # Minimal ERC-20 ABI for balanceOf
            usdc_abi = [
                {
                    "inputs": [{"name": "account", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "", "type": "uint256"}],
                    "stateMutability": "view",
                    "type": "function",
                }
            ]
            contract = self._w3.eth.contract(
                address=self.usdc_address, abi=usdc_abi
            )
            raw = contract.functions.balanceOf(
                Web3.to_checksum_address(self._wallet_address)
            ).call()
            return raw / (10 ** USDC_DECIMALS)

        except Exception as e:
            logger.error("SX Bet balance check failed", error=repr(e))
            return 10000.0 if self.dry_run else 0.0

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    async def get_active_sports(self) -> Dict[int, str]:
        """Return the cached sport ID -> label mapping."""
        return dict(self._sports)

    async def get_active_leagues(self, sport_id: Optional[int] = None) -> List[Dict]:
        """Fetch active leagues, optionally filtered by sport.

        Returns:
            List of league dicts with keys: leagueId, label, sportId, etc.
        """
        if not self._http:
            return []

        try:
            params = {}
            if sport_id is not None:
                params["sportIds"] = sport_id
            resp = await self._http.get("/leagues/active", params=params)
            resp.raise_for_status()
            body = resp.json()
            leagues = body.get("data", []) if isinstance(body, dict) else body
            return leagues if isinstance(leagues, list) else []
        except Exception as e:
            logger.debug("SX Bet leagues fetch failed", error=repr(e))
            return []

    async def get_trades(self, market_hash: Optional[str] = None) -> List[Dict]:
        """Fetch recent trade history.

        Args:
            market_hash: Optional market filter.

        Returns:
            List of trade dicts.
        """
        if not self._http:
            return []

        try:
            params: Dict[str, Any] = {}
            if market_hash:
                params["marketHashes"] = market_hash
            if self._wallet_address:
                params["maker"] = self._wallet_address
            resp = await self._http.get("/trades", params=params)
            resp.raise_for_status()
            body = resp.json()
            trades = body.get("data", []) if isinstance(body, dict) else body
            return trades if isinstance(trades, list) else []
        except Exception as e:
            logger.debug("SX Bet trades fetch failed", error=repr(e))
            return []
