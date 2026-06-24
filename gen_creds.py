
import asyncio
import os
from dotenv import load_dotenv, set_key
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

    # 3. Initialize Client & Derive API credentials / wallet
    print(f"\n🤖 Connecting to Polymarket (Polygon)...")
    try:
        from polymarket import SecureClient

        print("   Generating/Deriving API Credentials...")
        client = SecureClient.create(private_key=pk)
        try:
            creds = client.credentials
            wallet = client.wallet
            wallet_type = client.wallet_type

            print(f"   ✅ API Key:      {creds.key}")
            print(f"   ✅ API Secret:   {creds.secret[0:5]}...****")
            print(f"   ✅ Passphrase:   {creds.passphrase[0:5]}...****")
            print(f"   🎯 Wallet:       {wallet}")
            print(f"   🎯 Wallet Type:  {wallet_type}")

            set_key(".env", "POLYMARKET_API_KEY", creds.key)
            set_key(".env", "POLYMARKET_API_SECRET", creds.secret)
            set_key(".env", "POLYMARKET_PASSPHRASE", creds.passphrase)
            set_key(".env", "PROXY_WALLET_ADDRESS", wallet)
        finally:
            client.close()

        print(f"\n✨ SUMMARY:")
        print(f"EOA:    {eoa.address}")
        print(f"WALLET: {wallet}")
        print("👉 Credentials and PROXY_WALLET_ADDRESS were written to .env")
        
    except Exception as e:
        print(f"❌ Failed to derive credentials: {e}")

if __name__ == "__main__":
    asyncio.run(main())
