"""Quick connectivity test for SX Bet API.

Verifies:
  1. /metadata endpoint — contract addresses
  2. /sports endpoint — sport list
  3. /markets/active — market discovery (Politics, sportId=17)
  4. /orders/odds/best — best odds for a sample market
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchanges.sxbet import SxBetExchange, SXBET_SPORT_IDS


async def main():
    print("=" * 60)
    print("SX Bet API Connectivity Test")
    print("=" * 60)

    # Load API key from .env if available
    from config.settings import get_settings
    settings = get_settings()
    api_key = settings.sxbet_api_key or ""

    sx = SxBetExchange(
        api_key=api_key,
        api_url=settings.sxbet_api_url,
        rpc_url=settings.sxbet_rpc_url,
        chain_id=settings.sxbet_chain_id,
        usdc_address=settings.sxbet_usdc_address,
        dry_run=True,
    )

    # 1. Connect (fetches /metadata and /sports)
    print("\n[1] Connecting to SX Bet API...")
    await sx.connect()
    print(f"    Executor: {sx._executor_address}")
    print(f"    TokenTransferProxy: {sx._token_transfer_proxy}")
    print(f"    Sports loaded: {len(sx._sports)}")
    for sid, label in sorted(sx._sports.items()):
        print(f"      {sid:3d}: {label}")

    # 2. Fetch markets for a few sports
    print("\n[2] Fetching active markets...")
    test_sports = ["mma", "tennis", "politics", "crypto"]
    for sport in test_sports:
        markets = await sx.get_markets(sport=sport)
        print(f"    {sport}: {len(markets)} two-outcome markets")
        if markets:
            m = markets[0]
            print(f"      Sample: {m.question}")
            print(f"        market_id: {m.market_id[:40]}...")
            print(f"        team_a: {m.team_a}, team_b: {m.team_b}")
            print(f"        start_time: {m.start_time}")

    # 3. Fetch odds for first available market
    print("\n[3] Fetching odds for sample market...")
    for sport in test_sports:
        markets = await sx.get_markets(sport=sport)
        if markets:
            sample = markets[0]
            odds = await sx.get_odds(sample.market_id, trade_size=50.0, live=True)
            if odds:
                print(f"    Market: {sample.question[:60]}")
                print(f"    YES price: {odds.price_yes:.4f}")
                print(f"    NO price:  {odds.price_no:.4f}")
                print(f"    Total:     {odds.price_yes + odds.price_no:.4f}")
                print(f"    Depth YES: ${odds.max_size_yes:.2f}")
                print(f"    Depth NO:  ${odds.max_size_no:.2f}")
            else:
                print(f"    No odds returned for {sample.question[:40]}")
            break
    else:
        print("    No markets available to test odds")

    # 4. Fetch active leagues
    print("\n[4] Active leagues for Politics (sportId=17)...")
    leagues = await sx.get_active_leagues(sport_id=17)
    for league in leagues[:5]:
        print(f"    {league.get('leagueId')}: {league.get('label', 'unknown')}")

    await sx.disconnect()
    print("\n" + "=" * 60)
    print("SX Bet connectivity test complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
