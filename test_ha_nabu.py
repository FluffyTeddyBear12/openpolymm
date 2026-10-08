import os
import requests

url = f"{os.environ.get('HOMEASSISTANT_URL', 'https://your-instance.ui.nabu.casa')}/api/services/notify/notify"
token = os.environ.get('HOMEASSISTANT_TOKEN', 'your_token_here')
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
}
payload = {
    "title": "🎯 Polymarket Bot",
    "message": "Testing Home Assistant mobile notifications!",
}

if "your_token_here" in token:
    print("Warning: HOMEASSISTANT_TOKEN environment variable is not set. Using placeholder.")
else:
    r = requests.post(url, headers=headers, json=payload, timeout=10)
    print(f"Status: {r.status_code}, Response: {r.text}")
