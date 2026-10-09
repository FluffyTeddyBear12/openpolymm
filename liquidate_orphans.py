"""
Safe Orphan Position Unwinder for Polymarket Bot.
Unwinds active orphan positions using RollbackProtector to recover locked USDC capital.
"""

import json
import logging
import os
import sys
import time
import urllib.request
from typing import Dict, List, Optional
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("OrphanUnwinder")

load_dotenv()

from rollback_protector import RollbackProtector, _resolve_token_metadata, _round_to_tick_size

try:
    from py_clob_client_v2.client import ClobClient
except ImportError:
    from py_clob_client.client import ClobClient


def get_client() -> ClobClient:
    key = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("POLYGON_PRIVATE_KEY")
    proxy = os.getenv("POLYMARKET_ADDRESS") or os.getenv("POLYMARKET_PROXY_ADDRESS") or "0xe7d565d58c61e2b96adb0e4518e7c33978402e7f"
    try:
        sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", 2))
    except (ValueError, TypeError):
        sig_type = 2
    if sig_type not in (0, 1, 2):
        sig_type = 2
    host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")

    client = ClobClient(
        host,
        key=key,
        chain_id=137,
        signature_type=sig_type,
        funder=proxy,
    )
    if hasattr(client, "derive_api_key"):
        creds = client.derive_api_key()
        client.set_api_creds(creds)
    elif hasattr(client, "create_or_derive_api_creds"):
        client.set_api_creds(client.create_or_derive_api_creds())
    return client


def fetch_active_positions(proxy: str) -> List[dict]:
    url = f"https://data-api.polymarket.com/positions?user={proxy}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PolymarketBot/2.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [p for p in data if float(p.get("size", 0.0) or 0.0) >= 1.0 and float(p.get("currentValue", 0.0) or 0.0) > 0.05]


def unwind_all(dry_run: bool = True):
    proxy = (os.getenv("POLYMARKET_ADDRESS") or os.getenv("POLYMARKET_PROXY_ADDRESS") or "0xe7d565d58c61e2b96adb0e4518e7c33978402e7f").lower()
    if not proxy:
        logger.error("POLYMARKET_ADDRESS not set")
        return

    client = get_client()
    positions = fetch_active_positions(proxy)
    logger.info(f"Found {len(positions)} active liquidatable positions.")

    total_recovered = 0.0

    for p in positions:
        token_id = p.get("asset", "")
        title = p.get("title", "")
        outcome = p.get("outcome", "")
        size = float(p.get("size", 0.0))
        cur_val = float(p.get("currentValue", 0.0))
        avg_price = float(p.get("avgPrice", 0.0))

        tick, neg_risk = _resolve_token_metadata(client, token_id)
        book = RollbackProtector.fetch_order_book(client, token_id)
        best_bid = RollbackProtector.extract_best_bid(book)

        slippage = avg_price - best_bid if best_bid > 0 else 1.0

        logger.info(
            f"\n--- {title} ---"
            f"\nOutcome: {outcome} | Shares: {size:.1f} | Paid Avg: ${avg_price:.4f} | Best Bid: ${best_bid:.4f} | Spread Slippage: ${slippage:.4f}"
            f"\nTick: {tick} | NegRisk: {neg_risk}"
        )

        if dry_run:
            if best_bid > 0 and slippage <= 0.025:
                est_return = size * best_bid
                logger.info(f"[DRY RUN] Would execute MARKET EXIT at ${best_bid:.4f} -> recovers ~${est_return:.2f}")
                total_recovered += est_return
            else:
                logger.info(f"[DRY RUN] Spread wide (${slippage:.4f}). Would place GTC LIMIT SELL at purchase price ${avg_price:.4f}")
        else:
            # Live execution
            ok, action, details = RollbackProtector.safe_unwind_or_limit_exit(
                client=client,
                token_id=token_id,
                shares=size,
                buy_price=avg_price,
                label=outcome,
                max_loss_cents=0.025,  # Max 2.5c slippage allowed for market exit
                tick_size=tick,
                neg_risk=neg_risk,
            )
            logger.info(f"Result: ok={ok}, action={action}, details={details}")
            if ok and action == "MARKET_EXIT_SAFE":
                sell_p = details.get("sell_price", best_bid)
                total_recovered += size * sell_p
            time.sleep(1.0)

    logger.info(f"\nEstimated/Total Recovered Capital: ${total_recovered:.2f}")


if __name__ == "__main__":
    dry = "--live" not in sys.argv
    if dry:
        print("=== RUNNING IN DRY-RUN MODE (Pass --live to execute) ===")
    else:
        print("=== RUNNING IN LIVE EXECUTION MODE ===")
    unwind_all(dry_run=dry)
