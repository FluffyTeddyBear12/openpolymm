import json

with open('markets.json', 'r', encoding='utf-8') as f:
    payload = json.load(f)

markets = payload.get('data', [])

active_markets = [m for m in markets if m.get('active') and not m.get('closed')]
for top in active_markets[1:3]:
    print(f"Title: {top.get('question')}")
    print("Tokens (Asset IDs):")
    for t in top.get('tokens', []):
        print(f" - {t.get('token_id')}")
