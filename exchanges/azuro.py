"""Azuro exchange adapter.

Integrates with Azuro Protocol on Polygon via:
  - GraphQL (data-feed subgraph) for market/odds discovery
  - web3.py for on-chain betting (LP contract)

Azuro uses an AMM (Automated Market Maker) with a singleton liquidity
pool.  Bets are placed on-chain and result in NFT (ERC-721) bet slips
that pay out upon event resolution.
"""

import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

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
# Azuro LP ABI (minimal – only the functions we need)
# ---------------------------------------------------------------------------
AZURO_LP_ABI = [
    # bet(address core, uint128 amount, uint64 expiresAt, tuple(uint256,uint64) bet)
    # Simplified – the actual signature depends on the deployed version.
    {
        "inputs": [
            {"name": "core", "type": "address"},
            {"name": "amount", "type": "uint128"},
            {"name": "expiresAt", "type": "uint64"},
            {
                "name": "betData",
                "type": "tuple",
                "components": [
                    {"name": "conditionId", "type": "uint256"},
                    {"name": "outcomeId", "type": "uint64"},
                ],
            },
        ],
        "name": "bet",
        "outputs": [{"name": "tokenId", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # withdrawPayout(uint256 tokenId)
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "withdrawPayout",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

USDC_ABI_APPROVE = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


# ---------------------------------------------------------------------------
# Default GraphQL queries
# ---------------------------------------------------------------------------

# Query active sports games with conditions and outcomes
GAMES_QUERY = """
query ActiveGames($sport: String, $startsAt_gt: BigInt!) {
  games(
    where: {
      startsAt_gt: $startsAt_gt
      sport_: { name_contains_nocase: $sport }
    }
    first: 200
    orderBy: startsAt
    orderDirection: asc
  ) {
    gameId
    title
    slug
    startsAt
    sport { name slug }
    league { name slug country { name } }
    participants { name sortOrder }
    conditions(where: { isExpressForbidden: false }) {
      conditionId
      outcomes {
        outcomeId
        currentOdds
      }
      wonOutcomeIds
    }
  }
}
"""

# Query odds for a specific condition
ODDS_QUERY = """
query ConditionOdds($conditionId: BigInt!) {
  conditions(where: { conditionId: $conditionId }) {
    conditionId
    outcomes {
      outcomeId
      currentOdds
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class AzuroExchange(BaseExchange):
    """Azuro protocol adapter implementing BaseExchange.

    Configuration:
        subgraph_url: Azuro data-feed subgraph endpoint (Polygon).
        lp_address:   Azuro LP contract address on Polygon.
        rpc_url:      Polygon RPC endpoint.
        private_key:  Wallet private key for on-chain betting.
        usdc_address: USDC contract address on Polygon.
    """

    platform = Platform.AZURO

    def __init__(
        self,
        subgraph_url: str,
        lp_address: str = "",
        core_address: str = "",
        rpc_url: str = "https://polygon-rpc.com",
        private_key: str = "",
        usdc_address: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        dry_run: bool = False,
    ):
        self.subgraph_url = subgraph_url
        self.lp_address = lp_address
        self.core_address = core_address
        self.rpc_url = rpc_url
        self.private_key = private_key
        self.usdc_address = usdc_address
        self.dry_run = dry_run

        self._http: Optional[httpx.AsyncClient] = None
        self._w3: Optional[Web3] = None
        self._lp_contract = None
        self._usdc_contract = None

        # Cache: gameId -> UnifiedMarket
        self._markets_cache: Dict[str, UnifiedMarket] = {}
        # Cache: conditionId -> full condition data from subgraph
        self._conditions_cache: Dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self._http = httpx.AsyncClient(timeout=30.0)

        if self.private_key and self.lp_address:
            self._w3 = Web3(Web3.HTTPProvider(self.rpc_url))
            # Polygon is a POA chain
            self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

            self._lp_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(self.lp_address),
                abi=AZURO_LP_ABI,
            )
            self._usdc_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(self.usdc_address),
                abi=USDC_ABI_APPROVE,
            )
            logger.info(
                "AzuroExchange connected (full mode)",
                lp=self.lp_address,
            )
        else:
            logger.info("AzuroExchange connected (read-only / dry-run)")

    async def disconnect(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None
        logger.info("AzuroExchange disconnected")

    # ------------------------------------------------------------------
    # GraphQL helper
    # ------------------------------------------------------------------

    async def _graphql(self, query: str, variables: dict) -> dict:
        """Execute a GraphQL query against the Azuro subgraph."""
        if not self._http:
            raise RuntimeError("AzuroExchange not connected")

        resp = await self._http.post(
            self.subgraph_url,
            json={"query": query, "variables": variables},
        )
        resp.raise_for_status()
        body = resp.json()

        if "errors" in body:
            logger.error("Azuro GraphQL error", errors=body["errors"])
            raise RuntimeError(f"Azuro GraphQL errors: {body['errors']}")

        return body.get("data", {})

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def get_markets(self, sport: Optional[str] = None) -> List[UnifiedMarket]:
        """Fetch active Azuro games as UnifiedMarkets.

        Each *game* with at least one active binary condition is returned
        as a UnifiedMarket.  Multi-outcome conditions (3-way) are also
        included but marked in metadata.
        """
        now_ts = int(time.time())
        sport_filter = sport or ""

        data = await self._graphql(GAMES_QUERY, {
            "sport": sport_filter,
            "startsAt_gt": str(now_ts),
        })

        games = data.get("games", [])
        results: List[UnifiedMarket] = []

        for g in games:
            um = self._parse_game(g)
            if um:
                results.append(um)
                self._markets_cache[um.market_id] = um

        logger.info(
            "Azuro markets fetched",
            raw_games=len(games),
            parsed=len(results),
            sport=sport,
        )
        return results

    def _parse_game(self, g: dict) -> Optional[UnifiedMarket]:
        """Parse a subgraph game node into a UnifiedMarket."""
        try:
            conditions = g.get("conditions", [])
            if not conditions:
                return None

            # Find the first active binary condition (2 outcomes, not yet resolved)
            binary_cond = None
            for c in conditions:
                won = c.get("wonOutcomeIds") or []
                if len(won) == 0 and len(c.get("outcomes", [])) == 2:
                    binary_cond = c
                    break

            if not binary_cond:
                return None

            game_id = g["gameId"]

            # Cache condition data
            cond_id = binary_cond["conditionId"]
            self._conditions_cache[cond_id] = binary_cond

            participants = g.get("participants", [])
            team_a = participants[0]["name"] if len(participants) > 0 else ""
            team_b = participants[1]["name"] if len(participants) > 1 else ""

            sport_data = g.get("sport", {})
            league_data = g.get("league", {})

            return UnifiedMarket(
                platform=Platform.AZURO,
                market_id=game_id,
                question=g.get("title", f"{team_a} vs {team_b}"),
                sport=sport_data.get("slug", ""),
                league=league_data.get("name", ""),
                event_name=g.get("title", ""),
                start_time=float(g.get("startsAt", 0)),
                team_a=team_a,
                team_b=team_b,
                active=True,
                metadata={
                    "slug": g.get("slug", ""),
                    "condition_id": cond_id,
                    "outcomes": binary_cond.get("outcomes", []),
                    "all_conditions": [
                        {
                            "conditionId": c["conditionId"],
                            "num_outcomes": len(c.get("outcomes", [])),
                        }
                        for c in conditions
                    ],
                },
            )
        except (KeyError, IndexError) as e:
            logger.debug("Failed to parse Azuro game", error=repr(e))
            return None

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------

    async def get_odds(
        self, market_id: str, trade_size: float = 50.0
    ) -> Optional[UnifiedOdds]:
        """Get current odds for an Azuro market.

        The ``market_id`` is the Azuro ``gameId``.  We look up the cached
        binary condition and convert AMM odds to probability prices.

        Azuro odds are in decimal format (e.g. 2.0 = 50% implied prob).
        We convert to price = 1 / odds so they are comparable to Polymarket.

        Args:
            market_id: Azuro game ID.
            trade_size: Intended bet size for slippage estimation.

        Returns:
            UnifiedOdds with implied probability prices.
        """
        um = self._markets_cache.get(market_id)
        if not um:
            return None

        cond_id = um.metadata.get("condition_id")
        if not cond_id:
            return None

        # Fetch fresh odds from subgraph
        try:
            data = await self._graphql(ODDS_QUERY, {"conditionId": cond_id})
            conditions = data.get("conditions", [])
            if not conditions:
                return None

            cond = conditions[0]
            outcomes = cond.get("outcomes", [])
            if len(outcomes) < 2:
                return None

            # outcomes[0] = first outcome (typically "Yes" / team A win)
            # outcomes[1] = second outcome (typically "No" / team B win or draw)
            odds_yes = float(outcomes[0].get("currentOdds", 0))
            odds_no = float(outcomes[1].get("currentOdds", 0))

            # Convert decimal odds to implied probability (price)
            # Decimal odds of 2.0 = 50% probability = price $0.50
            price_yes = 1.0 / odds_yes if odds_yes > 0 else 1.0
            price_no = 1.0 / odds_no if odds_no > 0 else 1.0

            # Clamp to valid range
            price_yes = max(0.01, min(0.99, price_yes))
            price_no = max(0.01, min(0.99, price_no))

            return UnifiedOdds(
                platform=Platform.AZURO,
                market_id=market_id,
                price_yes=price_yes,
                price_no=price_no,
                max_size_yes=0.0,  # TODO: query maxBet from contract
                max_size_no=0.0,
                timestamp=time.time(),
            )

        except Exception as e:
            logger.error("Failed to fetch Azuro odds", error=repr(e), market_id=market_id)
            return None

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
        """Place a bet on Azuro via LP contract.

        Args:
            market_id: Azuro game ID.
            outcome: YES (outcome 0) or NO (outcome 1).
            amount: USDC amount to bet.
            min_odds: Minimum acceptable decimal odds.

        Returns:
            BetResult with tx hash on success.
        """
        start = time.time()
        um = self._markets_cache.get(market_id)
        if not um:
            return BetResult(
                status=BetResult.Status.FAILED,
                platform=Platform.AZURO,
                market_id=market_id,
                outcome=outcome,
                error_message="Market not in cache",
            )

        cond_id = um.metadata.get("condition_id")
        outcomes_data = um.metadata.get("outcomes", [])
        outcome_idx = 0 if outcome == OutcomeSide.YES else 1

        if outcome_idx >= len(outcomes_data):
            return BetResult(
                status=BetResult.Status.FAILED,
                platform=Platform.AZURO,
                market_id=market_id,
                outcome=outcome,
                error_message="Outcome index out of range",
            )

        outcome_id = outcomes_data[outcome_idx].get("outcomeId")

        if self.dry_run or not self._lp_contract:
            logger.info(
                "DRY RUN: Azuro would place bet",
                game=um.event_name[:50],
                outcome=outcome.value,
                amount=amount,
                min_odds=min_odds,
            )
            return BetResult(
                status=BetResult.Status.SKIPPED,
                platform=Platform.AZURO,
                market_id=market_id,
                outcome=outcome,
                amount=amount,
                effective_odds=min_odds,
                execution_time_ms=(time.time() - start) * 1000,
            )

        # On-chain bet
        try:
            tx_hash = await self._execute_on_chain_bet(
                condition_id=int(cond_id),
                outcome_id=int(outcome_id),
                amount_usdc=amount,
                min_odds=min_odds,
            )
            elapsed = (time.time() - start) * 1000

            return BetResult(
                status=BetResult.Status.SUCCESS,
                platform=Platform.AZURO,
                market_id=market_id,
                outcome=outcome,
                amount=amount,
                effective_odds=min_odds,
                tx_hash=tx_hash,
                execution_time_ms=elapsed,
            )

        except Exception as e:
            elapsed = (time.time() - start) * 1000
            logger.error("Azuro bet failed", error=repr(e))
            return BetResult(
                status=BetResult.Status.FAILED,
                platform=Platform.AZURO,
                market_id=market_id,
                outcome=outcome,
                amount=amount,
                error_message=str(e),
                execution_time_ms=elapsed,
            )

    async def _execute_on_chain_bet(
        self,
        condition_id: int,
        outcome_id: int,
        amount_usdc: float,
        min_odds: float,
    ) -> str:
        """Execute an on-chain bet transaction.

        This runs the Web3 calls in a thread to avoid blocking the event loop.

        Returns:
            Transaction hash as hex string.
        """
        w3 = self._w3
        lp = self._lp_contract
        usdc = self._usdc_contract
        private_key = self.private_key
        core_address = self.core_address

        def _do_bet() -> str:
            account = w3.eth.account.from_key(private_key)
            sender = account.address

            amount_wei = int(amount_usdc * 1e6)  # USDC has 6 decimals

            # Step 1: Approve USDC spending
            nonce = w3.eth.get_transaction_count(sender)
            approve_tx = usdc.functions.approve(
                Web3.to_checksum_address(lp.address),
                amount_wei,
            ).build_transaction({
                "from": sender,
                "nonce": nonce,
                "gas": 100_000,
                "gasPrice": w3.eth.gas_price,
            })
            signed_approve = w3.eth.account.sign_transaction(approve_tx, private_key)
            w3.eth.send_raw_transaction(signed_approve.raw_transaction)

            # Step 2: Place bet
            nonce += 1
            deadline = int(time.time()) + 300  # 5 min expiry

            bet_tx = lp.functions.bet(
                Web3.to_checksum_address(core_address),
                amount_wei,
                deadline,
                (condition_id, outcome_id),
            ).build_transaction({
                "from": sender,
                "nonce": nonce,
                "gas": 500_000,
                "gasPrice": w3.eth.gas_price,
            })
            signed_bet = w3.eth.account.sign_transaction(bet_tx, private_key)
            tx_hash = w3.eth.send_raw_transaction(signed_bet.raw_transaction)

            # Wait for receipt
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt["status"] != 1:
                raise RuntimeError(f"Bet tx reverted: {tx_hash.hex()}")

            return tx_hash.hex()

        return await asyncio.to_thread(_do_bet)

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    async def get_balance(self) -> float:
        """Get USDC balance on Polygon for the configured wallet."""
        if not self._w3 or not self._usdc_contract:
            return 10000.0 if self.dry_run else 0.0

        def _check():
            account = self._w3.eth.account.from_key(self.private_key)
            balance_wei = self._usdc_contract.functions.balanceOf(
                account.address
            ).call()
            return balance_wei / 1e6

        try:
            return await asyncio.to_thread(_check)
        except Exception as e:
            logger.error("Azuro balance check failed", error=repr(e))
            return 0.0
