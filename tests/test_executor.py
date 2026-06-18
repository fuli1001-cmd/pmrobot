"""Tests for order execution classification."""

import sys
import types
from unittest.mock import AsyncMock

import pytest

if "py_clob_client.client" not in sys.modules:
    py_clob_client = types.ModuleType("py_clob_client")
    py_clob_client_client = types.ModuleType("py_clob_client.client")
    py_clob_client_types = types.ModuleType("py_clob_client.clob_types")
    py_clob_client_constants = types.ModuleType("py_clob_client.order_builder.constants")

    class _StubClobClient:
        pass

    class _StubOrderArgs:
        pass

    class _StubClobOrderType:
        FOK = "FOK"
        GTC = "GTC"
        GTD = "GTD"

    py_clob_client_client.ClobClient = _StubClobClient
    py_clob_client_types.OrderArgs = _StubOrderArgs
    py_clob_client_types.OrderType = _StubClobOrderType
    py_clob_client_constants.BUY = "BUY"
    py_clob_client_constants.SELL = "SELL"

    sys.modules["py_clob_client"] = py_clob_client
    sys.modules["py_clob_client.client"] = py_clob_client_client
    sys.modules["py_clob_client.clob_types"] = py_clob_client_types
    sys.modules["py_clob_client.order_builder.constants"] = py_clob_client_constants

from core.executor import ExecutionResult, OrderExecutor
from models.market import Market
from models.order import ArbitrageOpportunity, OrderStatus, ShortArbitrageOpportunity
from models.position import AccountState


@pytest.mark.asyncio
async def test_concurrent_binary_both_failed_is_not_partial():
    executor = OrderExecutor.__new__(OrderExecutor)
    executor.dry_run = False
    executor._concurrent = True
    executor._emergency_exit = AsyncMock()
    executor.proxy_wallet = None
    executor._account_state = AccountState()

    async def fail_order(order):
        order.status = OrderStatus.FAILED
        return order

    executor._submit_order = fail_order

    opportunity = ArbitrageOpportunity(
        market=Market(
            condition_id="condition",
            token_id_yes="yes_token",
            token_id_no="no_token",
            question="Test market?",
            slug="test-market",
        ),
        avg_price_yes=0.45,
        avg_price_no=0.50,
        trade_size_usdc=5.0,
        total_cost=0.95,
        estimated_fee=0.0,
    )

    report = await executor.execute_arbitrage(opportunity)

    assert report.result == ExecutionResult.FAILED
    assert report.order_yes.status == OrderStatus.FAILED
    assert report.order_no.status == OrderStatus.FAILED
    executor._emergency_exit.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_arbitrage_skips_before_mint_when_sell_order_below_minimum():
    executor = OrderExecutor.__new__(OrderExecutor)
    executor.dry_run = False
    executor.proxy_wallet = None
    executor._account_state = AccountState()

    market = Market(
        condition_id="condition",
        token_id_yes="yes_token",
        token_id_no="no_token",
        question="Test market?",
        slug="test-market",
    )
    opportunity = ShortArbitrageOpportunity(
        market=market,
        bid_price_yes=0.16,
        bid_price_no=0.87,
        trade_size_usdc=5.0,
        total_revenue=1.03,
        mint_cost=1.0,
        estimated_gas_cost=0.0,
        estimated_fee=0.0,
    )
    ctf_contract = types.SimpleNamespace(mint=AsyncMock())

    report = await executor.execute_short_arbitrage(opportunity, ctf_contract)

    assert report.result == ExecutionResult.SKIPPED
    assert "notional=$0.80" in report.error_message
    ctf_contract.mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_arbitrage_failed_sells_leave_mergeable_inventory():
    executor = OrderExecutor.__new__(OrderExecutor)
    executor.dry_run = False
    executor.proxy_wallet = None
    executor._account_state = AccountState()

    async def fail_order(order):
        order.status = OrderStatus.FAILED
        return order

    executor._submit_order = fail_order

    market = Market(
        condition_id="condition",
        token_id_yes="yes_token",
        token_id_no="no_token",
        question="Test market?",
        slug="test-market",
    )
    opportunity = ShortArbitrageOpportunity(
        market=market,
        bid_price_yes=0.60,
        bid_price_no=0.50,
        trade_size_usdc=5.0,
        total_revenue=1.10,
        mint_cost=1.0,
        estimated_gas_cost=0.0,
        estimated_fee=0.0,
    )
    mint_report = types.SimpleNamespace(
        result="success",
        gas_cost_usd=0.0,
        tx_hash="0xabc",
        error_message=None,
    )

    from core.ctf import MintResult

    mint_report.result = MintResult.SUCCESS
    ctf_contract = types.SimpleNamespace(mint=AsyncMock(return_value=mint_report))

    report = await executor.execute_short_arbitrage(opportunity, ctf_contract)

    assert report.result == ExecutionResult.FAILED
    position = executor.account_state.get_position("condition")
    assert position is not None
    assert position.yes_balance == 5.0
    assert position.no_balance == 5.0


@pytest.mark.asyncio
async def test_short_arbitrage_skips_before_mint_when_proxy_wallet_mismatches_ctf_wallet():
    executor = OrderExecutor.__new__(OrderExecutor)
    executor.dry_run = False
    executor.proxy_wallet = "0x0000000000000000000000000000000000000001"
    executor._account_state = AccountState()

    market = Market(
        condition_id="condition",
        token_id_yes="yes_token",
        token_id_no="no_token",
        question="Test market?",
        slug="test-market",
    )
    opportunity = ShortArbitrageOpportunity(
        market=market,
        bid_price_yes=0.60,
        bid_price_no=0.50,
        trade_size_usdc=5.0,
        total_revenue=1.10,
        mint_cost=1.0,
        estimated_gas_cost=0.0,
        estimated_fee=0.0,
    )
    ctf_contract = types.SimpleNamespace(
        address="0x0000000000000000000000000000000000000002",
        mint=AsyncMock(),
    )

    report = await executor.execute_short_arbitrage(opportunity, ctf_contract)

    assert report.result == ExecutionResult.SKIPPED
    assert "mint wallet does not match CLOB trading wallet" in report.error_message
    ctf_contract.mint.assert_not_awaited()
