"""Tests for order execution classification."""

import types
from unittest.mock import AsyncMock

import pytest

import core.executor as executor_module
from core.executor import (
    DRY_RUN_PREFLIGHT_PASSED,
    DRY_RUN_PREFLIGHT_REJECTED,
    ExecutionResult,
    OrderExecutor,
)
from models.market import Market
from models.order import (
    ArbitrageOpportunity,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderSide,
    OrderStatus,
    OrderType,
    ShortArbitrageOpportunity,
)
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

    report = await executor.execute_arbitrage(
        opportunity,
        validate_before_execute=False,
    )

    assert report.result == ExecutionResult.FAILED
    assert report.order_yes.status == OrderStatus.FAILED
    assert report.order_no.status == OrderStatus.FAILED
    executor._emergency_exit.assert_not_awaited()


@pytest.mark.asyncio
async def test_binary_preflight_skips_without_opening_position_when_live_leg_unfillable():
    executor = OrderExecutor.__new__(OrderExecutor)
    executor.dry_run = False
    executor._concurrent = False
    executor._account_state = AccountState()
    executor.proxy_wallet = None
    executor._submit_order = AsyncMock()

    market = Market(
        condition_id="condition",
        token_id_yes="yes_token",
        token_id_no="no_token",
        question="Test market?",
        slug="test-market",
    )
    opportunity = ArbitrageOpportunity(
        market=market,
        avg_price_yes=0.51,
        avg_price_no=0.39,
        trade_size_usdc=4.0,
        total_cost=0.90,
        estimated_fee=0.0,
    )

    async def fetch_book(token_id):
        if token_id == "yes_token":
            return OrderBook(
                token_id=token_id,
                asks=[OrderBookLevel(price=0.51, size=40.0)],
            )
        return OrderBook(
            token_id=token_id,
            asks=[OrderBookLevel(price=0.39, size=2.0)],
        )

    executor._fetch_live_order_book = fetch_book

    report = await executor.execute_arbitrage(opportunity)

    assert report.result == ExecutionResult.SKIPPED
    executor._submit_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_binary_preflight_executes_more_fragile_live_leg_first():
    executor = OrderExecutor.__new__(OrderExecutor)
    executor.dry_run = False
    executor._concurrent = False
    executor._account_state = AccountState()
    executor.proxy_wallet = None
    executor.get_account_balance = AsyncMock(return_value=100.0)

    submitted = []

    async def fill_order(order):
        submitted.append(order.token_id)
        order.status = OrderStatus.FILLED
        order.filled_size = order.size
        order.filled_avg_price = order.price
        return order

    executor._submit_order = fill_order

    market = Market(
        condition_id="condition",
        token_id_yes="yes_token",
        token_id_no="no_token",
        question="Test market?",
        slug="test-market",
    )
    opportunity = ArbitrageOpportunity(
        market=market,
        avg_price_yes=0.51,
        avg_price_no=0.39,
        trade_size_usdc=4.0,
        total_cost=0.90,
        estimated_fee=0.0,
    )

    async def fetch_book(token_id):
        if token_id == "yes_token":
            return OrderBook(
                token_id=token_id,
                asks=[OrderBookLevel(price=0.51, size=80.0)],
            )
        return OrderBook(
            token_id=token_id,
            asks=[OrderBookLevel(price=0.39, size=20.0)],
        )

    executor._fetch_live_order_book = fetch_book

    report = await executor.execute_arbitrage(opportunity)

    assert report.result == ExecutionResult.SUCCESS
    assert submitted == ["no_token", "yes_token"]


