"""Tests for the market monitor module."""

import pytest
import time

from models.market import Market, FeeCategory, NegativeRiskEvent
from models.order import OrderBook, OrderBookLevel, ArbitrageOpportunity
from core.monitor import (
    ArbitrageDetector,
    MarketMonitor,
    NegativeRiskArbitrageDetector,
    NegativeRiskMarketMonitor,
    OrderBookManager,
    _market_subscription,
    _token_shards,
)


def test_market_subscription_formats():
    assert _market_subscription(["token_1"], initial=True) == {
        "assets_ids": ["token_1"],
        "initial_dump": True,
        "type": "market",
        "custom_feature_enabled": True,
    }
    assert _market_subscription(["token_2"], initial=False) == {
        "assets_ids": ["token_2"],
        "initial_dump": True,
        "operation": "subscribe",
        "custom_feature_enabled": True,
    }


def test_token_shards_are_bounded_and_deduplicated():
    shards = _token_shards(["a", "b", "a", "c"], shard_size=2)

    assert shards == [["a", "b"], ["c"]]


class TestOrderBookManager:
    """Test cases for OrderBookManager."""

    @pytest.fixture
    def manager(self):
        """Create an order book manager."""
        return OrderBookManager()

    def test_update_and_get(self, manager):
        """Test updating and retrieving order books."""
        data = {
            "bids": [
                {"price": "0.45", "size": "100"},
                {"price": "0.44", "size": "200"},
            ],
            "asks": [
                {"price": "0.46", "size": "150"},
                {"price": "0.47", "size": "250"},
            ],
        }

        book = manager.update("token_1", data)

        assert book.token_id == "token_1"
        assert len(book.bids) == 2
        assert len(book.asks) == 2
        assert book.best_bid == 0.45
        assert book.best_ask == 0.46

        # Retrieve
        retrieved = manager.get("token_1")
        assert retrieved == book

    def test_get_pair(self, manager):
        """Test getting Yes/No pair."""
        manager.update("yes_token", {"bids": [], "asks": [{"price": "0.5", "size": "100"}]})
        manager.update("no_token", {"bids": [], "asks": [{"price": "0.5", "size": "100"}]})

        yes_book, no_book = manager.get_pair("yes_token", "no_token")

        assert yes_book is not None
        assert no_book is not None

    def test_apply_price_change_updates_and_removes_levels(self, manager):
        manager.update(
            "token_1",
            {
                "bids": [{"price": "0.44", "size": "10"}],
                "asks": [
                    {"price": "0.46", "size": "20"},
                    {"price": "0.47", "size": "30"},
                ],
            },
        )

        manager.apply_price_change(
            "token_1",
            {"side": "SELL", "price": "0.46", "size": "25"},
            timestamp=100.0,
        )
        manager.apply_price_change(
            "token_1",
            {"side": "SELL", "price": "0.47", "size": "0"},
            timestamp=100.0,
        )
        manager.apply_price_change(
            "token_1",
            {"side": "BUY", "price": "0.45", "size": "12"},
            timestamp=100.0,
        )

        book = manager.get("token_1")
        assert [(level.price, level.size) for level in book.asks] == [(0.46, 25.0)]
        assert [(level.price, level.size) for level in book.bids] == [
            (0.45, 12.0),
            (0.44, 10.0),
        ]
        assert book.timestamp == 100.0

    def test_apply_price_change_requires_full_snapshot(self, manager):
        result = manager.apply_price_change(
            "missing",
            {"side": "SELL", "price": "0.46", "size": "25"},
        )

        assert result is None
        assert manager.get("missing") is None


