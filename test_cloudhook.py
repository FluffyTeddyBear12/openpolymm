import os
import requests

url = os.environ.get(
    "HOMEASSISTANT_CLOUDHOOK_URL",
    "https://hooks.nabu.casa/your_cloudhook_token_here",
)
payload = {
    "title": "🎯 Polymarket Bot Alert",
    "message": "Testing Cloudhook delivery from London VPS!",
}

if not os.environ.get("HOMEASSISTANT_CLOUDHOOK_URL") and "your_cloudhook_token_here" in url:
    print("Warning: HOMEASSISTANT_CLOUDHOOK_URL environment variable is not set. Using placeholder.")
else:
    r = requests.post(url, json=payload, timeout=10)
    print(f"Cloudhook Status: {r.status_code}, Response: {r.text}")
