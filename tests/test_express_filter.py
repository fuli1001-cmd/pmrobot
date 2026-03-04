"""Check if removing isExpressForbidden filter changes which condition gets picked for existing sports."""
import httpx
import time

SUBGRAPH_URL = (
    "https://thegraph-1.onchainfeed.org/subgraphs/name/"
    "azuro-protocol/azuro-data-feed-polygon"
)

def query(gql: str, variables: dict) -> dict:
    resp = httpx.post(SUBGRAPH_URL, json={"query": gql, "variables": variables}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", {})

now_ts = int(time.time())

# Query WITH filter (current)
Q_WITH = """
query WithFilter($sport: String, $startsAt_gt: BigInt!) {
  games(where: { startsAt_gt: $startsAt_gt, sport_: { name_contains_nocase: $sport } }, first: 5, orderBy: startsAt) {
    gameId
    title
    participants { name }
    conditions(where: { isExpressForbidden: false }) {
      conditionId
      outcomes { outcomeId currentOdds }
      wonOutcomeIds
    }
  }
}
"""

# Query WITHOUT filter (proposed)
Q_WITHOUT = """
query WithoutFilter($sport: String, $startsAt_gt: BigInt!) {
  games(where: { startsAt_gt: $startsAt_gt, sport_: { name_contains_nocase: $sport } }, first: 5, orderBy: startsAt) {
    gameId
    title
    participants { name }
    conditions {
      conditionId
      isExpressForbidden
      outcomes { outcomeId currentOdds }
      wonOutcomeIds
    }
  }
}
"""

def first_binary_cond(conditions):
    for c in conditions:
        won = c.get("wonOutcomeIds") or []
        if len(won) == 0 and len(c.get("outcomes", [])) == 2:
            return c["conditionId"]
    return None

for sport in ["Tennis", "Ice Hockey", "MMA", "Cricket", "Boxing"]:
    print(f"\n{'='*60}")
    print(f"Sport: {sport}")
    
    with_data = query(Q_WITH, {"sport": sport, "startsAt_gt": str(now_ts)})
    without_data = query(Q_WITHOUT, {"sport": sport, "startsAt_gt": str(now_ts)})
    
    with_games = {g["gameId"]: g for g in with_data.get("games", [])}
    without_games = {g["gameId"]: g for g in without_data.get("games", [])}
    
    for gid in list(with_games.keys())[:3]:
        g_w = with_games[gid]
        g_wo = without_games.get(gid, {})
        
        with_cond = first_binary_cond(g_w.get("conditions", []))
        without_cond = first_binary_cond(g_wo.get("conditions", []))
        
        parts = [p["name"] for p in g_w.get("participants", [])]
        match = "✅" if with_cond == without_cond else "❌ CHANGED"
        
        n_with = len(g_w.get("conditions", []))
        n_without = len(g_wo.get("conditions", []))
        
        print(f"  {' vs '.join(parts)}")
        print(f"    WITH filter:    {n_with:>3} conds, first_binary={with_cond}")
        print(f"    WITHOUT filter: {n_without:>3} conds, first_binary={without_cond}")
        print(f"    Same result: {match}")

print("\nDone!")
