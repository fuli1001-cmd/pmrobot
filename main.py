"""
Polymarket Arbitrage Bot - Main Entry Point

A fully automated arbitrage bot for Polymarket prediction markets.
"""

import argparse
import asyncio
import signal
import sys
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

            # Phase 2: Start monitoring and execution loops
            await asyncio.gather(
                self._run_monitor(markets),
                self._run_neg_risk_monitor(neg_risk_events),
                self._run_executor(),
                self._run_neg_risk_executor(),
                self._run_short_executor(),
                self._run_settler(),
                self._run_stats_reporter(),
                self._run_market_refresher(),
            )

        except asyncio.CancelledError:
            logger.info("Bot cancelled")
        except Exception as e:
            logger.error("Bot error", error=str(e))
            # Handled by main loop too, but good to have here
            await self.notifier.send_alert("Bot Error", str(e))
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
                logger.error("Neg Risk executor error", error=str(e))

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
                logger.error("Executor loop error", error=str(e))

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
                logger.error("Short executor loop error", error=str(e))

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
        Periodically re-scan markets to capture new opportunities.
        """
        interval = self.settings.market_refresh_interval
        if interval <= 0:
            logger.info("Market refresher disabled (interval=0)")
            return

        logger.info(f"Market refresher started (interval={interval}s)")

        while self._running:
            try:
                await asyncio.sleep(interval)
                
                logger.info("Refreshing markets...")

                min_liquidity = self.settings.single_trade_size * 2

                async with MarketScanner(rate_limit=self.settings.api_rate_limit) as scanner:
                    # 1. Scan for new Binary markets
                    new_binary = await scanner.fetch_all_markets(
                        fee_free_only=True,
                        max_markets=1000,
                    )
                    # 2. Scan for new Negative Risk events
                    new_events = await scanner.fetch_negative_risk_events(
                        min_outcomes=3,
                        max_events=100,
                    )

                # --- Binary markets ---
                tradeable_binary = [m for m in new_binary if m.liquidity >= min_liquidity]
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

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Market refresher error", error=str(e))


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Polymarket Arbitrage Bot",
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
        default="INFO",
        help="Logging level",
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
    
    setup_logging(
        level=args.log_level,
        json_format=args.log_json,
        log_file=os.path.join("logs", "pmrobot.log"),
    )

    # Load settings
    settings = get_settings()

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
        logger.critical("Bot crashed with unhandled exception", error=str(e), exc_info=True)
        # Attempt to notify about crash
        try:
             await bot.notifier.send_alert("⚠️ Bot Crashed", f"Unhandled Exception:\n{str(e)}")
        except Exception as notify_err:
             logger.error("Failed to send crash notification", error=str(notify_err))
        raise
    finally:
        await bot.stop()
        logger.info("Bot exited")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
