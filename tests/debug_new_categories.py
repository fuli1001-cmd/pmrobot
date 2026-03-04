"""Debug why new categories get 0 matches - check PM event structure."""
import httpx
import time
import json
from datetime import datetime

PM_API = "https://gamma-api.polymarket.com/events"
now = time.time()

def pm_events(tag_slug, limit=20):
    resp = httpx.get(PM_API, params={"tag_slug": tag_slug, "limit": limit, "active": True}, timeout=30)
    resp.raise_for_status()
    return resp.json()

# Check rugby
print("=" * 70)
print("RUGBY PM EVENTS (first 10)")
print("=" * 70)
events = pm_events("rugby", 10)
for e in events:
    title = e.get("title", "")
    markets = e.get("markets", [])
    print(f"\nEvent: {title}")
    for m in markets[:3]:
        q = m.get("question", "")
        gst = m.get("game_start_time", "")
        end = m.get("end_date", "")
        tags = [t.get("slug", "") for t in m.get("tags", [])] if isinstance(m.get("tags"), list) else m.get("tags", "")
        print(f"  Market: {q[:80]}")
        print(f"    game_start_time={gst}  end_date={end[:19]}")
        print(f"    tags={tags}")
    if len(markets) > 3:
        print(f"  ... and {len(markets)-3} more markets")

# Check counter-strike
print(f"\n\n{'='*70}")
print("COUNTER-STRIKE PM EVENTS (first 10)")
print("=" * 70)
events = pm_events("counter-strike", 10)
for e in events:
    title = e.get("title", "")
    markets = e.get("markets", [])
    print(f"\nEvent: {title}")
    for m in markets[:3]:
        q = m.get("question", "")
        gst = m.get("game_start_time", "")
        end = m.get("end_date", "")
        print(f"  Market: {q[:80]}")
        print(f"    game_start_time={gst}  end_date={end[:19]}")
    if len(markets) > 3:
        print(f"  ... and {len(markets)-3} more markets")

# Check league-of-legends
print(f"\n\n{'='*70}")
print("LEAGUE-OF-LEGENDS PM EVENTS (first 10)")
print("=" * 70)
events = pm_events("league-of-legends", 10)
for e in events:
    title = e.get("title", "")
    markets = e.get("markets", [])
    print(f"\nEvent: {title}")
    for m in markets[:3]:
        q = m.get("question", "")
        gst = m.get("game_start_time", "")
        end = m.get("end_date", "")
        print(f"  Market: {q[:80]}")
        print(f"    game_start_time={gst}  end_date={end[:19]}")
    if len(markets) > 3:
        print(f"  ... and {len(markets)-3} more markets")

# Also show AZ sample for comparison
print(f"\n\n{'='*70}")
print("AZURO RUGBY GAMES (sample titles)")
print("=" * 70)
SUBGRAPH_URL = "https://thegraph-1.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-data-feed-polygon"
resp = httpx.post(SUBGRAPH_URL, json={"query": """
query { games(where: { startsAt_gt: "%d", sport_: { name_contains_nocase: "rugby" } }, first: 10, orderBy: startsAt) {
  gameId title startsAt sport { name } participants { name }
} }""" % int(now)}, timeout=30)
games = resp.json().get("data", {}).get("games", [])
for g in games:
    parts = [p["name"] for p in g.get("participants", [])]
    starts = datetime.fromtimestamp(int(g["startsAt"])).strftime("%Y-%m-%d %H:%M")
    print(f"  {starts}  {' vs '.join(parts)}  [{g.get('sport',{}).get('name','')}]")

print(f"\n\n{'='*70}")
print("AZURO CS2 GAMES (sample titles + startsAt)")
resp = httpx.post(SUBGRAPH_URL, json={"query": """
query { games(where: { startsAt_gt: "%d", sport_: { name_contains_nocase: "Counter-Strike" } }, first: 5, orderBy: startsAt) {
  gameId title startsAt participants { name }
} }""" % int(now)}, timeout=30)
games = resp.json().get("data", {}).get("games", [])
for g in games:
    parts = [p["name"] for p in g.get("participants", [])]
    starts = datetime.fromtimestamp(int(g["startsAt"])).strftime("%Y-%m-%d %H:%M")
    print(f"  {starts}  {' vs '.join(parts)}")

print("\nDone!")
