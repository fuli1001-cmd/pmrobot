"""Tests for the market scanner module."""

import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock

from core.scanner import MarketScanner
from models.market import Market, FeeCategory, MarketType


# Sample API response data (camelCase keys matching real Gamma API format)
SAMPLE_MARKET_RESPONSE = [
    {
        "conditionId": "0x123abc",
        "question": "Will Bitcoin reach $100k in 2026?",
        "slug": "bitcoin-100k-2026",
        "clobTokenIds": '["token_yes_1", "token_no_1"]',
        "orderPriceMinTickSize": "0.01",
        "negRisk": False,
        "tags": ["crypto", "bitcoin"],
        "volume24hr": "50000",
        "liquidity": "10000",
        "active": True,
        "closed": False,
        "enableOrderBook": True,
    },
    {
        "conditionId": "0x456def",
        "question": "Will Democrats win 2028 election?",
        "slug": "democrats-2028",
        "clobTokenIds": '["token_yes_2", "token_no_2"]',
        "orderPriceMinTickSize": "0.01",
        "negRisk": False,
        "tags": ["politics", "elections"],
        "volume24hr": "100000",
        "liquidity": "50000",
        "active": True,
        "closed": False,
        "enableOrderBook": True,
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
            "conditionId": "0x789",
            "clobTokenIds": '["only_one"]',
            "enableOrderBook": True,
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

    @pytest.mark.asyncio
    async def test_context_manager_disables_compressed_gamma_responses(self, scanner):
        """Gamma Brotli responses can fail to decode in some httpx installs."""
        async with scanner:
            assert scanner._client.headers["Accept-Encoding"] == "identity"

    @pytest.mark.asyncio
    async def test_get_json_retries_decoding_error_without_compression(self, scanner):
        """A bad compressed response should not make market discovery return zero."""
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_MARKET_RESPONSE
        mock_response.raise_for_status = MagicMock()

        scanner._client = MagicMock()
        scanner._client.get = AsyncMock(
            side_effect=[
                httpx.DecodingError("bad brotli stream"),
                mock_response,
            ]
        )

        data = await scanner._get_json("/markets", {"limit": "10"})

        assert data == SAMPLE_MARKET_RESPONSE
        assert scanner._client.get.await_count == 2
        retry_kwargs = scanner._client.get.await_args_list[1].kwargs
        assert retry_kwargs["headers"]["Accept-Encoding"] == "identity"

    @pytest.mark.asyncio
    async def test_fetch_all_markets_marks_partial_scan_incomplete(self, scanner):
        full_page = [SAMPLE_MARKET_RESPONSE[0] for _ in range(100)]
        scanner._get_json = AsyncMock(
            side_effect=[full_page, httpx.ReadTimeout("page timeout")]
        )

        markets = await scanner.fetch_all_markets(max_markets=200)

        assert len(markets) == 100
        assert scanner.last_market_scan_complete is False
        assert scanner._get_json.await_count == 2

    @pytest.mark.asyncio
    async def test_fetch_all_markets_uses_raw_page_size_for_pagination(self, scanner):
        invalid = {"enableOrderBook": False}
        mixed_page = [SAMPLE_MARKET_RESPONSE[0] for _ in range(50)] + [
            invalid for _ in range(50)
        ]
        scanner._get_json = AsyncMock(side_effect=[mixed_page, []])

        markets = await scanner.fetch_all_markets(max_markets=200)

        assert len(markets) == 50
        assert scanner.last_market_scan_complete is True
        assert scanner._get_json.await_count == 2

    @pytest.mark.asyncio
    async def test_negative_risk_scan_marks_request_failure_incomplete(self, scanner):
        scanner._get_json = AsyncMock(side_effect=httpx.ReadTimeout("events timeout"))

        events = await scanner.fetch_negative_risk_events(max_events=10)

        assert events == []
        assert scanner.last_negative_risk_scan_complete is False


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
