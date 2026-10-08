from paper_trader import fetch_top_markets
import json

markets = fetch_top_markets(limit=10)
print(f"fetch_top_markets returned {len(markets)} markets:")
for m in markets:
    print(f" - {m.get('question')} | vol: {m.get('volume24hr')} | liq: {m.get('liquidity')} | rewards: {m.get('rewards_daily_rate')}")
