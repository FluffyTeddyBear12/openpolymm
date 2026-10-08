"""
Decoupled Local GPU LLM Market Auditor Daemon.

Runs in the background (cold path) on the RTX 5080 GPU, querying Ollama (qwen2.5:14b, temp=0.0)
to evaluate Polymarket UMA resolution fine print and ambiguous criteria.
Vetted markets are written to vetted_markets.json for sub-millisecond O(1) in-memory checks.
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Dict, List, Optional, Set, Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [MarketAuditor] %(message)s"
)
logger = logging.getLogger("MarketAuditorDaemon")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
VETTED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vetted_markets.json")


def query_ollama_audit(question: str, description: str) -> Dict[str, Any]:
    prompt = f"""You are an institutional prediction market auditor for delta-neutral arbitrage.
Evaluate this Polymarket contract for rule ambiguity, resolution dispute risk, subjective wording, or early cancellation traps.

Question: {question}
Description / Resolution Rules: {description}

Respond in strict JSON with no commentary:
{{
  "approved": true or false,
  "confidence": float between 0.0 and 1.0,
  "risk_category": "NONE" or "AMBIGUOUS_RULES" or "DISPUTE_RISK" or "SUBJECTIVE" or "FAST_EXPIRY",
  "reason": "short explanation"
}}
"""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.0,
            "num_predict": 150
        }
    }

    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_text = data.get("response", "").strip()
            # Extract JSON block
            if "{" in raw_text and "}" in raw_text:
                json_part = raw_text[raw_text.find("{"):raw_text.rfind("}")+1]
                return json.loads(json_part)
    except Exception as e:
        logger.debug(f"Ollama audit call failed: {e}")
    return {"approved": False, "confidence": 0.0, "risk_category": "OLLAMA_OFFLINE", "reason": "Ollama call failed"}


def fetch_candidate_markets() -> List[dict]:
    try:
        url = "https://gamma-api.polymarket.com/markets?limit=100&active=true&closed=false&order=volume24hr&ascending=false"
        req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBotAuditor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning(f"Failed to fetch candidate markets from Gamma API: {e}")
        return []


def run_audit_cycle():
    logger.info("Starting market auditor review cycle...")
    markets = fetch_candidate_markets()
    if not markets:
        logger.warning("No candidate markets fetched.")
        return

    existing_vetted = {}
    if os.path.exists(VETTED_FILE):
        try:
            with open(VETTED_FILE, "r", encoding="utf-8") as f:
                existing_vetted = json.load(f)
        except Exception:
            existing_vetted = {}

    vetted_count = len(existing_vetted.get("vetted_market_ids", []))
    logger.info(f"Loaded {vetted_count} existing vetted markets from cache.")

    vetted_ids = set(existing_vetted.get("vetted_market_ids", []))
    metadata = existing_vetted.get("metadata", {})

    newly_approved = 0
    for m in markets:
        cid = m.get("conditionId") or m.get("condition_id")
        if not cid or cid in vetted_ids:
            continue

        q = m.get("question", "")
        desc = m.get("description", "")
        audit = query_ollama_audit(q, desc)

        if audit.get("approved"):
            vetted_ids.add(cid)
            metadata[cid] = {
                "question": q,
                "confidence": audit.get("confidence", 1.0),
                "risk_category": audit.get("risk_category", "NONE"),
                "reason": audit.get("reason", "Approved"),
                "audited_at": time.time(),
            }
            newly_approved += 1
            logger.info(f"✅ Market Approved by Natsu GPU: {q[:60]} ({audit.get('reason')})")
        else:
            logger.info(f"❌ Market Rejected: {q[:60]} -> {audit.get('risk_category')}: {audit.get('reason')}")

    out_data = {
        "last_updated": time.time(),
        "total_vetted": len(vetted_ids),
        "vetted_market_ids": list(vetted_ids),
        "metadata": metadata
    }

    try:
        with open(VETTED_FILE, "w", encoding="utf-8") as f:
            json.dump(out_data, f, indent=2)
        logger.info(f"Cycle complete. {newly_approved} newly approved markets written to {VETTED_FILE}.")
    except Exception as e:
        logger.error(f"Failed to write vetted_markets.json: {e}")


def main():
    parser = argparse.ArgumentParser(description="Polymarket Decoupled GPU Market Auditor Daemon")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=300, help="Interval in seconds between audit runs")
    args = parser.parse_args()

    logger.info(f"Initializing Market Auditor Daemon (Ollama: {OLLAMA_URL}, Model: {OLLAMA_MODEL})")
    while True:
        try:
            run_audit_cycle()
        except Exception as e:
            logger.error(f"Error in audit cycle: {e}")

        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
