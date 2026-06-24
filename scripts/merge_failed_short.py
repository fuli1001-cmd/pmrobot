#!/usr/bin/env python3
"""Merge YES+NO tokens left after a failed short-arb sell.

The script defaults to preview mode. Pass ``--execute`` to submit the merge.
You can identify the market by slug, or pass condition/token ids explicitly.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


ERC1155_ABI = [
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSet", "type": "uint256"},
        ],
        "name": "getCollectionId",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "collectionId", "type": "bytes32"},
        ],
        "name": "getPositionId",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "pure",
        "type": "function",
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge paired Polymarket CTF tokens left by failed short-arb sells."
    )
    parser.add_argument("--slug", help="Gamma market slug to resolve condition/token ids")
    parser.add_argument("--condition-id", help="CTF condition id")
    parser.add_argument("--yes-token-id", help="YES outcome token id")
    parser.add_argument("--no-token-id", help="NO outcome token id")
    parser.add_argument(
        "--amount",
        type=float,
        help="Amount to merge in USDC/token units. Defaults to min(YES, NO) wallet balance.",
    )
    parser.add_argument(
        "--wallet",
        help="Wallet that holds the tokens. Defaults to PROXY_WALLET_ADDRESS, then signer EOA.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit the merge transaction. Without this flag, only prints a preview.",
    )
    return parser.parse_args()


def resolve_market_by_slug(slug: str) -> Tuple[str, str, str]:
    from polymarket import PublicClient

    with PublicClient() as client:
        market = client.get_market(slug=slug)

    condition_id = market.condition_id
    yes_token_id = market.outcomes.yes.token_id
    no_token_id = market.outcomes.no.token_id

    if not condition_id or not yes_token_id or not no_token_id:
        raise RuntimeError(f"Market {slug!r} does not expose a binary condition/token pair")

    return str(condition_id), str(yes_token_id), str(no_token_id)


def signer_address(private_key: Optional[str]) -> Optional[str]:
    from web3 import Web3

    if not private_key:
        return None
    return Web3().eth.account.from_key(private_key).address


def token_balance(w3, wallet: str, token_id: str) -> Tuple[float, int]:
    from config.constants import CTF_CONTRACT_ADDRESS
    from web3 import Web3

    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS),
        abi=ERC1155_ABI,
    )
    raw = ctf.functions.balanceOf(
        Web3.to_checksum_address(wallet),
        int(token_id),
    ).call()
    return raw / 1e6, raw


def ctf_position_token_ids(w3, condition_id: str, collateral_token: str) -> Tuple[str, str]:
    from config.constants import CTF_CONTRACT_ADDRESS
    from web3 import Web3

    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS),
        abi=ERC1155_ABI,
    )
    cid_hex = condition_id[2:] if condition_id.startswith("0x") else condition_id
    condition_bytes = bytes.fromhex(cid_hex)
    collateral = Web3.to_checksum_address(collateral_token)
    yes_collection = ctf.functions.getCollectionId(bytes(32), condition_bytes, 1).call()
    no_collection = ctf.functions.getCollectionId(bytes(32), condition_bytes, 2).call()
    yes_position = ctf.functions.getPositionId(collateral, yes_collection).call()
    no_position = ctf.functions.getPositionId(collateral, no_collection).call()
    return str(yes_position), str(no_position)


def print_wallet_positions(wallet: str, condition_id: str) -> None:
    from polymarket import PublicClient

    def describe(position) -> str:
        size = position.size if position.size is not None else "?"
        value = position.current_value if position.current_value is not None else "?"
        outcome = position.outcome or "?"
        token_id = str(position.token_id) if position.token_id else "?"
        condition = str(position.condition_id) if position.condition_id else "?"
        return (
            f"  condition={condition} outcome={outcome} "
            f"size={size} value={value} token={token_id}"
        )

    try:
        with PublicClient() as client:
            current = client.list_positions(
                user=wallet,
                market=[condition_id],
                size_threshold=0.0,
                page_size=20,
            ).first_page().items
            if current:
                print("Open positions for requested condition:")
                for position in current:
                    print(describe(position))
                return

            all_positions = client.list_positions(
                user=wallet,
                size_threshold=0.0,
                page_size=20,
            ).first_page().items
    except Exception as exc:
        print(f"Could not query Polymarket positions: {exc!r}")
        return

    if not all_positions:
        print("No open Polymarket positions found for this wallet.")
        return

    print("No position found for requested condition. Other open positions:")
    for position in all_positions[:10]:
        print(describe(position))


async def main() -> int:
    args = parse_args()

    try:
        from config.settings import get_settings
        from core.settler import PositionSettler
        from models.position import Position
        from web3 import Web3
    except ImportError as exc:
        raise SystemExit(
            "This script requires the project environment with Python 3.11+ and "
            "pydantic v2. Activate the pmrobot env and install requirements."
        ) from exc

    settings = get_settings()

    if args.slug:
        condition_id, yes_token_id, no_token_id = resolve_market_by_slug(args.slug)
    else:
        condition_id = args.condition_id
        yes_token_id = args.yes_token_id
        no_token_id = args.no_token_id

    if not condition_id or not yes_token_id or not no_token_id:
        raise SystemExit(
            "Provide either --slug, or all of --condition-id --yes-token-id --no-token-id."
        )

    rpc_url = (
        settings.polygon_testnet_rpc_url
        if settings.is_testnet
        else settings.polygon_rpc_url
    )
    signer = signer_address(settings.private_key)
    wallet = args.wallet or settings.proxy_wallet_address or signer
    if not wallet:
        raise SystemExit("No wallet found. Pass --wallet or configure PRIVATE_KEY.")

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise SystemExit(f"Could not connect to RPC: {rpc_url}")

    collateral_token = settings.ctf_collateral_address
    ctf_yes_token_id, ctf_no_token_id = ctf_position_token_ids(
        w3,
        condition_id,
        collateral_token,
    )

    clob_yes_balance, clob_yes_raw = token_balance(w3, wallet, yes_token_id)
    clob_no_balance, clob_no_raw = token_balance(w3, wallet, no_token_id)
    yes_balance, yes_raw = token_balance(w3, wallet, ctf_yes_token_id)
    no_balance, no_raw = token_balance(w3, wallet, ctf_no_token_id)
    amount = args.amount if args.amount is not None else min(yes_balance, no_balance)

    print(f"wallet:       {wallet}")
    print(f"condition:    {condition_id}")
    print(f"collateral:   {collateral_token}")
    print(f"CLOB YES:     {yes_token_id} balance={clob_yes_balance:.6f} raw={clob_yes_raw}")
    print(f"CLOB NO:      {no_token_id} balance={clob_no_balance:.6f} raw={clob_no_raw}")
    print(f"CTF YES:      {ctf_yes_token_id} balance={yes_balance:.6f} raw={yes_raw}")
    print(f"CTF NO:       {ctf_no_token_id} balance={no_balance:.6f} raw={no_raw}")
    print(f"merge amount: {amount:.6f}")

    if amount <= 0:
        print_wallet_positions(wallet, condition_id)
        raise SystemExit("Nothing mergeable: min(YES, NO) balance is zero.")
    if amount > min(yes_balance, no_balance):
        raise SystemExit("Requested --amount exceeds mergeable YES/NO balance.")

    position = Position(
        condition_id=condition_id,
        yes_token_id=ctf_yes_token_id,
        no_token_id=ctf_no_token_id,
        yes_balance=amount,
        no_balance=amount,
    )

    if not args.execute:
        print("preview only: rerun with --execute to submit the merge transaction.")
        return 0

    settler = PositionSettler(
        rpc_url=rpc_url,
        private_key=settings.private_key,
        min_merge_amount=0.0,
        merge_interval=settings.merge_interval,
        builder_api_key=settings.builder_api_key,
        builder_secret=settings.builder_secret,
        builder_passphrase=settings.builder_passphrase,
        relay_tx_type=settings.relayer_tx_type,
        is_testnet=settings.is_testnet,
        dry_run=False,
        collateral_token_address=collateral_token,
    )
    relayer_active = getattr(settler, "_use_relayer", False)
    if signer and wallet.lower() != signer.lower() and not relayer_active:
        raise SystemExit(
            "Token wallet differs from signer and relayer is not active. "
            "Configure builder relayer credentials or pass an EOA --wallet that holds the tokens."
        )
    success = await settler._merge_position(position)
    print("merge submitted successfully" if success else "merge failed")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
