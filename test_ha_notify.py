import os
import requests

url = os.environ.get("HOMEASSISTANT_CLOUDHOOK_URL", "https://hooks.nabu.casa/your_cloudhook_token_here")
payload = {
    "title": "🎯 Polymarket Bot",
    "message": "Live test trade notification delivered to your phone!",
}

if "your_cloudhook_token_here" in url:
    print("Warning: HOMEASSISTANT_CLOUDHOOK_URL environment variable is not set. Using placeholder.")
else:
    r = requests.post(url, json=payload, timeout=10)
    print(f"Status: {r.status_code}, Body: {r.text}")
