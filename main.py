"""
Prediction Market Arbitrage Bot - Main Entry Point

A fully automated arbitrage bot supporting Polymarket internal
strategies and Polymarket/SX Bet cross-platform strategies.
"""

import argparse
import asyncio
import signal
import sys
import traceback
from typing import Optional

from config.settings import get_settings, Settings
from config.constants import POLYGON_CHAIN_ID
from core.scanner import MarketScanner
from core.monitor import MarketMonitor, NegativeRiskArbitrageDetector, OrderBookManager, NegativeRiskMarketMonitor
from core.executor import (
    DRY_RUN_PREFLIGHT_PASSED,
    OrderExecutor,
    create_executor,
    ExecutionResult,
)
from core.settler import PositionSettler, create_settler
from core.ctf import CTFContract
from core.risk import RiskManager, RiskConfig
from models.market import NegativeRiskEvent, NegativeRiskArbitrageOpportunity
from models.order import ArbitrageOpportunity, ShortArbitrageOpportunity
from utils.logger import setup_logging, get_logger
from utils.notifier import create_notifier
from utils.geoblock import GeoblockCheckError, check_polymarket_geoblock

# Cross-platform imports (lazy-loaded when enabled)
from exchanges.base import BaseExchange
from exchanges.polymarket import PolymarketExchange
from exchanges.sxbet import SxBetExchange
from core.alignment import MarketAligner
from core.cross_platform import CrossPlatformDetector, CrossPlatformExecutor
from core.cross_monitor import CrossPlatformMonitor
from exchanges.sxbet_ws import SxBetWebSocket
from models.cross_models import CrossPlatformOpportunity, CrossExecutionReport

logger = get_logger(__name__)


