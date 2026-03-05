"""Constants for Polymarket Arbitrage Bot."""

# API Endpoints
GAMMA_API_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_API_BASE_URL = "https://clob.polymarket.com"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Polygon Network
POLYGON_CHAIN_ID = 137
POLYGON_AMOY_CHAIN_ID = 80002

# CTF Contract Address (Polygon Mainnet)
CTF_CONTRACT_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Fee-free market tags (政治、体育、长期事件等)
FEE_FREE_TAGS = [
    "politics",
    "elections",
    "sports",
    "entertainment",
    "science",
    "society",
]

# 15-minute crypto market identifiers
CRYPTO_15MIN_TAGS = ["crypto-15min", "btc-15min", "eth-15min"]

# Maximum taker fee for 15-min crypto markets
MAX_CRYPTO_TAKER_FEE = 0.0315  # 3.15%

# Order Types
ORDER_TYPE_GTC = "GTC"  # Good Till Cancelled
ORDER_TYPE_FOK = "FOK"  # Fill Or Kill
ORDER_TYPE_GTD = "GTD"  # Good Till Date

# Signature Types
SIGNATURE_TYPE_EOA = 0
SIGNATURE_TYPE_POLY_PROXY = 1
SIGNATURE_TYPE_POLY_GNOSIS_SAFE = 2

# Relayer URLs
RELAYER_URL = "https://relayer-v2.polymarket.com"
RELAYER_URL_TESTNET = "https://relayer-v2-staging.polymarket.dev"

# Tiered Profit Thresholds (adjusted for testing)
# Binary markets: 1% threshold for stable profit per trade
PROFIT_THRESHOLD_BINARY = 0.01  # 1% (restored from 0.5%)
# Negative Risk markets: lowered for more opportunities
PROFIT_THRESHOLD_NEG_RISK_SMALL = 0.01  # 1% for 3-5 outcomes (was 2%)
PROFIT_THRESHOLD_NEG_RISK_LARGE = 0.015  # 1.5% for 6+ outcomes (was 2.5%)

# Short Arbitrage (Mint + Sell) Constants
USDC_CONTRACT_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC on Polygon
ESTIMATED_MINT_GAS_COST_USD = 0.05  # Conservative estimate for Polygon Mint gas cost
MIN_SHORT_ARBITRAGE_SIZE = 50.0  # Minimum $50 to make short arbitrage worthwhile
SHORT_ARBITRAGE_THRESHOLD = 0.01  # 1.0% min profit for short (was 1.5%)


def get_profit_threshold(outcome_count: int = 2) -> float:
    """
    Get appropriate profit threshold based on number of outcomes.
    
    Args:
        outcome_count: Number of outcomes in the market (2 for binary)
        
    Returns:
        Recommended profit threshold as decimal (e.g., 0.01 = 1%)
    """
    if outcome_count <= 2:
        return PROFIT_THRESHOLD_BINARY
    elif outcome_count <= 5:
        return PROFIT_THRESHOLD_NEG_RISK_SMALL
    else:
        return PROFIT_THRESHOLD_NEG_RISK_LARGE


# ═══ Azuro Protocol Constants ═══
AZURO_DATA_FEED_POLYGON_URL = (
    "https://thegraph-1.onchainfeed.org/subgraphs/name/"
    "azuro-protocol/azuro-data-feed-polygon"
)

# Cross-platform profit threshold (higher than intra-platform
# due to Azuro bet exit risk — NFTs cannot be resold instantly)
CROSS_PLATFORM_PROFIT_THRESHOLD = 0.03  # 3%

# Default slippage buffer for Azuro minOdds parameter
DEFAULT_AZURO_MIN_ODDS_SLIPPAGE = 0.02  # 2%

# Estimated gas cost per Azuro bet on Polygon (USDC)
ESTIMATED_AZURO_GAS_COST_USD = 0.02

# Cross-platform sport mapping: (PM Events-API tag_slug, Azuro sport name)
# Each tuple is scanned independently to keep Cartesian products small.
#
# NOTE: soccer and basketball are excluded because Polymarket only offers
# season/tournament-level markets for these sports (e.g. "2026 NBA Champion",
# "EPL Winner"), NOT individual match outcomes.  SX Bet only has match-level
# markets, so no cross-platform pairing is possible.
#
# Azuro mapping (DEPRECATED — Azuro disabled):
#   CROSS_SPORT_MAP was (pm_tag, azuro_sport_name) tuples.
#
# SX Bet mapping: (pm_tag_slug, sx_sport_slug)
# The sx_sport_slug is used by SxBetExchange.get_markets(sport=...) which
# looks up the numeric sportId via SXBET_SPORT_IDS.
CROSS_SPORT_MAP: list[tuple[str, str]] = [
    # ("soccer", "soccer"),          # PM = season only, no matches
    # ("basketball", "basketball"),  # PM = season only, no matches
    ("mma", "mma"),
    ("tennis", "tennis"),
    ("baseball", "baseball"),
    ("hockey", "hockey"),
    ("nfl", "nfl"),
    ("boxing", "boxing"),
    ("cricket", "cricket"),
    # ── New categories (added after cross-platform research) ──
    ("rugby", "rugby"),
    ("counter-strike", "counter-strike"),        # Esport
    ("league-of-legends", "league-of-legends"),  # Esport
    # ── SX Bet unique categories ──
    ("politics", "politics"),                    # SX sportId=17
    ("crypto", "crypto"),                        # SX sportId=14
    ("entertainment", "entertainment"),           # SX sportId=18
]

