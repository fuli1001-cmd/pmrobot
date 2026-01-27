
import asyncio
import os
from dotenv import load_dotenv, set_key
from py_clob_client.client import ClobClient
from eth_account import Account

async def main():
    # 1. Load Environment
    load_dotenv()
    pk = os.getenv("PRIVATE_KEY")
    
    if not pk:
        print("❌ Error: No PRIVATE_KEY found in .env")
        return

    # 2. Derive EOA
    try:
        eoa = Account.from_key(pk)
        print(f"\n🔑 Private Key Loaded")
        print(f"👤 EOA Address:   {eoa.address}")
    except Exception as e:
        print(f"❌ Invalid Private Key: {e}")
        return

    # 3. Initialize Client & Derive Proxy
    print(f"\n🤖 Connecting to Polymarket (Polygon)...")
    try:
        client = ClobClient("https://clob.polymarket.com/", key=pk, chain_id=137)
        
        # Derive API Credentials
        print("   Generating/Deriving API Credentials...")
        creds = client.create_or_derive_api_creds()
        print(f"   ✅ API Key:      {creds.api_key}")
        print(f"   ✅ API Secret:   {creds.api_secret[0:5]}...****")
        print(f"   ✅ Passphrase:   {creds.api_passphrase[0:5]}...****")
        
        # Derive Proxy Address
        print("   Fetching Proxy Address...")
        # Try multiple methods just in case SDK changed
        proxy = None
        try:
            proxy = client.get_proxy_address() # Common method
        except:
             # Fallback or older SDK method check
             pass
             
        if not proxy:
             # Try deriving from creds if possible or another call
             # Usually create_or_derive_api_creds makes sure proxy exists
             # Let's try to get it from the client object internal state if needed or re-call
             try:
                 proxy = client.get_address() # Some SDK versions
             except:
                 pass
        
        if proxy:
            print(f"   🎯 Proxy Address: {proxy}")
        else:
             print("   ⚠️ Could not auto-fetch Proxy Address. It might be 'null' if never created.")
             
        print(f"\n✨ SUMMARY:")
        if proxy:
             print(f"EOA:   {eoa.address}")
             print(f"PROXY: {proxy}")
             print("👉 Please ensure this PROXY address matches the one in your .env PROXY_WALLET_ADDRESS")
        
    except Exception as e:
        print(f"❌ Failed to derive credentials: {e}")

if __name__ == "__main__":
    asyncio.run(main())