@pytest.mark.asyncio
async def test_dry_run_binary_executes_live_preflight_before_skipping_orders():
    executor = OrderExecutor.__new__(OrderExecutor)
    executor.dry_run = True
    executor._concurrent = False
    executor._account_state = AccountState()
    executor.proxy_wallet = None
    executor._submit_order = AsyncMock()

    market = Market(
        condition_id="condition",
        token_id_yes="yes_token",
        token_id_no="no_token",
        question="Test market?",
        slug="test-market",
    )
    opportunity = ArbitrageOpportunity(
        market=market,
        avg_price_yes=0.51,
        avg_price_no=0.39,
        trade_size_usdc=4.0,
        total_cost=0.90,
        estimated_fee=0.0,
    )

    async def fetch_book(token_id):
        if token_id == "yes_token":
            return OrderBook(
                token_id=token_id,
                asks=[OrderBookLevel(price=0.51, size=80.0)],
            )
        return OrderBook(
            token_id=token_id,
            asks=[OrderBookLevel(price=0.39, size=20.0)],
        )

    executor._fetch_live_order_book = fetch_book

    report = await executor.execute_arbitrage(opportunity)

    assert report.result == ExecutionResult.SKIPPED
    assert report.error_message == DRY_RUN_PREFLIGHT_PASSED
    assert report.order_yes.size == report.order_no.size
    assert report.order_yes.size > 0
    executor._submit_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_binary_rejects_when_live_preflight_fails():
    executor = OrderExecutor.__new__(OrderExecutor)
    executor.dry_run = True
    executor._concurrent = False
    executor._account_state = AccountState()
    executor.proxy_wallet = None
    executor._submit_order = AsyncMock()

    market = Market(
        condition_id="condition",
        token_id_yes="yes_token",
        token_id_no="no_token",
        question="Test market?",
        slug="test-market",
    )
    opportunity = ArbitrageOpportunity(
        market=market,
        avg_price_yes=0.51,
        avg_price_no=0.39,
        trade_size_usdc=4.0,
        total_cost=0.90,
        estimated_fee=0.0,
    )

    async def fetch_book(token_id):
        if token_id == "yes_token":
            return OrderBook(
                token_id=token_id,
                asks=[OrderBookLevel(price=0.51, size=80.0)],
            )
        return OrderBook(
            token_id=token_id,
            asks=[OrderBookLevel(price=0.39, size=1.0)],
        )

    executor._fetch_live_order_book = fetch_book

    report = await executor.execute_arbitrage(opportunity)

    assert report.result == ExecutionResult.SKIPPED
    assert report.error_message == DRY_RUN_PREFLIGHT_REJECTED
    executor._submit_order.assert_not_awaited()


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
async def test_short_arbitrage_merges_and_stops_when_sellable_balance_unavailable():
    executor = OrderExecutor.__new__(OrderExecutor)
    executor.dry_run = False
    executor.proxy_wallet = None
    executor._account_state = AccountState()
    executor._submit_order = AsyncMock()
    executor._wait_for_sellable_short_balances = AsyncMock(
        return_value=(False, {"yes": 0.0, "no": 0.0})
    )

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

    from core.ctf import MintResult

    mint_report = types.SimpleNamespace(
        result=MintResult.SUCCESS,
        gas_cost_usd=0.0,
        tx_hash="0xabc",
        error_message=None,
    )
    merge_report = types.SimpleNamespace(
        result=MintResult.SUCCESS,
        tx_hash="0xmerge",
        error_message=None,
    )
    ctf_contract = types.SimpleNamespace(
        mint=AsyncMock(return_value=mint_report),
        merge=AsyncMock(return_value=merge_report),
    )

    report = await executor.execute_short_arbitrage(opportunity, ctf_contract)

    assert report.result == ExecutionResult.FAILED
    assert report.fatal_error is True
    assert "did not report sellable token balances" in report.error_message
    ctf_contract.merge.assert_awaited_once_with("condition", 5.0)
    executor._submit_order.assert_not_awaited()
    assert executor.account_state.get_position("condition") is None


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
async def test_short_arbitrage_mint_failure_is_fatal_and_does_not_sell():
    executor = OrderExecutor.__new__(OrderExecutor)
    executor.dry_run = False
    executor.proxy_wallet = None
    executor._account_state = AccountState()

    sell_order = AsyncMock()
    executor._submit_order = sell_order

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

    from core.ctf import MintResult

    mint_report = types.SimpleNamespace(
        result=MintResult.FAILED,
        gas_cost_usd=0.0,
        tx_hash="0xdead",
        error_message="Relayer mint failed state=STATE_FAILED",
        proxy_wallet="0x0000000000000000000000000000000000000001",
        signer_address="0x0000000000000000000000000000000000000002",
        relayer_tx_type="SAFE",
        relayer_transaction_id="tx-id",
        relayer_state="STATE_FAILED",
    )
    ctf_contract = types.SimpleNamespace(mint=AsyncMock(return_value=mint_report))

    report = await executor.execute_short_arbitrage(opportunity, ctf_contract)

    assert report.result == ExecutionResult.FAILED
    assert report.fatal_error is True
    assert "Relayer mint failed" in report.error_message
    sell_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_emergency_exit_sells_book_chunks_from_high_to_low(monkeypatch):
    executor = OrderExecutor.__new__(OrderExecutor)
    monkeypatch.setattr(executor_module, "EMERGENCY_EXIT_SETTLEMENT_DELAY_SECONDS", 0.0)

    filled_order = Order(
        token_id="filled_token",
        side=OrderSide.BUY,
        price=0.35,
        size=10.0,
        order_type=OrderType.FOK,
        status=OrderStatus.FILLED,
        filled_size=10.0,
        filled_avg_price=0.35,
    )
    executor._fetch_exit_bids = AsyncMock(
        return_value=[
            OrderBookLevel(price=0.34, size=4.0),
            OrderBookLevel(price=0.33, size=3.0),
            OrderBookLevel(price=0.32, size=3.0),
        ]
    )

    submitted = []

    async def fill_order(order):
        submitted.append((order.price, order.size))
        order.status = OrderStatus.FILLED
        order.filled_size = order.size
        order.filled_avg_price = order.price
        return order

    executor._submit_order = fill_order

    exited = await executor._exit_filled_order(filled_order, context="test exit")

    assert exited is True
    assert submitted == [(0.34, 4.0), (0.32, 6.0)]


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
