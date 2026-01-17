"""Debug script to inspect Gamma API response."""

import requests
import json

def debug_api():
    response = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={
            "active": "true",
            "closed": "false",
            "limit": "3",
        },
        timeout=30
    )
    data = response.json()
    
    print(f"Total markets returned: {len(data)}")
    print("=" * 80)
    
    for i, market in enumerate(data[:3]):
        print(f"\n--- Market {i+1} ---")
        print(f"Question: {market.get('question', 'N/A')[:80]}")
        print(f"Slug: {market.get('slug', 'N/A')}")
        print(f"Condition ID: {market.get('condition_id', 'N/A')}")
        print(f"Active: {market.get('active')}")
        print(f"Closed: {market.get('closed')}")
        print(f"Enable Order Book: {market.get('enable_order_book')}")
        print(f"Tags: {market.get('tags', [])}")
        print(f"Liquidity: {market.get('liquidity', 'N/A')}")
        
        # Check tokens structure
        tokens = market.get("tokens", [])
        print(f"Tokens count: {len(tokens)}")
        for t in tokens:
            token_id = str(t.get('token_id', 'N/A'))[:20] if t.get('token_id') else 'N/A'
            print(f"  - Token ID: {token_id}..., Outcome: {t.get('outcome', 'N/A')}")
        
        # Full raw data for first market
        if i == 0:
            print("\n--- Full raw data (first market) ---")
            print(json.dumps(market, indent=2, default=str)[:3000])

if __name__ == "__main__":
    debug_api()
