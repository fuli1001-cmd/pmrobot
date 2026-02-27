"""Position settlement (merge) module.

Supports two modes:
1. **Relayer mode** (preferred): Gasless merge via Polymarket Relayer API.
   Requires Builder Program credentials (builder_api_key/secret/passphrase).
2. **Direct EOA mode** (fallback): Direct on-chain transaction signed by
   the wallet private key.  Requires the wallet hold POL for gas.
"""

import asyncio
import time
from typing import List, Optional

from web3 import Web3

# web3.py v6+ compatibility
try:
    from web3.middleware import ExtraDataToPOAMiddleware as geth_poa_middleware
except ImportError:
    try:
        from web3.middleware.geth import geth_poa_middleware
    except ImportError:
        from web3.middleware import geth_poa_middleware

from config.settings import get_settings
from config.constants import (
    CTF_CONTRACT_ADDRESS,
    POLYGON_CHAIN_ID,
    RELAYER_URL,
    RELAYER_URL_TESTNET,
)
from models.position import AccountState, Position
from utils.logger import get_logger
from utils.notifier import create_notifier

logger = get_logger(__name__)

# CTF Contract ABI (only mergePositions function)
CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "mergePositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# USDC Contract Address on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


class PositionSettler:
    """
    Settles (merges) Yes+No token pairs back to USDC.

    Preferred path uses the Polymarket *Relayer API* for gasless
    meta-transactions.  Falls back to direct EOA signing when Relayer
    credentials are not configured or the Relayer SDK is not installed.
    """

    def __init__(
        self,
        rpc_url: str,
        private_key: Optional[str] = None,
        min_merge_amount: float = 10.0,
        merge_interval: int = 600,
        # Relayer credentials (Builder Program)
        builder_api_key: Optional[str] = None,
        builder_secret: Optional[str] = None,
        builder_passphrase: Optional[str] = None,
        relay_tx_type: str = "PROXY",
        is_testnet: bool = False,
    ):
        """
        Initialize the position settler.

        Args:
            rpc_url: Polygon RPC URL
            private_key: Wallet private key for signing
            min_merge_amount: Minimum amount to trigger merge (USDC)
            merge_interval: Seconds between merge attempts
            builder_api_key: Polymarket Builder API Key (enables Relayer)
            builder_secret: Polymarket Builder Secret
            builder_passphrase: Polymarket Builder Passphrase
            relay_tx_type: "SAFE" or "PROXY" (wallet type on Relayer)
            is_testnet: Use staging Relayer URL
        """
        self.min_merge_amount = min_merge_amount
        self.merge_interval = merge_interval
        self._last_merge_time = 0.0
        self._running = False
        self._enabled = bool(private_key)

        # ------------------------------------------------------------------
        # Mode selection: try Relayer first, fall back to direct EOA
        # ------------------------------------------------------------------
        self._use_relayer = False
        self._relay_client = None  # type: ignore[assignment]
        self.w3: Optional[Web3] = None
        self.account = None

        has_builder_creds = all([builder_api_key, builder_secret, builder_passphrase])

        if private_key and has_builder_creds:
            relayer_url = RELAYER_URL_TESTNET if is_testnet else RELAYER_URL
            self._init_relayer(
                private_key, builder_api_key, builder_secret,  # type: ignore[arg-type]
                builder_passphrase, relayer_url, relay_tx_type, rpc_url,  # type: ignore[arg-type]
            )

        if not self._use_relayer:
            # Fall back to direct EOA signing
            self._init_eoa(rpc_url, private_key)

        # Shared: ABI-only contract for encoding calldata (no provider needed)
        self._ctf_for_abi = Web3().eth.contract(
            address=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS),
            abi=CTF_ABI,
        )

        # Initialize notifier
        settings = get_settings()
        self.notifier = create_notifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            settings.wechat_webhook_url,
        )

        mode = "Relayer" if self._use_relayer else "EOA"
        address = (
            self.account.address
            if self.account
            else (self._relay_client and "relayer-managed")
            or "N/A"
        )
        logger.info(
            "Position settler initialized",
            mode=mode,
            address=address,
            enabled=self._enabled,
            min_merge=min_merge_amount,
            interval=merge_interval,
        )

    # ------------------------------------------------------------------
    # Initializers
    # ------------------------------------------------------------------

    def _init_relayer(
        self,
        private_key: str,
        api_key: str,
        secret: str,
        passphrase: str,
        relayer_url: str,
        relay_tx_type: str,
        rpc_url: str,
    ) -> None:
        """Try to initialise the Relayer client.  Sets ``_use_relayer``."""
        try:
            from py_builder_relayer_client import RelayClient
            from py_builder_relayer_client.models import RelayerTxType
            from py_builder_signing_sdk.config import BuilderConfig

            builder_config = BuilderConfig(
                local_builder_creds={
                    "key": api_key,
                    "secret": secret,
                    "passphrase": passphrase,
                }
            )

            tx_type = (
                RelayerTxType.PROXY
                if relay_tx_type.upper() == "PROXY"
                else RelayerTxType.SAFE
            )

            self._relay_client = RelayClient(
                relayer_url=relayer_url,
                chain_id=POLYGON_CHAIN_ID,
                private_key=private_key,
                builder_config=builder_config,
                relay_tx_type=tx_type,
                rpc_url=rpc_url,
            )
            self._use_relayer = True
            logger.info(
                "Relayer client initialised",
                url=relayer_url,
                wallet_type=tx_type.value,
            )
        except ImportError:
            logger.warning(
                "py-builder-relayer-client not installed – falling back to direct EOA. "
                "Install with: pip install py-builder-relayer-client py-builder-signing-sdk"
            )
        except Exception as e:
            logger.warning("Relayer init failed, falling back to EOA", error=str(e))

    def _init_eoa(self, rpc_url: str, private_key: Optional[str]) -> None:
        """Initialise Web3 + account for direct on-chain signing."""
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)

        if private_key:
            try:
                self.account = self.w3.eth.account.from_key(private_key)
            except Exception as e:
                logger.error("Invalid private key, merging disabled", error=str(e))
                self._enabled = False

        # CTF contract bound to provider (needed for build_transaction)
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS),
            abi=CTF_ABI,
        )

    async def start(self, account_state: AccountState) -> None:
        """
        Start the automatic merge loop.

        Args:
            account_state: Account state to monitor
        """
        self._running = True
        logger.info("Starting position settler", enabled=self._enabled)

        if not self._enabled:
            logger.warning("Position merging disabled: No private key provided")
            return

        while self._running:
            try:
                await self._check_and_merge(account_state)
            except Exception as e:
                logger.error("Merge loop error", error=str(e))

            await asyncio.sleep(self.merge_interval)

    async def stop(self) -> None:
        """Stop the merge loop."""
        self._running = False

    async def _check_and_merge(self, account_state: AccountState) -> None:
        """Check positions and merge if conditions are met."""
        total_mergeable = account_state.total_mergeable_value

        if total_mergeable < self.min_merge_amount:
            logger.debug(
                "Below merge threshold",
                mergeable=total_mergeable,
                threshold=self.min_merge_amount,
            )
            return

        logger.info(
            "Merging positions",
            total_mergeable=f"${total_mergeable:.2f}",
            num_positions=len(account_state.positions),
        )

        merged_total = 0.0
        for condition_id, position in list(account_state.positions.items()):
            if position.can_merge:
                success = await self._merge_position(position)
                if success:
                    merged_amount = position.mergeable_amount
                    account_state.clear_merged_position(condition_id, merged_amount)
                    merged_total += merged_amount

        if merged_total > 0:
            await self.notifier.send_alert(
                "Positions Merged",
                f"Successfully merged ${merged_total:.2f} USDC",
            )

    async def _merge_position(self, position: Position) -> bool:
        """
        Merge a single position.  Routes to Relayer or direct EOA.

        Args:
            position: Position to merge

        Returns:
            True if merge was successful
        """
        if self._use_relayer:
            return await self._merge_via_relayer(position)
        return await self._merge_via_eoa(position)

    # ------------------------------------------------------------------
    # Relayer path (gasless)
    # ------------------------------------------------------------------

    async def _merge_via_relayer(self, position: Position) -> bool:
        """Merge via Polymarket Relayer API (zero-gas meta-transaction)."""
        from py_builder_relayer_client.models import Transaction as RelayerTransaction

        amount = position.mergeable_amount
        amount_wei = int(amount * 1e6)  # USDC 6 decimals

        condition_id_hex = position.condition_id
        if condition_id_hex.startswith("0x"):
            condition_id_hex = condition_id_hex[2:]

        try:
            # Encode calldata (no provider required)
            calldata = self._ctf_for_abi.encodeABI(
                fn_name="mergePositions",
                args=[
                    Web3.to_checksum_address(USDC_ADDRESS),
                    bytes(32),  # parentCollectionId (root)
                    bytes.fromhex(condition_id_hex),
                    [1, 2],  # partition
                    amount_wei,
                ],
            )

            txn = RelayerTransaction(
                to=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS),
                data=calldata,
                value="0",
            )

            # RelayClient.execute / response.wait are synchronous (requests)
            def _execute():
                response = self._relay_client.execute(
                    [txn],
                    f"Merge ${amount:.2f} USDC",
                )
                result = response.wait()  # polls until MINED / CONFIRMED
                return response, result

            response, result = await asyncio.to_thread(_execute)

            if result and result.get("state") in (
                "STATE_MINED",
                "STATE_CONFIRMED",
            ):
                tx_hash = result.get(
                    "transactionHash",
                    getattr(response, "transaction_hash", "unknown"),
                )
                logger.info(
                    "Merge via Relayer successful",
                    tx_hash=tx_hash,
                    amount=amount,
                    condition_id=position.condition_id[:16] + "…",
                )
                return True

            state = result.get("state") if result else "no_result"
            logger.error(
                "Merge via Relayer failed",
                state=state,
                condition_id=position.condition_id[:16] + "…",
            )
            return False

        except Exception as e:
            logger.error(
                "Relayer merge error",
                error=str(e),
                condition_id=position.condition_id,
            )
            return False

    # ------------------------------------------------------------------
    # Direct EOA path (legacy fallback)
    # ------------------------------------------------------------------

    async def _merge_via_eoa(self, position: Position) -> bool:
        """Merge via direct on-chain transaction (requires POL for gas)."""
        amount = position.mergeable_amount
        amount_wei = int(amount * 1e6)  # USDC has 6 decimals

        try:
            # All Web3 calls are synchronous and blocking; run them in a
            # thread so we don't stall the asyncio event loop.
            def _build_sign_send():
                partition = [1, 2]
                tx = self.ctf.functions.mergePositions(
                    Web3.to_checksum_address(USDC_ADDRESS),
                    bytes(32),  # parentCollectionId (empty for root)
                    bytes.fromhex(position.condition_id[2:]),  # conditionId
                    partition,
                    amount_wei,
                ).build_transaction({
                    "from": self.account.address,
                    "nonce": self.w3.eth.get_transaction_count(self.account.address),
                    "gas": 200000,
                    "gasPrice": self.w3.eth.gas_price,
                })
                signed_tx = self.w3.eth.account.sign_transaction(tx, self.account.key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                return tx_hash, receipt

            tx_hash, receipt = await asyncio.to_thread(_build_sign_send)

            if receipt["status"] == 1:
                logger.info(
                    "Merge (EOA) successful",
                    tx_hash=tx_hash.hex(),
                    gas_used=receipt["gasUsed"],
                    amount=amount,
                )
                return True
            else:
                logger.error("Merge (EOA) transaction failed", tx_hash=tx_hash.hex())
                return False

        except Exception as e:
            logger.error("EOA merge error", error=str(e), condition_id=position.condition_id)
            return False

    async def force_merge_all(self, account_state: AccountState) -> float:
        """
        Force merge all positions immediately.

        Args:
            account_state: Account state

        Returns:
            Total amount merged
        """
        total_merged = 0.0

        for condition_id, position in list(account_state.positions.items()):
            if position.can_merge:
                if await self._merge_position(position):
                    merged = position.mergeable_amount
                    account_state.clear_merged_position(condition_id, merged)
                    total_merged += merged

        return total_merged


def create_settler() -> PositionSettler:
    """Create a settler from settings.

    Uses Relayer mode when Builder credentials are configured,
    otherwise falls back to direct EOA signing.
    """
    settings = get_settings()
    return PositionSettler(
        rpc_url=settings.rpc_url,
        private_key=settings.private_key,
        min_merge_amount=10.0,
        merge_interval=settings.merge_interval,
        builder_api_key=settings.builder_api_key,
        builder_secret=settings.builder_secret,
        builder_passphrase=settings.builder_passphrase,
        relay_tx_type=settings.relayer_tx_type,
        is_testnet=settings.is_testnet,
    )
