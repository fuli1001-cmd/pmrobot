"""Diagnostic: find the correct Gamma API endpoint for fresh market prices."""
import asyncio
import json
import httpx

GAMMA = "https://gamma-api.polymarket.com"


async def main():
    async with httpx.AsyncClient(timeout=15) as http:
        # Get a known market's condition_id and clob_token_id
        r = await http.get(f"{GAMMA}/events", params={
            "tag_slug": "tennis", "limit": "1",
            "active": "true", "closed": "false",
            "order": "startDate", "ascending": "false",
        })
        ev = r.json()[0]
        mk = ev["markets"][0]
        cid = mk["conditionId"]
        q = mk["question"]
        tids = mk.get("clobTokenIds", [])
        slug = mk.get("slug", "")
        print(f"Test market: {q[:60]}")
        print(f"  conditionId: {cid}")
        print(f"  slug: {slug}")
        print(f"  token_ids: {tids}")
        print(f"  outcomePrices: {mk.get('outcomePrices', '')}")
        print()

        # Try various endpoints:
        endpoints = [
            ("GET /markets?condition_ids=CID", {"condition_ids": cid}),
            ("GET /markets?clob_token_ids=TID0", {"clob_token_ids": tids[0]} if tids else {}),
            ("GET /markets?id=CID", {"id": cid}),
            ("GET /markets?slug=SLUG", {"slug": slug} if slug else {}),
        ]
        for label, params in endpoints:
            if not params:
                continue
            try:
                r2 = await http.get(f"{GAMMA}/markets", params=params)
                data = r2.json()
                n = len(data) if isinstance(data, list) else 1
                print(f"{label}: {r2.status_code}, {n} result(s)")
                if isinstance(data, list) and data:
                    m = data[0]
                    print(f"  outcomePrices: {m.get('outcomePrices', 'N/A')}")
                elif isinstance(data, dict) and "question" in data:
                    print(f"  outcomePrices: {data.get('outcomePrices', 'N/A')}")
            except Exception as e:
                print(f"{label}: ERROR {e}")

        # Also try /markets/CID (single market)
        try:
            r3 = await http.get(f"{GAMMA}/markets/{cid}")
            print(f"\nGET /markets/{cid[:20]}...: {r3.status_code}")
            if r3.status_code == 200:
                d = r3.json()
                if isinstance(d, dict):
                    print(f"  outcomePrices: {d.get('outcomePrices', 'N/A')}")
        except Exception as e:
            print(f"  ERROR: {e}")

        # Try CLOB API /prices endpoint
        try:
            r4 = await http.get(f"https://clob.polymarket.com/prices", params={"token_ids": ",".join(tids)} if tids else {})
            print(f"\nGET clob/prices: {r4.status_code}")
            print(f"  {r4.text[:200]}")
        except Exception as e:
            print(f"  ERROR: {e}")

        # Try CLOB API /price endpoint (singular)
        if tids:
            try:
                r5 = await http.get(f"https://clob.polymarket.com/price", params={"token_id": tids[0]})
                print(f"\nGET clob/price?token_id=...: {r5.status_code}")
                print(f"  {r5.text[:200]}")
            except Exception as e:
                print(f"  ERROR: {e}")

        # Try CLOB /midpoint endpoint
        if tids:
            try:
                r6 = await http.get(f"https://clob.polymarket.com/midpoint", params={"token_id": tids[0]})
                print(f"\nGET clob/midpoint?token_id=...: {r6.status_code}")
                print(f"  {r6.text[:200]}")
            except Exception as e:
                print(f"  ERROR: {e}")


if __name__ == "__main__":
    asyncio.run(main())
