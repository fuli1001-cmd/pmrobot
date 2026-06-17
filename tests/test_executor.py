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
from models.order import ArbitrageOpportunity, OrderStatus


@pytest.mark.asyncio
async def test_concurrent_binary_both_failed_is_not_partial():
    executor = OrderExecutor.__new__(OrderExecutor)
    executor.dry_run = False
    executor._concurrent = True
    executor._emergency_exit = AsyncMock()

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
