import json

with open("/home/ubuntu/polymarket_bot/dashboard_state.json") as f:
    d = json.load(f)

mkts = d.get("markets", {})
print(f"Total markets in dashboard_state: {len(mkts)}")
keys = list(mkts.keys())
for cid in keys[:10]:
    m = mkts[cid]
    print(f"{cid[:16]} | {m.get('question')} | vol: {m.get('volume24hr')} | ask_yes: {m.get('best_ask_yes')} | ask_no: {m.get('best_ask_no')}")
