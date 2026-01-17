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