class TestMarketMonitor:
    @pytest.fixture
    def sample_market(self):
        return Market(
            condition_id="0x123",
            token_id_yes="yes_token",
            token_id_no="no_token",
            question="Test Market?",
            slug="test-market",
            fee_category=FeeCategory.FREE,
        )

    @pytest.mark.asyncio
    async def test_price_change_batch_is_applied_before_detection(self, sample_market):
        opportunities = []
        monitor = MarketMonitor(
            markets=[sample_market],
            profit_threshold=0.03,
            trade_size=3.0,
            depth_safety_multiplier=1.0,
            on_opportunity=opportunities.append,
        )
        monitor.order_books.update(
            "yes_token",
            {"bids": [], "asks": [{"price": "0.55", "size": "100"}]},
        )
        monitor.order_books.update(
            "no_token",
            {"bids": [], "asks": [{"price": "0.55", "size": "100"}]},
        )

        await monitor._process_single_message(
            {
                "event_type": "price_change",
                "price_changes": [
                    {
                        "asset_id": "yes_token",
                        "side": "SELL",
                        "price": "0.55",
                        "size": "0",
                    },
                    {
                        "asset_id": "yes_token",
                        "side": "SELL",
                        "price": "0.45",
                        "size": "100",
                    },
                    {
                        "asset_id": "no_token",
                        "side": "SELL",
                        "price": "0.55",
                        "size": "0",
                    },
                    {
                        "asset_id": "no_token",
                        "side": "SELL",
                        "price": "0.45",
                        "size": "100",
                    },
                ],
            }
        )

        assert len(opportunities) == 1
        assert opportunities[0].avg_price_yes == pytest.approx(0.45)
        assert opportunities[0].avg_price_no == pytest.approx(0.45)
        assert monitor._price_change_count == 4

    @pytest.mark.asyncio
    async def test_stale_pair_is_not_detected(self, sample_market):
        opportunities = []
        monitor = MarketMonitor(
            markets=[sample_market],
            profit_threshold=0.03,
            trade_size=3.0,
            depth_safety_multiplier=1.0,
            book_max_age_seconds=2.0,
            book_max_skew_seconds=0.5,
            on_opportunity=opportunities.append,
        )
        monitor.order_books.update(
            "yes_token",
            {"bids": [], "asks": [{"price": "0.45", "size": "100"}]},
        )
        no_book = monitor.order_books.update(
            "no_token",
            {"bids": [], "asks": [{"price": "0.45", "size": "100"}]},
        )
        no_book.timestamp -= 3.0

        await monitor._detect_market(sample_market)

        assert opportunities == []
        assert monitor._stale_pair_skip_count == 1

    @pytest.mark.asyncio
    async def test_price_change_before_snapshot_is_aggregated(self, sample_market):
        monitor = MarketMonitor(markets=[sample_market])

        await monitor._process_single_message(
            {
                "event_type": "price_change",
                "price_changes": [
                    {
                        "asset_id": "yes_token",
                        "side": "SELL",
                        "price": "0.45",
                        "size": "100",
                    }
                ],
            }
        )

        assert monitor._price_change_ignored_count == 1
        assert monitor._tokens_awaiting_snapshot == {"yes_token"}

        await monitor._handle_book_update(
            {
                "asset_id": "yes_token",
                "bids": [],
                "asks": [{"price": "0.45", "size": "100"}],
            }
        )

        assert monitor._tokens_awaiting_snapshot == set()

    @pytest.mark.asyncio
    async def test_market_refresh_replaces_removed_subscriptions(self, sample_market):
        replacement = Market(
            condition_id="0x456",
            token_id_yes="new_yes",
            token_id_no="new_no",
            question="Replacement?",
            slug="replacement",
        )
        monitor = MarketMonitor(markets=[sample_market])
        monitor.order_books.update("yes_token", {"bids": [], "asks": []})

        await monitor.update_markets([replacement])

        assert set(monitor.markets) == {"0x456"}
        assert set(monitor.token_to_market) == {"new_yes", "new_no"}
        assert monitor.order_books.get("yes_token") is None
        assert monitor._subscription_revision == 1


