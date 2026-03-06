"""Cross-platform arbitrage detection and execution.

Compares aligned market pairs (Polymarket vs alternative platform) to
discover synthetic arbitrage opportunities and execute them concurrently.

Supported alternative platforms:
  - SX Bet (CLOB, 0% taker fee, 5% oracle fee on profit)
  - Azuro  (AMM, disabled — see exchanges/azuro.py for details)

Arbitrage condition (binary hedge):
    price_yes(A) + price_no(B) < 1.0 - fees - execution_cost

Both legs are fired concurrently with slippage protection:
  - Polymarket: FOK limit order
  - SX Bet: fill/v2 with desiredOdds + oddsSlippage
"""

import asyncio
import time
from typing import List, Optional

from core.alignment import AlignedMarketPair
from exchanges.base import BaseExchange, OutcomeSide, Platform, UnifiedOdds
from models.cross_models import (
    CrossExecutionReport,
    CrossPlatformOpportunity,
    CrossPlatformStrategy,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# Estimated execution cost per alternative-platform bet (in USDC terms).
# SX Bet on SX Network: very low gas (~$0.001), but we budget $0.01.
# Azuro on Polygon: ~$0.02 gas per bet.
_ALT_EXECUTION_COST_USD = 0.01

# SX Bet CLOB slippage buffer.  Unlike Azuro's AMM (which needed 3% buffer
# for price impact), SX Bet's CLOB has deterministic best-ask prices.
# We add a small buffer for order-book movement between quote and fill.
# NOTE: With VWAP pricing the fill cost is already realistic; this buffer
# covers book changes between the quote timestamp and execution.
_ALT_SLIPPAGE_BUFFER = 0.005  # 0.5% (was 3% for Azuro AMM)

# SX Bet oracle fee: 5% on winning profit only.  Since we hedge both
# sides, one side always wins.  Effective fee on trade = ~2.5% of profit.
# We conservatively model this as a flat cost percentage on the trade.
_SXBET_ORACLE_FEE_PCT = 0.025  # 2.5% effective on trade profit

# Minimum CLOB depth (USDC) required on the Polymarket side before we
# consider a cross-platform opportunity executable.  Markets with thin
# order books cannot be fully filled via FOK and would result in a
# naked Azuro position.  Should be >= trade_size.
_MIN_PM_CLOB_DEPTH_USD = 100.0


class CrossPlatformDetector:
    """Scans aligned market pairs for cross-platform arbitrage.

    For each pair, the detector:
    1. Fetches odds from both platforms.
    2. Computes the best combination (buy YES on cheaper side,
       buy NO on the other).
    3. If total cost < 1.0 - threshold, emits an opportunity.
    """

    def __init__(
        self,
        pm_exchange: BaseExchange,
        alt_exchange: BaseExchange,
        profit_threshold: float = 0.03,
        trade_size: float = 50.0,
    ):
        self.pm = pm_exchange
        self.alt = alt_exchange
        self.profit_threshold = profit_threshold
        self.trade_size = trade_size

    async def scan(
        self, pairs: List[AlignedMarketPair]
    ) -> List[CrossPlatformOpportunity]:
        """Scan aligned pairs for cross-platform arbitrage.

        Args:
            pairs: Output from MarketAligner.align().

        Returns:
            Profitable opportunities sorted by net profit descending.
        """
        opportunities: List[CrossPlatformOpportunity] = []

        # De-duplicate input pairs by (PM market_id, AZ market_id).
        # The alignment step can produce the same combination multiple
        # times when NegRisk events overlap or the same AZ condition
        # matches identical PM questions from different event groups.
        seen_pair_keys: set[tuple[str, str]] = set()
        unique_pairs: list[AlignedMarketPair] = []
        for pair in pairs:
            key = (pair.polymarket.market_id, pair.azuro.market_id)
            if key not in seen_pair_keys:
                seen_pair_keys.add(key)
                unique_pairs.append(pair)
        if len(unique_pairs) < len(pairs):
            logger.info(
                "De-duplicated input pairs",
                original=len(pairs),
                unique=len(unique_pairs),
                dropped=len(pairs) - len(unique_pairs),
            )
        pairs = unique_pairs

        for pair in pairs:
            opp = await self._evaluate_pair(pair)
            if opp and opp.is_profitable(self.profit_threshold):
                opportunities.append(opp)

        # De-duplicate: keep only the best opportunity per AZ market.
        # The same real-world event can be matched to multiple PM markets
        # (e.g. "Match Winner" AND "Set 1 Winner").  Only one can be bet.
        best_by_az: dict[str, CrossPlatformOpportunity] = {}
        for opp in opportunities:
            existing = best_by_az.get(opp.az_market_id)
            if existing is None or opp.net_profit_pct > existing.net_profit_pct:
                best_by_az[opp.az_market_id] = opp
        opportunities = list(best_by_az.values())

        # Sort by profit (best first)
        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)

        if opportunities:
            logger.info(
                "Cross-platform opportunities found",
                count=len(opportunities),
                best_profit=f"{opportunities[0].net_profit_pct:.2%}",
            )
        else:
            logger.debug(
                "No cross-platform opportunities",
                pairs_checked=len(pairs),
            )

        return opportunities

    async def _evaluate_pair(
        self, pair: AlignedMarketPair
    ) -> Optional[CrossPlatformOpportunity]:
        """Evaluate a single aligned pair for arbitrage.

        Checks both directions:
          - YES on PM + NO on Azuro
          - YES on Azuro + NO on PM
        and returns the more profitable combo (if any).
        """
        try:
            # Fetch odds concurrently.
            # PM uses live=True to re-fetch prices from the Gamma API
            # AND CLOB order-book depth.  CLOB best_ask is preferred
            # over Gamma outcomePrices when available (it is the actual
            # fill price for a FOK order).
            # Alt platform (SX Bet) uses live=True for depth data.
            pm_odds, alt_odds = await asyncio.gather(
                self.pm.get_odds(pair.polymarket.market_id, self.trade_size, live=True),
                self.alt.get_odds(pair.azuro.market_id, self.trade_size, live=True),
            )

            if not pm_odds or not alt_odds:
                logger.debug(
                    "Pair skipped: missing odds",
                    pm_q=pair.polymarket.question[:40],
                    pm_odds=pm_odds is not None,
                    alt_odds=alt_odds is not None,
                )
                return None

            # ── CLOB depth gate ──
            # If BOTH YES and NO sides on PM have no CLOB depth, the
            # market cannot be traded via FOK.  Skip to avoid a naked
            # alt-platform position from partial fills.
            if pm_odds.max_size_yes < _MIN_PM_CLOB_DEPTH_USD and pm_odds.max_size_no < _MIN_PM_CLOB_DEPTH_USD:
                logger.debug(
                    "Pair skipped: no PM CLOB depth",
                    pm_q=pair.polymarket.question[:40],
                    depth_yes=f"${pm_odds.max_size_yes:.0f}",
                    depth_no=f"${pm_odds.max_size_no:.0f}",
                    min_required=f"${_MIN_PM_CLOB_DEPTH_USD:.0f}",
                )
                return None

            # When teams are in reversed order across platforms,
            # alt YES (team_a wins) == PM NO (team_b wins) and vice versa.
            # Swap alt YES/NO prices so they align with PM's perspective.
            if pair.teams_reversed:
                alt_odds = UnifiedOdds(
                    platform=alt_odds.platform,
                    market_id=alt_odds.market_id,
                    price_yes=alt_odds.price_no,
                    price_no=alt_odds.price_yes,
                    max_size_yes=alt_odds.max_size_no,
                    max_size_no=alt_odds.max_size_yes,
                    timestamp=alt_odds.timestamp,
                )

            # Determine the alt platform enum for combo computation
            alt_platform = self.alt.platform

            # Direction 1: YES on PM, NO on alt
            combo1 = self._compute_combo(
                pm_odds, alt_odds,
                yes_platform=Platform.POLYMARKET,
                no_platform=alt_platform,
            )

            # Direction 2: YES on alt, NO on PM
            combo2 = self._compute_combo(
                pm_odds, alt_odds,
                yes_platform=alt_platform,
                no_platform=Platform.POLYMARKET,
            )

            # Pick the better one
            best = None
            if combo1 and combo2:
                best = combo1 if combo1.net_profit_pct >= combo2.net_profit_pct else combo2
            elif combo1:
                best = combo1
            elif combo2:
                best = combo2

            # Log evaluation result for diagnostics (even when rejected)
            if best:
                logger.debug(
                    "Pair evaluated",
                    pm_q=pair.polymarket.question[:40],
                    total_cost=f"{best.total_cost:.4f}",
                    net_profit=f"{best.net_profit_pct:.4f}",
                    threshold=f"{self.profit_threshold:.4f}",
                    reversed=pair.teams_reversed,
                )
            else:
                # Log raw prices for debugging total_cost >= 1.0 cases
                logger.debug(
                    "Pair no arbitrage",
                    pm_q=pair.polymarket.question[:40],
                    pm_yes=f"{pm_odds.price_yes:.4f}",
                    pm_no=f"{pm_odds.price_no:.4f}",
                    alt_yes=f"{alt_odds.price_yes:.4f}",
                    alt_no=f"{alt_odds.price_no:.4f}",
                    reversed=pair.teams_reversed,
                )

            if best:
                # Fill in market IDs and questions
                best.pm_market_id = pair.polymarket.market_id
                best.az_market_id = pair.azuro.market_id
                best.pm_question = pair.polymarket.question
                best.az_question = pair.azuro.question
                best.trade_size_usdc = self.trade_size

            return best

        except Exception as e:
            logger.error(
                "Failed to evaluate pair",
                pm=pair.polymarket.question[:40],
                error=repr(e),
            )
            return None

    def _compute_combo(
        self,
        pm_odds: UnifiedOdds,
        alt_odds: UnifiedOdds,
        yes_platform: Platform,
        no_platform: Platform,
    ) -> Optional[CrossPlatformOpportunity]:
        """Compute profit for a specific YES/NO platform assignment.

        Also validates that the Polymarket side has sufficient CLOB depth
        for the specific outcome (YES or NO) that would be traded.
        """

        if yes_platform == Platform.POLYMARKET:
            price_yes = pm_odds.price_yes
            price_no = alt_odds.price_no
            pm_depth = pm_odds.max_size_yes  # We'd buy YES on PM
        else:
            price_yes = alt_odds.price_yes
            price_no = pm_odds.price_no
            pm_depth = pm_odds.max_size_no  # We'd buy NO on PM

        if price_yes <= 0 or price_no <= 0:
            return None

        # Reject if PM side lacks CLOB depth for this direction.
        # Depth must be >= trade_size so that a FOK order can fill.
        if pm_depth < max(_MIN_PM_CLOB_DEPTH_USD, self.trade_size):
            return None

        # Reject if alt platform lacks depth for the traded direction.
        # With VWAP pricing, price will already be 0.0 when depth is
        # insufficient (leading to early return above), but an explicit
        # depth gate is safer and gives cleaner logging.
        if yes_platform != Platform.POLYMARKET:
            alt_depth = alt_odds.max_size_yes
        else:
            alt_depth = alt_odds.max_size_no
        if alt_depth < self.trade_size:
            return None

        # Apply CLOB slippage buffer to the alternative platform leg.
        # For SX Bet (CLOB) this is a small buffer for order-book
        # movement between quote and fill (~0.5%).
        # For Azuro (AMM) this was 3% — but Azuro is now disabled.
        if yes_platform != Platform.POLYMARKET:
            price_yes *= (1.0 + _ALT_SLIPPAGE_BUFFER)
        if no_platform != Platform.POLYMARKET:
            price_no *= (1.0 + _ALT_SLIPPAGE_BUFFER)

        total_cost = price_yes + price_no
        if total_cost >= 1.0:
            return None  # No arbitrage

        gross_profit_pct = (1.0 - total_cost) / total_cost

        # Fee estimation:
        # - Execution cost (gas) as fraction of trade
        # - SX Bet oracle fee (5% on profit, ~2.5% effective)
        execution_fee = _ALT_EXECUTION_COST_USD / self.trade_size
        # Oracle fee applies to the gross profit, not the full trade
        oracle_fee = gross_profit_pct * _SXBET_ORACLE_FEE_PCT
        estimated_fees = execution_fee + oracle_fee
        net_profit_pct = gross_profit_pct - estimated_fees

        if net_profit_pct <= 0:
            return None

        return CrossPlatformOpportunity(
            pm_market_id="",  # Filled later
            az_market_id="",
            pm_question="",
            az_question="",
            strategy=CrossPlatformStrategy.BINARY_HEDGE,
            yes_platform=yes_platform,
            no_platform=no_platform,
            price_yes=price_yes,
            price_no=price_no,
            total_cost=total_cost,
            gross_profit_pct=gross_profit_pct,
            estimated_fees=estimated_fees,
            net_profit_pct=net_profit_pct,
            trade_size_usdc=self.trade_size,
        )


