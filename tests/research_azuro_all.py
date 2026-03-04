"""Count all active Azuro games by category."""
import httpx
import time
from collections import Counter
from datetime import datetime

now_ts = str(int(time.time()))

r = httpx.post(
    "https://thegraph.azuro.org/subgraphs/name/azuro-protocol/azuro-api-polygon-v3",
    json={
        "query": """
        query($startsAt_gt: BigInt!) {
          games(
            where: { startsAt_gt: $startsAt_gt, status: Created }
            first: 1000
            orderBy: startsAt
          ) { gameId title startsAt sport { slug name } league { name } participants { name } }
        }""",
        "variables": {"startsAt_gt": now_ts},
    },
    timeout=30,
)
games = r.json().get("data", {}).get("games", [])

sc = Counter(g.get("sport", {}).get("slug", "") for g in games)
print(f"Azuro total active future games: {len(games)}")
for sport, cnt in sc.most_common():
    print(f"  {sport:25s}: {cnt}")

# Show table-tennis and esports samples
print("\n=== NOTABLE NON-COVERED SPORTS ===")
for slug in ["table-tennis", "volleyball", "rugby-league", "rugby-union",
             "dota-2", "cs2", "lol", "politics", "unique"]:
    subset = [g for g in games if g.get("sport", {}).get("slug") == slug]
    if subset:
        print(f"\n{slug}: {len(subset)} games")
        for g in subset[:5]:
            title = g.get("title", "")[:60]
            league = g.get("league", {}).get("name", "")
            parts = [p.get("name", "") for p in g.get("participants", [])]
            ts = int(g.get("startsAt", 0))
            dt = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            print(f"  [{dt}] {title} | {league} | {parts[:2]}")

# Check PM for table-tennis, volleyball, rugby match-level markets
print("\n\n=== PM MATCH-LEVEL CHECK ===")
for tag in ["table-tennis", "volleyball", "rugby"]:
    r2 = httpx.get(
        "https://gamma-api.polymarket.com/events",
        params={"active": "true", "closed": "false", "tag_slug": tag, "limit": "50"},
        timeout=30,
    )
    events = r2.json()
    match_events = [e for e in events if " vs " in (e.get("title", "") or "").lower()
                    or " v " in (e.get("title", "") or "").lower()]
    total_mkts = sum(len(e.get("markets", [])) for e in events)
    print(f"\n{tag}: {len(events)} events, {len(match_events)} match-level, {total_mkts} markets")
    for ev in match_events[:5]:
        title = ev.get("title", "")[:70]
        sd = (ev.get("startDate", "") or "")[:19]
        n = len(ev.get("markets", []))
        print(f"  [{n}m] {title} | start={sd}")
    # Show non-match events too
    for ev in [e for e in events if e not in match_events][:3]:
        title = ev.get("title", "")[:70]
        n = len(ev.get("markets", []))
        print(f"  [{n}m] [season] {title}")

# Check PM esports match-level
print("\n=== PM ESPORTS MATCH-LEVEL ===")
for tag in ["esports", "league-of-legends", "counter-strike", "valorant"]:
    r3 = httpx.get(
        "https://gamma-api.polymarket.com/events",
        params={"active": "true", "closed": "false", "tag_slug": tag, "limit": "50"},
        timeout=30,
    )
    events = r3.json()
    match_events = [e for e in events if " vs " in (e.get("title", "") or "").lower()
                    or " v " in (e.get("title", "") or "").lower()]
    print(f"\n{tag}: {len(events)} events, {len(match_events)} match-level")
    for ev in match_events[:5]:
        title = ev.get("title", "")[:80]
        sd = (ev.get("startDate", "") or "")[:19]
        n = len(ev.get("markets", []))
        qs = [m.get("question", "")[:60] for m in ev.get("markets", [])[:2]]
        print(f"  [{n}m] {title} | start={sd}")
        for q in qs:
            print(f"        Q: {q}")
