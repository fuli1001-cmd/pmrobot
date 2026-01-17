"""Order execution engine using CLOB API."""

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType as ClobOrderType
from py_clob_client.order_builder.constants import BUY

from config.settings import get_settings
from config.constants import CLOB_API_BASE_URL, POLYGON_CHAIN_ID, SIGNATURE_TYPE_POLY_GNOSIS_SAFE
from models.order import ArbitrageOpportunity, Order, OrderSide, OrderStatus, OrderType
from models.position import AccountState
from utils.logger import get_logger
from utils.rate_limiter import RateLimiter

logger = get_logger(__name__)


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

        # Initialize CLOB client with private key for signing
        self.client = ClobClient(
            host=CLOB_API_BASE_URL,
            key=private_key,
            chain_id=chain_id,
            signature_type=SIGNATURE_TYPE_POLY_GNOSIS_SAFE if proxy_wallet else 0,
            funder=proxy_wallet,
        )
        
        # Set API credentials for authentication
        from py_clob_client.clob_types import ApiCreds
        self.client.set_api_creds(ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=passphrase,
        ))

        logger.info(
            "Order executor initialized",
            dry_run=dry_run,
            has_proxy=bool(proxy_wallet),
        )

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

        # Create FOK orders for both sides
        order_yes = Order(
            token_id=opportunity.market.token_id_yes,
            side=OrderSide.BUY,
            price=opportunity.avg_price_yes,
            size=opportunity.trade_size_usdc / 2 / opportunity.avg_price_yes,
            order_type=OrderType.FOK,
        )

        order_no = Order(
            token_id=opportunity.market.token_id_no,
            side=OrderSide.BUY,
            price=opportunity.avg_price_no,
            size=opportunity.trade_size_usdc / 2 / opportunity.avg_price_no,
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
            # Execute both orders concurrently
            results = await asyncio.gather(
                self._submit_order(order_yes),
                self._submit_order(order_no),
                return_exceptions=True,
            )

            order_yes, order_no = results[0], results[1]
            execution_time = (time.time() - start_time) * 1000

            # Analyze results
            if isinstance(order_yes, Exception) or isinstance(order_no, Exception):
                # Handle exceptions
                error_msg = str(order_yes if isinstance(order_yes, Exception) else order_no)
                return ExecutionReport(
                    result=ExecutionResult.FAILED,
                    opportunity=opportunity,
                    error_message=error_msg,
                    execution_time_ms=execution_time,
                )

            yes_success = order_yes.is_filled
            no_success = order_no.is_filled

            if yes_success and no_success:
                # Both filled - success!
                self._update_position(opportunity, order_yes, order_no)
                return ExecutionReport(
                    result=ExecutionResult.SUCCESS,
                    opportunity=opportunity,
                    order_yes=order_yes,
                    order_no=order_no,
                    execution_time_ms=execution_time,
                )

            elif yes_success or no_success:
                # Partial fill - need emergency exit
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

            else:
                # Both failed (FOK rejected)
                return ExecutionReport(
                    result=ExecutionResult.FAILED,
                    opportunity=opportunity,
                    order_yes=order_yes,
                    order_no=order_no,
                    execution_time_ms=execution_time,
                )

        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error("Execution failed", error=str(e))
            return ExecutionReport(
                result=ExecutionResult.FAILED,
                opportunity=opportunity,
                error_message=str(e),
                execution_time_ms=execution_time,
            )

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
            # Build order arguments
            order_args = OrderArgs(
                token_id=order.token_id,
                price=order.price,
                size=order.size,
                side=BUY,
            )

            # Create and sign order
            signed_order = self.client.create_order(order_args)

            # Submit with FOK time-in-force
            response = self.client.post_order(
                signed_order,
                order_type=ClobOrderType.FOK,
            )

            # Parse response
            if response.get("success"):
                order.order_id = response.get("orderID")
                order.status = OrderStatus.FILLED
                order.filled_size = order.size
                order.filled_avg_price = order.price
                logger.info(
                    "Order filled",
                    order_id=order.order_id,
                    token_id=order.token_id[:8],
                    price=order.price,
                    size=order.size,
                )
            else:
                order.status = OrderStatus.FAILED
                logger.warning(
                    "Order rejected",
                    token_id=order.token_id[:8],
                    reason=response.get("errorMsg"),
                )

        except Exception as e:
            order.status = OrderStatus.FAILED
            logger.error("Order submission failed", error=str(e))

        return order

    async def _emergency_exit(
        self,
        opportunity: ArbitrageOpportunity,
        filled_yes: Optional[Order],
        filled_no: Optional[Order],
    ) -> None:
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
            return

        logger.warning(
            "Emergency exit: selling position",
            token_id=filled_order.token_id[:8],
            size=filled_order.filled_size,
        )

        # Create sell order at market (use a lower price to ensure fill)
        sell_price = filled_order.filled_avg_price * 0.95  # 5% below buy price

        try:
            order_args = OrderArgs(
                token_id=filled_order.token_id,
                price=sell_price,
                size=filled_order.filled_size,
                side="SELL",
            )
            signed_order = self.client.create_order(order_args)
            response = self.client.post_order(signed_order)

            if response.get("success"):
                logger.info("Emergency exit successful", order_id=response.get("orderID"))
            else:
                logger.error("Emergency exit failed", reason=response.get("errorMsg"))

        except Exception as e:
            logger.error("Emergency exit exception", error=str(e))

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

    @property
    def account_state(self) -> AccountState:
        """Get current account state."""
        return self._account_state


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
        dry_run=dry_run or settings.dry_run,
    )
