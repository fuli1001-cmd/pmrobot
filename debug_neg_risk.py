"""Debug script to analyze Negative Risk market structure from Gamma API."""

import requests
import json

def debug_neg_risk_markets():
    """Fetch and analyze Negative Risk markets."""
    
    print("Fetching markets from Gamma API...")
    
    response = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={
            "active": "true",
            "closed": "false",
            "limit": "500",
        },
        timeout=30
    )
    
    markets = response.json()
    print(f"Total markets fetched: {len(markets)}")
    
    # Find Negative Risk markets
    neg_risk_markets = [m for m in markets if m.get("negRisk", False)]
    print(f"Negative Risk markets: {len(neg_risk_markets)}")
    
    if not neg_risk_markets:
        print("No Negative Risk markets found!")
        return
    
    # Group by potential parent (using question prefix or other fields)
    # Let's examine the structure first
    print("\n=== Sample Negative Risk Market Structure ===")
    sample = neg_risk_markets[0]
    for key, value in sample.items():
        if isinstance(value, str) and len(value) > 100:
            print(f"  {key}: {value[:100]}...")
        else:
            print(f"  {key}: {value}")
    
    # Look for grouping fields
    print("\n=== Analyzing Grouping Fields ===")
    
    # Check if there's a groupItemTitle or similar field
    group_fields = set()
    for m in neg_risk_markets:
        if "groupItemTitle" in m:
            group_fields.add(m.get("groupItemTitle"))
        if "parentMarketId" in m:
            group_fields.add(m.get("parentMarketId"))
    
    print(f"Unique groupItemTitle values: {len(group_fields)}")
    
    # Try to find related markets by examining questions
    print("\n=== Sample Questions from Negative Risk Markets ===")
    for i, m in enumerate(neg_risk_markets[:10]):
        q = m.get("question", "")[:80]
        slug = m.get("slug", "")[:40]
        condition_id = m.get("conditionId", "")[:20]
        group_title = m.get("groupItemTitle", "N/A")
        print(f"{i+1}. Q: {q}")
        print(f"   Slug: {slug}")
        print(f"   ConditionID: {condition_id}...")
        print(f"   GroupTitle: {group_title}")
        print()
    
    # Check for events endpoint which might group related markets
    print("\n=== Checking Events Endpoint ===")
    try:
        events_response = requests.get(
            "https://gamma-api.polymarket.com/events",
            params={
                "active": "true",
                "closed": "false", 
                "limit": "10",
            },
            timeout=30
        )
        events = events_response.json()
        print(f"Events fetched: {len(events)}")
        
        if events:
            print("\n=== Sample Event Structure ===")
            sample_event = events[0]
            for key, value in sample_event.items():
                if key == "markets":
                    print(f"  markets: [{len(value)} markets]")
                    if value:
                        print(f"    First market keys: {list(value[0].keys())[:10]}")
                elif isinstance(value, str) and len(value) > 100:
                    print(f"  {key}: {value[:100]}...")
                else:
                    print(f"  {key}: {value}")
            
            # Find events with negRisk markets
            print("\n=== Events with Negative Risk Markets ===")
            for event in events:
                event_markets = event.get("markets", [])
                neg_markets = [m for m in event_markets if m.get("negRisk", False)]
                if neg_markets:
                    print(f"Event: {event.get('title', 'N/A')[:60]}")
                    print(f"  Total outcomes: {len(neg_markets)}")
                    for nm in neg_markets[:5]:
                        print(f"    - {nm.get('groupItemTitle', nm.get('question', ''))[:50]}")
                    if len(neg_markets) > 5:
                        print(f"    ... and {len(neg_markets) - 5} more")
                    print()
                    
    except Exception as e:
        print(f"Events endpoint error: {e}")


if __name__ == "__main__":
    debug_neg_risk_markets()
