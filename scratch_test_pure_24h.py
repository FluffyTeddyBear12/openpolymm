import urllib.request
import json

req = urllib.request.Request(
    'https://gamma-api.polymarket.com/markets?limit=10&active=true&closed=false&order=volume24hr&ascending=false',
    headers={'User-Agent': 'Mozilla/5.0'}
)
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode('utf-8'))

print("Top 10 markets ordered strictly by volume24hr:")
for m in data:
    print(f" - {m.get('question')} | 24h vol: ${float(m.get('volume24hr') or 0.0):,.2f}")
