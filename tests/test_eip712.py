"""Verify EIP-712 encode_typed_data works with SX Bet nested types."""
from eth_account import Account
from eth_account.messages import encode_typed_data

full = {
    "types": {
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        "Details": [
            {"name": "action", "type": "string"},
            {"name": "market", "type": "string"},
            {"name": "betting", "type": "string"},
            {"name": "stake", "type": "string"},
            {"name": "worstOdds", "type": "string"},
            {"name": "worstReturning", "type": "string"},
            {"name": "fills", "type": "FillObject"},
        ],
        "FillObject": [
            {"name": "stakeWei", "type": "string"},
            {"name": "marketHash", "type": "string"},
            {"name": "baseToken", "type": "string"},
            {"name": "desiredOdds", "type": "string"},
            {"name": "oddsSlippage", "type": "uint256"},
            {"name": "isTakerBettingOutcomeOne", "type": "bool"},
            {"name": "fillSalt", "type": "uint256"},
            {"name": "beneficiary", "type": "address"},
            {"name": "beneficiaryType", "type": "uint8"},
            {"name": "cashOutTarget", "type": "bytes32"},
        ],
    },
    "primaryType": "Details",
    "domain": {
        "name": "SX Bet",
        "version": "6.0",
        "chainId": 4162,
        "verifyingContract": "0x845a2Da2D70fEDe8474b1C8518200798c60aC364",
    },
    "message": {
        "action": "N/A",
        "betting": "N/A",
        "stake": "N/A",
        "worstOdds": "N/A",
        "worstReturning": "N/A",
        "market": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "fills": {
            "stakeWei": "50000000",
            "marketHash": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
            "baseToken": "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B",
            "desiredOdds": "83000000000000000000",
            "oddsSlippage": 5,
            "isTakerBettingOutcomeOne": True,
            "fillSalt": 12345678901234567890,
            "beneficiary": "0x0000000000000000000000000000000000000000",
            "beneficiaryType": 0,
            "cashOutTarget": "0x0000000000000000000000000000000000000000000000000000000000000000",
        },
    },
}

signable = encode_typed_data(full_message=full)
key = "0x40705018fd82e33134f7a439a9aa913c004a2f979caa5c6cd3a337901d742d7e"
signed = Account.sign_message(signable, private_key=key)
print(f"sig: 0x{signed.signature.hex()[:40]}...")
print("OK")
