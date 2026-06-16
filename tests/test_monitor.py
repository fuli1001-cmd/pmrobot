"""Tests for the market monitor module."""

import pytest
import time

from models.market import Market, FeeCategory
from models.order import OrderBook, OrderBookLevel, ArbitrageOpportunity
from core.monitor import OrderBookManager, ArbitrageDetector


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

        assert opportunity.gross_profit_pct == 0.10
        assert opportunity.net_profit_pct == 0.10
        assert opportunity.net_profit_usdc == 10.0

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
