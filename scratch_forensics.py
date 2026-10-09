import json
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

load_dotenv("/home/ubuntu/polymarket_bot/.env")

print("==================================================================")
print("CLOB FORENSICS: LIVE EXECUTOR & POSITION TRACE")
print("==================================================================")

key = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("POLYGON_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
address = os.getenv("POLYMARKET_PROXY_ADDRESS") or os.getenv("POLYMARKET_ADDRESS")
sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "3"))

print(f"Address: {address}")
print(f"Signature Type: {sig_type}")
print(f"Key loaded: {bool(key)}")

try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
    from py_clob_client_v2.constants import POLYGON

    host = "https://clob.polymarket.com"
    client = ClobClient(
        host=host,
        key=key,
        chain_id=POLYGON,
        signature_type=sig_type,
        funder=address
    )
    creds = client.derive_api_key()
    client.set_api_creds(creds)
    print("CLOB client initialized and creds set.")

    # 1. Collateral Balance
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    bal_resp = client.get_balance_allowance(params)
    print(f"Live Collateral Response: {bal_resp}")

    # 2. Open Orders
    open_orders = client.get_open_orders()
    print(f"\n--- OPEN ORDERS COUNT: {len(open_orders) if isinstance(open_orders, list) else open_orders} ---")
    if isinstance(open_orders, list):
        for o in open_orders:
            print(f"  ID: {o.get('id')} | Token: {o.get('asset_id')} | Side: {o.get('side')} | Price: {o.get('price')} | Size: {o.get('original_size')} | Matched: {o.get('size_matched')}")

    # 3. Recent Trades on CLOB
    raw_trades = client.get_trades()
    print(f"\n--- CONFIRMED CLOB TRADES (LAST 50) COUNT: {len(raw_trades) if isinstance(raw_trades, list) else 0} ---")
    if isinstance(raw_trades, list):
        for t in raw_trades[:50]:
            ts = int(t.get("match_time", 0) or 0)
            t_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts > 0 else "N/A"
            print(f"  {t_str} | Market: {str(t.get('market'))[:20]} | Side: {t.get('side')} | Outcome: {t.get('outcome')} | Price: {t.get('price')} | Size: {t.get('size')} | Status: {t.get('status')} | Tx: {t.get('transaction_hash', '')[:12]}")

except Exception as e:
    print(f"Error querying CLOB: {e}")
    import traceback
    traceback.print_exc()

print("==================================================================")
