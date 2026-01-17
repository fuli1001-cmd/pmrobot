"""Debug script to check WebSocket message format and order book data."""

import asyncio
import json
import websockets

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Get a few token IDs from a real market
# We'll use a popular market's tokens
TEST_TOKEN_IDS = [
    "101676997363687199724245607342877036148401850938023978421879460310389391082353",
    "4153292802911610701832309484716814274802943278345248636922528170020319407796",
]


async def debug_ws():
    print(f"Connecting to {CLOB_WS_URL}...")
    
    async with websockets.connect(CLOB_WS_URL) as ws:
        # Subscribe to order books
        subscribe_msg = {
            "type": "subscribe",
            "channel": "book",
            "assets_ids": TEST_TOKEN_IDS,
        }
        await ws.send(json.dumps(subscribe_msg))
        print(f"Sent subscribe message for {len(TEST_TOKEN_IDS)} tokens")
        
        # Listen for messages
        message_count = 0
        book_updates = 0
        
        async for message in ws:
            message_count += 1
            try:
                data = json.loads(message)
                
                # Print first few messages in full
                if message_count <= 5:
                    print(f"\n--- Message {message_count} ---")
                    print(json.dumps(data, indent=2)[:1500])
                
                # Count message types
                if isinstance(data, list):
                    print(f"Message {message_count}: LIST with {len(data)} items")
                    for item in data:
                        if isinstance(item, dict):
                            msg_type = item.get("type", item.get("event_type", "unknown"))
                            if msg_type == "book":
                                book_updates += 1
                elif isinstance(data, dict):
                    msg_type = data.get("type", data.get("event_type", "unknown"))
                    print(f"Message {message_count}: DICT type={msg_type}")
                    if msg_type == "book":
                        book_updates += 1
                        # Check book data structure
                        print(f"  asset_id: {data.get('asset_id', 'N/A')[:20]}...")
                        print(f"  bids: {len(data.get('bids', []))} levels")
                        print(f"  asks: {len(data.get('asks', []))} levels")
                else:
                    print(f"Message {message_count}: Unknown type {type(data)}")
                    
            except json.JSONDecodeError as e:
                print(f"Message {message_count}: JSON decode error: {e}")
            
            # Stop after 30 messages or 30 seconds
            if message_count >= 30:
                print(f"\n=== Summary ===")
                print(f"Total messages: {message_count}")
                print(f"Book updates: {book_updates}")
                break


if __name__ == "__main__":
    asyncio.run(debug_ws())