class TestNegativeRiskMonitor:
    @staticmethod
    def _event() -> NegativeRiskEvent:
        outcomes = [
            Market(
                condition_id=f"condition_{index}",
                token_id_yes=f"yes_{index}",
                token_id_no=f"no_{index}",
                question=f"Outcome {index}?",
                slug=f"outcome-{index}",
            )
            for index in range(3)
        ]
        return NegativeRiskEvent(
            event_id="event_1",
            title="Test event",
            slug="test-event",
            outcomes=outcomes,
        )

    @pytest.mark.asyncio
    async def test_price_change_preserves_full_snapshot_levels(self):
        event = self._event()
        monitor = NegativeRiskMarketMonitor(events=[event])
        monitor.order_books.update(
            "yes_0",
            {
                "bids": [{"price": "0.20", "size": "10"}],
                "asks": [
                    {"price": "0.30", "size": "10"},
                    {"price": "0.31", "size": "20"},
                ],
            },
        )

        await monitor._handle_price_change(
            {
                "price_changes": [
                    {
                        "asset_id": "yes_0",
                        "side": "SELL",
                        "price": "0.30",
                        "size": "15",
                    }
                ]
            }
        )

        book = monitor.order_books.get("yes_0")
        assert [(level.price, level.size) for level in book.asks] == [
            (0.30, 15.0),
            (0.31, 20.0),
        ]
        assert [(level.price, level.size) for level in book.bids] == [(0.20, 10.0)]

    def test_detector_uses_equal_tokens_without_exceeding_budget(self):
        event = self._event()
        books = OrderBookManager()
        for outcome in event.outcomes:
            books.update(
                outcome.token_id_yes,
                {"bids": [], "asks": [{"price": "0.30", "size": "100"}]},
            )
            books.update(
                outcome.token_id_no,
                {"bids": [], "asks": [{"price": "0.70", "size": "100"}]},
            )

        detector = NegativeRiskArbitrageDetector(
            profit_threshold=0.03,
            trade_size=3.0,
        )
        opportunity = detector.detect(event, books)

        assert opportunity is not None
        assert opportunity.token_size == pytest.approx(3.0 / 0.9)
        assert opportunity.trade_size_usdc == pytest.approx(3.0)
        assert opportunity.trade_size_usdc <= detector.trade_size


class TestOrderBook:
    """Test cases for OrderBook."""

    def test_calculate_average_buy_price_single_level(self):
        """Test average price calculation with single level."""
        book = OrderBook(
            token_id="test",
            asks=[OrderBookLevel(price=0.50, size=1000)],
        )

        avg_price = book.calculate_average_buy_price(100)

        assert avg_price == 0.50

    def test_calculate_average_buy_price_multiple_levels(self):
        """Test average price calculation across multiple levels."""
        book = OrderBook(
            token_id="test",
            asks=[
                OrderBookLevel(price=0.50, size=100),  # $50 value
                OrderBookLevel(price=0.55, size=100),  # $55 value
            ],
        )

        # Buy $75 worth
        avg_price = book.calculate_average_buy_price(75)

        # First 100 tokens at 0.50 = $50, then 50 tokens at 0.55 = $27.50
        # Total: 150 tokens for $75
        # Wait, let's recalculate:
        # Level 1: 100 tokens * 0.50 = $50 value. Take all for $50, get 100 tokens
        # Level 2: need $25 more at 0.55. $25 / 0.55 = 45.45 tokens
        # Total: 145.45 tokens for $75
        # Average: $75 / 145.45 = 0.516...
        assert avg_price is not None
        assert 0.50 < avg_price < 0.55

    def test_calculate_average_buy_price_insufficient_liquidity(self):
        """Test when there's not enough liquidity."""
        book = OrderBook(
            token_id="test",
            asks=[OrderBookLevel(price=0.50, size=10)],  # Only $5 value
        )

        avg_price = book.calculate_average_buy_price(100)

        assert avg_price is None


