import httpx, time, json
now_ts = str(int(time.time()))
print("now_ts:", now_ts)

# Try without date filter to see latest games
r = httpx.post(
    "https://thegraph.azuro.org/subgraphs/name/azuro-protocol/azuro-api-polygon-v3",
    json={
        "query": '{ games(where: {status: Created}, first: 10, orderBy: startsAt, orderDirection: desc) { gameId title startsAt status sport { slug } } }'
    },
    timeout=30,
)
games = r.json().get("data", {}).get("games", [])
print(f"Games with status=Created (latest 10):")
for g in games:
    sa = g.get("startsAt", "")
    sport = g.get("sport", {}).get("slug", "")
    title = g.get("title", "")[:60]
    from datetime import datetime
    dt = datetime.utcfromtimestamp(int(sa)).strftime("%Y-%m-%d %H:%M") if sa else "?"
    print(f"  [{dt}] {sport:20s} {title}")

# Also check with future filter, try lower timestamp
print()
one_hour_ago = str(int(time.time()) - 3600)
r2 = httpx.post(
    "https://thegraph.azuro.org/subgraphs/name/azuro-protocol/azuro-api-polygon-v3",
    json={
        "query": """
        query($startsAt_gt: BigInt!) {
          games(
            where: { startsAt_gt: $startsAt_gt, status: Created }
            first: 10
            orderBy: startsAt
          ) { gameId title startsAt sport { slug } }
        }""",
        "variables": {"startsAt_gt": one_hour_ago},
    },
    timeout=30,
)
games2 = r2.json().get("data", {}).get("games", [])
print(f"Games starting after 1 hour ago: {len(games2)}")
for g in games2[:5]:
    sa = g.get("startsAt", "")
    sport = g.get("sport", {}).get("slug", "")
    title = g.get("title", "")[:60]
    dt = datetime.utcfromtimestamp(int(sa)).strftime("%Y-%m-%d %H:%M") if sa else "?"
    print(f"  [{dt}] {sport:20s} {title}")
