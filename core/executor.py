"""Order execution engine using CLOB API."""

import asyncio
from importlib import metadata
import math
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from config.settings import get_settings
from config.constants import POLYGON_CHAIN_ID
from models.market import NegativeRiskArbitrageOpportunity, NegativeRiskStrategy
from models.order import ArbitrageOpportunity, ShortArbitrageOpportunity, Order, OrderSide, OrderStatus, OrderType
from models.position import AccountState
from utils.logger import get_logger
from utils.rate_limiter import RateLimiter

logger = get_logger(__name__)

MIN_MARKETABLE_ORDER_USDC = 1.0
MIN_POLYMARKET_CLIENT_VERSION = "0.1.0b9"


def _version_tuple(version: str) -> Tuple[int, ...]:
    """Parse package versions well enough for minimum-version checks."""
    return tuple(int(part) for part in re.findall(r"\d+", version))


class ExecutionResult(Enum):
    """Result of arbitrage execution."""
    SUCCESS = "success"  # Both orders filled
    PARTIAL = "partial"  # One order filled, one failed
    FAILED = "failed"  # Both orders failed
    SKIPPED = "skipped"  # Opportunity no longer valid


@dataclass
class ExecutionReport:
    """Report of an arbitrage execution attempt."""
    result: ExecutionResult
    opportunity: ArbitrageOpportunity
    order_yes: Optional[Order] = None
    order_no: Optional[Order] = None
    execution_time_ms: float = 0.0
    error_message: Optional[str] = None
    final_balance: Optional[float] = None
    fatal_error: bool = False

    @property
    def is_success(self) -> bool:
        """Check if execution was successful."""
        return self.result == ExecutionResult.SUCCESS