class ArbitrageBot:
    """
    Main arbitrage bot orchestrator.
    
    Coordinates all modules: Scanner, Monitor, Executor, Settler, Risk.
    """

    def __init__(
        self,
        settings: Settings,
        dry_run: bool = False,
    ):
        """
        Initialize the arbitrage bot.

        Args:
            settings: Application settings
            dry_run: If True, don't execute real trades
        """
        self.settings = settings
        self.dry_run = dry_run
        self._running = False
        self._opportunity_queue: asyncio.Queue[ArbitrageOpportunity] = asyncio.Queue(maxsize=100)
        self._neg_risk_opportunity_queue: asyncio.Queue[NegativeRiskArbitrageOpportunity] = asyncio.Queue(maxsize=100)
        self._short_opportunity_queue: asyncio.Queue[ShortArbitrageOpportunity] = asyncio.Queue(maxsize=100)
        self._cross_opportunity_queue: asyncio.Queue[CrossPlatformOpportunity] = asyncio.Queue(maxsize=100)
        self._neg_risk_events: list = []
        self._tasks: list[asyncio.Task] = []
        self._stop_lock = asyncio.Lock()

        # Initialize components
        self.risk_manager = RiskManager(
            RiskConfig(
                max_slippage=settings.max_slippage,
                stop_on_loss=settings.stop_on_loss,
            )
        )
        self.executor: Optional[OrderExecutor] = None
        self.settler: Optional[PositionSettler] = None
        self.ctf_contract: Optional[CTFContract] = None
        self.monitor: Optional[MarketMonitor] = None
        self.neg_risk_monitor: Optional[NegativeRiskMarketMonitor] = None
        self.notifier = create_notifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            settings.wechat_webhook_url,
        )

        # Cross-platform components (initialised in start() if enabled)
        self._pm_exchange: Optional[PolymarketExchange] = None
        self._sx_exchange: Optional[SxBetExchange] = None
        self._alt_exchange: Optional[BaseExchange] = None  # Active alt platform
        self._market_aligner: Optional[MarketAligner] = None
        self._cross_detector: Optional[CrossPlatformDetector] = None
        self._cross_executor: Optional[CrossPlatformExecutor] = None
        self._cross_monitor: Optional[CrossPlatformMonitor] = None
        self._sx_ws: Optional[SxBetWebSocket] = None

    @staticmethod
    def _execution_report_has_fill(result) -> bool:
        """Return True if an execution report shows any filled leg."""
        for order in (getattr(result, "order_yes", None), getattr(result, "order_no", None)):
            if order and (order.is_filled or order.filled_size > 0):
                return True
        return False

    async def _verify_geoblock(self) -> None:
        """Check whether this egress IP is eligible for live Polymarket trading."""
        try:
            geo = await check_polymarket_geoblock()
        except GeoblockCheckError as exc:
            logger.error("Polymarket geoblock precheck failed", error=repr(exc))
            if self.dry_run:
                logger.warning(
                    "Continuing despite geoblock precheck failure because dry run is enabled"
                )
                return

            await self.notifier.send_alert(
                "Polymarket geoblock precheck failed",
                f"Live trading aborted before startup.\nReason: {exc}",
            )
            raise RuntimeError(
                f"live trading blocked: could not verify geoblock status: {exc}"
            ) from exc

        logger.info(
            "Polymarket geoblock status",
            blocked=geo.blocked,
            country=geo.country,
            region=geo.region,
            ip=geo.ip,
        )

        if geo.blocked and not self.dry_run:
            details = (
                f"Detected egress IP {geo.ip} in {geo.location}. "
                "Live trading aborted before startup."
            )
            logger.error(
                "Polymarket trading blocked by geoblock precheck",
                ip=geo.ip,
                country=geo.country,
                region=geo.region,
            )
            await self.notifier.send_alert(
                "Polymarket trading blocked",
                details,
            )
            raise RuntimeError(details)

    async def start(self) -> None:
        """Start the arbitrage bot."""
        logger.info(
            "Starting Polymarket Arbitrage Bot",
            env=self.settings.env,
            dry_run=self.dry_run,
            profit_threshold=f"{self.settings.profit_threshold:.2%}",
            trade_size=f"${self.settings.max_trade_size:.2f}",
        )

        await self._verify_geoblock()

        self._running = True

        # Initialize executor
        self.executor = create_executor(dry_run=self.dry_run)
        self.executor._concurrent = self.settings.pm_arb_concurrent

        # Initialize CTF contract for short arbitrage (Mint)
        self.ctf_contract = CTFContract(
            private_key=self.settings.private_key or "",
            rpc_url=self.settings.rpc_url,
            chain_id=POLYGON_CHAIN_ID if not self.settings.is_testnet else 80002,
            proxy_wallet=self.settings.proxy_wallet_address,
            relayer_api_key=self.settings.relayer_api_key,
            relayer_api_key_address=self.settings.relayer_api_key_address,
            relayer_tx_type=self.settings.relayer_tx_type,
            collateral_token_address=self.settings.ctf_collateral_address,
            is_testnet=self.settings.is_testnet,
            dry_run=self.dry_run,
        )
        
        # Fetch initial balance for logging
        initial_balance = await self.executor.get_account_balance()
        logger.info(f"Initial Account Balance: ${initial_balance:.2f}")

        # Send startup notification
        await self.notifier.send(
            f"🚀 <font color=\"info\"><b>Arbitrage Bot Started</b></font>\n\n"
            f"> Mode: {'DRY RUN' if self.dry_run else 'LIVE'}\n"
            f"> Balance: ${initial_balance:.2f}\n"
            f"> Threshold: {self.settings.profit_threshold:.2%}\n"
            f"> Max Trade Size: ${self.settings.max_trade_size:.2f}"
        )

        try:
            # Phase 1: Scan for markets
            markets = await self._scan_markets()
            if not markets:
                logger.error("No tradeable markets found")
                return

            logger.info(f"Found {len(markets)} tradeable Binary markets")

            # Phase 1b: Scan for Negative Risk events
            neg_risk_events = await self._scan_negative_risk_events()
            logger.info(f"Found {len(neg_risk_events)} Negative Risk events")

            # Phase 1c: Initialise cross-platform (if enabled)
            cross_alt_enabled = (
                self.settings.cross_platform_enabled
                and self.settings.sxbet_enabled
            )
            if cross_alt_enabled:
                await self._init_cross_platform()

            # Phase 2: Start monitoring and execution loops
            task_specs = [
                ("settler", self._run_settler()),
                ("stats_reporter", self._run_stats_reporter()),
                ("market_refresher", self._run_market_refresher()),
            ]

            # Binary / NegRisk internal arbitrage (can be disabled)
            if self.settings.pm_internal_arb_enabled:
                internal_tasks = [
                    ("market_monitor", self._run_monitor(markets)),
                    ("neg_risk_monitor", self._run_neg_risk_monitor(neg_risk_events)),
                    ("executor", self._run_executor()),
                    ("neg_risk_executor", self._run_neg_risk_executor()),
                ]
                if self.settings.pm_short_arb_enabled:
                    internal_tasks.append(("short_executor", self._run_short_executor()))
                else:
                    logger.info("PM short mint+sell arbitrage DISABLED (PM_SHORT_ARB_ENABLED=false)")
                task_specs.extend(internal_tasks)
            else:
                logger.info("PM internal arbitrage DISABLED (PM_INTERNAL_ARB_ENABLED=false)")

            # Add cross-platform executor + WebSocket monitors
            if self._cross_executor:
                task_specs.append(("cross_platform_executor", self._run_cross_platform_executor()))
            if self._cross_monitor:
                task_specs.append(("cross_platform_pm_ws", self._cross_monitor.run_pm_ws()))
                logger.info("Cross-platform WS tasks added (PM WS + SX Ably WS)")

            self._tasks = [
                asyncio.create_task(coro, name=f"pmrobot:{name}")
                for name, coro in task_specs
            ]
            await asyncio.gather(*self._tasks)

        except asyncio.CancelledError:
            logger.info("Bot cancelled")
        except Exception as e:
            logger.error("Bot error", error=repr(e))
            # Handled by main loop too, but good to have here
            await self.notifier.send_alert("Bot Error", repr(e))
            raise # Re-raise to let main() handle the crash notification
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the arbitrage bot."""
        async with self._stop_lock:
            active_tasks = [task for task in self._tasks if not task.done()]
            if not self._running and not active_tasks:
                return

            was_running = self._running
            if was_running:
                logger.info("Stopping arbitrage bot...")
            self._running = False

            async def cleanup(label: str, awaitable) -> None:
                try:
                    await awaitable
                except Exception as exc:
                    logger.warning("Cleanup step failed", step=label, error=repr(exc))

            if self.monitor:
                await cleanup("market_monitor", self.monitor.stop())

            if self.neg_risk_monitor:
                await cleanup("neg_risk_monitor", self.neg_risk_monitor.stop())

            if self.settler:
                await cleanup("settler", self.settler.stop())

            # Cross-platform WebSocket cleanup
            if self._cross_monitor:
                await cleanup("cross_monitor", self._cross_monitor.stop())
            if self._sx_ws:
                await cleanup("sx_ws", self._sx_ws.close())
            if self._sx_exchange:
                await cleanup("sx_exchange", self._sx_exchange.disconnect())
            if self._pm_exchange:
                await cleanup("pm_exchange", self._pm_exchange.disconnect())

            current_task = asyncio.current_task()
            tasks_to_cancel = [
                task for task in self._tasks
                if task is not current_task and not task.done()
            ]
            for task in tasks_to_cancel:
                task.cancel()

            if tasks_to_cancel:
                await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

            self._tasks = [
                task for task in self._tasks
                if task is current_task and not task.done()
            ]

            if was_running:
                # Final stats
                stats = self.risk_manager.get_stats_summary()
                await self.notifier.send(
                    f"🛑 <font color=\"warning\"><b>Arbitrage Bot Stopped</b></font>\n\n"
                    f"> Total Trades: {stats['total_trades']}\n"
                    f"> Success Rate: {stats['success_rate']}\n"
                    f"> Net Profit: {stats['net_profit']}"
                )

    async def _scan_markets(self) -> list:
        """Scan for tradeable markets."""
        logger.info("Scanning for markets...")

        async with MarketScanner(rate_limit=self.settings.api_rate_limit) as scanner:
            markets = await scanner.fetch_all_markets(
                fee_free_only=True,
                max_markets=1000,  # Scan more markets
            )

        # Filter for minimum liquidity
        min_liquidity = self.settings.max_trade_size * 2
        tradeable = [m for m in markets if m.liquidity >= min_liquidity]

        logger.info(
            "Market scan complete",
            total_found=len(markets),
            tradeable=len(tradeable),
            min_liquidity=min_liquidity,
        )

        return tradeable

    async def _scan_negative_risk_events(self) -> list:
        """Scan for Negative Risk events."""
        logger.info("Scanning for Negative Risk events...")

        async with MarketScanner(rate_limit=self.settings.api_rate_limit) as scanner:
            events = await scanner.fetch_negative_risk_events(
                min_outcomes=3,  # At least 3 outcomes for meaningful arbitrage
                max_events=100,
            )

        # Filter for minimum liquidity
        min_liquidity = self.settings.max_trade_size * 2
        tradeable = [e for e in events if e.liquidity >= min_liquidity]

        logger.info(
            "Negative Risk scan complete",
            total_found=len(events),
            tradeable=len(tradeable),
            total_outcomes=sum(e.outcome_count for e in tradeable),
        )

        self._neg_risk_events = tradeable
        return tradeable

    async def _run_monitor(self, markets: list) -> None:
        """Run the market monitor."""
        def on_opportunity(opp: ArbitrageOpportunity):
            """Callback when opportunity is detected."""
            if self.risk_manager.can_trade_market(opp.market.condition_id):
                logger.info(
                    "Binary opportunity detected - queuing for execution",
                    market=opp.market.slug[:50],
                    net_profit_pct=f"{opp.net_profit_pct:.2%}",
                    profit_usdc=f"${opp.net_profit_usdc:.2f}",
                    trade_size=f"${opp.trade_size_usdc:.2f}",
                    safe_max_trade_size=f"${opp.safe_max_trade_size_usdc:.2f}",
                    configured_max_trade_size=f"${opp.configured_max_trade_size_usdc:.2f}",
                    depth_safety_multiplier=opp.depth_safety_multiplier,
                )
                try:
                    self._opportunity_queue.put_nowait(opp)
                except asyncio.QueueFull:
                    logger.warning("Opportunity queue full, dropping oldest")
            else:
                logger.warning(
                    "Binary opportunity detected but rejected by risk manager",
                    market=opp.market.slug[:50],
                    net_profit_pct=f"{opp.net_profit_pct:.2%}",
                    reason="cooldown or circuit breaker",
                )

        def on_short_opportunity(opp: ShortArbitrageOpportunity):
            """Callback when short (Mint+Sell) opportunity is detected."""
            if not self.settings.pm_short_arb_enabled:
                logger.debug(
                    "Short opportunity ignored because short arbitrage is disabled",
                    market=opp.market.slug[:50],
                )
                return

            if self.risk_manager.can_trade_market(opp.market.condition_id):
                logger.info(
                    "Short opportunity detected - queuing for execution",
                    market=opp.market.slug[:50],
                    net_profit_pct=f"{opp.net_profit_pct:.2%}",
                    profit_usdc=f"${opp.net_profit_usdc:.2f}",
                    trade_size=f"${opp.trade_size_usdc:.2f}",
                )
                try:
                    self._short_opportunity_queue.put_nowait(opp)
                except asyncio.QueueFull:
                    logger.warning("Short opportunity queue full, dropping")
            else:
                logger.warning(
                    "Short opportunity rejected by risk manager",
                    market=opp.market.slug[:50],
                )

        self.monitor = MarketMonitor(
            markets=markets,
            profit_threshold=self.settings.profit_threshold,
            trade_size=self.settings.max_trade_size,
            max_slippage=self.settings.max_slippage,
            depth_safety_multiplier=self.settings.depth_safety_multiplier,
            book_max_age_seconds=self.settings.binary_book_max_age_seconds,
            book_max_skew_seconds=self.settings.binary_book_max_skew_seconds,
            on_opportunity=on_opportunity,
            on_short_opportunity=on_short_opportunity,
        )

        await self.monitor.start()

    async def _run_neg_risk_monitor(self, events: list) -> None:
        """Run the Negative Risk event monitor."""
        if not events:
            logger.info("No Negative Risk events to monitor")
            return

        def on_neg_risk_opportunity(opp: NegativeRiskArbitrageOpportunity):
            """Callback when Negative Risk opportunity is detected."""
            # Note: event_id is not directly usable for risk management,
            # but we can use the event's slug or check all outcomes
            if self.risk_manager.can_trade():
                logger.info(
                    "NegRisk opportunity detected - queuing for execution",
                    event=opp.event.title[:50],
                    strategy=opp.strategy.value,
                    net_profit_pct=f"{opp.net_profit_pct:.2%}",
                    profit_usdc=f"${opp.net_profit_usdc:.2f}",
                    outcomes=opp.event.outcome_count,
                )
                try:
                    self._neg_risk_opportunity_queue.put_nowait(opp)
                except asyncio.QueueFull:
                    logger.warning("NegRisk opportunity queue full, dropping")
            else:
                logger.warning(
                    "NegRisk opportunity detected but rejected by risk manager",
                    event=opp.event.title[:50],
                    net_profit_pct=f"{opp.net_profit_pct:.2%}",
                    reason="circuit breaker triggered",
                )

        self.neg_risk_monitor = NegativeRiskMarketMonitor(
            events=events,
            profit_threshold=self.settings.profit_threshold,
            trade_size=self.settings.max_trade_size,
            max_slippage=self.settings.max_slippage,
            on_opportunity=on_neg_risk_opportunity,
        )

        await self.neg_risk_monitor.start()

    async def _run_neg_risk_executor(self) -> None:
        """Run the Negative Risk execution loop."""
        while self._running:
            try:
                # Wait for opportunity (with timeout)
                try:
                    opportunity = await asyncio.wait_for(
                        self._neg_risk_opportunity_queue.get(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue

                if not self.risk_manager.can_trade():
                    logger.debug("NegRisk execution paused - risk manager check failed")
                    continue

                # Execute Negative Risk arbitrage
                logger.info(
                    "Executing NegRisk arbitrage",
                    event=opportunity.event.title[:50],
                    strategy=opportunity.strategy.value,
                    net_profit=f"{opportunity.net_profit_pct:.2%}",
                    trade_size=f"${opportunity.trade_size_usdc:.2f}",
                    outcomes=opportunity.event.outcome_count,
                )
                result = await self.executor.execute_neg_risk_arbitrage(opportunity)
                
                # Record results
                if result.is_success:
                    # Real success
                    # Note: NegRisk doesn't have a single "market" object compatible with notification 
                    # in the same way, need to adapt notification
                    await self.risk_manager.record_success(
                        opportunity, # This accepts generic opportunity
                        opportunity.net_profit_usdc,
                    )
                    
                    # Feature 1: Report Balance
                    balance_info = ""
                    if result.final_balance is not None:
                        balance_info = f"\n💰 Balance: ${result.final_balance:.2f}"

                    await self.notifier.send_trade_notification(
                        market=opportunity.event.title, # Use event title
                        profit_pct=opportunity.net_profit_pct,
                        trade_size=opportunity.trade_size_usdc,
                        success=True,
                        extra_info=balance_info
                    )
                elif result.result == ExecutionResult.PARTIAL:
                    # Partial fill handled by emergency exit in executor, recording loss
                    # Assume 10% loss on trade size (roughly dump penalty)
                    loss = opportunity.trade_size_usdc * 0.10
                    await self.risk_manager.record_failure(
                        opportunity,
                        is_partial=True,
                        loss_usdc=loss,
                    )
                    await self.notifier.send_alert(
                        "🚨 Circuit Breaker Triggered", 
                        f"Bot paused due to NegRisk partial fill.\n"
                        f"Event: {opportunity.event.title}\n"
                        f"Estimated Loss: ${loss:.2f}"
                    )
                    # Implementation of Feature 2: Auto-Pause on Loss
                    logger.error("Circuit breaker triggered: Stopping bot due to partial fill loss")
                    asyncio.create_task(self.stop())
                    return
                elif result.result == ExecutionResult.SKIPPED and self.dry_run:
                    # Dry-run mode: record as simulated success
                    await self.risk_manager.record_success(
                        opportunity,
                        opportunity.net_profit_usdc,
                        is_simulated=True,
                    )
                    logger.info(
                        "DRY RUN: Simulated NegRisk arbitrage",
                        event=opportunity.event.title[:50],
                        strategy=opportunity.strategy.value,
                        net_profit=f"{opportunity.net_profit_pct:.2%}",
                        profit_usdc=f"${opportunity.net_profit_usdc:.2f}",
                    )
                else:
                    # Failed
                    if result.fatal_error or self._execution_report_has_fill(result):
                        await self.risk_manager.record_failure(
                            opportunity,
                            is_partial=False,
                            loss_usdc=0.0,
                        )
                    else:
                        logger.info(
                            "NegRisk FOK attempt had zero fills; not counting as risk failure",
                            event=opportunity.event.title[:50],
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Neg Risk executor error", error=repr(e))

    async def _run_executor(self) -> None:
        """Run the execution loop."""
        while self._running:
            try:
                # Wait for opportunity (with timeout)
                try:
                    opportunity = await asyncio.wait_for(
                        self._opportunity_queue.get(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue

                if not self.risk_manager.can_trade():
                    logger.debug("Execution paused - risk manager check failed")
                    continue

                # Execute arbitrage
                logger.info(
                    "Executing Binary arbitrage",
                    market=opportunity.market.slug[:50],
                    net_profit=f"{opportunity.net_profit_pct:.2%}",
                    trade_size=f"${opportunity.trade_size_usdc:.2f}",
                )
                result = await self.executor.execute_arbitrage(opportunity)

                # Record results
                if result.is_success:
                    # Real success
                    await self.risk_manager.record_success(
                        opportunity,
                        opportunity.net_profit_usdc,
                    )
                    
                    # Feature 1: Report Balance
                    balance_info = ""
                    if result.final_balance is not None:
                        balance_info = f"\n💰 Balance: ${result.final_balance:.2f}"

                    await self.notifier.send_trade_notification(
                        market=opportunity.market.question,
                        profit_pct=opportunity.net_profit_pct,
                        trade_size=opportunity.trade_size_usdc,
                        success=True,
                        extra_info=balance_info
                    )
                elif (
                    result.result == ExecutionResult.SKIPPED
                    and self.dry_run
                    and result.error_message == DRY_RUN_PREFLIGHT_PASSED
                ):
                    # Dry-run mode: record as simulated success
                    await self.risk_manager.record_success(
                        opportunity,
                        opportunity.net_profit_usdc,
                        is_simulated=True,
                    )
                    logger.info(
                        "DRY RUN: Simulated arbitrage",
                        market=opportunity.market.slug,
                        net_profit=f"{opportunity.net_profit_pct:.2%}",
                        profit_usdc=f"${opportunity.net_profit_usdc:.2f}",
                    )
                elif result.result == ExecutionResult.SKIPPED and self.dry_run:
                    logger.info(
                        "DRY RUN: Binary preflight rejected; not recording simulated arbitrage",
                        market=opportunity.market.slug[:50],
                        reason=result.error_message,
                    )
                else:
                    is_partial = result.result == ExecutionResult.PARTIAL
                    loss = opportunity.trade_size_usdc * 0.05 if is_partial else 0
                    if is_partial or result.fatal_error or self._execution_report_has_fill(result):
                        await self.risk_manager.record_failure(
                            opportunity,
                            is_partial=is_partial,
                            loss_usdc=loss,
                        )
                    else:
                        logger.info(
                            "Binary FOK attempt had zero fills; not counting as risk failure",
                            market=opportunity.market.slug[:50],
                            reason=result.error_message,
                        )
                    
                    if is_partial:
                        # Feature 2: Circuit Breaker for Binary Markets too
                        await self.notifier.send_alert(
                            "🚨 Circuit Breaker Triggered", 
                            f"Bot paused due to Binary partial fill.\n"
                            f"Market: {opportunity.market.question}\n"
                            f"Estimated Loss: ${loss:.2f}"
                        )
                        logger.error("Circuit breaker triggered: Stopping bot due to partial fill loss")
                        asyncio.create_task(self.stop())
                        return

            except Exception as e:
                logger.error("Executor loop error", error=repr(e))

    async def _run_short_executor(self) -> None:
        """
        Run the Short Arbitrage (Mint+Sell) execution loop.

        Consumes ``ShortArbitrageOpportunity`` items from
        ``_short_opportunity_queue`` and executes them via the
        ``OrderExecutor.execute_short_arbitrage()`` pipeline:

            detect_short() → queue → mint() → SELL-Yes + SELL-No
        """
        while self._running:
            try:
                try:
                    opportunity = await asyncio.wait_for(
                        self._short_opportunity_queue.get(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue

                if not self.risk_manager.can_trade():
                    logger.debug("Short execution paused - risk manager check failed")
                    continue

                logger.info(
                    "Executing Short Arbitrage (Mint+Sell)",
                    market=opportunity.market.slug[:50],
                    net_profit=f"{opportunity.net_profit_pct:.2%}",
                    trade_size=f"${opportunity.trade_size_usdc:.2f}",
                )
                result = await self.executor.execute_short_arbitrage(
                    opportunity,
                    self.ctf_contract,
                )

                if result.is_success:
                    await self.risk_manager.record_success(
                        opportunity,
                        opportunity.net_profit_usdc,
                    )

                    balance_info = ""
                    if result.final_balance is not None:
                        balance_info = f"\n💰 Balance: ${result.final_balance:.2f}"

                    await self.notifier.send_trade_notification(
                        market=f"[SHORT] {opportunity.market.question}",
                        profit_pct=opportunity.net_profit_pct,
                        trade_size=opportunity.trade_size_usdc,
                        success=True,
                        extra_info=balance_info,
                    )
                elif result.result == ExecutionResult.SKIPPED:
                    if self.dry_run:
                        await self.risk_manager.record_success(
                            opportunity,
                            opportunity.net_profit_usdc,
                            is_simulated=True,
                        )
                        logger.info(
                            "DRY RUN: Simulated short arbitrage",
                            market=opportunity.market.slug[:50],
                            net_profit=f"{opportunity.net_profit_pct:.2%}",
                            profit_usdc=f"${opportunity.net_profit_usdc:.2f}",
                        )
                    else:
                        logger.info(
                            "Short arbitrage skipped",
                            market=opportunity.market.slug[:50],
                            reason=result.error_message,
                        )
                else:
                    is_partial = result.result == ExecutionResult.PARTIAL
                    loss = opportunity.trade_size_usdc * 0.10 if is_partial else 0
                    await self.risk_manager.record_failure(
                        opportunity,
                        is_partial=is_partial,
                        loss_usdc=loss,
                    )
                    if result.fatal_error:
                        await self.notifier.send_alert(
                            "Short Arb Fatal Error",
                            f"Market: {opportunity.market.question}\n"
                            f"Reason: {result.error_message}",
                        )
                        logger.critical(
                            "Stopping bot after fatal short arbitrage error",
                            market=opportunity.market.slug[:50],
                            error=result.error_message,
                        )
                        asyncio.create_task(self.stop())
                        return
                    if is_partial:
                        await self.notifier.send_alert(
                            "🚨 Short Arb Partial Fill",
                            f"Market: {opportunity.market.question}\n"
                            f"Estimated Loss: ${loss:.2f}",
                        )
                        logger.error("Circuit breaker: short arb partial fill")
                        asyncio.create_task(self.stop())
                        return

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Short executor loop error", error=repr(e))

    async def _run_settler(self) -> None:
        """Run the settlement loop."""
        self.settler = create_settler()
        await self.settler.start(self.executor.account_state)

    async def _run_stats_reporter(self) -> None:
        """Periodically report statistics."""
        while self._running:
            try:
                await asyncio.sleep(300)  # Every 5 minutes
                if not self._running:
                    break
                stats = self.risk_manager.get_stats_summary()
                logger.info("Stats report", **stats)
            except asyncio.CancelledError:
                break

    async def _run_market_refresher(self) -> None:
        """
        Periodically re-scan Polymarket markets (Binary + NegRisk) and,
        when cross-platform mode is enabled, also run the alignment /
        detection pipeline.  A single ``MARKET_REFRESH_INTERVAL`` controls
        both jobs so that the Gamma API is only called **once** per cycle.

        Because *sports* is a fee-free tag (see ``FEE_FREE_TAGS``), the
        ``fee_free_only=True`` result set already contains every sports
        market — no extra fetch is needed for the cross-platform pipeline.
        """
        interval = self.settings.market_refresh_interval
        if interval <= 0:
            logger.info("Market refresher disabled (interval=0)")
            return

        logger.info(f"Market refresher started (interval={interval}s)")

        # ── Initial cross-platform alignment (populate WS subscriptions) ──
        if self._cross_monitor:
            await self._cross_platform_alignment_cycle()

        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    break

                logger.info("Refreshing markets...")

                min_liquidity = self.settings.max_trade_size * 2

                async with MarketScanner(rate_limit=self.settings.api_rate_limit) as scanner:
                    # Fetch fee-free markets (sports ⊂ fee-free, so this
                    # single call serves both Binary monitor and cross-platform).
                    all_markets = await scanner.fetch_all_markets(
                        fee_free_only=True,
                        max_markets=2000,
                    )
                    # Negative Risk events (separate endpoint)
                    new_events = await scanner.fetch_negative_risk_events(
                        min_outcomes=3,
                        max_events=100,
                    )

                # --- Binary markets ---
                tradeable_binary = [
                    m for m in all_markets
                    if m.liquidity >= min_liquidity
                ]
                if tradeable_binary and self.monitor and hasattr(self.monitor, 'update_markets'):
                    await self.monitor.update_markets(tradeable_binary)

                # --- Negative Risk events ---
                tradeable_events = [e for e in new_events if e.liquidity >= min_liquidity]

                if tradeable_events and self.neg_risk_monitor and hasattr(self.neg_risk_monitor, 'update_events'):
                    await self.neg_risk_monitor.update_events(tradeable_events)

                # Update local cache of events
                current_ids = {e.event_id for e in self._neg_risk_events}
                new_count = 0
                for event in tradeable_events:
                    if event.event_id not in current_ids:
                        self._neg_risk_events.append(event)
                        current_ids.add(event.event_id)
                        new_count += 1

                if new_count > 0:
                    logger.info("Bot state updated with new events", count=new_count)
                else:
                    logger.debug("Refresher: No new events found")

                # --- Cross-platform alignment refresh (WS handles evaluation) ---
                if self._cross_monitor and self._pm_exchange:
                    await self._cross_platform_alignment_cycle()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Market refresher error", error=repr(e), traceback=traceback.format_exc())


    # ------------------------------------------------------------------
    # Cross-platform methods
    # ------------------------------------------------------------------

    async def _init_cross_platform(self) -> None:
        """Initialise cross-platform exchange adapters and components."""
        logger.info("Initialising cross-platform arbitrage...")

        # Polymarket exchange adapter
        self._pm_exchange = PolymarketExchange(dry_run=self.dry_run)
        await self._pm_exchange.connect()

        # Alternative platform — SX Bet
        # (Azuro has been retired; see exchanges/azuro.py Bug 14-18 notes)
        self._sx_exchange = SxBetExchange(
            api_key=self.settings.sxbet_api_key or "",
            api_url=self.settings.sxbet_api_url,
            rpc_url=self.settings.sxbet_rpc_url,
            chain_id=self.settings.sxbet_chain_id,
            usdc_address=self.settings.sxbet_usdc_address,
            private_key=self.settings.sxbet_private_key or self.settings.private_key or "",
            dry_run=self.dry_run,
        )
        await self._sx_exchange.connect()
        self._alt_exchange = self._sx_exchange
        logger.info("Cross-platform: using SX Bet as alternative platform")

        # Market aligner
        self._market_aligner = MarketAligner(
            use_llm=self.settings.alignment_use_llm,
            llm_api_key=self.settings.llm_api_key or "",
            llm_base_url=self.settings.llm_base_url,
            llm_model=self.settings.llm_model,
        )

        # Detector and executor
        self._cross_detector = CrossPlatformDetector(
            pm_exchange=self._pm_exchange,
            alt_exchange=self._alt_exchange,
            profit_threshold=self.settings.cross_profit_threshold,
            trade_size=self.settings.cross_trade_size,
        )
        self._cross_executor = CrossPlatformExecutor(
            pm_exchange=self._pm_exchange,
            alt_exchange=self._alt_exchange,
            dry_run=self.dry_run,
        )

        # ── SX Bet WebSocket (Ably) ──
        self._sx_ws = SxBetWebSocket(
            api_key=self.settings.sxbet_api_key or "",
            api_url=self.settings.sxbet_api_url,
            base_token=self.settings.sxbet_usdc_address,
        )

        # ── Cross-platform monitor (dual WS) ──
        self._cross_monitor = CrossPlatformMonitor(
            sx_ws=self._sx_ws,
            opportunity_queue=self._cross_opportunity_queue,
            profit_threshold=self.settings.cross_profit_threshold,
            trade_size=self.settings.cross_trade_size,
        )
        # Wire SX WS callbacks
        self._sx_ws._on_book_update = self._cross_monitor.on_sx_book_update

        # Connect SX Bet WebSocket
        await self._sx_ws.connect()

        logger.info(
            "Cross-platform arbitrage initialised (WS mode)",
            profit_threshold=f"{self.settings.cross_profit_threshold:.2%}",
            trade_size=f"${self.settings.cross_trade_size:.2f}",
        )

    async def _cross_platform_alignment_cycle(self) -> None:
        """Run one cross-platform market alignment cycle.

        Discovers matching market pairs across PM and SX Bet, then
        pushes them to the ``CrossPlatformMonitor`` which manages WS
        subscriptions and real-time evaluation.
        """
        from config.constants import CROSS_SPORT_MAP

        try:
            all_pairs = []
            total_pm = 0
            total_alt = 0

            for pm_tag, alt_sport in CROSS_SPORT_MAP:
                pm_markets, alt_markets = await asyncio.gather(
                    self._pm_exchange.get_markets(sport=pm_tag),
                    self._alt_exchange.get_markets(sport=alt_sport),
                )

                if not pm_markets or not alt_markets:
                    continue

                total_pm += len(pm_markets)
                total_alt += len(alt_markets)

                pairs = await self._market_aligner.align(
                    pm_markets, alt_markets,
                )
                if pairs:
                    logger.info(
                        "Cross-align sport matched",
                        sport=pm_tag,
                        pairs=len(pairs),
                        pm=len(pm_markets),
                        alt=len(alt_markets),
                    )
                    all_pairs.extend(pairs)

            logger.info(
                "Cross-platform alignment complete",
                sports=len(CROSS_SPORT_MAP),
                total_pm=total_pm,
                total_alt=total_alt,
                total_pairs=len(all_pairs),
            )

            if all_pairs:
                await self._cross_monitor.update_pairs(all_pairs)

        except Exception as e:
            logger.error(
                "Cross-platform alignment error",
                error=repr(e),
                traceback=traceback.format_exc(),
            )

    async def _run_cross_platform_executor(self) -> None:
        """Execute cross-platform arbitrage opportunities from the queue."""
        while self._running:
            try:
                try:
                    opportunity = await asyncio.wait_for(
                        self._cross_opportunity_queue.get(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue

                if not self.risk_manager.can_trade():
                    logger.debug("Cross execution paused - risk check failed")
                    continue

                logger.info(
                    "Executing cross-platform arbitrage",
                    pm_q=opportunity.pm_question[:50],
                    profit=f"{opportunity.net_profit_pct:.2%}",
                    size=f"${opportunity.trade_size_usdc:.2f}",
                    yes_on=opportunity.yes_platform.value,
                )

                report = await self._cross_executor.execute(opportunity)

                if report.is_success:
                    est_profit = opportunity.net_profit_pct * opportunity.trade_size_usdc
                    logger.info(
                        "Cross-platform arb SUCCESS",
                        profit=f"${est_profit:.2f}",
                    )
                    await self.notifier.send_trade_notification(
                        market=f"[CROSS] {opportunity.pm_question[:50]}",
                        profit_pct=opportunity.net_profit_pct,
                        trade_size=opportunity.trade_size_usdc,
                        success=True,
                    )
                elif report.result == CrossExecutionReport.Result.PARTIAL:
                    loss = opportunity.trade_size_usdc * 0.10
                    await self.notifier.send_alert(
                        "🚨 Cross-Platform Partial Fill",
                        f"PM: {opportunity.pm_question[:50]}\n"
                        f"ALT: {opportunity.alt_question[:50]}\n"
                        f"Error: {report.error_message}\n"
                        f"Estimated Loss: ${loss:.2f}",
                    )
                    logger.error("Cross-platform partial fill — pausing bot")
                    asyncio.create_task(self.stop())
                    return
                elif report.result == CrossExecutionReport.Result.SKIPPED and self.dry_run:
                    est_profit = opportunity.net_profit_pct * opportunity.trade_size_usdc
                    logger.info(
                        "DRY RUN: Simulated cross-platform arb",
                        pm_market=opportunity.pm_question[:60],
                        alt_market=opportunity.alt_question[:60],
                        strategy=opportunity.strategy.value,
                        yes_on=opportunity.yes_platform.value,
                        price_yes=f"{opportunity.price_yes:.4f}",
                        price_no=f"{opportunity.price_no:.4f}",
                        total_cost=f"{opportunity.total_cost:.4f}",
                        net_profit_pct=f"{opportunity.net_profit_pct:.2%}",
                        profit_usdc=f"${est_profit:.2f}",
                        trade_size=f"${opportunity.trade_size_usdc:.2f}",
                    )
                    # Record simulated success for stats tracking
                    await self.risk_manager.record_success(
                        opportunity,
                        est_profit,
                        is_simulated=True,
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Cross executor loop error", error=repr(e))


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Prediction Market Arbitrage Bot (Polymarket + SX Bet)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--env",
        choices=["production", "testnet"],
        default="production",
        help="Environment to run in",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry mode (no real trades)",
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Logging level (default: from .env LOG_LEVEL, else INFO)",
    )

    parser.add_argument(
        "--log-json",
        action="store_true",
        help="Output logs in JSON format",
    )

    return parser.parse_args()


async def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Setup logging
    import os
    os.makedirs("logs", exist_ok=True)

    # Load settings first so LOG_LEVEL from .env is available as a fallback
    settings = get_settings()

    log_level = args.log_level or settings.log_level or "INFO"
    setup_logging(
        level=log_level,
        json_format=args.log_json,
        log_file=os.path.join("logs", "pmrobot.log"),
    )

    # Override settings from args
    if args.env:
        settings.env = args.env

    # Create and run bot
    dry_run = args.dry_run or settings.dry_run
    bot = ArbitrageBot(settings, dry_run=dry_run)

    # Handle signals
    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("Shutdown signal received")
        asyncio.create_task(bot.stop())
        stop_event.set()

    # Generic approach that works better across platforms in asyncio
    try:
        if sys.platform != "win32":
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, signal_handler)
        
        await bot.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Keyboard interrupt received, stopping...")
        await bot.notifier.send_alert("Bot Stopped", "Keyboard Interrupt")
    except Exception as e:
        logger.critical("Bot crashed with unhandled exception", error=repr(e), exc_info=True)
        # Attempt to notify about crash
        try:
             await bot.notifier.send_alert("⚠️ Bot Crashed", f"Unhandled Exception:\n{repr(e)}")
        except Exception as notify_err:
             logger.error("Failed to send crash notification", error=repr(notify_err))
        raise
    finally:
        await bot.stop()
        logger.info("Bot exited")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
