"""Debug SX Bet API response format."""
import asyncio
import json
import httpx


async def main():
    async with httpx.AsyncClient(base_url="https://api.sx.bet", timeout=15) as c:
        # Sports structure
        print("=== SPORTS (first 2) ===")
        r0 = await c.get("/sports")
        d0 = r0.json()
        print(f"Type: {type(d0)}, keys: {list(d0.keys()) if isinstance(d0, dict) else 'N/A'}")
        sd = d0.get("data", d0) if isinstance(d0, dict) else d0
        if isinstance(sd, list) and sd:
            print(json.dumps(sd[0], indent=2))

        # Markets proper parse
        print("\n=== MARKETS sportIds=7 (MMA) ===")
        r = await c.get("/markets/active", params={"sportIds": "7", "pageSize": 3})
        data = r.json()
        inner = data.get("data", {})
        if isinstance(inner, dict):
            markets = inner.get("markets", [])
            next_key = inner.get("nextKey")
            print(f"Count: {len(markets)}, nextKey: {next_key}")
        else:
            print(f"inner type: {type(inner)}")

        # Best odds structure detail
        if markets:
            mh = markets[0]["marketHash"]
            usdc = "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"
            print(f"\n=== BEST ODDS detail for {mh[:30]} ===")
            r3 = await c.get("/orders/odds/best", params={"marketHashes": mh, "baseToken": usdc})
            odds_data = r3.json()
            best = odds_data.get("data", {}).get("bestOdds", [])
            if best:
                entry = best[0]
                o1 = entry.get("outcomeOne", {})
                o2 = entry.get("outcomeTwo", {})
                pct1 = int(o1.get("percentageOdds", 0))
                pct2 = int(o2.get("percentageOdds", 0))
                print(f"Outcome1 makerPct: {pct1 / 10**20:.4f}  takerPrice: {1 - pct1/10**20:.4f}")
                print(f"Outcome2 makerPct: {pct2 / 10**20:.4f}  takerPrice: {1 - pct2/10**20:.4f}")
                total = (1 - pct1/10**20) + (1 - pct2/10**20)
                print(f"Total taker price: {total:.4f}  overround: {(total - 1)*100:.2f}%")

            # Orders detail
            print(f"\n=== ORDERS detail ===")
            r4 = await c.get("/orders", params={"marketHashes": mh, "baseToken": usdc})
            od = r4.json()
            orders = od.get("data", []) if isinstance(od.get("data"), list) else od if isinstance(od, list) else []
            print(f"Order count: {len(orders)}")
            for o in orders[:4]:
                total_size = int(o.get("totalBetSize", 0))
                fill_amt = int(o.get("fillAmount", 0))
                remaining = total_size - fill_amt
                pct = int(o.get("percentageOdds", 0))
                maker_betting_one = o.get("isMakerBettingOutcomeOne")
                print(f"  maker_one={maker_betting_one}, pct={pct/10**20:.4f}, "
                      f"total={total_size}, filled={fill_amt}, remaining={remaining}")


asyncio.run(main())
