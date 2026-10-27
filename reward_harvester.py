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
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

logger = logging.getLogger("RewardHarvester")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "dashboard_state.json")
MARKETS_FILE = os.path.join(BASE_DIR, "markets.json")


@dataclass(frozen=True)
class PairedRewardQuote:
    market_id: str
    bid_yes: float
    bid_no: float
    size_shares: float
    required_capital_usd: float
    q_one: float
    q_two: float
    q_min: float
    locked_pair_edge: float
    estimated_daily_usdc: float


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (ValueError, TypeError):
        return default


def compute_quadratic_order_score(
    max_spread_cents: float,
    distance_cents: float,
    size: float
) -> float:
    """
    Computes single-order Polymarket quadratic reward score:
        S(v, s) = ((v - s) / v)^2 * size
    where:
        v = max_spread_cents (max incentive spread distance, e.g. 3.5 cents)
        s = distance_cents from midpoint
        size = order size in shares
    Returns 0.0 if distance >= max_spread, max_spread <= 0, or size <= 0.
    """
    v = _safe_float(max_spread_cents, 0.0)
    s = _safe_float(distance_cents, 0.0)
    sz = _safe_float(size, 0.0)

    if v <= 0.0 or sz <= 0.0:
        return 0.0
    if s < 0.0:
        s = abs(s)
    if s >= v:
        return 0.0

    score = (((v - s) / v) ** 2) * sz
    return float(score)


def combine_two_sided_q_score(
    q_one: float,
    q_two: float,
    midpoint: float = 0.50,
    c: float = 3.0
) -> float:
    """
    Combines two-sided order scores under Polymarket's [0.10, 0.90] midpoint rule.
    In-band (0.10 <= midpoint <= 0.90):
        single-sided liquidity receives 1/c (default 1/3) credit:
        combined = max(min(q_one, q_two), max(q_one, q_two) / c)
    Outside band (midpoint < 0.10 or midpoint > 0.90):
        only two-sided liquidity scores:
        combined = min(q_one, q_two)
    """
    q1 = max(0.0, _safe_float(q_one, 0.0))
    q2 = max(0.0, _safe_float(q_two, 0.0))
    mid = _safe_float(midpoint, 0.50)
    divisor = max(_safe_float(c, 3.0), 1e-6)

    if 0.10 <= mid <= 0.90:
        return float(max(min(q1, q2), max(q1, q2) / divisor))
    return float(min(q1, q2))


def compute_reward_efficiency_index(
    rewards_daily_rate: float,
    min_size: float,
    mid_price: float = 0.50,
    max_spread: float = 3.5
) -> float:
    """
    Computes Reward Efficiency Index (REI).
    Formula:
        capital_commitment = max(min_size * max(mid_price, 0.05), 1.0)
        spread_factor = max(max_spread, 0.5)
        rei = rewards_daily_rate / (capital_commitment * spread_factor)
    """
    rate = _safe_float(rewards_daily_rate, 0.0)
    size = _safe_float(min_size, 200.0)
    price = _safe_float(mid_price, 0.50)
    spread = _safe_float(max_spread, 3.5)

    capital_commitment = max(size * max(price, 0.05), 1.0)
    spread_factor = max(spread, 0.5)
    rei = rate / (capital_commitment * spread_factor)
    return round(rei, 6)


