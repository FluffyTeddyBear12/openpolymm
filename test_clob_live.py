import time
from py_clob_client.client import ClobClient
from dotenv import dotenv_values
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

cfg = dotenv_values('.env')
t0 = time.time()
c = ClobClient(
    cfg.get('POLYMARKET_HOST', 'https://clob.polymarket.com'),
    key=cfg.get('POLYMARKET_PRIVATE_KEY'),
    chain_id=137,
    signature_type=int(cfg.get('POLYMARKET_SIGNATURE_TYPE', 2))
)
c.set_api_creds(c.create_or_derive_api_creds())
auth_ms = (time.time() - t0) * 1000

t1 = time.time()
res = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
query_ms = (time.time() - t1) * 1000

bal = float(res['balance']) / 1e6
print(f"=== London VPS Polymarket CLOB Connection Test ===")
print(f"Server location: AWS eu-west-2 (London)")
print(f"Auth derivation time: {auth_ms:.1f}ms")
print(f"Live CLOB query round-trip: {query_ms:.1f}ms")
print(f"Verified CLOB Collateral Balance: ${bal:.4f} USDC")