class TestArbitrageDetector:
    """Test cases for ArbitrageDetector."""

    @pytest.fixture
    def detector(self):
        """Create an arbitrage detector."""
        return ArbitrageDetector(
            profit_threshold=0.008,  # 0.8%
            trade_size=100.0,
            max_slippage=0.02,
        )

    @pytest.fixture
    def sample_market(self):
        """Create a sample market."""
        return Market(
            condition_id="0x123",
            token_id_yes="yes_token",
            token_id_no="no_token",
            question="Test Market?",
            slug="test-market",
            fee_category=FeeCategory.FREE,
        )

    def test_detect_profitable_opportunity(self, detector, sample_market):
        """Test detection of a profitable opportunity."""
        # Yes at 0.45, No at 0.45 = total cost 0.90 = 10% profit
        book_yes = OrderBook(
            token_id="yes_token",
            asks=[OrderBookLevel(price=0.45, size=1000)],
        )
        book_no = OrderBook(
            token_id="no_token",
            asks=[OrderBookLevel(price=0.45, size=1000)],
        )

        opportunity = detector.detect(sample_market, book_yes, book_no)

        assert opportunity is not None
        assert opportunity.gross_profit_pct == pytest.approx(0.10, abs=0.01)
        assert opportunity.is_profitable(0.008)

    def test_detect_no_opportunity(self, detector, sample_market):
        """Test when there's no profitable opportunity."""
        # Yes at 0.50, No at 0.51 = total cost 1.01 = no profit
        book_yes = OrderBook(
            token_id="yes_token",
            asks=[OrderBookLevel(price=0.50, size=1000)],
        )
        book_no = OrderBook(
            token_id="no_token",
            asks=[OrderBookLevel(price=0.51, size=1000)],
        )

        opportunity = detector.detect(sample_market, book_yes, book_no)

        assert opportunity is None

    def test_detect_insufficient_liquidity(self, detector, sample_market):
        """Test when there's insufficient liquidity."""
        book_yes = OrderBook(
            token_id="yes_token",
            asks=[OrderBookLevel(price=0.45, size=1)],  # Very low liquidity
        )
        book_no = OrderBook(
            token_id="no_token",
            asks=[OrderBookLevel(price=0.45, size=1000)],
        )

        opportunity = detector.detect(sample_market, book_yes, book_no)

        assert opportunity is None

    def test_detect_short_uses_configured_trade_size_as_minimum(self, sample_market):
        """Short arbitrage should use MAX_TRADE_SIZE, not a hard-coded $50 floor."""
        detector = ArbitrageDetector(
            profit_threshold=0.008,
            trade_size=5.0,
            max_slippage=0.02,
        )
        book_yes = OrderBook(
            token_id="yes_token",
            bids=[OrderBookLevel(price=0.56, size=100)],
        )
        book_no = OrderBook(
            token_id="no_token",
            bids=[OrderBookLevel(price=0.56, size=100)],
        )

        opportunity = detector.detect_short(sample_market, book_yes, book_no)

        assert opportunity is not None
        assert opportunity.trade_size_usdc == 5.0

    def test_detect_short_skips_when_depth_cannot_fill_configured_size(self, sample_market):
        """Short arbitrage should not silently shrink below MAX_TRADE_SIZE."""
        detector = ArbitrageDetector(
            profit_threshold=0.008,
            trade_size=5.0,
            max_slippage=0.02,
        )
        book_yes = OrderBook(
            token_id="yes_token",
            bids=[OrderBookLevel(price=0.56, size=20)],
        )
        book_no = OrderBook(
            token_id="no_token",
            bids=[OrderBookLevel(price=0.56, size=20)],
        )

        opportunity = detector.detect_short(sample_market, book_yes, book_no)

        assert opportunity is None


class TestArbitrageOpportunity:
    """Test cases for ArbitrageOpportunity."""

    @pytest.fixture
    def sample_market(self):
        """Create a sample market."""
        return Market(
            condition_id="0x123",
            token_id_yes="yes_token",
            token_id_no="no_token",
            question="Test?",
            slug="test",
            fee_category=FeeCategory.FREE,
        )

    def test_profit_calculations(self, sample_market):
        """Test profit percentage calculations."""
        opportunity = ArbitrageOpportunity(
            market=sample_market,
            avg_price_yes=0.45,
            avg_price_no=0.45,
            trade_size_usdc=100.0,
            total_cost=0.90,
            estimated_fee=0.0,
            timestamp=time.time(),
        )

        assert opportunity.gross_profit_pct == pytest.approx(0.10)
        assert opportunity.net_profit_pct == pytest.approx(0.10)
        assert opportunity.net_profit_usdc == pytest.approx(10.0)

    def test_is_profitable_with_threshold(self, sample_market):
        """Test profitability check with threshold."""
        opportunity = ArbitrageOpportunity(
            market=sample_market,
            avg_price_yes=0.49,
            avg_price_no=0.50,
            trade_size_usdc=100.0,
            total_cost=0.99,
            estimated_fee=0.0,
            timestamp=time.time(),
        )

        # 1% profit, should pass 0.8% threshold
        assert opportunity.is_profitable(0.008)

        # Should fail 2% threshold
        assert not opportunity.is_profitable(0.02)
