"""CTF (Conditional Token Framework) contract interactions.

This module handles Mint and Redeem operations for Polymarket binary markets.
Mint: Split USDC into Yes+No tokens
Redeem: Merge Yes+No tokens back into USDC

All blocking Web3 calls are wrapped with ``asyncio.to_thread`` so the
asyncio event loop is never stalled.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Optional
from enum import Enum
from urllib import error as urlerror
from urllib import parse, request

from web3 import Web3

# web3.py v6+ compatibility
try:
    from web3.middleware import ExtraDataToPOAMiddleware as geth_poa_middleware
except ImportError:
    try:
        from web3.middleware.geth import geth_poa_middleware
    except ImportError:
        from web3.middleware import geth_poa_middleware

from config.constants import (
    CTF_CONTRACT_ADDRESS,
    USDC_CONTRACT_ADDRESS,
    POLYGON_CHAIN_ID,
    ESTIMATED_MINT_GAS_COST_USD,
    RELAYER_URL,
    RELAYER_URL_TESTNET,
)
from utils.logger import get_logger
from utils.web3_compat import encode_contract_call, get_raw_transaction

logger = get_logger(__name__)

RELAYER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)

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
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
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
    relayer_transaction_id: Optional[str] = None
    relayer_state: Optional[str] = None
    relayer_tx_type: Optional[str] = None
    proxy_wallet: Optional[str] = None
    signer_address: Optional[str] = None
    collateral_token: Optional[str] = None
    collateral_balance_wei: Optional[int] = None
    collateral_allowance_wei: Optional[int] = None


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
        proxy_wallet: Optional[str] = None,
        relayer_api_key: Optional[str] = None,
        relayer_api_key_address: Optional[str] = None,
        relayer_tx_type: str = "SAFE",
        collateral_token_address: str = USDC_CONTRACT_ADDRESS,
        is_testnet: bool = False,
        dry_run: bool = False,
    ):
        """
        Initialize CTF contract interaction.
        
        Args:
            private_key: Wallet private key for signing transactions
            rpc_url: Polygon RPC endpoint
            chain_id: Chain ID (137 for mainnet)
            proxy_wallet: Polymarket proxy wallet address for relayer execution
            relayer_api_key: Polymarket Relayer API key
            relayer_api_key_address: Address that owns the Relayer API key
            relayer_tx_type: Relayer wallet type, "SAFE" or "PROXY"
            collateral_token_address: Token used as CTF collateral
            is_testnet: Use staging relayer endpoint
            dry_run: If True, simulate but don't execute transactions
        """
        self.dry_run = dry_run
        self.chain_id = chain_id
        self.proxy_wallet = Web3.to_checksum_address(proxy_wallet) if proxy_wallet else None
        self.relayer_api_key = relayer_api_key
        self.relayer_api_key_address = (
            Web3.to_checksum_address(relayer_api_key_address)
            if relayer_api_key_address
            else None
        )
        self.collateral_token_address = Web3.to_checksum_address(collateral_token_address)
        self.relayer_tx_type = relayer_tx_type.upper()
        self.relayer_url = RELAYER_URL_TESTNET if is_testnet else RELAYER_URL
        self._use_relayer_mint = False
        
        if dry_run:
            self.w3 = None
            self.account = None
            self.ctf_contract = None
            self.usdc_contract = None
            self.signer_address = None
            self.address = self.proxy_wallet
            logger.info("CTF Contract initialized (DRY RUN - no web3)")
            return
        
        # Initialize Web3
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        
        # Set up account
        self.account = self.w3.eth.account.from_key(private_key)
        self.signer_address = Web3.to_checksum_address(self.account.address)
        
        # Initialize contracts
        self.ctf_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS),
            abi=CTF_ABI,
        )
        self.usdc_contract = self.w3.eth.contract(
            address=self.collateral_token_address,
            abi=USDC_ABI,
        )

        has_relayer_config = all(
            [self.proxy_wallet, self.relayer_api_key, self.relayer_api_key_address]
        )
        if has_relayer_config:
            if self.relayer_api_key_address != self.signer_address:
                logger.error(
                    "Relayer signer mismatch; proxy mint disabled",
                    signer=self.signer_address,
                    relayer_key_address=self.relayer_api_key_address,
                )
            elif self.relayer_tx_type not in ("SAFE", "PROXY"):
                logger.error(
                    "Invalid relayer transaction type; proxy mint disabled",
                    relayer_tx_type=self.relayer_tx_type,
                )
            else:
                self._use_relayer_mint = True

        self.address = self.proxy_wallet if self._use_relayer_mint else self.signer_address
        
        logger.info(
            "CTF Contract initialized",
            address=self.address,
            signer=self.signer_address,
            chain_id=chain_id,
            mint_mode="Relayer" if self._use_relayer_mint else "EOA",
            relayer_tx_type=self.relayer_tx_type if self._use_relayer_mint else None,
            collateral_token=self.collateral_token_address,
        )

    def _relayer_headers(self) -> dict:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": RELAYER_USER_AGENT,
            "RELAYER_API_KEY": self.relayer_api_key or "",
            "RELAYER_API_KEY_ADDRESS": self.relayer_api_key_address or "",
        }

    def _relayer_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        url = self.relayer_url.rstrip("/") + path
        if params:
            query = parse.urlencode(params)
            url = f"{url}?{query}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(
            url,
            data=data,
            headers=self._relayer_headers(),
            method=method,
        )
        try:
            with request.urlopen(req, timeout=30) as resp:
                payload = resp.read().decode("utf-8")
                return json.loads(payload) if payload else {}
        except urlerror.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Relayer {method} {path} failed: status={e.code} body={body}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"Relayer {method} {path} failed: {e}") from e

    def _get_relayer_nonce(self) -> str:
        resp = self._relayer_request(
            "GET",
            "/nonce",
            params={
                "address": self.signer_address,
                "type": self.relayer_tx_type,
            },
        )
        nonce = resp.get("nonce")
        if nonce is None:
            raise RuntimeError(f"Relayer nonce response missing nonce: {resp}")
        return str(nonce)

    def _get_relayer_transaction(self, transaction_id: str) -> Optional[dict]:
        resp = self._relayer_request(
            "GET",
            "/transaction",
            params={"id": transaction_id},
        )
        if isinstance(resp, list):
            return resp[0] if resp else None
        if isinstance(resp, dict) and "transactions" in resp:
            txns = resp.get("transactions") or []
            return txns[0] if txns else None
        return resp if isinstance(resp, dict) else None

    def _wait_relayer_transaction(self, transaction_id: str) -> dict:
        terminal_success = {"STATE_MINED", "STATE_CONFIRMED"}
        terminal_failure = {"STATE_FAILED", "STATE_INVALID"}

        last_txn: Optional[dict] = None
        for _ in range(20):
            time.sleep(3)
            txn = self._get_relayer_transaction(transaction_id)
            if not txn:
                continue
            last_txn = txn
            state = txn.get("state")
            if state in terminal_success:
                return txn
            if state in terminal_failure:
                return txn

        return last_txn or {
            "state": "TIMEOUT",
            "transactionID": transaction_id,
        }

    @staticmethod
    def _pack_safe_signature(signature_hex: str) -> str:
        sig = signature_hex[2:] if signature_hex.startswith("0x") else signature_hex
        if len(sig) != 130:
            raise RuntimeError(f"Unexpected signature length: {len(sig)}")

        v = int(sig[128:130], 16)
        if v in (0, 1):
            v += 31
        elif v in (27, 28):
            v += 4
        else:
            raise RuntimeError(f"Invalid signature v: {v}")

        return "0x" + sig[:128] + f"{v:02x}"

    def _safe_tx_hash(
        self,
        to: str,
        data: str,
        operation: int,
        nonce: str,
    ) -> bytes:
        from eth_abi import encode

        zero_address = "0x0000000000000000000000000000000000000000"
        domain_type_hash = Web3.keccak(
            text="EIP712Domain(uint256 chainId,address verifyingContract)"
        )
        safe_tx_type_hash = Web3.keccak(
            text=(
                "SafeTx(address to,uint256 value,bytes data,uint8 operation,"
                "uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,"
                "address gasToken,address refundReceiver,uint256 nonce)"
            )
        )
        domain_separator = Web3.keccak(
            encode(
                ["bytes32", "uint256", "address"],
                [domain_type_hash, self.chain_id, self.proxy_wallet],
            )
        )
        struct_hash = Web3.keccak(
            encode(
                [
                    "bytes32",
                    "address",
                    "uint256",
                    "bytes32",
                    "uint8",
                    "uint256",
                    "uint256",
                    "uint256",
                    "address",
                    "address",
                    "uint256",
                ],
                [
                    safe_tx_type_hash,
                    Web3.to_checksum_address(to),
                    0,
                    Web3.keccak(hexstr=data),
                    operation,
                    0,
                    0,
                    0,
                    zero_address,
                    zero_address,
                    int(nonce),
                ],
            )
        )
        return Web3.keccak(b"\x19\x01" + domain_separator + struct_hash)

    def _sign_safe_request(self, to: str, data: str, nonce: str) -> str:
        from eth_account.messages import encode_defunct

        tx_hash = self._safe_tx_hash(to=to, data=data, operation=0, nonce=nonce)
        signed = self.account.sign_message(encode_defunct(hexstr="0x" + tx_hash.hex()))
        return self._pack_safe_signature(signed.signature.hex())

    def _build_safe_relayer_request(
        self,
        to: str,
        data: str,
        nonce: str,
        metadata: str,
    ) -> dict:
        signature = self._sign_safe_request(to=to, data=data, nonce=nonce)
        return {
            "from": self.signer_address,
            "to": Web3.to_checksum_address(to),
            "proxyWallet": self.proxy_wallet,
            "data": data,
            "nonce": nonce,
            "signature": signature,
            "signatureParams": {
                "gasPrice": "0",
                "operation": "0",
                "safeTxnGas": "0",
                "baseGas": "0",
                "gasToken": "0x0000000000000000000000000000000000000000",
                "refundReceiver": "0x0000000000000000000000000000000000000000",
            },
            "type": "SAFE",
            "metadata": metadata,
        }

    async def _execute_relayer_transaction(
        self,
        to: str,
        data: str,
        metadata: str,
    ) -> dict:
        if self.relayer_tx_type != "SAFE":
            raise RuntimeError("Relayer API key mint currently supports SAFE tx type only")

        def _execute() -> dict:
            nonce = self._get_relayer_nonce()
            body = self._build_safe_relayer_request(to=to, data=data, nonce=nonce, metadata=metadata)
            submit_resp = self._relayer_request("POST", "/submit", body=body)
            transaction_id = submit_resp.get("transactionID")
            if not transaction_id:
                raise RuntimeError(f"Relayer submit response missing transactionID: {submit_resp}")

            logger.info(
                "Relayer transaction submitted",
                transaction_id=transaction_id,
                state=submit_resp.get("state"),
                metadata=metadata,
            )
            final = self._wait_relayer_transaction(str(transaction_id))
            final.setdefault("transactionID", transaction_id)
            return final

        return await asyncio.to_thread(_execute)

    async def _mint_via_relayer(
        self,
        condition_id: str,
        amount_usdc: float,
        amount_wei: int,
        start_time: float,
    ) -> MintReport:
        condition_id_hex = condition_id[2:] if condition_id.startswith("0x") else condition_id

        try:
            def _get_collateral_state():
                return (
                    self.usdc_contract.functions.balanceOf(self.proxy_wallet).call(),
                    self.usdc_contract.functions.allowance(
                        self.proxy_wallet,
                        CTF_CONTRACT_ADDRESS,
                    ).call(),
                )

            collateral_balance, allowance = await asyncio.to_thread(_get_collateral_state)

            if collateral_balance < amount_wei:
                execution_time = (time.time() - start_time) * 1000
                logger.error(
                    "Relayer mint preflight failed: insufficient proxy collateral",
                    condition_id=condition_id[:16] + "...",
                    amount_usdc=amount_usdc,
                    required_wei=amount_wei,
                    collateral_balance_wei=collateral_balance,
                    collateral_token=self.collateral_token_address,
                    proxy_wallet=self.proxy_wallet,
                    signer=self.signer_address,
                    relayer_tx_type=self.relayer_tx_type,
                )
                return MintReport(
                    result=MintResult.INSUFFICIENT_BALANCE,
                    condition_id=condition_id,
                    amount_usdc=amount_usdc,
                    error_message=(
                        "Insufficient proxy collateral for mint "
                        f"required_wei={amount_wei} balance_wei={collateral_balance} "
                        f"collateral={self.collateral_token_address}"
                    ),
                    execution_time_ms=execution_time,
                    relayer_tx_type=self.relayer_tx_type,
                    proxy_wallet=self.proxy_wallet,
                    signer_address=self.signer_address,
                    collateral_token=self.collateral_token_address,
                    collateral_balance_wei=collateral_balance,
                    collateral_allowance_wei=allowance,
                )

            if allowance < amount_wei:
                approve_data = encode_contract_call(
                    self.usdc_contract,
                    "approve",
                    [
                        Web3.to_checksum_address(CTF_CONTRACT_ADDRESS),
                        amount_wei * 10,
                    ],
                )
                approve_txn = await self._execute_relayer_transaction(
                    to=self.collateral_token_address,
                    data=approve_data,
                    metadata=f"Approve CTF for short mint ${amount_usdc:.2f}",
                )
                approve_state = approve_txn.get("state")
                if approve_state not in ("STATE_MINED", "STATE_CONFIRMED"):
                    raise RuntimeError(
                        "Relayer approval failed "
                        f"state={approve_state} tx={approve_txn.get('transactionHash')}"
                    )

            split_data = encode_contract_call(
                self.ctf_contract,
                "splitPosition",
                [
                    self.collateral_token_address,
                    bytes(32),
                    bytes.fromhex(condition_id_hex),
                    [1, 2],
                    amount_wei,
                ],
            )
            mint_txn = await self._execute_relayer_transaction(
                to=CTF_CONTRACT_ADDRESS,
                data=split_data,
                metadata=f"Short mint ${amount_usdc:.2f}",
            )
            state = mint_txn.get("state")
            tx_hash = mint_txn.get("transactionHash") or mint_txn.get("hash")
            transaction_id = mint_txn.get("transactionID")

            execution_time = (time.time() - start_time) * 1000
            if state not in ("STATE_MINED", "STATE_CONFIRMED"):
                logger.error(
                    "Relayer mint failed",
                    state=state,
                    transaction_id=transaction_id,
                    tx_hash=tx_hash,
                    condition_id=condition_id[:16] + "...",
                    amount_usdc=amount_usdc,
                    proxy_wallet=self.proxy_wallet,
                    signer=self.signer_address,
                    relayer_tx_type=self.relayer_tx_type,
                )
                return MintReport(
                    result=MintResult.FAILED,
                    condition_id=condition_id,
                    amount_usdc=amount_usdc,
                    tx_hash=tx_hash,
                    error_message=f"Relayer mint failed state={state}",
                    execution_time_ms=execution_time,
                    relayer_transaction_id=transaction_id,
                    relayer_state=state,
                    relayer_tx_type=self.relayer_tx_type,
                    proxy_wallet=self.proxy_wallet,
                    signer_address=self.signer_address,
                    collateral_token=self.collateral_token_address,
                    collateral_balance_wei=collateral_balance,
                    collateral_allowance_wei=allowance,
                )

            logger.info(
                "Relayer mint successful",
                state=state,
                transaction_id=transaction_id,
                tx_hash=tx_hash,
                condition_id=condition_id[:16] + "...",
                amount_usdc=amount_usdc,
                proxy_wallet=self.proxy_wallet,
                signer=self.signer_address,
                relayer_tx_type=self.relayer_tx_type,
            )
            return MintReport(
                result=MintResult.SUCCESS,
                condition_id=condition_id,
                amount_usdc=amount_usdc,
                gas_cost_usd=0.0,
                tx_hash=tx_hash,
                execution_time_ms=execution_time,
                relayer_transaction_id=transaction_id,
                relayer_state=state,
                relayer_tx_type=self.relayer_tx_type,
                proxy_wallet=self.proxy_wallet,
                signer_address=self.signer_address,
                collateral_token=self.collateral_token_address,
                collateral_balance_wei=collateral_balance,
                collateral_allowance_wei=allowance,
            )

        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(
                "Relayer mint exception",
                error=repr(e),
                condition_id=condition_id[:16] + "...",
                amount_usdc=amount_usdc,
                proxy_wallet=self.proxy_wallet,
                signer=self.signer_address,
                relayer_tx_type=self.relayer_tx_type,
            )
            return MintReport(
                result=MintResult.FAILED,
                condition_id=condition_id,
                amount_usdc=amount_usdc,
                error_message=repr(e),
                execution_time_ms=execution_time,
                relayer_tx_type=self.relayer_tx_type,
                proxy_wallet=self.proxy_wallet,
                signer_address=self.signer_address,
                collateral_token=self.collateral_token_address,
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

        All Web3 calls execute inside ``asyncio.to_thread`` to avoid
        blocking the event loop.
        
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

        if self._use_relayer_mint:
            return await self._mint_via_relayer(
                condition_id=condition_id,
                amount_usdc=amount_usdc,
                amount_wei=amount_wei,
                start_time=start_time,
            )
        
        try:
            def _do_mint():
                """Synchronous Web3 mint logic (runs in thread)."""
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
                    approve_hash = self.w3.eth.send_raw_transaction(
                        get_raw_transaction(signed_approve)
                    )
                    approve_receipt = self.w3.eth.wait_for_transaction_receipt(approve_hash)
                    if approve_receipt.get("status") != 1:
                        raise RuntimeError(f"USDC approval reverted: {approve_hash.hex()}")
                    logger.info("USDC approved", tx_hash=approve_hash.hex())
                
                # Step 2: Call splitPosition (Mint)
                partition = [1, 2]
                parent_collection_id = bytes(32)
                cid_hex = condition_id[2:] if condition_id.startswith("0x") else condition_id
                
                mint_tx = self.ctf_contract.functions.splitPosition(
                    self.collateral_token_address,
                    parent_collection_id,
                    bytes.fromhex(cid_hex),
                    partition,
                    amount_wei,
                ).build_transaction({
                    'from': self.address,
                    'nonce': self.w3.eth.get_transaction_count(self.address),
                    'gas': 250000,
                    'gasPrice': self.w3.eth.gas_price,
                    'chainId': self.chain_id,
                })
                
                signed_mint = self.account.sign_transaction(mint_tx)
                mint_hash = self.w3.eth.send_raw_transaction(
                    get_raw_transaction(signed_mint)
                )
                receipt = self.w3.eth.wait_for_transaction_receipt(mint_hash)
                return mint_hash, receipt

            mint_hash, receipt = await asyncio.to_thread(_do_mint)
            
            execution_time = (time.time() - start_time) * 1000
            gas_used = receipt['gasUsed']

            if receipt.get("status") != 1:
                logger.error(
                    "Mint transaction reverted",
                    condition_id=condition_id[:16] + "...",
                    gas_used=gas_used,
                    tx_hash=mint_hash.hex(),
                )
                return MintReport(
                    result=MintResult.FAILED,
                    condition_id=condition_id,
                    amount_usdc=amount_usdc,
                    gas_used=gas_used,
                    tx_hash=mint_hash.hex(),
                    error_message="Mint transaction reverted",
                    execution_time_ms=execution_time,
                )

            # Estimate gas cost in USD (blocking call in thread)
            gas_price = await asyncio.to_thread(lambda: self.w3.eth.gas_price)
            gas_cost_matic = gas_used * gas_price / 1e18
            gas_cost_usd = gas_cost_matic * 0.80  # rough POL→USD
            
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
            logger.error("Mint failed", error=repr(e))
            return MintReport(
                result=MintResult.FAILED,
                condition_id=condition_id,
                amount_usdc=amount_usdc,
                error_message=repr(e),
                execution_time_ms=execution_time,
            )

    async def _merge_via_relayer(
        self,
        condition_id: str,
        amount_tokens: float,
        amount_wei: int,
        start_time: float,
    ) -> MintReport:
        condition_id_hex = condition_id[2:] if condition_id.startswith("0x") else condition_id

        try:
            merge_data = encode_contract_call(
                self.ctf_contract,
                "mergePositions",
                [
                    self.collateral_token_address,
                    bytes(32),
                    bytes.fromhex(condition_id_hex),
                    [1, 2],
                    amount_wei,
                ],
            )
            merge_txn = await self._execute_relayer_transaction(
                to=CTF_CONTRACT_ADDRESS,
                data=merge_data,
                metadata=f"Merge CTF position ${amount_tokens:.2f}",
            )
            state = merge_txn.get("state")
            tx_hash = merge_txn.get("transactionHash") or merge_txn.get("hash")
            transaction_id = merge_txn.get("transactionID")
            execution_time = (time.time() - start_time) * 1000

            if state not in ("STATE_MINED", "STATE_CONFIRMED"):
                logger.error(
                    "Relayer merge failed",
                    state=state,
                    transaction_id=transaction_id,
                    tx_hash=tx_hash,
                    condition_id=condition_id[:16] + "...",
                    amount_tokens=amount_tokens,
                    proxy_wallet=self.proxy_wallet,
                    signer=self.signer_address,
                    relayer_tx_type=self.relayer_tx_type,
                )
                return MintReport(
                    result=MintResult.FAILED,
                    condition_id=condition_id,
                    amount_usdc=amount_tokens,
                    tx_hash=tx_hash,
                    error_message=f"Relayer merge failed state={state}",
                    execution_time_ms=execution_time,
                    relayer_transaction_id=transaction_id,
                    relayer_state=state,
                    relayer_tx_type=self.relayer_tx_type,
                    proxy_wallet=self.proxy_wallet,
                    signer_address=self.signer_address,
                    collateral_token=self.collateral_token_address,
                )

            logger.info(
                "Relayer merge successful",
                state=state,
                transaction_id=transaction_id,
                tx_hash=tx_hash,
                condition_id=condition_id[:16] + "...",
                amount_tokens=amount_tokens,
                proxy_wallet=self.proxy_wallet,
                signer=self.signer_address,
                relayer_tx_type=self.relayer_tx_type,
            )
            return MintReport(
                result=MintResult.SUCCESS,
                condition_id=condition_id,
                amount_usdc=amount_tokens,
                gas_cost_usd=0.0,
                tx_hash=tx_hash,
                execution_time_ms=execution_time,
                relayer_transaction_id=transaction_id,
                relayer_state=state,
                relayer_tx_type=self.relayer_tx_type,
                proxy_wallet=self.proxy_wallet,
                signer_address=self.signer_address,
                collateral_token=self.collateral_token_address,
            )

        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(
                "Relayer merge exception",
                error=repr(e),
                condition_id=condition_id[:16] + "...",
                amount_tokens=amount_tokens,
                proxy_wallet=self.proxy_wallet,
                signer=self.signer_address,
                relayer_tx_type=self.relayer_tx_type,
            )
            return MintReport(
                result=MintResult.FAILED,
                condition_id=condition_id,
                amount_usdc=amount_tokens,
                error_message=repr(e),
                execution_time_ms=execution_time,
                relayer_tx_type=self.relayer_tx_type,
                proxy_wallet=self.proxy_wallet,
                signer_address=self.signer_address,
                collateral_token=self.collateral_token_address,
            )
    
    async def merge(
        self,
        condition_id: str,
        amount_tokens: float,
    ) -> MintReport:
        """
        Merge Yes+No tokens back into USDC.

        This is the inverse of Mint – calls ``mergePositions`` on the CTF
        contract.  All Web3 calls run in a thread.
        
        Args:
            condition_id: Market condition ID
            amount_tokens: Number of token pairs to merge
            
        Returns:
            MintReport with operation result
        """
        start_time = time.time()
        amount_wei = int(amount_tokens * 1_000_000)

        if self.dry_run:
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

        if self._use_relayer_mint:
            return await self._merge_via_relayer(
                condition_id=condition_id,
                amount_tokens=amount_tokens,
                amount_wei=amount_wei,
                start_time=start_time,
            )

        try:
            def _do_merge():
                partition = [1, 2]
                parent_collection_id = bytes(32)
                cid_hex = condition_id[2:] if condition_id.startswith("0x") else condition_id

                merge_tx = self.ctf_contract.functions.mergePositions(
                    self.collateral_token_address,
                    parent_collection_id,
                    bytes.fromhex(cid_hex),
                    partition,
                    amount_wei,
                ).build_transaction({
                    'from': self.address,
                    'nonce': self.w3.eth.get_transaction_count(self.address),
                    'gas': 200000,
                    'gasPrice': self.w3.eth.gas_price,
                    'chainId': self.chain_id,
                })

                signed = self.account.sign_transaction(merge_tx)
                tx_hash = self.w3.eth.send_raw_transaction(
                    get_raw_transaction(signed)
                )
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                return tx_hash, receipt

            tx_hash, receipt = await asyncio.to_thread(_do_merge)
            execution_time = (time.time() - start_time) * 1000

            if receipt['status'] == 1:
                logger.info(
                    "Merge successful",
                    condition_id=condition_id[:16] + "...",
                    amount=amount_tokens,
                    gas_used=receipt['gasUsed'],
                    tx_hash=tx_hash.hex(),
                )
                return MintReport(
                    result=MintResult.SUCCESS,
                    condition_id=condition_id,
                    amount_usdc=amount_tokens,
                    gas_used=receipt['gasUsed'],
                    tx_hash=tx_hash.hex(),
                    execution_time_ms=execution_time,
                )
            else:
                logger.error("Merge transaction reverted", tx_hash=tx_hash.hex())
                return MintReport(
                    result=MintResult.FAILED,
                    condition_id=condition_id,
                    amount_usdc=amount_tokens,
                    error_message="Transaction reverted",
                    execution_time_ms=execution_time,
                )

        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error("Merge failed", error=repr(e))
            return MintReport(
                result=MintResult.FAILED,
                condition_id=condition_id,
                amount_usdc=amount_tokens,
                error_message=repr(e),
                execution_time_ms=execution_time,
            )
