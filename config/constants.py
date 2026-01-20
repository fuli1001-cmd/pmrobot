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

# Tiered Profit Thresholds (LOWERED for dry-run testing)
# Binary markets: lower threshold since only 2 trades needed
PROFIT_THRESHOLD_BINARY = 0.005  # 0.5% (was 1%)
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

