"""
Dynamic Hedge Router with Elastic Edge Compression for Polymarket CLOB Arbitrage.

Calculates exact zero-loss elastic taker price ceilings and multi-level VWAP sweeps.
Eliminates adverse selection losses on Leg 2 by enforcing elastic edge compression.
"""

import math
from typing import Any, List, Tuple, Union


class DynamicHedgeRouter:
    """
    Elastic edge compression and multi-level book sweeping router.
    """

    @staticmethod
    def calculate_elastic_taker_price(
        maker_price: float,
        fee_rate: float = 0.0,
        breakeven_buffer: float = 0.0005,
        tick_size: float = 0.001,
    ) -> float:
        """
        Calculates the maximum acceptable taker price ensuring net arbitrage profit >= 0.0000:
        Floor((1.0000 - maker_price) / (1.0 + fee_rate) - breakeven_buffer) aligned to tick_size.
        """
        if tick_size <= 0:
            tick_size = 0.001

        raw_taker_ceiling = (1.0000 - maker_price) / (1.0 + fee_rate) - breakeven_buffer
        if raw_taker_ceiling <= 0:
            return 0.0

        # Floor to tick size
        steps = math.floor(round(raw_taker_ceiling / tick_size, 6))
        elastic_price = round(steps * tick_size, 4)
        return max(0.0, elastic_price)

    @staticmethod
    def calculate_vwap_sweep(
        asks: List[Union[dict, Tuple[float, float], List[float]]],
        required_size: float,
        max_acceptable_cost: float,
        fee_rate: float = 0.0,
    ) -> Tuple[bool, float, float]:
        """
        Multi-level volume-weighted average price (VWAP) calculation for taker sweeps.
        Returns:
          (can_fill: bool, vwap_price: float, total_cost_with_fees: float)
        """
        if not asks or required_size <= 0:
            return False, 0.0, 0.0

        parsed_asks: List[Tuple[float, float]] = []
        for a in asks:
            try:
                if isinstance(a, dict):
                    p = float(a.get("price", 0.0))
                    s = float(a.get("size", 0.0))
                else:
                    p = float(a[0])
                    s = float(a[1])
                if p > 0 and s > 0:
                    parsed_asks.append((p, s))
            except (ValueError, TypeError, IndexError):
                continue

        parsed_asks.sort(key=lambda x: x[0])

        accumulated_size = 0.0
        accumulated_notional = 0.0

        for price, size in parsed_asks:
            needed = required_size - accumulated_size
            if needed <= 0:
                break
            fill = min(needed, size)
            accumulated_size += fill
            accumulated_notional += fill * price

        if accumulated_size < (required_size - 1e-6):
            vwap = accumulated_notional / accumulated_size if accumulated_size > 0 else 0.0
            total_cost = accumulated_notional * (1.0 + fee_rate)
            return False, round(vwap, 4), round(total_cost, 4)

        vwap = accumulated_notional / required_size
        total_cost = accumulated_notional * (1.0 + fee_rate)
        effective_unit_cost = total_cost / required_size

        if effective_unit_cost > max_acceptable_cost:
            return False, round(vwap, 4), round(total_cost, 4)

        return True, round(vwap, 4), round(total_cost, 4)
