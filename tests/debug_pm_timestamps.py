"""Check what event-level fields PM actually returns for rugby/esports events."""
import httpx

PM_API = "https://gamma-api.polymarket.com/events"

for tag in ["rugby", "counter-strike", "league-of-legends"]:
    resp = httpx.get(PM_API, params={"tag_slug": tag, "limit": 3, "active": True}, timeout=30)
    events = resp.json()
    print(f"\n{'='*60}")
    print(f"Tag: {tag}")
    for e in events[:2]:
        title = e.get("title", "")
        start_date = e.get("startDate", "N/A")
        start_time = e.get("startTime", "N/A")
        end_date_event = e.get("endDate", "N/A")
        creation = e.get("creationDate", "N/A")
        
        print(f"\n  Event: {title}")
        print(f"    startDate={start_date}")
        print(f"    startTime={start_time}")
        print(f"    endDate={end_date_event}")
        print(f"    creationDate={creation}")
        
        # Check event-level keys
        time_keys = [k for k in e.keys() if "time" in k.lower() or "date" in k.lower() or "start" in k.lower()]
        print(f"    Time-related keys: {time_keys}")
        for k in time_keys:
            val = e.get(k)
            if val:
                print(f"      {k}={val}")
        
        # Show market[0] fields
        markets = e.get("markets", [])
        if markets:
            m = markets[0]
            print(f"    market[0] question: {m.get('question','')[:60]}")
            print(f"    market[0] endDate: {m.get('endDate', 'N/A')}")
            print(f"    market[0] game_start_time: {m.get('game_start_time', 'N/A')}")
            print(f"    market[0] gameStartTime: {m.get('gameStartTime', 'N/A')}")
            print(f"    market[0] startDate: {m.get('startDate', 'N/A')}")
            # Show all keys
            mkt_keys = sorted(m.keys())
            print(f"    market[0] all keys: {mkt_keys}")