class CrossPlatformExecutor:
    """Executes cross-platform arbitrage opportunities.

    Fires both legs concurrently with slippage protection.
    Handles partial fills (one leg succeeds, other fails).
    """

    def __init__(
        self,
        pm_exchange: BaseExchange,
        alt_exchange: BaseExchange,
        min_odds_slippage: float = 0.02,
        dry_run: bool = False,
    ):
        self.pm = pm_exchange
        self.alt = alt_exchange
        self.min_odds_slippage = min_odds_slippage
        self.dry_run = dry_run

    async def execute(
        self, opportunity: CrossPlatformOpportunity
    ) -> CrossExecutionReport:
        """Execute a cross-platform opportunity.

        Both legs are submitted concurrently.  If one fails, the other
        is flagged for emergency handling (Polymarket can be unwound
        via market sell; Azuro positions are held to settlement).

        Args:
            opportunity: Opportunity to execute.

        Returns:
            Execution report.
        """
        start = time.time()

        if self.dry_run:
            logger.info(
                "DRY RUN: Would execute cross-platform arb",
                pm_q=opportunity.pm_question[:50],
                az_q=opportunity.az_question[:50],
                yes_on=opportunity.yes_platform.value,
                profit=f"{opportunity.net_profit_pct:.2%}",
                size=f"${opportunity.trade_size_usdc:.2f}",
            )
            return CrossExecutionReport(
                result=CrossExecutionReport.Result.SKIPPED,
                opportunity=opportunity,
                execution_time_ms=0,
            )

        # Determine which exchange handles which side
        yes_exchange, yes_market_id, no_exchange, no_market_id = (
            self._resolve_sides(opportunity)
        )

        # Compute min acceptable prices (with slippage buffer)
        min_price_yes = opportunity.price_yes * (1 + self.min_odds_slippage)
        min_price_no = opportunity.price_no * (1 + self.min_odds_slippage)
        amount_per_leg = opportunity.trade_size_usdc / 2

        # Fire both legs concurrently
        try:
            yes_result, no_result = await asyncio.gather(
                yes_exchange.place_bet(
                    yes_market_id, OutcomeSide.YES, amount_per_leg, min_price_yes
                ),
                no_exchange.place_bet(
                    no_market_id, OutcomeSide.NO, amount_per_leg, min_price_no
                ),
                return_exceptions=True,
            )

            elapsed = (time.time() - start) * 1000

            # Handle exceptions from gather
            if isinstance(yes_result, Exception):
                logger.error("YES leg exception", error=str(yes_result))
                yes_result = None
            if isinstance(no_result, Exception):
                logger.error("NO leg exception", error=str(no_result))
                no_result = None

            yes_ok = yes_result and yes_result.status == yes_result.Status.SUCCESS
            no_ok = no_result and no_result.status == no_result.Status.SUCCESS

            if yes_ok and no_ok:
                logger.info(
                    "Cross-platform arb SUCCESS",
                    profit=f"{opportunity.net_profit_pct:.2%}",
                    exec_ms=f"{elapsed:.0f}",
                )
                return CrossExecutionReport(
                    result=CrossExecutionReport.Result.SUCCESS,
                    opportunity=opportunity,
                    yes_bet=yes_result,
                    no_bet=no_result,
                    execution_time_ms=elapsed,
                )

            if yes_ok or no_ok:
                # PARTIAL fill — dangerous state
                filled_side = "YES" if yes_ok else "NO"
                failed_side = "NO" if yes_ok else "YES"
                filled_platform = (
                    opportunity.yes_platform.value if yes_ok
                    else opportunity.no_platform.value
                )
                logger.warning(
                    "PARTIAL FILL — emergency handling needed",
                    filled=f"{filled_side} on {filled_platform}",
                    failed=failed_side,
                )
                # TODO: If Polymarket side filled, attempt market-sell unwind
                # SX Bet side: could attempt cancel/reverse if not yet settled
                return CrossExecutionReport(
                    result=CrossExecutionReport.Result.PARTIAL,
                    opportunity=opportunity,
                    yes_bet=yes_result,
                    no_bet=no_result,
                    execution_time_ms=elapsed,
                    error_message=f"{failed_side} leg failed, {filled_side} leg filled",
                )

            # Both failed — safe state
            logger.info("Both legs failed — no exposure")
            return CrossExecutionReport(
                result=CrossExecutionReport.Result.FAILED,
                opportunity=opportunity,
                yes_bet=yes_result,
                no_bet=no_result,
                execution_time_ms=elapsed,
            )

        except Exception as e:
            elapsed = (time.time() - start) * 1000
            logger.error("Cross execution failed", error=repr(e))
            return CrossExecutionReport(
                result=CrossExecutionReport.Result.FAILED,
                opportunity=opportunity,
                error_message=repr(e),
                execution_time_ms=elapsed,
            )

    def _resolve_sides(self, opp: CrossPlatformOpportunity):
        """Map opportunity sides to exchange instances + market IDs."""
        if opp.yes_platform == Platform.POLYMARKET:
            return (self.pm, opp.pm_market_id, self.alt, opp.az_market_id)
        else:
            return (self.alt, opp.az_market_id, self.pm, opp.pm_market_id)
