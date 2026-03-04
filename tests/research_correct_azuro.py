"""Research Azuro categories using the CORRECT data-feed subgraph endpoint.

The bot uses: https://thegraph-1.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-data-feed-polygon
Previous research incorrectly used: https://thegraph.azuro.org/subgraphs/name/azuro-protocol/azuro-api-polygon-v3
"""
import httpx
import time
import json

SUBGRAPH_URL = (
    "https://thegraph-1.onchainfeed.org/subgraphs/name/"
    "azuro-protocol/azuro-data-feed-polygon"
)

def query(gql: str, variables: dict) -> dict:
    resp = httpx.post(SUBGRAPH_URL, json={"query": gql, "variables": variables}, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        print(f"GraphQL errors: {body['errors']}")
    return body.get("data", {})


# 1. Discover all sports on this subgraph
print("=" * 60)
print("1. ALL SPORTS on Azuro data-feed-polygon")
print("=" * 60)

SPORTS_QUERY = """
query AllSports {
  sports(first: 100) {
    name
    slug
    sporthub { name }
  }
}
"""
data = query(SPORTS_QUERY, {})
sports = data.get("sports", [])
for s in sports:
    hub = s.get("sporthub", {}).get("name", "?")
    print(f"  {s['name']:<25} slug={s['slug']:<25} hub={hub}")

print(f"\nTotal sports: {len(sports)}")


# 2. For each sport, count future active games
print("\n" + "=" * 60)
print("2. FUTURE ACTIVE GAMES per sport")
print("=" * 60)

now_ts = int(time.time())
print(f"   now_ts = {now_ts}")

GAMES_COUNT_QUERY = """
query GamesCount($sport: String, $startsAt_gt: BigInt!) {
  games(
    where: {
      startsAt_gt: $startsAt_gt
      sport_: { name_contains_nocase: $sport }
    }
    first: 200
    orderBy: startsAt
    orderDirection: asc
  ) {
    gameId
    title
    startsAt
    sport { name slug }
    league { name slug }
    participants { name }
    conditions(where: { isExpressForbidden: false }) {
      conditionId
      outcomes {
        outcomeId
        currentOdds
      }
      wonOutcomeIds
    }
  }
}
"""

sport_games = {}
for s in sports:
    slug = s["slug"]
    name = s["name"]
    data = query(GAMES_COUNT_QUERY, {"sport": name, "startsAt_gt": str(now_ts)})
    games = data.get("games", [])
    
    # Count games with at least 1 binary condition
    binary_count = 0
    for g in games:
        conds = g.get("conditions", [])
        for c in conds:
            won = c.get("wonOutcomeIds") or []
            if len(won) == 0 and len(c.get("outcomes", [])) == 2:
                binary_count += 1
                break
    
    sport_games[slug] = {"total": len(games), "binary": binary_count, "name": name}
    if len(games) > 0:
        print(f"  {name:<25} total={len(games):>3}  binary={binary_count:>3}")
        # Show sample games
        for g in games[:3]:
            parts = [p["name"] for p in g.get("participants", [])]
            league = g.get("league", {}).get("name", "?")
            print(f"    -> {' vs '.join(parts) or g.get('title','')}  [{league}]")
        if len(games) > 3:
            print(f"    ... and {len(games)-3} more")
    else:
        print(f"  {name:<25} total=  0")

# 3. Cross-reference with PM categories
print("\n" + "=" * 60)
print("3. CROSS-PLATFORM OVERLAP ANALYSIS")
print("=" * 60)

# PM categories from previous research:
# rugby: 49 match-level events
# esports: 25 match-level (LoL 19, Valorant 7, CS 0)
# table-tennis: 0 match-level
# volleyball: 0 match-level

# Current CROSS_SPORT_MAP:
current = {
    "tennis": "tennis",
    "hockey": "ice hockey", 
    "cricket": "cricket",
    "mma": "mma",
    "baseball": "baseball",
    "nfl": "american football",
    "boxing": "boxing",
}

print("\nCurrent sport map (already configured):")
for pm, az in current.items():
    slug_match = [s for s in sports if s["name"].lower() == az.lower()]
    az_slug = slug_match[0]["slug"] if slug_match else "?"
    g = sport_games.get(az_slug, {})
    print(f"  PM:{pm:<12} -> AZ:{az:<20} games={g.get('total',0)}")

print("\nPotential NEW categories to add:")
candidates = [
    ("rugby", ["rugby league", "rugby union"]),
    ("esports", ["dota 2", "cs2", "counter-strike", "csgo", "league of legends", "lol"]),
    ("table-tennis", ["table tennis"]),
    ("volleyball", ["volleyball"]),
    ("politics", ["politics"]),
]

for pm_tag, az_names in candidates:
    print(f"\n  PM tag: {pm_tag}")
    for az_name in az_names:
        slug_match = [s for s in sports if s["name"].lower() == az_name.lower()]
        if slug_match:
            sl = slug_match[0]["slug"]
            g = sport_games.get(sl, {})
            print(f"    AZ: {az_name:<25} slug={sl:<20} games={g.get('total',0)}")
        else:
            # Try partial match
            partial = [s for s in sports if az_name.lower() in s["name"].lower()]
            if partial:
                for p in partial:
                    sl = p["slug"]
                    g = sport_games.get(sl, {})
                    print(f"    AZ: {p['name']:<25} slug={sl:<20} games={g.get('total',0)} (partial match)")
            else:
                print(f"    AZ: {az_name:<25} NOT FOUND on Azuro")


# 4. Also check what games Azuro has with empty sport filter (like the bot does for unknown sports)
print("\n" + "=" * 60)
print("4. ALL FUTURE GAMES (no sport filter, up to 200)")
print("=" * 60)

ALL_GAMES_QUERY = """
query AllFutureGames($startsAt_gt: BigInt!) {
  games(
    where: {
      startsAt_gt: $startsAt_gt
    }
    first: 200
    orderBy: startsAt
    orderDirection: asc
  ) {
    gameId
    title
    startsAt
    sport { name slug }
    league { name slug }
    participants { name }
  }
}
"""
data = query(ALL_GAMES_QUERY, {"startsAt_gt": str(now_ts)})
all_games = data.get("games", [])
print(f"Total future games (no filter): {len(all_games)}")

# Group by sport
from collections import Counter
sport_counter = Counter()
for g in all_games:
    sn = g.get("sport", {}).get("name", "unknown")
    sport_counter[sn] += 1

print("\nBy sport:")
for sport_name, count in sport_counter.most_common():
    print(f"  {sport_name:<25} {count:>3} games")
    # Show sample
    samples = [g for g in all_games if g.get("sport", {}).get("name") == sport_name][:2]
    for g in samples:
        parts = [p["name"] for p in g.get("participants", [])]
        league = g.get("league", {}).get("name", "?")
        print(f"    -> {' vs '.join(parts) or g.get('title','')}  [{league}]")

print("\nDone!")