class RewardHarvester:
    """
    Polymarket Liquidity Rewards Harvester and Arbitrage Priority Enhancer.
    """

    compute_reward_efficiency_index = staticmethod(compute_reward_efficiency_index)
    compute_quadratic_order_score = staticmethod(compute_quadratic_order_score)
    combine_two_sided_q_score = staticmethod(combine_two_sided_q_score)

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
        if isinstance(rewards, (int, float)) and not isinstance(rewards, bool):
            val = _safe_float(rewards, 0.0)
            if val > 0:
                return val
        elif isinstance(rewards, dict):
            r_val = rewards.get("rewards_daily_rate") or rewards.get("daily_rate") or rewards.get("rate")
            if r_val is not None:
                val = _safe_float(r_val, 0.0)
                if val > 0:
                    return val
            rates = rewards.get("rates", [])
            if isinstance(rates, list) and len(rates) > 0:
                first = rates[0]
                if isinstance(first, dict):
                    val = _safe_float(first.get("rewards_daily_rate") or first.get("rate") or first.get("daily_rate"), 0.0)
                    if val > 0:
                        return val
        elif isinstance(rewards, list) and len(rewards) > 0:
            first = rewards[0]
            if isinstance(first, dict):
                val = _safe_float(first.get("rewards_daily_rate") or first.get("rate") or first.get("daily_rate"), 0.0)
                if val > 0:
                    return val

        meta = market_data.get("market_meta")
        if isinstance(meta, dict):
            return RewardHarvester.extract_daily_rate(meta)

        return 0.0

    @staticmethod
    def extract_min_size(market_data: dict, default: float = 200.0) -> float:
        """Extract min qualifying size for rewards from various Polymarket schemas."""
        if not isinstance(market_data, dict):
            return default

        for key in ("rewards_min_size", "min_size"):
            if key in market_data and market_data[key] is not None:
                val = _safe_float(market_data[key], 0.0)
                if val > 0:
                    return val

        rewards = market_data.get("rewards")
        if isinstance(rewards, dict):
            for key in ("rewards_min_size", "min_size"):
                if key in rewards and rewards[key] is not None:
                    val = _safe_float(rewards[key], 0.0)
                    if val > 0:
                        return val
            rates = rewards.get("rates", [])
            if isinstance(rates, list) and len(rates) > 0 and isinstance(rates[0], dict):
                first = rates[0]
                for key in ("rewards_min_size", "min_size"):
                    if key in first and first[key] is not None:
                        val = _safe_float(first[key], 0.0)
                        if val > 0:
                            return val
        elif isinstance(rewards, list) and len(rewards) > 0 and isinstance(rewards[0], dict):
            first = rewards[0]
            for key in ("rewards_min_size", "min_size"):
                if key in first and first[key] is not None:
                    val = _safe_float(first[key], 0.0)
                    if val > 0:
                        return val

        meta = market_data.get("market_meta")
        if isinstance(meta, dict):
            val = RewardHarvester.extract_min_size(meta, default=0.0)
            if val > 0:
                return val

        return default

    @staticmethod
    def extract_max_spread(market_data: dict, default: float = 3.5) -> float:
        """Extract max spread threshold for rewards from various Polymarket schemas."""
        if not isinstance(market_data, dict):
            return default

        for key in ("rewards_max_spread", "max_spread"):
            if key in market_data and market_data[key] is not None:
                val = _safe_float(market_data[key], 0.0)
                if val > 0:
                    return val

        rewards = market_data.get("rewards")
        if isinstance(rewards, dict):
            for key in ("rewards_max_spread", "max_spread"):
                if key in rewards and rewards[key] is not None:
                    val = _safe_float(rewards[key], 0.0)
                    if val > 0:
                        return val
            rates = rewards.get("rates", [])
            if isinstance(rates, list) and len(rates) > 0 and isinstance(rates[0], dict):
                first = rates[0]
                for key in ("rewards_max_spread", "max_spread"):
                    if key in first and first[key] is not None:
                        val = _safe_float(first[key], 0.0)
                        if val > 0:
                            return val
        elif isinstance(rewards, list) and len(rewards) > 0 and isinstance(rewards[0], dict):
            first = rewards[0]
            for key in ("rewards_max_spread", "max_spread"):
                if key in first and first[key] is not None:
                    val = _safe_float(first[key], 0.0)
                    if val > 0:
                        return val

        meta = market_data.get("market_meta")
        if isinstance(meta, dict):
            val = RewardHarvester.extract_max_spread(meta, default=0.0)
            if val > 0:
                return val

        return default

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
                        min_sz = self.extract_min_size(m_data)
                        max_sp = self.extract_max_spread(m_data)
                        rei_val = self.compute_reward_efficiency_index(rate, min_size=min_sz, max_spread=max_sp)
                        new_rewards[str(m_id)] = {
                            "market_id": str(m_id),
                            "question": m_data.get("question", f"Market {str(m_id)[-6:]}"),
                            "rewards_daily_rate": rate,
                            "min_size": min_sz,
                            "max_spread": max_sp,
                            "rei": rei_val,
                            "liquidity": _safe_float(m_data.get("liquidity", 10000.0), 10000.0)
                        }

            # Source 2: market_token_map
            if isinstance(self.market_token_map, dict):
                for m_id, m_data in self.market_token_map.items():
                    rate = self.extract_daily_rate(m_data)
                    if rate > 0:
                        min_sz = self.extract_min_size(m_data)
                        max_sp = self.extract_max_spread(m_data)
                        rei_val = self.compute_reward_efficiency_index(rate, min_size=min_sz, max_spread=max_sp)
                        new_rewards[str(m_id)] = {
                            "market_id": str(m_id),
                            "question": m_data.get("question", f"Market {str(m_id)[-6:]}"),
                            "rewards_daily_rate": rate,
                            "min_size": min_sz,
                            "max_spread": max_sp,
                            "rei": rei_val,
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
                        min_sz = self.extract_min_size(m_data)
                        max_sp = self.extract_max_spread(m_data)
                        rei_val = self.compute_reward_efficiency_index(rate, min_size=min_sz, max_spread=max_sp)
                        entry = new_rewards.get(str(m_id), {})
                        entry.update({
                            "market_id": str(m_id),
                            "question": m_data.get("question", entry.get("question", f"Market {str(m_id)[-6:]}")),
                            "rewards_daily_rate": rate,
                            "min_size": min_sz if "min_size" not in entry else entry["min_size"],
                            "max_spread": max_sp if "max_spread" not in entry else entry["max_spread"],
                            "rei": rei_val,
                            "cost": _safe_float(m_data.get("cost")),
                            "edge": _safe_float(m_data.get("edge"))
                        })
                        new_rewards[str(m_id)] = entry

            self.reward_markets = new_rewards
            return len(self.reward_markets)

    def can_quote_reward_market(
        self,
        market_id: str,
        available_cash: float,
        mid_price: float = 0.50
    ) -> bool:
        """
        Enforce capital gating to avoid locking up disproportionate capital
        on oversized reward pools.
        Required capital (min_size * mid_price) must not exceed 40% of available cash
        (with a $5.00 min threshold for small balances).
        """
        with self.lock:
            market = self.reward_markets.get(str(market_id))
            if market is None:
                market = self.market_token_map.get(str(market_id), {})
            min_size = _safe_float(market.get("min_size", 200.0), 200.0)

        cash = _safe_float(available_cash, 0.0)
        # Sizing against full binary pair ($1.00) ensures sufficient margin for Leg 2 taker
        req_capital = min_size * 1.00
        max_allowed = max(cash * 0.40, 5.0)
        return req_capital <= max_allowed

    def get_top_reward_markets(self, limit: int = 10, sort_by: str = "rei") -> List[dict]:
        """
        Returns top markets sorted by REI ("rei") or daily rate ("rate").
        Computes REI for each market if not already populated.
        """
        with self.lock:
            markets = list(self.reward_markets.values())
            for m in markets:
                if "rei" not in m or m["rei"] is None:
                    rate = _safe_float(m.get("rewards_daily_rate"), 0.0)
                    min_size = _safe_float(m.get("min_size"), 200.0)
                    max_spread = _safe_float(m.get("max_spread"), 3.5)
                    m["rei"] = self.compute_reward_efficiency_index(rate, min_size=min_size, max_spread=max_spread)

            if sort_by == "rate":
                sorted_mkts = sorted(
                    markets,
                    key=lambda m: float(m.get("rewards_daily_rate", 0.0) or 0.0),
                    reverse=True
                )
            else:  # default "rei"
                sorted_mkts = sorted(
                    markets,
                    key=lambda m: float(m.get("rei", 0.0) or 0.0),
                    reverse=True
                )

            if limit is not None and limit > 0:
                return sorted_mkts[:limit]
            return sorted_mkts

    def calculate_reward_priority(
        self,
        market_id: str,
        edge: float = 0.0,
        expected_profit: Optional[float] = None,
        available_cash: Optional[float] = None
    ) -> float:
        """
        Returns a combined priority score factoring in arbitrage expected profit / edge
        and daily reward pool rate.
        Favors reward markets over identical-edge non-reward markets.
        If available_cash is provided and not can_quote_reward_market(market_id, available_cash):
            returns base edge/expected_profit without reward bonus.
        Otherwise adds bonus weighted by REI and daily rate.
        """
        base = float(expected_profit) if expected_profit is not None else float(edge)

        if available_cash is not None:
            if not self.can_quote_reward_market(market_id, available_cash=available_cash):
                return round(base, 6)

        with self.lock:
            market = self.reward_markets.get(str(market_id), {})
            daily_rate = float(market.get("rewards_daily_rate", 0.0) or 0.0)
            rei = float(market.get("rei", 0.0) or 0.0)
            if rei <= 0.0 and daily_rate > 0.0:
                min_size = float(market.get("min_size", 200.0) or 200.0)
                max_spread = float(market.get("max_spread", 3.5) or 3.5)
                rei = self.compute_reward_efficiency_index(daily_rate, min_size=min_size, max_spread=max_spread)

        if daily_rate <= 0.0:
            return round(base, 6)

        reward_factor = 0.002 if expected_profit is not None else 0.001
        bonus = (daily_rate * reward_factor) * (1.0 + min(rei, 10.0))
        combined_score = base + bonus
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
            top_mkts = self.get_top_reward_markets(limit=1, sort_by="rate")
            top_rate = top_mkts[0]["rewards_daily_rate"] if top_mkts else 0.0
            total_pool = sum(float(m.get("rewards_daily_rate", 0.0) or 0.0) for m in self.reward_markets.values())
            return {
                "active_reward_pools": len(self.reward_markets),
                "top_daily_pool_rate": top_rate,
                "total_daily_pool": total_pool,
                "status": "🟢 HARVESTING ACTIVE"
            }

    def build_paired_bid_quote(
        self,
        market_id: str,
        bid_yes: float,
        bid_no: float,
        size_shares: Optional[float] = None,
        midpoint_yes: Optional[float] = None,
        midpoint_no: Optional[float] = None,
        max_spread_cents: Optional[float] = None,
        daily_rate_override: Optional[float] = None,
    ) -> Optional[PairedRewardQuote]:
        """
        Constructs a PairedRewardQuote with quadratic scoring and estimated daily USDC rewards.
        Enforces binary locked pair pricing (edge = 1.0 - (bid_yes + bid_no)).
        """
        with self.lock:
            market = self.reward_markets.get(str(market_id))
            if market is None:
                market = self.market_token_map.get(str(market_id), {})

        b_yes = _safe_float(bid_yes, 0.0)
        b_no = _safe_float(bid_no, 0.0)

        if size_shares is None:
            sz = _safe_float(market.get("min_size", 200.0), 200.0)
        else:
            sz = _safe_float(size_shares, 200.0)

        if max_spread_cents is None:
            max_sp = self.extract_max_spread(market, default=3.5)
            v_cents = max_sp * 100.0 if max_sp <= 0.10 else max_sp
        else:
            v_cents = _safe_float(max_spread_cents, 3.5)

        if midpoint_yes is not None:
            mid_y = _safe_float(midpoint_yes, 0.50)
        else:
            mid_y = (b_yes + (1.0 - b_no)) / 2.0 if (b_yes > 0 and b_no > 0) else 0.50

        if midpoint_no is not None:
            mid_n = _safe_float(midpoint_no, 1.0 - mid_y)
        else:
            mid_n = 1.0 - mid_y

        dist_y_cents = abs(mid_y - b_yes) * 100.0 if b_yes <= 1.0 else abs(mid_y - b_yes)
        dist_n_cents = abs(mid_n - b_no) * 100.0 if b_no <= 1.0 else abs(mid_n - b_no)

        q_one = compute_quadratic_order_score(v_cents, dist_y_cents, sz)
        q_two = compute_quadratic_order_score(v_cents, dist_n_cents, sz)
        q_min = min(q_one, q_two)

        required_capital = (b_yes + b_no) * sz
        locked_edge = 1.0000 - (b_yes + b_no)

        rate = daily_rate_override if daily_rate_override is not None else self.extract_daily_rate(market)
        combined_q = combine_two_sided_q_score(q_one, q_two, midpoint=mid_y)
        if rate > 0.0 and combined_q > 0.0:
            competing = max(5000.0, _safe_float(market.get("liquidity", 10000.0), 10000.0))
            share = combined_q / (competing + combined_q)
            est_daily = rate * share
        else:
            est_daily = 0.0

        return PairedRewardQuote(
            market_id=str(market_id),
            bid_yes=round(b_yes, 4),
            bid_no=round(b_no, 4),
            size_shares=round(sz, 4),
            required_capital_usd=round(required_capital, 4),
            q_one=round(q_one, 4),
            q_two=round(q_two, 4),
            q_min=round(q_min, 4),
            locked_pair_edge=round(locked_edge, 6),
            estimated_daily_usdc=round(est_daily, 4),
        )

    def rank_paired_reward_markets(
        self,
        quotes: Optional[List[PairedRewardQuote]] = None,
        sort_by: str = "daily_usdc",
        limit: Optional[int] = None,
    ) -> List[PairedRewardQuote]:
        """
        Ranks paired reward quotes by estimated daily USDC, locked edge, or quadratic score.
        """
        quote_list = list(quotes) if quotes is not None else []
        if sort_by in ("edge", "locked_edge", "locked_pair_edge"):
            sorted_quotes = sorted(
                quote_list,
                key=lambda q: (q.locked_pair_edge, q.estimated_daily_usdc, q.q_min),
                reverse=True,
            )
        elif sort_by in ("q_min", "q_score", "score"):
            sorted_quotes = sorted(
                quote_list,
                key=lambda q: (q.q_min, q.estimated_daily_usdc, q.locked_pair_edge),
                reverse=True,
            )
        else:  # default "daily_usdc"
            sorted_quotes = sorted(
                quote_list,
                key=lambda q: (q.estimated_daily_usdc, q.locked_pair_edge, q.q_min),
                reverse=True,
            )

        if limit is not None and limit > 0:
            return sorted_quotes[:limit]
        return sorted_quotes
