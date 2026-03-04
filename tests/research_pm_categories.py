"""Check Polymarket match-level events for candidate new categories."""
import httpx
import json

PM_API = "https://gamma-api.polymarket.com/events"

def pm_events(tag_slug: str, limit: int = 50) -> list:
    resp = httpx.get(PM_API, params={"tag_slug": tag_slug, "limit": limit, "active": True}, timeout=30)
    resp.raise_for_status()
    return resp.json()

def is_match_level(event: dict) -> bool:
    """Check if event title looks like match-level (X vs Y pattern)."""
    title = event.get("title", "")
    return " vs " in title.lower() or " v " in title.lower()


# Check each candidate
candidates = ["rugby", "volleyball", "esports", "league-of-legends", "valorant", "counter-strike", "table-tennis"]

for tag in candidates:
    events = pm_events(tag)
    match_events = [e for e in events if is_match_level(e)]
    
    print(f"\n{'='*60}")
    print(f"PM tag: {tag}")
    print(f"  Total events: {len(events)}")
    print(f"  Match-level:  {len(match_events)}")
    
    # Count markets in match-level events
    total_markets = sum(len(e.get("markets", [])) for e in match_events)
    print(f"  Total markets in match events: {total_markets}")
    
    # Show samples
    for e in match_events[:5]:
        markets = e.get("markets", [])
        market_q = markets[0]["question"] if markets else "?"
        print(f"    -> {e['title']}")
        print(f"       market[0]: {market_q}")
        if len(markets) > 1:
            print(f"       market[1]: {markets[1]['question']}")

    # Also show non-match events (season-level)
    non_match = [e for e in events if not is_match_level(e)]
    if non_match:
        print(f"  Season-level examples ({len(non_match)}):")
        for e in non_match[:3]:
            print(f"    -> {e['title']}")

print("\nDone!")
