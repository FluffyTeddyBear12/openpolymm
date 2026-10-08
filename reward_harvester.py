"""
Polymarket Liquidity Rewards Harvester Module.

Monitors active incentive markets (rewards_daily_rate > 0) from Polymarket CLOB,
calculates priority scoring combining arbitrage edge and daily liquidity mining pool rates,
and projects estimated daily USDC yields from resting maker limit orders.
"""

import os
import json
import math
import logging
import threading
from typing import Dict, List, Optional, Any

logger = logging.getLogger("RewardHarvester")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "dashboard_state.json")
MARKETS_FILE = os.path.join(BASE_DIR, "markets.json")


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (ValueError, TypeError):
        return default


class RewardHarvester:
    """
    Polymarket Liquidity Rewards Harvester and Arbitrage Priority Enhancer.
    """

    def __init__(
        self,
        market_token_map: Optional[Dict[str, dict]] = None,
        dash_state: Optional[Any] = None,
        state_file: Optional[str] = None
    ):
        self.lock = threading.RLock()
        self.market_token_map = market_token_map or {}
        self.dash_state = dash_state
        self.state_file = state_file or STATE_FILE
        self.reward_markets: Dict[str, dict] = {}

        # Initial populate
        self.update_reward_markets()

    @staticmethod
    def extract_daily_rate(market_data: dict) -> float:
        """Extract daily USDC reward pool rate from various Polymarket schemas."""
        if not isinstance(market_data, dict):
            return 0.0

        r = market_data.get("rewards_daily_rate")
        if r is not None:
            val = _safe_float(r, 0.0)
            if val > 0:
                return val

        rewards = market_data.get("rewards")
        if isinstance(rewards, dict):
            rates = rewards.get("rates", [])
            if isinstance(rates, list) and len(rates) > 0:
                first = rates[0]
                if isinstance(first, dict):
                    val = _safe_float(first.get("rewards_daily_rate"), 0.0)
                    if val > 0:
                        return val
        return 0.0

    def update_reward_markets(
        self,
        market_token_map: Optional[Dict[str, dict]] = None,
        markets_dict: Optional[Dict[str, dict]] = None
    ) -> int:
        """
        Maintains and updates active reward markets (rewards_daily_rate > 0)
        from market_token_map and dashboard_state["markets"].
        """
        if market_token_map is not None:
            self.market_token_map = market_token_map

        new_rewards: Dict[str, dict] = {}

        with self.lock:
            # Source 1: Passed markets_dict
            if isinstance(markets_dict, dict):
                for m_id, m_data in markets_dict.items():
                    rate = self.extract_daily_rate(m_data)
                    if rate > 0:
                        new_rewards[str(m_id)] = {
                            "market_id": str(m_id),
                            "question": m_data.get("question", f"Market {str(m_id)[-6:]}"),
                            "rewards_daily_rate": rate,
                            "min_size": _safe_float(m_data.get("min_size", 200.0), 200.0),
                            "max_spread": _safe_float(m_data.get("max_spread", 3.5), 3.5),
                            "liquidity": _safe_float(m_data.get("liquidity", 10000.0), 10000.0)
                        }

            # Source 2: market_token_map
            if isinstance(self.market_token_map, dict):
                for m_id, m_data in self.market_token_map.items():
                    rate = self.extract_daily_rate(m_data)
                    if rate > 0:
                        new_rewards[str(m_id)] = {
                            "market_id": str(m_id),
                            "question": m_data.get("question", f"Market {str(m_id)[-6:]}"),
                            "rewards_daily_rate": rate,
                            "min_size": _safe_float(m_data.get("min_size", 200.0), 200.0),
                            "max_spread": _safe_float(m_data.get("max_spread", 3.5), 3.5),
                            "liquidity": _safe_float(m_data.get("liquidity", 10000.0), 10000.0)
                        }

            # Source 3: dash_state or state_file on disk
            dash_markets = None
            if self.dash_state is not None:
                if hasattr(self.dash_state, "state") and isinstance(self.dash_state.state, dict):
                    dash_markets = self.dash_state.state.get("markets")
                elif isinstance(self.dash_state, dict):
                    dash_markets = self.dash_state.get("markets")

            if dash_markets is None and os.path.exists(self.state_file):
                try:
                    with open(self.state_file, "r", encoding="utf-8") as f:
                        s_data = json.load(f)
                    if isinstance(s_data, dict):
                        dash_markets = s_data.get("markets")
                except Exception:
                    dash_markets = None

            if isinstance(dash_markets, dict):
                for m_id, m_data in dash_markets.items():
                    rate = self.extract_daily_rate(m_data)
                    if rate > 0:
                        entry = new_rewards.get(str(m_id), {})
                        entry.update({
                            "market_id": str(m_id),
                            "question": m_data.get("question", entry.get("question", f"Market {str(m_id)[-6:]}")),
                            "rewards_daily_rate": rate,
                            "cost": _safe_float(m_data.get("cost")),
                            "edge": _safe_float(m_data.get("edge"))
                        })
                        new_rewards[str(m_id)] = entry

            self.reward_markets = new_rewards
            return len(self.reward_markets)

    def get_top_reward_markets(self, limit: int = 10) -> List[dict]:
        """Returns top markets sorted by rewards_daily_rate descending."""
        with self.lock:
            sorted_mkts = sorted(
                self.reward_markets.values(),
                key=lambda m: float(m.get("rewards_daily_rate", 0.0) or 0.0),
                reverse=True
            )
            if limit is not None and limit > 0:
                return sorted_mkts[:limit]
            return sorted_mkts

    def calculate_reward_priority(
        self,
        market_id: str,
        edge: float = 0.0,
        expected_profit: Optional[float] = None
    ) -> float:
        """
        Returns a combined priority score factoring in arbitrage expected profit / edge
        and daily reward pool rate.
        Favors reward markets over identical-edge non-reward markets.
        """
        with self.lock:
            market = self.reward_markets.get(str(market_id), {})
            daily_rate = float(market.get("rewards_daily_rate", 0.0) or 0.0)

        base = float(expected_profit) if expected_profit is not None else float(edge)
        reward_factor = 1e-4 if expected_profit is not None else 1e-5
        combined_score = base + (daily_rate * reward_factor)
        return round(combined_score, 6)

    def estimate_daily_reward_share(self, resting_capital: float, market_id: str) -> float:
        """
        Calculates conservative estimated daily USDC yield based on resting capital
        share against estimated competing qualifying book depth.
        """
        capital = float(resting_capital)
        if capital <= 0.0:
            return 0.0

        with self.lock:
            market = self.reward_markets.get(str(market_id), {})
            daily_rate = float(market.get("rewards_daily_rate", 0.0) or 0.0)
            if daily_rate <= 0.0:
                return 0.0

            competing_depth = max(5000.0, float(market.get("liquidity", 10000.0) or 10000.0))
            share = capital / (competing_depth + capital)
            estimated_yield = daily_rate * share
            return round(estimated_yield, 4)

    def get_summary(self) -> dict:
        """Returns status telemetry for dashboard integration."""
        with self.lock:
            top_mkts = self.get_top_reward_markets(limit=1)
            top_rate = top_mkts[0]["rewards_daily_rate"] if top_mkts else 0.0
            total_pool = sum(float(m.get("rewards_daily_rate", 0.0) or 0.0) for m in self.reward_markets.values())
            return {
                "active_reward_pools": len(self.reward_markets),
                "top_daily_pool_rate": top_rate,
                "total_daily_pool": total_pool,
                "status": "🟢 HARVESTING ACTIVE"
            }
