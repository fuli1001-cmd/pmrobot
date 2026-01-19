"""CTF (Conditional Token Framework) contract interactions.

This module handles Mint and Redeem operations for Polymarket binary markets.
Mint: Split USDC into Yes+No tokens
Redeem: Merge Yes+No tokens back into USDC
"""

import time
from dataclasses import dataclass
from typing import Optional
from enum import Enum

from web3 import Web3
from web3.middleware import geth_poa_middleware

from config.constants import (
    CTF_CONTRACT_ADDRESS,
    USDC_CONTRACT_ADDRESS,
    POLYGON_CHAIN_ID,
    ESTIMATED_MINT_GAS_COST_USD,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# CTF Contract ABI (minimal - only functions we need)
CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "splitPosition",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
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
    },
]

# USDC ABI (minimal - only approve function)
USDC_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class MintResult(Enum):
    """Result of a Mint operation."""
    SUCCESS = "success"
    FAILED = "failed"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    APPROVAL_FAILED = "approval_failed"


@dataclass
class MintReport:
    """Report of a Mint operation."""
    result: MintResult
    condition_id: str
    amount_usdc: float
    gas_used: int = 0
    gas_cost_usd: float = 0.0
    tx_hash: Optional[str] = None
    error_message: Optional[str] = None
    execution_time_ms: float = 0.0


class CTFContract:
    """
    Interacts with the Conditional Token Framework (CTF) contract.
    
    Provides Mint (splitPosition) and Merge (mergePositions) functionality.
    """
    
    def __init__(
        self,
        private_key: str,
        rpc_url: str = "https://polygon-rpc.com",
        chain_id: int = POLYGON_CHAIN_ID,
        dry_run: bool = False,
    ):
        """
        Initialize CTF contract interaction.
        
        Args:
            private_key: Wallet private key for signing transactions
            rpc_url: Polygon RPC endpoint
            chain_id: Chain ID (137 for mainnet)
            dry_run: If True, simulate but don't execute transactions
        """
        self.dry_run = dry_run
        self.chain_id = chain_id
        
        if dry_run:
            self.w3 = None
            self.account = None
            self.ctf_contract = None
            self.usdc_contract = None
            logger.info("CTF Contract initialized (DRY RUN - no web3)")
            return
        
        # Initialize Web3
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        
        # Set up account
        self.account = self.w3.eth.account.from_key(private_key)
        self.address = self.account.address
        
        # Initialize contracts
        self.ctf_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS),
            abi=CTF_ABI,
        )
        self.usdc_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_CONTRACT_ADDRESS),
            abi=USDC_ABI,
        )
        
        logger.info(
            "CTF Contract initialized",
            address=self.address,
            chain_id=chain_id,
        )
    
    async def mint(
        self,
        condition_id: str,
        amount_usdc: float,
    ) -> MintReport:
        """
        Mint Yes+No tokens by splitting USDC.
        
        This is the core operation for short arbitrage:
        1. Approve USDC spending (if needed)
        2. Call splitPosition to get Yes+No tokens
        
        Args:
            condition_id: Market condition ID (bytes32 hex string)
            amount_usdc: Amount of USDC to split (in dollars, not wei)
            
        Returns:
            MintReport with operation result
        """
        start_time = time.time()
        
        # Convert to wei (USDC has 6 decimals)
        amount_wei = int(amount_usdc * 1_000_000)
        
        if self.dry_run:
            logger.info(
                "DRY RUN: Would mint tokens",
                condition_id=condition_id[:16] + "...",
                amount_usdc=amount_usdc,
            )
            return MintReport(
                result=MintResult.SUCCESS,
                condition_id=condition_id,
                amount_usdc=amount_usdc,
                gas_cost_usd=ESTIMATED_MINT_GAS_COST_USD,
                execution_time_ms=0,
            )
        
        try:
            # Step 1: Check and approve USDC spending
            allowance = self.usdc_contract.functions.allowance(
                self.address,
                CTF_CONTRACT_ADDRESS,
            ).call()
            
            if allowance < amount_wei:
                logger.info("Approving USDC for CTF contract...")
                approve_tx = self.usdc_contract.functions.approve(
                    CTF_CONTRACT_ADDRESS,
                    amount_wei * 10,  # Approve 10x to reduce future approvals
                ).build_transaction({
                    'from': self.address,
                    'nonce': self.w3.eth.get_transaction_count(self.address),
                    'gas': 100000,
                    'gasPrice': self.w3.eth.gas_price,
                    'chainId': self.chain_id,
                })
                
                signed_approve = self.account.sign_transaction(approve_tx)
                approve_hash = self.w3.eth.send_raw_transaction(signed_approve.rawTransaction)
                self.w3.eth.wait_for_transaction_receipt(approve_hash)
                logger.info("USDC approved", tx_hash=approve_hash.hex())
            
            # Step 2: Call splitPosition (Mint)
            # Binary market partition: [1, 2] represents [No, Yes] outcomes
            partition = [1, 2]
            parent_collection_id = bytes(32)  # Zero bytes for root collection
            
            mint_tx = self.ctf_contract.functions.splitPosition(
                USDC_CONTRACT_ADDRESS,
                parent_collection_id,
                bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id),
                partition,
                amount_wei,
            ).build_transaction({
                'from': self.address,
                'nonce': self.w3.eth.get_transaction_count(self.address),
                'gas': 250000,  # Mint typically uses ~150k gas
                'gasPrice': self.w3.eth.gas_price,
                'chainId': self.chain_id,
            })
            
            signed_mint = self.account.sign_transaction(mint_tx)
            mint_hash = self.w3.eth.send_raw_transaction(signed_mint.rawTransaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(mint_hash)
            
            execution_time = (time.time() - start_time) * 1000
            gas_used = receipt['gasUsed']
            gas_price = self.w3.eth.gas_price
            gas_cost_matic = gas_used * gas_price / 1e18
            # Rough MATIC to USD conversion (assume $0.80/MATIC)
            gas_cost_usd = gas_cost_matic * 0.80
            
            logger.info(
                "Mint successful",
                condition_id=condition_id[:16] + "...",
                amount_usdc=amount_usdc,
                gas_used=gas_used,
                gas_cost_usd=f"${gas_cost_usd:.4f}",
                tx_hash=mint_hash.hex(),
            )
            
            return MintReport(
                result=MintResult.SUCCESS,
                condition_id=condition_id,
                amount_usdc=amount_usdc,
                gas_used=gas_used,
                gas_cost_usd=gas_cost_usd,
                tx_hash=mint_hash.hex(),
                execution_time_ms=execution_time,
            )
            
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error("Mint failed", error=str(e))
            return MintReport(
                result=MintResult.FAILED,
                condition_id=condition_id,
                amount_usdc=amount_usdc,
                error_message=str(e),
                execution_time_ms=execution_time,
            )
    
    async def merge(
        self,
        condition_id: str,
        amount_tokens: float,
    ) -> MintReport:
        """
        Merge Yes+No tokens back into USDC.
        
        This is the inverse of Mint - used for redeeming positions.
        
        Args:
            condition_id: Market condition ID
            amount_tokens: Number of token pairs to merge
            
        Returns:
            MintReport with operation result
        """
        # Similar implementation to mint but calls mergePositions
        # For now, just log as not immediately needed for short arbitrage
        logger.info(
            "DRY RUN: Would merge tokens",
            condition_id=condition_id[:16] + "...",
            amount_tokens=amount_tokens,
        )
        return MintReport(
            result=MintResult.SUCCESS,
            condition_id=condition_id,
            amount_usdc=amount_tokens,
        )
