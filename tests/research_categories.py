"""Research Azuro non-sports categories and Polymarket overlap."""
import httpx
import time

now_ts = str(int(time.time()))

# ── Azuro: Check active events in each category ──
print("=== AZURO ACTIVE GAMES BY CATEGORY ===")
for slug in ["politics", "unique", "dota-2", "csgo", "cs2", "lol",
             "table-tennis", "volleyball", "rugby-league", "rugby-union"]:
    r = httpx.post(
        "https://thegraph.azuro.org/subgraphs/name/azuro-protocol/azuro-api-polygon-v3",
        json={
            "query": """
            query($sport: String, $startsAt_gt: BigInt!) {
              games(
                where: { sport_: { slug: $sport }, startsAt_gt: $startsAt_gt, status: Created }
                first: 200
                orderBy: startsAt
              ) { gameId title startsAt participants { name } sport { slug } league { name } }
            }""",
            "variables": {"sport": slug, "startsAt_gt": now_ts},
        },
        timeout=30,
    )
    data = r.json()
    games = data.get("data", {}).get("games", [])
    print(f"\n{slug}: {len(games)} active games")
    for g in games[:3]:
        title = g.get("title", "")[:70]
        league = g.get("league", {}).get("name", "")
        parts = [p.get("name", "") for p in g.get("participants", [])]
        print(f"  {title}  | league={league} | teams={parts}")

# ── Polymarket: Check politics and esports tags ──
print("\n\n=== POLYMARKET EVENTS BY TAG ===")
for tag in ["politics", "elections", "pop-culture", "entertainment",
            "esports", "gaming", "science", "technology", "crypto"]:
    r2 = httpx.get(
        "https://gamma-api.polymarket.com/events",
        params={"active": "true", "closed": "false", "tag_slug": tag, "limit": "5"},
        timeout=30,
    )
    events = r2.json()
    total_mkts = sum(len(ev.get("markets", [])) for ev in events)
    print(f"\n{tag}: {len(events)} events, ~{total_mkts} markets")
    for ev in events[:3]:
        title = ev.get("title", "")[:70]
        n_mkts = len(ev.get("markets", []))
        print(f"  [{n_mkts}m] {title}")

# ── Polymarket: Check esports specifically ──
print("\n\n=== PM ESPORTS/OTHER SEARCH ===")
for tag in ["esports", "dota", "csgo", "league-of-legends", "counter-strike",
            "valorant", "gaming", "table-tennis", "volleyball", "rugby"]:
    r3 = httpx.get(
        "https://gamma-api.polymarket.com/events",
        params={"active": "true", "closed": "false", "tag_slug": tag, "limit": "5"},
        timeout=30,
    )
    events = r3.json()
    if events:
        total_m = sum(len(ev.get("markets", [])) for ev in events)
        print(f"\n{tag}: {len(events)} events, {total_m} markets")
        for ev in events[:2]:
            title = ev.get("title", "")[:70]
            n = len(ev.get("markets", []))
            print(f"  [{n}m] {title}")
    else:
        print(f"  {tag}: 0 events")
