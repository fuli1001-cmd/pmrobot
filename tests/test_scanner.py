"""Tests for the market scanner module."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from core.scanner import MarketScanner
from models.market import Market, FeeCategory, MarketType


# Sample API response data
SAMPLE_MARKET_RESPONSE = [
    {
        "condition_id": "0x123abc",
        "question": "Will Bitcoin reach $100k in 2026?",
        "slug": "bitcoin-100k-2026",
        "tokens": [
            {"token_id": "token_yes_1", "outcome": "Yes"},
            {"token_id": "token_no_1", "outcome": "No"},
        ],
        "minimum_tick_size": "0.01",
        "neg_risk": False,
        "tags": ["crypto", "bitcoin"],
        "volume_24hr": "50000",
        "liquidity": "10000",
        "active": True,
        "closed": False,
        "enable_order_book": True,
    },
    {
        "condition_id": "0x456def",
        "question": "Will Democrats win 2028 election?",
        "slug": "democrats-2028",
        "tokens": [
            {"token_id": "token_yes_2", "outcome": "Yes"},
            {"token_id": "token_no_2", "outcome": "No"},
        ],
        "minimum_tick_size": "0.01",
        "neg_risk": False,
        "tags": ["politics", "elections"],
        "volume_24hr": "100000",
        "liquidity": "50000",
        "active": True,
        "closed": False,
        "enable_order_book": True,
    },
]


class TestMarketScanner:
    """Test cases for MarketScanner."""

    @pytest.fixture
    def scanner(self):
        """Create a scanner instance."""
        return MarketScanner(rate_limit=100.0)

    def test_parse_market_valid(self, scanner):
        """Test parsing a valid market."""
        market = scanner._parse_market(SAMPLE_MARKET_RESPONSE[0])

        assert market is not None
        assert market.condition_id == "0x123abc"
        assert market.token_id_yes == "token_yes_1"
        assert market.token_id_no == "token_no_1"
        assert market.question == "Will Bitcoin reach $100k in 2026?"
        assert market.market_type == MarketType.BINARY

    def test_parse_market_fee_category_politics(self, scanner):
        """Test that politics markets are marked as fee-free."""
        market = scanner._parse_market(SAMPLE_MARKET_RESPONSE[1])

        assert market is not None
        assert market.fee_category == FeeCategory.FREE
        assert market.is_fee_free is True

    def test_parse_market_insufficient_tokens(self, scanner):
        """Test that markets with insufficient tokens return None."""
        invalid_data = {
            "condition_id": "0x789",
            "tokens": [{"token_id": "only_one", "outcome": "Yes"}],
        }
        market = scanner._parse_market(invalid_data)
        assert market is None

    def test_determine_fee_category(self, scanner):
        """Test fee category determination."""
        assert scanner._determine_fee_category(["politics"]) == FeeCategory.FREE
        assert scanner._determine_fee_category(["elections"]) == FeeCategory.FREE
        assert scanner._determine_fee_category(["sports"]) == FeeCategory.FREE
        assert scanner._determine_fee_category(["crypto-15min"]) == FeeCategory.CRYPTO_15MIN
        assert scanner._determine_fee_category(["random-tag"]) == FeeCategory.FREE

    @pytest.mark.asyncio
    async def test_fetch_active_markets(self, scanner):
        """Test fetching active markets with mocked HTTP."""
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_MARKET_RESPONSE
        mock_response.raise_for_status = MagicMock()

        with patch.object(scanner, "_client") as mock_client:
            mock_client.get = AsyncMock(return_value=mock_response)
            scanner._client = mock_client

            markets = await scanner.fetch_active_markets(limit=10)

            assert len(markets) == 2
            assert all(isinstance(m, Market) for m in markets)


class TestMarket:
    """Test cases for Market model."""

    def test_estimate_fee_free(self):
        """Test fee estimation for free markets."""
        market = Market(
            condition_id="0x123",
            token_id_yes="yes",
            token_id_no="no",
            question="Test?",
            slug="test",
            fee_category=FeeCategory.FREE,
        )

        assert market.estimate_fee(0.5) == 0.0
        assert market.estimate_fee(0.9) == 0.0

    def test_estimate_fee_crypto_15min(self):
        """Test fee estimation for 15-min crypto markets."""
        market = Market(
            condition_id="0x123",
            token_id_yes="yes",
            token_id_no="no",
            question="BTC 15min?",
            slug="btc-15min",
            fee_category=FeeCategory.CRYPTO_15MIN,
        )

        # At 50% odds, fee should be maximum (3.15%)
        assert abs(market.estimate_fee(0.5) - 0.0315) < 0.001

        # At 0% or 100%, fee should be 0
        assert abs(market.estimate_fee(0.0) - 0.0) < 0.001
        assert abs(market.estimate_fee(1.0) - 0.0) < 0.001

        # At 75%, fee should be half
        assert abs(market.estimate_fee(0.75) - 0.01575) < 0.001