class OrderExecutor:
    """
    Executes arbitrage orders using Polymarket CLOB API.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        private_key: Optional[str] = None,
        proxy_wallet: Optional[str] = None,
        signature_type: Optional[int] = None,
        chain_id: int = POLYGON_CHAIN_ID,
        dry_run: bool = False,
    ):
        """
        Initialize the order executor.

        Args:
            api_key: Polymarket API key
            api_secret: Polymarket API secret
            passphrase: Polymarket passphrase
            private_key: Wallet private key for order signing (required for live trading)
            proxy_wallet: Gnosis Safe proxy wallet address
            chain_id: Polygon chain ID
            dry_run: If True, don't actually submit orders
        """
        self.dry_run = dry_run
        self.rate_limiter = RateLimiter(rate=10.0)
        self._account_state = AccountState()

        # For dry-run mode, we don't need a real client with private key
        if dry_run and not private_key:
            self.client = None
            logger.info(
                "Order executor initialized (DRY RUN - no client)",
                dry_run=dry_run,
            )
            return

        # Validate private key for live trading
        if not private_key:
            raise ValueError("Private key is required for live trading")

        self._check_polymarket_client_version()

        from polymarket import ApiKeyCreds, SecureClient

        credentials = None
        if api_key and api_secret and passphrase:
            credentials = ApiKeyCreds(
                key=api_key,
                secret=api_secret,
                passphrase=passphrase,
            )
        else:
            logger.info("No API credentials provided, deriving from Private Key...")

        try:
            self.client = SecureClient.create(
                private_key=private_key,
                wallet=proxy_wallet,
                credentials=credentials,
            )
        except Exception as e:
            logger.error("Failed to initialize Polymarket client", error=repr(e))
            raise ValueError(f"Could not initialize Polymarket client: {e}")

        if not credentials:
            logger.info("Successfully derived API credentials")

        logger.info(
            "Order executor initialized",
            dry_run=dry_run,
        )

        if proxy_wallet:
            logger.info("Using Proxy Wallet", address=proxy_wallet)
        if signature_type is not None:
            logger.warning(
                "signature_type is ignored by polymarket-client; wallet type is auto-detected",
                signature_type=signature_type,
            )
        
        self.proxy_wallet = proxy_wallet

    @staticmethod
    def _check_polymarket_client_version() -> None:
        """Fail fast when the installed Polymarket SDK is too old."""
        try:
            installed = metadata.version("polymarket-client")
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                "polymarket-client is not installed; run "
                "`pip install --pre polymarket-client` with Python 3.11+"
            ) from exc

        if _version_tuple(installed) < _version_tuple(MIN_POLYMARKET_CLIENT_VERSION):
            raise RuntimeError(
                "Installed polymarket-client is too old for live trading "
                f"(installed={installed}, required>={MIN_POLYMARKET_CLIENT_VERSION}); "
                "run `pip install --pre -U polymarket-client`"
            )

        logger.info("polymarket-client version checked", version=installed)

    async def get_account_balance(self) -> float:
        """
        Get current USDC balance.
        
        Checks standard API first (likely Bridged USDC).
        If 0, falls back to Web3 check for Native USDC on Proxy.
        
        Returns:
            float: Balance in USDC
        """
        if self.dry_run and not self.client:
            return 10000.0

        api_balance = 0.0
        try:
            balance = await asyncio.to_thread(
                self.client.get_balance_allowance,
                asset_type="COLLATERAL",
            )
            api_balance = float(balance.balance) / 1e6
        except Exception as e:
            logger.error("Failed to fetch API balance", error=repr(e))
        
        # If API returns > 0, trust it (it matches the token the API expects)
        if api_balance > 0:
            return api_balance
            
        # Fallback: Check Native USDC on Proxy via Web3
        if self.client and self.proxy_wallet:
            try:
                from web3 import Web3

                proxy_wallet = self.proxy_wallet  # capture for closure

                def _check_web3_balance():
                    abi = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]
                    rpc = "https://polygon-rpc.com"
                    w3 = Web3(Web3.HTTPProvider(rpc))
                    if not w3.is_connected():
                        return 0.0
                    native_addr = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
                    bridged_addr = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                    c_native = w3.eth.contract(address=native_addr, abi=abi)
                    c_bridged = w3.eth.contract(address=bridged_addr, abi=abi)
                    bal_n = c_native.functions.balanceOf(proxy_wallet).call()
                    bal_b = c_bridged.functions.balanceOf(proxy_wallet).call()
                    return (bal_n + bal_b) / 1e6

                total_bal = await asyncio.to_thread(_check_web3_balance)

                if total_bal > 0:
                    logger.info(
                        "Funds Detected via Web3",
                        total=total_bal,
                    )
                    return total_bal
            except Exception as w3e:
                logger.debug("Web3 fallback balance check failed", error=str(w3e))
                
        return api_balance

    async def execute_arbitrage(
        self,
        opportunity: ArbitrageOpportunity,
        validate_before_execute: bool = True,
    ) -> ExecutionReport:
        """
        Execute an arbitrage opportunity.

        Uses asyncio.gather to submit both orders concurrently (best-effort atomicity).

        Args:
            opportunity: Arbitrage opportunity to execute
            validate_before_execute: If True, re-validate opportunity before executing

        Returns:
            Execution report with results
        """
        start_time = time.time()

        # Equal token counts for binary arbitrage:
        # Each YES+NO pair resolves to $1.00 regardless of outcome.
        # Allocate USDC proportionally to prices, NOT 50/50.
        total_cost_per_pair = opportunity.avg_price_yes + opportunity.avg_price_no
        num_tokens = opportunity.trade_size_usdc / total_cost_per_pair

        order_yes = Order(
            token_id=opportunity.market.token_id_yes,
            side=OrderSide.BUY,
            price=opportunity.avg_price_yes,
            size=num_tokens,
            order_type=OrderType.FOK,
        )

        order_no = Order(
            token_id=opportunity.market.token_id_no,
            side=OrderSide.BUY,
            price=opportunity.avg_price_no,
            size=num_tokens,
            order_type=OrderType.FOK,
        )

        if self.dry_run:
            logger.info(
                "DRY RUN: Would execute arbitrage",
                market=opportunity.market.slug,
                profit=f"{opportunity.net_profit_pct:.4f}",
            )
            return ExecutionReport(
                result=ExecutionResult.SKIPPED,
                opportunity=opportunity,
                order_yes=order_yes,
                order_no=order_no,
                execution_time_ms=0,
            )

        try:
            concurrent = getattr(self, '_concurrent', False)

            if concurrent:
                # Concurrent execution: fire both legs simultaneously.
                # Minimises time-window for order-book changes between legs.
                # Risk: if one FOK fails we must emergency-exit the other.
                order_yes, order_no = await asyncio.gather(
                    self._submit_order(order_yes),
                    self._submit_order(order_no),
                )
            else:
                # Sequential execution: submit the more fragile side first.
                # More levels consumed at detection time usually means thinner book.
                # Tie-break with higher price, because equal token counts require more
                # USDC on that leg and it tends to move out from under us sooner.
                # If the first leg FOK fails -> zero loss. If it fills and the second
                # fails -> emergency exit still works, but this ordering reduces risk.
                yes_fragility = (opportunity.levels_yes, opportunity.avg_price_yes)
                no_fragility = (opportunity.levels_no, opportunity.avg_price_no)
                if yes_fragility >= no_fragility:
                    first_order, second_order = order_yes, order_no
                    first_label, second_label = "yes", "no"
                else:
                    first_order, second_order = order_no, order_yes
                    first_label, second_label = "no", "yes"

                first_order = await self._submit_order(first_order)
                if not first_order.is_filled:
                    # Thin side failed — no position opened, zero loss
                    execution_time = (time.time() - start_time) * 1000
                    if first_label == "yes":
                        order_yes = first_order
                    else:
                        order_no = first_order
                    return ExecutionReport(
                        result=ExecutionResult.FAILED,
                        opportunity=opportunity,
                        order_yes=order_yes,
                        order_no=order_no,
                        execution_time_ms=execution_time,
                    )

                # Thin side filled, now submit thick side
                second_order = await self._submit_order(second_order)

                # Map back to yes/no
                if first_label == "yes":
                    order_yes, order_no = first_order, second_order
                else:
                    order_no, order_yes = first_order, second_order

            execution_time = (time.time() - start_time) * 1000
            yes_success = order_yes.is_filled
            no_success = order_no.is_filled

            if yes_success and no_success:
                # Both filled - success!
                self._update_position(opportunity, order_yes, order_no)
                final_balance = await self.get_account_balance()
                return ExecutionReport(
                    result=ExecutionResult.SUCCESS,
                    opportunity=opportunity,
                    order_yes=order_yes,
                    order_no=order_no,
                    execution_time_ms=execution_time,
                    final_balance=final_balance,
                )

            if not yes_success and not no_success:
                logger.warning(
                    "Both arbitrage orders failed",
                    yes_status=order_yes.status.value,
                    no_status=order_no.status.value,
                )
                return ExecutionReport(
                    result=ExecutionResult.FAILED,
                    opportunity=opportunity,
                    order_yes=order_yes,
                    order_no=order_no,
                    execution_time_ms=execution_time,
                )

            else:
                # Partial fill — thick side failed after thin side filled
                logger.warning(
                    "Partial fill detected - initiating emergency exit",
                    yes_filled=yes_success,
                    no_filled=no_success,
                )
                await self._emergency_exit(
                    opportunity,
                    order_yes if yes_success else None,
                    order_no if no_success else None,
                )
                return ExecutionReport(
                    result=ExecutionResult.PARTIAL,
                    opportunity=opportunity,
                    order_yes=order_yes,
                    order_no=order_no,
                    execution_time_ms=execution_time,
                )

        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error("Execution failed", error=repr(e))
            return ExecutionReport(
                result=ExecutionResult.FAILED,
                opportunity=opportunity,
                error_message=repr(e),
                execution_time_ms=execution_time,
            )

    async def execute_neg_risk_arbitrage(
        self,
        opportunity: NegativeRiskArbitrageOpportunity,
    ) -> ExecutionReport:
        """
        Execute a Negative Risk arbitrage opportunity (Multi-Leg).
        
        Submits multiple FOK orders concurrently.
        """
        start_time = time.time()
        
        # Build orders for all legs
        orders: List[Order] = []
        for condition_id, price in opportunity.outcome_prices.items():
            # Find the token ID for this condition based on strategy
            # We need to look up the specific token ID from the market/event
            # This requires mapping condition_id to token_id
            # TODO: The opportunity object should ideally provide token_ids directly
            # For now, we iterate the event outcomes to match condition_id
            
            token_id = None
            for outcome in opportunity.event.outcomes:
                if outcome.condition_id == condition_id:
                    if opportunity.strategy == NegativeRiskStrategy.BUY_ALL_YES:
                        token_id = outcome.token_id_yes
                    elif opportunity.strategy in (NegativeRiskStrategy.BUY_ALL_NO, NegativeRiskStrategy.SHORT_REBALANCE):
                        token_id = outcome.token_id_no
                    break
            
            if not token_id:
                logger.error("Could not find token ID for condition", condition_id=condition_id)
                continue

            orders.append(Order(
                token_id=token_id,
                side=OrderSide.BUY,
                price=price,
                size=opportunity.trade_size_usdc / len(opportunity.outcome_prices) / price,
                order_type=OrderType.FOK,
            ))

        if self.dry_run:
            logger.info(
                "DRY RUN: Would execute NegRisk arbitrage",
                event=opportunity.event.title[:40],
                strategy=opportunity.strategy.value,
                legs=len(orders),
                net_profit=f"{opportunity.net_profit_pct:.2%}",
            )
            return ExecutionReport(
                result=ExecutionResult.SKIPPED,
                opportunity=opportunity,  # This might need casting or generic type update
                execution_time_ms=0,
            )

        # Execute vector
        results = await self._execute_vector(orders)
        execution_time = (time.time() - start_time) * 1000

        # Check for partials
        successful_orders = [o for o in results if o.is_filled]
        failed_orders = [o for o in results if not o.is_filled]

        if len(successful_orders) == len(orders):
            # Perfect execution
            final_balance = await self.get_account_balance()
            return ExecutionReport(
                result=ExecutionResult.SUCCESS,
                opportunity=opportunity,
                execution_time_ms=execution_time,
                final_balance=final_balance,
            )
        elif len(successful_orders) == 0:
            # Total failure (safe)
            return ExecutionReport(
                result=ExecutionResult.FAILED,
                opportunity=opportunity,
                execution_time_ms=execution_time,
            )
        else:
            # Partial fill - EMERGENCY EXIT
            logger.warning(
                "NegRisk Partial fill detected - initiating emergency exit",
                filled=len(successful_orders),
                total=len(orders),
            )
            await self._emergency_exit_vector(successful_orders)
            return ExecutionReport(
                result=ExecutionResult.PARTIAL,
                opportunity=opportunity,
                execution_time_ms=execution_time,
            )

    async def _execute_vector(self, orders: List[Order]) -> List[Order]:
        """Execute a list of orders concurrently."""
        tasks = [self._submit_order(order) for order in orders]
        return await asyncio.gather(*tasks)

    async def _emergency_exit_vector(self, filled_orders: List[Order]) -> None:
        """Sell all filled orders in a failed vector execution."""
        if not filled_orders:
            return

        logger.warning(
            "Emergency exit vector: selling positions",
            count=len(filled_orders),
        )

        tasks = []
        for order in filled_orders:
            tasks.append(self._submit_sell_exit(order))
        
        await asyncio.gather(*tasks)

    async def _submit_sell_exit(self, filled_order: Order) -> None:
        """Submit a market sell to exit a position (with retry)."""
        # Wait for Polygon settlement before selling (tokens need ~3-5s to be credited)
        await asyncio.sleep(3.0)

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            # Hybrid discount: percentage-based for high prices, absolute for low prices
            discount = 0.90 - 0.05 * (attempt - 1)  # 10%, 15%, 20% discount
            pct_price = round(filled_order.filled_avg_price * discount, 2)
            abs_price = round(filled_order.filled_avg_price - 0.01 * attempt, 2)
            sell_price = min(pct_price, abs_price)  # Use the more aggressive price
            sell_price = max(sell_price, 0.01)
            try:
                sell_size = math.floor(filled_order.filled_size * 100) / 100
                if sell_size <= 0:
                    logger.warning("Vector exit size too small", raw=filled_order.filled_size)
                    return
                exit_order = Order(
                    token_id=filled_order.token_id,
                    side=OrderSide.SELL,
                    price=sell_price,
                    size=sell_size,
                    order_type=OrderType.FOK,
                )
                result = await self._submit_order(exit_order)
                if result.is_filled:
                    logger.info("Vector exit order filled", attempt=attempt)
                    return
            except Exception as e:
                logger.error(
                    "Emergency exit failed for order",
                    error=repr(e),
                    attempt=attempt,
                )
            if attempt < max_retries:
                await asyncio.sleep(0.5 * attempt)

    async def _submit_order(self, order: Order) -> Order:
        """
        Submit a single order to CLOB.

        Args:
            order: Order to submit

        Returns:
            Updated order with status
        """
        await self.rate_limiter.acquire()

        try:
            price, size, order_usdc = self._prepare_clob_order_values(
                order.side,
                order.price,
                order.size,
            )

            if size <= 0:
                order.status = OrderStatus.FAILED
                logger.warning("Order size too small after rounding", raw_size=order.size)
                return order

            # Polymarket requires minimum $1 per marketable order
            if order_usdc < MIN_MARKETABLE_ORDER_USDC:
                order.status = OrderStatus.FAILED
                logger.warning("Order USDC below $1 minimum", usdc=f"${order_usdc:.2f}", size=size, price=price)
                return order

            if order.side == OrderSide.BUY:
                response = await asyncio.to_thread(
                    self.client.place_market_order,
                    token_id=order.token_id,
                    side="BUY",
                    amount=f"{order_usdc:.2f}",
                    max_price=str(price),
                    order_type="FOK",
                )
            else:
                response = await asyncio.to_thread(
                    self.client.place_market_order,
                    token_id=order.token_id,
                    side="SELL",
                    shares=str(size),
                    min_price=str(price),
                    order_type="FOK",
                )

            # Parse response
            if getattr(response, "ok", False):
                order.order_id = getattr(response, "order_id", None)
                order.status = OrderStatus.FILLED
                order.filled_size = size
                order.filled_avg_price = price
                logger.info(
                    "Order filled",
                    order_id=order.order_id,
                    token_id=order.token_id[:8],
                    price=price,
                    size=size,
                )
            else:
                order.status = OrderStatus.FAILED
                reason = getattr(response, "message", None) or repr(response)
                logger.warning(
                    "Order rejected",
                    token_id=order.token_id[:8],
                    reason=reason,
                )

        except Exception as e:
            order.status = OrderStatus.FAILED
            error_text = repr(e)
            if "fully filled or killed" in error_text:
                logger.warning(
                    "Order not fillable under FOK",
                    token_id=order.token_id[:8],
                    side=order.side.value,
                    price=order.price,
                    size=size,
                    error=error_text,
                )
            else:
                logger.error("Order submission failed", error=error_text)

        return order

    @staticmethod
    def _prepare_clob_order_values(
        side: OrderSide,
        raw_price: float,
        raw_size: float,
    ) -> Tuple[float, float, float]:
        """Round price/size to CLOB-compatible values and return notional USDC."""
        # Polymarket CLOB requires:
        #   BUY:  maker_amount (USDC = size*price) <= 2 decimal places
        #   SELL: maker_amount (shares) <= 2 decimal places
        # Use GCD to find the largest valid BUY size so size*price is exact 2-dec.
        price = round(raw_price, 2)
        if side == OrderSide.BUY:
            price_cents = round(price * 100)
            step = 100 // math.gcd(price_cents, 100)
            size_cents = (int(raw_size * 100) // step) * step
            size = size_cents / 100
        else:
            size = math.floor(raw_size * 100) / 100

        return price, size, size * price

    async def _emergency_exit(
        self,
        opportunity: ArbitrageOpportunity,
        filled_yes: Optional[Order],
        filled_no: Optional[Order],
    ) -> bool:
        """
        Emergency exit when only one side is filled.

        Attempts to sell the filled position immediately.

        Args:
            opportunity: Original opportunity
            filled_yes: Filled Yes order (or None)
            filled_no: Filled No order (or None)
        """
        filled_order = filled_yes or filled_no
        if not filled_order:
            return False

        logger.warning(
            "Emergency exit: selling position",
            token_id=filled_order.token_id[:8],
            size=filled_order.filled_size,
        )

        # Wait for Polygon settlement before selling (tokens need ~3-5s to be credited)
        await asyncio.sleep(3.0)

        # Retry up to 3 times with increasing price discount
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            # Hybrid discount: percentage-based for high prices, absolute for low prices
            discount = 0.95 - 0.03 * (attempt - 1)  # 5%, 8%, 11% discount
            pct_price = round(filled_order.filled_avg_price * discount, 2)
            abs_price = round(filled_order.filled_avg_price - 0.01 * attempt, 2)
            sell_price = min(pct_price, abs_price)  # Use the more aggressive price
            sell_price = max(sell_price, 0.01)  # Floor at CLOB min tick

            try:
                sell_size = math.floor(filled_order.filled_size * 100) / 100
                if sell_size <= 0:
                    logger.warning("Emergency exit size too small", raw=filled_order.filled_size)
                    return False
                exit_order = Order(
                    token_id=filled_order.token_id,
                    side=OrderSide.SELL,
                    price=sell_price,
                    size=sell_size,
                    order_type=OrderType.FOK,
                )
                response = await self._submit_order(exit_order)

                if response.is_filled:
                    logger.info(
                        "Emergency exit successful",
                        order_id=response.order_id,
                        attempt=attempt,
                    )
                    return True  # Success — stop retrying
                else:
                    logger.error(
                        "Emergency exit rejected",
                        reason=response.status.value,
                        attempt=attempt,
                    )
            except Exception as e:
                logger.error(
                    "Emergency exit exception",
                    error=repr(e),
                    attempt=attempt,
                )

            if attempt < max_retries:
                await asyncio.sleep(1.0 * attempt)

        return False

    def _update_position(
        self,
        opportunity: ArbitrageOpportunity,
        order_yes: Order,
        order_no: Order,
    ) -> None:
        """Update account state after successful arbitrage."""
        self._account_state.update_position(
            condition_id=opportunity.market.condition_id,
            yes_token_id=opportunity.market.token_id_yes,
            no_token_id=opportunity.market.token_id_no,
            yes_delta=order_yes.filled_size,
            no_delta=order_no.filled_size,
        )

    def _update_short_inventory(
        self,
        opportunity: ShortArbitrageOpportunity,
        yes_delta: float = 0.0,
        no_delta: float = 0.0,
    ) -> None:
        """Track minted short-arb inventory so the settler can merge leftovers."""
        self._account_state.update_position(
            condition_id=opportunity.market.condition_id,
            yes_token_id=opportunity.market.token_id_yes,
            no_token_id=opportunity.market.token_id_no,
            yes_delta=yes_delta,
            no_delta=no_delta,
        )

    @property
    def account_state(self) -> AccountState:
        """Get current account state."""
        return self._account_state

    # ------------------------------------------------------------------
    # Short Arbitrage: Mint → Sell-Yes + Sell-No
    # ------------------------------------------------------------------

    async def execute_short_arbitrage(
        self,
        opportunity: ShortArbitrageOpportunity,
        ctf_contract,
    ) -> ExecutionReport:
        """
        Execute a short arbitrage: Mint Yes+No tokens, then sell both.

        Flow:
          1. Mint ``trade_size_usdc`` worth of Yes+No via CTF splitPosition.
          2. Sell Yes tokens at ``bid_price_yes``.
          3. Sell No tokens at ``bid_price_no``.

        Both SELL orders use FOK so they either fill completely or not at all.
        If only one sell fills, trigger emergency exit for the other side.

        Args:
            opportunity: Short arbitrage opportunity from the detector.
            ctf_contract: ``CTFContract`` (or compatible) with an async
                ``mint(condition_id, amount_usdc)`` method.

        Returns:
            ExecutionReport summarising the result.
        """
        start_time = time.time()
        market = opportunity.market
        trade_size = opportunity.trade_size_usdc

        # How many tokens we get per $1 mint → 1 Yes + 1 No per $1
        # Token quantity = trade_size (USDC) → we get ``trade_size`` Yes + ``trade_size`` No
        num_tokens = trade_size  # 1:1 mint ratio

        if self.dry_run:
            logger.info(
                "DRY RUN: Would execute short arbitrage (Mint+Sell)",
                market=market.slug,
                trade_size=f"${trade_size:.2f}",
                profit=f"{opportunity.net_profit_pct:.4f}",
            )
            # Build placeholder orders for the report
            order_yes = Order(
                token_id=market.token_id_yes,
                side=OrderSide.SELL,
                price=opportunity.bid_price_yes,
                size=num_tokens,
                order_type=OrderType.FOK,
            )
            order_no = Order(
                token_id=market.token_id_no,
                side=OrderSide.SELL,
                price=opportunity.bid_price_no,
                size=num_tokens,
                order_type=OrderType.FOK,
            )
            return ExecutionReport(
                result=ExecutionResult.SKIPPED,
                opportunity=opportunity,
                order_yes=order_yes,
                order_no=order_no,
                execution_time_ms=0,
            )

        # Do not mint if either sell leg cannot satisfy CLOB marketable-order
        # requirements. Once minted, a rejected sell leaves us holding tokens.
        sell_checks = [
            ("YES", market.token_id_yes, opportunity.bid_price_yes),
            ("NO", market.token_id_no, opportunity.bid_price_no),
        ]
        invalid_sells = []
        for outcome, token_id, bid_price in sell_checks:
            price, size, order_usdc = self._prepare_clob_order_values(
                OrderSide.SELL,
                bid_price,
                num_tokens,
            )
            if size <= 0 or order_usdc < MIN_MARKETABLE_ORDER_USDC:
                invalid_sells.append(
                    f"{outcome} token {token_id[:8]} notional=${order_usdc:.2f} "
                    f"(price={price}, size={size})"
                )

        if invalid_sells:
            reason = "; ".join(invalid_sells)
            logger.warning(
                "Short arb skipped before mint: sell order below CLOB minimum",
                market=market.slug[:50],
                reason=reason,
            )
            return ExecutionReport(
                result=ExecutionResult.SKIPPED,
                opportunity=opportunity,
                error_message=reason,
                execution_time_ms=(time.time() - start_time) * 1000,
            )

        ctf_address = getattr(ctf_contract, "address", None)
        if self.proxy_wallet and ctf_address:
            if ctf_address.lower() != self.proxy_wallet.lower():
                reason = (
                    "CTF mint wallet does not match CLOB trading wallet "
                    f"(mint_wallet={ctf_address}, trading_wallet={self.proxy_wallet})"
                )
                logger.warning(
                    "Short arb skipped before mint: wallet mismatch",
                    market=market.slug[:50],
                    reason=reason,
                )
                return ExecutionReport(
                    result=ExecutionResult.SKIPPED,
                    opportunity=opportunity,
                    error_message=reason,
                    execution_time_ms=(time.time() - start_time) * 1000,
                )

        try:
            # ── Step 1: Mint tokens ──────────────────────────────────
            condition_id = market.condition_id
            logger.info(
                "Short arb – minting tokens",
                market=market.slug[:50],
                amount=f"${trade_size:.2f}",
            )
            mint_report = await ctf_contract.mint(condition_id, trade_size)

            from core.ctf import MintResult  # local import to avoid circular

            if mint_report.result != MintResult.SUCCESS:
                logger.error(
                    "Short arb – mint failed",
                    error=mint_report.error_message,
                    market=market.slug[:50],
                    condition_id=condition_id,
                    amount_usdc=trade_size,
                    proxy_wallet=getattr(mint_report, "proxy_wallet", None),
                    signer_address=getattr(mint_report, "signer_address", None),
                    relayer_tx_type=getattr(mint_report, "relayer_tx_type", None),
                    relayer_transaction_id=getattr(mint_report, "relayer_transaction_id", None),
                    relayer_state=getattr(mint_report, "relayer_state", None),
                    collateral_token=getattr(mint_report, "collateral_token", None),
                    collateral_balance_wei=getattr(
                        mint_report,
                        "collateral_balance_wei",
                        None,
                    ),
                    collateral_allowance_wei=getattr(
                        mint_report,
                        "collateral_allowance_wei",
                        None,
                    ),
                    tx_hash=mint_report.tx_hash,
                )
                return ExecutionReport(
                    result=ExecutionResult.FAILED,
                    opportunity=opportunity,
                    error_message=f"Mint failed: {mint_report.error_message}",
                    execution_time_ms=(time.time() - start_time) * 1000,
                    fatal_error=True,
                )

            logger.info(
                "Short arb – mint succeeded, selling tokens",
                gas_cost=f"${mint_report.gas_cost_usd:.4f}",
                tx_hash=mint_report.tx_hash or "N/A",
            )
            self._update_short_inventory(
                opportunity,
                yes_delta=num_tokens,
                no_delta=num_tokens,
            )

            # ── Step 2: Sell Yes + No concurrently ───────────────────
            order_yes = Order(
                token_id=market.token_id_yes,
                side=OrderSide.SELL,
                price=opportunity.bid_price_yes,
                size=num_tokens,
                order_type=OrderType.FOK,
            )
            order_no = Order(
                token_id=market.token_id_no,
                side=OrderSide.SELL,
                price=opportunity.bid_price_no,
                size=num_tokens,
                order_type=OrderType.FOK,
            )

            results = await asyncio.gather(
                self._submit_order(order_yes),
                self._submit_order(order_no),
                return_exceptions=True,
            )

            order_yes_res, order_no_res = results[0], results[1]
            execution_time = (time.time() - start_time) * 1000

            if isinstance(order_yes_res, Exception) or isinstance(order_no_res, Exception):
                error_msg = str(
                    order_yes_res if isinstance(order_yes_res, Exception) else order_no_res
                )
                return ExecutionReport(
                    result=ExecutionResult.FAILED,
                    opportunity=opportunity,
                    error_message=error_msg,
                    execution_time_ms=execution_time,
                )

            yes_ok = order_yes_res.is_filled
            no_ok = order_no_res.is_filled

            if yes_ok and no_ok:
                self._update_short_inventory(
                    opportunity,
                    yes_delta=-order_yes_res.filled_size,
                    no_delta=-order_no_res.filled_size,
                )
                final_balance = await self.get_account_balance()
                logger.info(
                    "Short arb – both sells filled!",
                    profit_est=f"${opportunity.net_profit_usdc:.2f}",
                    exec_ms=f"{execution_time:.0f}",
                )
                return ExecutionReport(
                    result=ExecutionResult.SUCCESS,
                    opportunity=opportunity,
                    order_yes=order_yes_res,
                    order_no=order_no_res,
                    execution_time_ms=execution_time,
                    final_balance=final_balance,
                )

            if yes_ok or no_ok:
                if yes_ok:
                    self._update_short_inventory(
                        opportunity,
                        yes_delta=-order_yes_res.filled_size,
                    )
                if no_ok:
                    self._update_short_inventory(
                        opportunity,
                        no_delta=-order_no_res.filled_size,
                    )

                logger.warning(
                    "Short arb – partial sell, emergency exit needed",
                    yes_filled=yes_ok,
                    no_filled=no_ok,
                )
                residual_order = order_no_res if yes_ok else order_yes_res
                residual_size = math.floor(residual_order.size * 100) / 100
                emergency_order = Order(
                    token_id=residual_order.token_id,
                    side=OrderSide.SELL,
                    price=residual_order.price,
                    size=residual_order.size,
                    order_type=OrderType.FOK,
                    status=OrderStatus.FILLED,
                    filled_size=residual_size,
                    filled_avg_price=residual_order.price,
                )
                exited = await self._emergency_exit(
                    opportunity,
                    emergency_order,
                    None,
                )
                if exited:
                    if residual_order.token_id == market.token_id_yes:
                        self._update_short_inventory(
                            opportunity,
                            yes_delta=-residual_size,
                        )
                    else:
                        self._update_short_inventory(
                            opportunity,
                            no_delta=-residual_size,
                        )
                return ExecutionReport(
                    result=ExecutionResult.PARTIAL,
                    opportunity=opportunity,
                    order_yes=order_yes_res,
                    order_no=order_no_res,
                    execution_time_ms=execution_time,
                )

            # Both sells rejected – we're stuck holding minted tokens
            logger.error(
                "Short arb – both sells failed (holding tokens)",
                market=market.slug[:50],
            )
            return ExecutionReport(
                result=ExecutionResult.FAILED,
                opportunity=opportunity,
                order_yes=order_yes_res,
                order_no=order_no_res,
                execution_time_ms=execution_time,
            )

        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error("Short arb execution failed", error=repr(e))
            return ExecutionReport(
                result=ExecutionResult.FAILED,
                opportunity=opportunity,
                error_message=repr(e),
                execution_time_ms=execution_time,
            )


def create_executor(dry_run: bool = False) -> OrderExecutor:
    """
    Create an executor from settings.

    Args:
        dry_run: If True, don't actually submit orders

    Returns:
        Configured OrderExecutor
    """
    settings = get_settings()
    return OrderExecutor(
        api_key=settings.polymarket_api_key,
        api_secret=settings.polymarket_api_secret,
        passphrase=settings.polymarket_passphrase,
        private_key=settings.private_key,
        proxy_wallet=settings.proxy_wallet_address,
        signature_type=settings.signature_type,
        dry_run=dry_run or settings.dry_run,
    )
