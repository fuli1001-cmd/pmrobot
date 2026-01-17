"""Position settlement (merge) module."""

import asyncio
import time
from datetime import datetime
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
from config.constants import CTF_CONTRACT_ADDRESS, POLYGON_CHAIN_ID
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
    """

    def __init__(
        self,
        rpc_url: str,
        private_key: Optional[str] = None,
        min_merge_amount: float = 10.0,
        merge_interval: int = 600,
    ):
        """
        Initialize the position settler.

        Args:
            rpc_url: Polygon RPC URL
            private_key: Wallet private key for signing (optional)
            min_merge_amount: Minimum amount to trigger merge (USDC)
            merge_interval: Seconds between merge attempts
        """
        self.min_merge_amount = min_merge_amount
        self.merge_interval = merge_interval
        self._last_merge_time = 0.0
        self._running = False
        self._enabled = bool(private_key)

        # Initialize Web3
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)

        # Set up account if private key provided
        self.account = None
        if private_key:
            try:
                self.account = self.w3.eth.account.from_key(private_key)
            except Exception as e:
                logger.error("Invalid private key, merging disabled", error=str(e))
                self._enabled = False

        # Initialize CTF contract
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS),
            abi=CTF_ABI,
        )

        # Initialize notifier
        settings = get_settings()
        self.notifier = create_notifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        )

        logger.info(
            "Position settler initialized",
            address=self.account.address if self.account else "N/A",
            enabled=self._enabled,
            min_merge=min_merge_amount,
            interval=merge_interval,
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
        Merge a single position.

        Args:
            position: Position to merge

        Returns:
            True if merge was successful
        """
        amount = position.mergeable_amount
        amount_wei = int(amount * 1e6)  # USDC has 6 decimals

        try:
            # Build transaction
            # Partition for binary market: [1, 2] represents Yes and No
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

            # Sign and send
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.account.key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)

            logger.info(
                "Merge transaction sent",
                tx_hash=tx_hash.hex(),
                amount=amount,
            )

            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt["status"] == 1:
                logger.info(
                    "Merge successful",
                    tx_hash=tx_hash.hex(),
                    gas_used=receipt["gasUsed"],
                )
                return True
            else:
                logger.error("Merge transaction failed", tx_hash=tx_hash.hex())
                return False

        except Exception as e:
            logger.error("Merge error", error=str(e), condition_id=position.condition_id)
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
    """Create a settler from settings."""
    settings = get_settings()
    return PositionSettler(
        rpc_url=settings.rpc_url,
        private_key=settings.private_key,
        min_merge_amount=10.0,
        merge_interval=settings.merge_interval,
    )
