"""
Prediction Market Arbitrage Bot - Main Entry Point

A fully automated arbitrage bot supporting Polymarket and Azuro
prediction markets (single-platform and cross-platform strategies).
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
from core.executor import OrderExecutor, create_executor, ExecutionResult
from core.settler import PositionSettler, create_settler
from core.ctf import CTFContract
from core.risk import RiskManager, RiskConfig
from models.market import NegativeRiskEvent, NegativeRiskArbitrageOpportunity
from models.order import ArbitrageOpportunity, ShortArbitrageOpportunity
from utils.logger import setup_logging, get_logger
from utils.notifier import create_notifier

# Cross-platform imports (lazy-loaded when enabled)
from exchanges.base import BaseExchange
from exchanges.polymarket import PolymarketExchange
from exchanges.azuro import AzuroExchange
from core.alignment import MarketAligner
from core.cross_platform import CrossPlatformDetector, CrossPlatformExecutor
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

        # Initialize components
        self.risk_manager = RiskManager(
            RiskConfig(
                max_slippage=settings.max_slippage,
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
        self._az_exchange: Optional[AzuroExchange] = None
        self._market_aligner: Optional[MarketAligner] = None
        self._cross_detector: Optional[CrossPlatformDetector] = None
        self._cross_executor: Optional[CrossPlatformExecutor] = None

    async def start(self) -> None:
        """Start the arbitrage bot."""
        logger.info(
            "Starting Polymarket Arbitrage Bot",
            env=self.settings.env,
            dry_run=self.dry_run,
            profit_threshold=f"{self.settings.profit_threshold:.2%}",
            trade_size=f"${self.settings.single_trade_size:.2f}",
        )

        self._running = True

        # Initialize executor
        self.executor = create_executor(dry_run=self.dry_run)

        # Initialize CTF contract for short arbitrage (Mint)
        self.ctf_contract = CTFContract(
            private_key=self.settings.private_key or "",
            rpc_url=self.settings.rpc_url,
            chain_id=POLYGON_CHAIN_ID if not self.settings.is_testnet else 80002,
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
            f"> Trade Size: ${self.settings.single_trade_size:.2f}"
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
            if self.settings.cross_platform_enabled and self.settings.azuro_enabled:
                await self._init_cross_platform()

            # Phase 2: Start monitoring and execution loops
            tasks = [
                self._run_monitor(markets),
                self._run_neg_risk_monitor(neg_risk_events),
                self._run_executor(),
                self._run_neg_risk_executor(),
                self._run_short_executor(),
                self._run_settler(),
                self._run_stats_reporter(),
                self._run_market_refresher(),
            ]

            # Add cross-platform executor (scanner merged into refresher)
            if self._cross_detector and self._cross_executor:
                tasks.append(self._run_cross_platform_executor())
                logger.info("Cross-platform executor task added (scanner merged into refresher)")

            await asyncio.gather(*tasks)

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
        if not self._running:
            return
            
        logger.info("Stopping arbitrage bot...")
        self._running = False

        if self.monitor:
            await self.monitor.stop()

        if self.neg_risk_monitor:
            await self.neg_risk_monitor.stop()

        if self.settler:
            await self.settler.stop()

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
        min_liquidity = self.settings.single_trade_size * 2
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
        min_liquidity = self.settings.single_trade_size * 2
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
            trade_size=self.settings.single_trade_size,
            max_slippage=self.settings.max_slippage,
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
            trade_size=self.settings.single_trade_size,
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
                     await self.risk_manager.record_failure(
                        opportunity,
                        is_partial=False,
                        loss_usdc=0.0,
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
                elif result.result == ExecutionResult.SKIPPED and self.dry_run:
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
                else:
                    is_partial = result.result == ExecutionResult.PARTIAL
                    loss = opportunity.trade_size_usdc * 0.05 if is_partial else 0
                    await self.risk_manager.record_failure(
                        opportunity,
                        is_partial=is_partial,
                        loss_usdc=loss,
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
                elif result.result == ExecutionResult.SKIPPED and self.dry_run:
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
                    is_partial = result.result == ExecutionResult.PARTIAL
                    loss = opportunity.trade_size_usdc * 0.10 if is_partial else 0
                    await self.risk_manager.record_failure(
                        opportunity,
                        is_partial=is_partial,
                        loss_usdc=loss,
                    )
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
            await asyncio.sleep(300)  # Every 5 minutes
            stats = self.risk_manager.get_stats_summary()
            logger.info("Stats report", **stats)

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

        # ── Initial cross-platform scan (runs once before the sleep loop
        #    so that cross-platform detection starts immediately) ──
        if self._cross_detector:
            await self._cross_platform_scan_cycle()

        while self._running:
            try:
                await asyncio.sleep(interval)

                logger.info("Refreshing markets...")

                min_liquidity = self.settings.single_trade_size * 2

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

                # --- Cross-platform scan (reuse all_markets) ---
                if self._cross_detector and self._pm_exchange:
                    await self._cross_platform_scan_cycle(all_markets)

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

        # Azuro exchange adapter
        self._az_exchange = AzuroExchange(
            subgraph_url=self.settings.azuro_subgraph_url,
            lp_address=self.settings.azuro_lp_address or "",
            core_address=self.settings.azuro_core_address or "",
            rpc_url=self.settings.rpc_url,
            private_key=self.settings.private_key or "",
            dry_run=self.dry_run,
        )
        await self._az_exchange.connect()

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
            az_exchange=self._az_exchange,
            profit_threshold=self.settings.cross_profit_threshold,
            trade_size=self.settings.cross_trade_size,
        )
        self._cross_executor = CrossPlatformExecutor(
            pm_exchange=self._pm_exchange,
            az_exchange=self._az_exchange,
            dry_run=self.dry_run,
        )

        logger.info(
            "Cross-platform arbitrage initialised",
            profit_threshold=f"{self.settings.cross_profit_threshold:.2%}",
            trade_size=f"${self.settings.cross_trade_size:.2f}",
        )

    async def _cross_platform_scan_cycle(
        self, pm_raw_markets: list = None,
    ) -> None:
        """Run one cross-platform alignment + detection cycle.

        Iterates over every sport in ``CROSS_SPORT_MAP`` so each sport's
        Cartesian product (PM × Azuro) stays small.  The optional
        *pm_raw_markets* pre-fetched list is used only to warm the
        internal pricing cache—it is NOT used for alignment.
        """
        from config.constants import CROSS_SPORT_MAP

        try:
            # ── Cache warm (if the refresher already pulled all markets) ──
            if pm_raw_markets is not None:
                self._pm_exchange.update_cache(pm_raw_markets)

            all_pairs = []
            total_pm = 0
            total_az = 0

            for pm_tag, az_sport in CROSS_SPORT_MAP:
                # Fetch both sides concurrently for this sport
                pm_markets, az_markets = await asyncio.gather(
                    self._pm_exchange.get_markets(sport=pm_tag),
                    self._az_exchange.get_markets(sport=az_sport),
                )

                if not pm_markets or not az_markets:
                    logger.debug(
                        "Cross-scan: no markets for sport",
                        pm_tag=pm_tag,
                        az_sport=az_sport,
                        pm=len(pm_markets) if pm_markets else 0,
                        az=len(az_markets) if az_markets else 0,
                    )
                    continue

                total_pm += len(pm_markets)
                total_az += len(az_markets)

                # Align per sport (keeps Cartesian products small)
                pairs = await self._market_aligner.align(
                    pm_markets, az_markets,
                )
                if pairs:
                    logger.info(
                        "Cross-scan sport matched",
                        sport=pm_tag,
                        pairs=len(pairs),
                        pm=len(pm_markets),
                        az=len(az_markets),
                    )
                    all_pairs.extend(pairs)

            logger.info(
                "Cross-scan cycle complete",
                sports=len(CROSS_SPORT_MAP),
                total_pm=total_pm,
                total_az=total_az,
                total_pairs=len(all_pairs),
            )

            if all_pairs:
                opportunities = await self._cross_detector.scan(all_pairs)
                for opp in opportunities:
                    if self.risk_manager.can_trade():
                        logger.info(
                            "Cross opportunity detected - queuing for execution",
                            pm_market=opp.pm_question[:60],
                            az_market=opp.az_question[:60],
                            strategy=opp.strategy.value,
                            yes_on=opp.yes_platform.value,
                            price_yes=f"{opp.price_yes:.4f}",
                            price_no=f"{opp.price_no:.4f}",
                            total_cost=f"{opp.total_cost:.4f}",
                            net_profit_pct=f"{opp.net_profit_pct:.2%}",
                            profit_usdc=f"${opp.net_profit_pct * opp.trade_size_usdc:.2f}",
                            trade_size=f"${opp.trade_size_usdc:.2f}",
                        )
                        try:
                            self._cross_opportunity_queue.put_nowait(opp)
                        except asyncio.QueueFull:
                            logger.warning("Cross-platform queue full")
                    else:
                        logger.warning(
                            "Cross opportunity detected but rejected by risk manager",
                            pm_market=opp.pm_question[:60],
                            net_profit_pct=f"{opp.net_profit_pct:.2%}",
                            reason="cooldown or circuit breaker",
                        )

        except Exception as e:
            logger.error(
                "Cross-platform scan cycle error",
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
                        f"AZ: {opportunity.az_question[:50]}\n"
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
                        az_market=opportunity.az_question[:60],
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
        description="Prediction Market Arbitrage Bot (Polymarket + Azuro)",
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
