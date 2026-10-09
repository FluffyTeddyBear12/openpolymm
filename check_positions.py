import json
import os
import urllib.request
import sys
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')

load_dotenv()
proxy = os.getenv("POLYMARKET_PROXY_ADDRESS", "0xe7d565d58c61e2b96adb0e4518e7c33978402e7f").lower()
if not proxy:
    print("POLYMARKET_PROXY_ADDRESS not found in .env")
    exit(1)

url = f"https://data-api.polymarket.com/positions?user={proxy}"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PolymarketBot/2.0"})

try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        positions = json.loads(resp.read().decode("utf-8"))
except Exception as e:
    print(f"Failed to fetch positions: {e}")
    exit(1)

active = [p for p in positions if float(p.get("size", 0) or 0) > 0.001]
print(f"Active positions count: {len(active)}")
total_cur_val = 0.0
total_init_val = 0.0

for p in active:
    title = p.get("title", "")
    outcome = p.get("outcome", "")
    size = float(p.get("size", 0.0) or 0.0)
    cur_val = float(p.get("currentValue", 0.0) or 0.0)
    init_val = float(p.get("initialValue", 0.0) or 0.0)
    cur_price = float(p.get("curPrice", 0.0) or 0.0)
    avg_price = float(p.get("avgPrice", 0.0) or 0.0)
    asset = p.get("asset", "")
    total_cur_val += cur_val
    total_init_val += init_val
    pnl = cur_val - init_val
    print(f"[{outcome}] {size:.1f} shares @ avg ${avg_price:.4f} (cur: ${cur_price:.4f}) | Value: ${cur_val:.2f} | PnL: ${pnl:.2f} | Token: {asset[:10]}... | {title}")

print(f"\nTotal Invested: ${total_init_val:.2f}")
print(f"Total Market Value: ${total_cur_val:.2f}")
print(f"Total Unrealized PnL: ${total_cur_val - total_init_val:.2f}")
