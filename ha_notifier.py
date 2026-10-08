"""
Home Assistant Push Notification Service for Polymarket Bot.
Dispatches non-blocking push alerts for executed arbitrage trades via Nabu Casa REST API.
"""

import os
import sys
import json
import logging
import threading
from typing import Optional

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger("HANotifier")

DEFAULT_HA_CLOUDHOOK_URL = os.environ.get(
    "HOMEASSISTANT_CLOUDHOOK_URL",
    "https://hooks.nabu.casa/your_cloudhook_token_here",
)
DEFAULT_HA_URL = os.environ.get(
    "HOMEASSISTANT_URL",
    "https://your-instance.ui.nabu.casa",
)
DEFAULT_HA_TOKEN = os.environ.get(
    "HOMEASSISTANT_TOKEN",
    "your_homeassistant_token_here",
)


def format_trade_message(
    question: str,
    trade_size: float,
    expected_profit: float,
    edge_pct: float = 0.0,
    execution_style: str = "maker_taker",
    wallet_balance: float = 0.0,
    market_id: str = "",
) -> str:
    """Format push notification body for an executed trade."""
    m_id_disp = f" ({market_id[-6:]})" if market_id else ""
    return (
        f"📊 Market: {question}{m_id_disp}\n"
        f"💰 Size: ${trade_size:.2f}\n"
        f"📈 Expected Profit: +${expected_profit:.4f} ({edge_pct:.2f}% edge)\n"
        f"⚡ Execution Style: {execution_style}\n"
        f"💵 Cash Remaining: ${wallet_balance:.2f}"
    )


def _dispatch_notification_sync(
    title: str,
    message: str,
    ha_cloudhook_url: Optional[str] = None,
    ha_url: Optional[str] = None,
    ha_token: Optional[str] = None,
    timeout: float = 6.0,
) -> bool:
    """
    Synchronously post notification to Home Assistant.
    Attempts primary endpoint: Nabu Casa Cloudhook (webhook trigger),
    falling back to REST API (/api/services/notify/notify, /api/services/notify/mobile_app_jimmies_phone).
    """
    payload = {
        "title": title,
        "message": message,
    }

    # 1. Primary: Cloudhook (Fastest, SSL-verified, no bearer token needed)
    cloudhook = ha_cloudhook_url or os.environ.get("HOMEASSISTANT_CLOUDHOOK_URL", DEFAULT_HA_CLOUDHOOK_URL)
    if cloudhook:
        try:
            if requests is not None:
                resp = requests.post(cloudhook, json=payload, timeout=timeout)
                if resp.status_code in (200, 201):
                    logger.info("HA push notification successfully dispatched via cloudhook.")
                    return True
                logger.warning(f"HA cloudhook returned status {resp.status_code}: {resp.text}")
            else:
                import urllib.request
                req = urllib.request.Request(
                    cloudhook,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status in (200, 201):
                        logger.info("HA push notification successfully dispatched via urllib cloudhook.")
                        return True
        except Exception as e:
            logger.warning(f"Failed to post HA notification to cloudhook: {e}")

    # 2. Secondary Fallback: Direct REST Endpoints
    url_base = (ha_url or os.environ.get("HOMEASSISTANT_URL", DEFAULT_HA_URL)).rstrip("/")
    token = ha_token or os.environ.get("HOMEASSISTANT_TOKEN", DEFAULT_HA_TOKEN)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    endpoints = [
        f"{url_base}/api/services/notify/notify",
        f"{url_base}/api/services/notify/mobile_app_jimmies_phone",
    ]

    for endpoint in endpoints:
        try:
            if requests is not None:
                resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
                if resp.status_code in (200, 201):
                    logger.info(f"HA push notification dispatched via REST fallback {endpoint}")
                    return True
                logger.warning(
                    f"HA notification request to {endpoint} returned status {resp.status_code}: {resp.text}"
                )
            else:
                import urllib.request
                req = urllib.request.Request(
                    endpoint,
                    data=json.dumps(payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status in (200, 201):
                        logger.info(f"HA push notification dispatched via urllib REST fallback {endpoint}")
                        return True
        except Exception as e:
            logger.warning(f"Failed to post HA notification to {endpoint}: {e}")

    logger.error("All Home Assistant notification endpoints failed.")
    return False


def send_trade_notification(
    question: str,
    trade_size: float,
    expected_profit: float,
    edge_pct: float = 0.0,
    execution_style: str = "maker_taker",
    wallet_balance: float = 0.0,
    market_id: str = "",
    sync: bool = False,
) -> Optional[threading.Thread]:
    """
    Dispatches a trade push notification in a daemon background thread
    so it NEVER blocks the trade execution loop.
    """
    title = "🎯 Polymarket Trade Executed"
    message = format_trade_message(
        question=question,
        trade_size=trade_size,
        expected_profit=expected_profit,
        edge_pct=edge_pct,
        execution_style=execution_style,
        wallet_balance=wallet_balance,
        market_id=market_id,
    )

    if sync:
        _dispatch_notification_sync(title, message)
        return None

    t = threading.Thread(
        target=_dispatch_notification_sync,
        args=(title, message),
        daemon=True,
        name=f"HANotify-{market_id[-6:] if market_id else 'Trade'}",
    )
    t.start()
    return t
