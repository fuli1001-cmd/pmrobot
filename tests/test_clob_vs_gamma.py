"""Diagnostic: compare CLOB book vs Gamma outcomePrices for sports markets."""
import asyncio
import httpx


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


async def main():
    async with httpx.AsyncClient(timeout=15) as http:
        # 1) Fetch active/current tennis events
        r = await http.get(f"{GAMMA_API}/events", params={
            "tag_slug": "tennis", "limit": "5",
            "active": "true", "closed": "false",
            "order": "startDate", "ascending": "false",
        })
        events = r.json()
        print(f"Got {len(events)} tennis events\n")

        cids = []
        market_info = {}
        for ev in events[:5]:
            for mk in ev.get("markets", [])[:1]:
                cid = mk.get("conditionId", "")
                q = mk.get("question", "")
                ops = mk.get("outcomePrices", "")
                tids = mk.get("clobTokenIds", [])
                cids.append(cid)
                market_info[cid] = {"q": q, "ops": ops, "tids": tids}
                print(f"Events API: {q[:60]}")
                print(f"  outcomePrices: {ops}")

        # 2) Re-fetch same markets from /markets for fresh prices
        if cids:
            r2 = await http.get(f"{GAMMA_API}/markets", params={
                "condition_ids": ",".join(cids),
            })
            fresh = r2.json()
            print(f"\n--- Fresh /markets endpoint ({len(fresh)} results) ---\n")
            for m in fresh:
                cid = m.get("conditionId", "")
                q = m.get("question", "")
                ops_fresh = m.get("outcomePrices", "")
                info = market_info.get(cid, {})
                ops_old = info.get("ops", "")
                print(f"  {q[:60]}")
                print(f"    Events API:  {ops_old}")
                print(f"    /markets:    {ops_fresh}")
                same = "YES" if ops_old == ops_fresh else "DIFFERENT!"
                print(f"    Same? {same}")

        # 3) Check CLOB book for these markets
        print(f"\n--- CLOB Order Books ---\n")
        for cid, info in list(market_info.items())[:5]:
            tids = info.get("tids", [])
            if len(tids) < 2:
                continue
            print(f"  {info['q'][:60]}")
            for i, label in [(0, "YES"), (1, "NO")]:
                resp = await http.get(f"{CLOB_API}/book", params={"token_id": tids[i]})
                d = resp.json()
                asks = d.get("asks", [])
                bids = d.get("bids", [])
                print(f"    {label}: {len(bids)}b / {len(asks)}a", end="")
                if asks:
                    print(f"  bestAsk={asks[0]['price']}", end="")
                if bids:
                    print(f"  bestBid={bids[0]['price']}", end="")
                if not asks and not bids:
                    print("  ** EMPTY **", end="")
                print()

        # 4) Also test hockey & boxing
        for sport in ["hockey", "boxing"]:
            r3 = await http.get(f"{GAMMA_API}/events", params={
                "tag_slug": sport, "limit": "2",
                "active": "true", "closed": "false",
            })
            evts = r3.json()
            print(f"\n--- {sport.upper()} ({len(evts)} events) ---")
            for ev in evts[:2]:
                for mk in ev.get("markets", [])[:1]:
                    q = mk.get("question", "")
                    ops = mk.get("outcomePrices", "")
                    tids = mk.get("clobTokenIds", [])
                    print(f"  {q[:60]}")
                    print(f"    outcomePrices: {ops}")
                    if len(tids) >= 2:
                        for idx, lab in [(0, "YES"), (1, "NO")]:
                            resp = await http.get(f"{CLOB_API}/book", params={"token_id": tids[idx]})
                            d = resp.json()
                            asks = d.get("asks", [])
                            bids = d.get("bids", [])
                            ba = asks[0]["price"] if asks else "EMPTY"
                            bb = bids[0]["price"] if bids else "EMPTY"
                            print(f"    {lab}: {len(bids)}b/{len(asks)}a  ask={ba}  bid={bb}")


if __name__ == "__main__":
    asyncio.run(main())
