"""Check Azuro esports condition structures to understand why 0 binary."""
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
    return resp.json().get("data", {})

now_ts = int(time.time())

# Get esports games with ALL conditions (not just isExpressForbidden: false)
ESPORTS_QUERY = """
query EsportsGames($startsAt_gt: BigInt!) {
  games(
    where: {
      startsAt_gt: $startsAt_gt
      sport_: { name_contains_nocase: "Counter-Strike" }
    }
    first: 5
    orderBy: startsAt
    orderDirection: asc
  ) {
    gameId
    title
    sport { name }
    league { name }
    participants { name sortOrder }
    conditions {
      conditionId
      isExpressForbidden
      outcomes {
        outcomeId
        currentOdds
      }
      wonOutcomeIds
    }
  }
}
"""

data = query(ESPORTS_QUERY, {"startsAt_gt": str(now_ts)})
games = data.get("games", [])

print(f"CS2 games: {len(games)}\n")
for g in games[:3]:
    parts = [p["name"] for p in g.get("participants", [])]
    print(f"Game: {' vs '.join(parts)}  [{g.get('league',{}).get('name','')}]")
    conditions = g.get("conditions", [])
    print(f"  Total conditions: {len(conditions)}")
    for c in conditions[:5]:
        outcomes = c.get("outcomes", [])
        won = c.get("wonOutcomeIds") or []
        express = c.get("isExpressForbidden", False)
        print(f"  Condition {c['conditionId']}: {len(outcomes)} outcomes, won={won}, express_forbidden={express}")
        for o in outcomes:
            print(f"    outcome {o['outcomeId']}: odds={o['currentOdds']}")
    if len(conditions) > 5:
        print(f"  ... and {len(conditions)-5} more conditions")
    print()

# Also check LoL
print("="*60)
LOL_QUERY = """
query LoLGames($startsAt_gt: BigInt!) {
  games(
    where: {
      startsAt_gt: $startsAt_gt
      sport_: { name_contains_nocase: "League of Legends" }
    }
    first: 3
    orderBy: startsAt
    orderDirection: asc
  ) {
    gameId
    title
    sport { name }
    league { name }
    participants { name sortOrder }
    conditions {
      conditionId
      isExpressForbidden
      outcomes {
        outcomeId
        currentOdds
      }
      wonOutcomeIds
    }
  }
}
"""
data = query(LOL_QUERY, {"startsAt_gt": str(now_ts)})
games = data.get("games", [])

print(f"\nLoL games: {len(games)}\n")
for g in games:
    parts = [p["name"] for p in g.get("participants", [])]
    print(f"Game: {' vs '.join(parts)}  [{g.get('league',{}).get('name','')}]")
    conditions = g.get("conditions", [])
    print(f"  Total conditions: {len(conditions)}")
    for c in conditions[:5]:
        outcomes = c.get("outcomes", [])
        won = c.get("wonOutcomeIds") or []
        express = c.get("isExpressForbidden', False)")
        print(f"  Condition {c['conditionId']}: {len(outcomes)} outcomes, won={won}, express_forbidden={express}")
        for o in outcomes:
            print(f"    outcome {o['outcomeId']}: odds={o['currentOdds']}")
    if len(conditions) > 5:
        print(f"  ... and {len(conditions)-5} more conditions")
    print()
