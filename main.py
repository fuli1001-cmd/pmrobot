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
from core.scanner import MarketScanner
from core.monitor import MarketMonitor, NegativeRiskArbitrageDetector, OrderBookManager, NegativeRiskMarketMonitor
from core.executor import OrderExecutor, create_executor, ExecutionResult
from core.settler import PositionSettler, create_settler
from core.risk import RiskManager, RiskConfig
from models.market import NegativeRiskEvent, NegativeRiskArbitrageOpportunity
from models.order import ArbitrageOpportunity
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
        self._opportunity_queue: asyncio.Queue[ArbitrageOpportunity] = asyncio.Queue()
        self._neg_risk_opportunity_queue: asyncio.Queue[NegativeRiskArbitrageOpportunity] = asyncio.Queue()
        self._neg_risk_events: list = []

        # Initialize components
        self.risk_manager = RiskManager(
            RiskConfig(
                max_slippage=settings.max_slippage,
            )
        )
        self.executor: Optional[OrderExecutor] = None
        self.settler: Optional[PositionSettler] = None
        self.monitor: Optional[MarketMonitor] = None
        self.neg_risk_monitor: Optional[NegativeRiskMarketMonitor] = None
        self.notifier = create_notifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
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

        # Send startup notification
        await self.notifier.send(
            f"🚀 <b>Arbitrage Bot Started</b>\n\n"
            f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}\n"
            f"Threshold: {self.settings.profit_threshold:.2%}\n"
            f"Trade Size: ${self.settings.single_trade_size:.2f}"
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
                self._run_settler(),
                self._run_stats_reporter(),
            )

        except asyncio.CancelledError:
            logger.info("Bot cancelled")
        except Exception as e:
            logger.error("Bot error", error=str(e))
            await self.notifier.send_alert("Bot Error", str(e))
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the arbitrage bot."""
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
            f"🛑 <b>Arbitrage Bot Stopped</b>\n\n"
            f"Total Trades: {stats['total_trades']}\n"
            f"Success Rate: {stats['success_rate']}\n"
            f"Net Profit: {stats['net_profit']}"
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
                asyncio.create_task(self._opportunity_queue.put(opp))

        self.monitor = MarketMonitor(
            markets=markets,
            profit_threshold=self.settings.profit_threshold,
            trade_size=self.settings.single_trade_size,
            max_slippage=self.settings.max_slippage,
            on_opportunity=on_opportunity,
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
            asyncio.create_task(self._neg_risk_opportunity_queue.put(opp))

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
                    continue

                if self.dry_run:
                    # Record as simulated success (direct stats update for NegRisk)
                    self.risk_manager.stats.total_trades += 1
                    self.risk_manager.stats.simulated_trades += 1
                    self.risk_manager.stats.simulated_profit_usdc += opportunity.net_profit_usdc
                    logger.info(
                        "DRY RUN: Simulated Neg Risk arbitrage",
                        event=opportunity.event.title[:40],
                        strategy=opportunity.strategy.value,
                        net_profit=f"{opportunity.net_profit_pct:.2%}",
                        profit_usdc=f"${opportunity.net_profit_usdc:.2f}",
                    )
                else:
                    # TODO: Implement actual Negative Risk execution
                    # This would require placing multiple orders for all outcomes
                    logger.warning("Live Negative Risk execution not yet implemented")

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
                    continue

                # Execute arbitrage
                result = await self.executor.execute_arbitrage(opportunity)

                # Record results
                if result.is_success:
                    # Real success
                    await self.risk_manager.record_success(
                        opportunity,
                        opportunity.net_profit_usdc,
                    )
                    await self.notifier.send_trade_notification(
                        market=opportunity.market.question,
                        profit_pct=opportunity.net_profit_pct,
                        trade_size=opportunity.trade_size_usdc,
                        success=True,
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

            except Exception as e:
                logger.error("Executor loop error", error=str(e))

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
    setup_logging(
        level=args.log_level,
        json_format=args.log_json,
        log_file="log.txt",  # Auto-generate log file
    )

    # Load settings
    settings = get_settings()

    # Override settings from args
    if args.env:
        settings.env = args.env

    # Create and run bot
    bot = ArbitrageBot(settings, dry_run=args.dry_run)

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
        logger.info("Keyboard interrupt received, shutting down...")
    finally:
        await bot.stop()
        logger.info("Bot exited gracefully")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
