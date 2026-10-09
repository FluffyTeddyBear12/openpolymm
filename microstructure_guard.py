"""
Microstructure Guard for Polymarket CLOB Arbitrage.

Tracks real-time order book quotes and trade streams to detect adverse selection,
depth collapses, micro-price drift, and toxic order flow imbalance (DOBI).
Provides pre-fill toxicity evasion to abort Leg 1 resting maker orders with $0.00 loss.
"""

from collections import deque
from dataclasses import dataclass
import math
import time
from typing import Dict, List, Optional, Tuple


@dataclass
class QuoteRecord:
    timestamp: float
    best_bid: float
    bid_size: float
    best_ask: float
    ask_size: float


@dataclass
class TradeRecord:
    timestamp: float
    price: float
    size: float
    side: str


class MicrostructureGuard:
    """
    Real-time high-frequency microstructure tracking and toxicity evasion shield.
    Maintains a sliding window of top-of-book quotes and trade ticks per token.
    """

    def __init__(self, max_window_seconds: float = 120.0):
        self.max_window_seconds = max_window_seconds
        self.quotes: Dict[str, deque] = {}
        self.trades: Dict[str, deque] = {}

    def _prune(self, token_id: str, now: float):
        cutoff = now - self.max_window_seconds
        q_queue = self.quotes.get(token_id)
        if q_queue:
            while q_queue and q_queue[0].timestamp < cutoff:
                q_queue.popleft()
        t_queue = self.trades.get(token_id)
        if t_queue:
            while t_queue and t_queue[0].timestamp < cutoff:
                t_queue.popleft()

    def record_quote(
        self,
        token_id: str,
        best_bid: float,
        bid_size: float,
        best_ask: float,
        ask_size: float,
        timestamp: Optional[float] = None,
    ):
        now = time.time() if timestamp is None else float(timestamp)
        if token_id not in self.quotes:
            self.quotes[token_id] = deque()
        self.quotes[token_id].append(
            QuoteRecord(
                timestamp=now,
                best_bid=float(best_bid or 0.0),
                bid_size=float(bid_size or 0.0),
                best_ask=float(best_ask or 0.0),
                ask_size=float(ask_size or 0.0),
            )
        )
        self._prune(token_id, now)

    def record_trade(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
        timestamp: Optional[float] = None,
    ):
        now = time.time() if timestamp is None else float(timestamp)
        if token_id not in self.trades:
            self.trades[token_id] = deque()
        self.trades[token_id].append(
            TradeRecord(
                timestamp=now,
                price=float(price or 0.0),
                size=float(size or 0.0),
                side=str(side).upper(),
            )
        )
        self._prune(token_id, now)

    def get_latest_quote(self, token_id: str) -> Optional[QuoteRecord]:
        q = self.quotes.get(token_id)
        if q and len(q) > 0:
            return q[-1]
        return None

    def compute_obi(self, token_id: str) -> float:
        """
        Level-1 Order Book Imbalance (OBI):
        (Vb - Va) / (Vb + Va)
        Bounded between -1.0 (pure ask pressure) and +1.0 (pure bid pressure).
        """
        quote = self.get_latest_quote(token_id)
        if not quote:
            return 0.0
        vb = quote.bid_size
        va = quote.ask_size
        denom = vb + va
        if denom <= 1e-9:
            return 0.0
        obi = (vb - va) / denom
        return max(-1.0, min(1.0, obi))

    def compute_dual_obi(self, token_yes: str, token_no: str) -> float:
        """
        Cross-Outcome Dual-Book Imbalance (DOBI):
        ((Vb_yes - Va_yes) - (Vb_no - Va_no)) / Sum(V)
        where Sum(V) = Vb_yes + Va_yes + Vb_no + Va_no.
        Measures asymmetric order book skew across complementary outcomes.
        """
        q_yes = self.get_latest_quote(token_yes)
        q_no = self.get_latest_quote(token_no)
        vb_yes = q_yes.bid_size if q_yes else 0.0
        va_yes = q_yes.ask_size if q_yes else 0.0
        vb_no = q_no.bid_size if q_no else 0.0
        va_no = q_no.ask_size if q_no else 0.0

        sum_v = vb_yes + va_yes + vb_no + va_no
        if sum_v <= 1e-9:
            return 0.0

        numerator = (vb_yes - va_yes) - (vb_no - va_no)
        dobi = numerator / sum_v
        return max(-1.0, min(1.0, dobi))

    def compute_micro_price(self, token_id: str, quote: Optional[QuoteRecord] = None) -> float:
        """
        Stoikov Micro-Price:
        (Vb * Pa + Va * Pb) / (Vb + Va)
        Volume-weighted fair value inside the spread.
        """
        q = quote or self.get_latest_quote(token_id)
        if not q:
            return 0.0
        pa = q.best_ask
        pb = q.best_bid
        va = q.ask_size
        vb = q.bid_size
        denom = vb + va
        if denom <= 1e-9:
            if pa > 0 and pb > 0:
                return (pa + pb) / 2.0
            return pa if pa > 0 else pb
        return (vb * pa + va * pb) / denom

    def compute_micro_drift(self, token_id: str, window_ms: float = 500.0) -> float:
        """
        Drift of Stoikov micro-price over a trailing window (default 500ms).
        Returns: current_micro_price - historical_micro_price.
        """
        q_queue = self.quotes.get(token_id)
        if not q_queue or len(q_queue) < 2:
            return 0.0

        curr_micro = self.compute_micro_price(token_id, q_queue[-1])
        target_time = q_queue[-1].timestamp - (window_ms / 1000.0)

        hist_quote = None
        for i in range(len(q_queue) - 2, -1, -1):
            if q_queue[i].timestamp <= target_time:
                hist_quote = q_queue[i]
                break

        if not hist_quote:
            hist_quote = q_queue[0]

        hist_micro = self.compute_micro_price(token_id, hist_quote)
        return curr_micro - hist_micro

    def compute_trade_velocity_surge(
        self,
        token_id: str,
        short_ms: float = 500.0,
        baseline_sec: float = 60.0,
    ) -> float:
        """
        Ratio of short-term volume rate (e.g. 500ms) vs 60s baseline volume rate.
        Ratio >= 4.5 indicates an aggressive toxic sweep or burst.
        """
        t_queue = self.trades.get(token_id)
        if not t_queue:
            return 1.0

        now = t_queue[-1].timestamp
        short_cutoff = now - (short_ms / 1000.0)
        baseline_cutoff = now - baseline_sec

        short_vol = 0.0
        baseline_vol = 0.0

        for t in reversed(t_queue):
            if t.timestamp >= baseline_cutoff:
                baseline_vol += t.size
                if t.timestamp >= short_cutoff:
                    short_vol += t.size
            else:
                break

        short_duration = short_ms / 1000.0
        short_rate = short_vol / short_duration if short_duration > 0 else 0.0
        baseline_rate = baseline_vol / baseline_sec if baseline_sec > 0 else 0.0

        if baseline_rate <= 1e-9:
            if short_vol > 0:
                return 5.0
            return 1.0

        return short_rate / baseline_rate

    def check_toxicity_evasion(
        self,
        token_maker: str,
        token_taker: str,
        maker_price: float,
        initial_taker_depth: float,
        initial_taker_price: float,
        fee_rate: float = 0.0,
    ) -> Tuple[bool, str, dict]:
        """
        Pre-fill Toxicity Evasion Engine.
        Evaluates 5 adverse selection triggers during Leg 1 resting phase:
          Trigger 1: Leg 2 Depth Collapse (live_taker_depth / depth_500ms_ago <= 0.50).
          Trigger 2: Parity Edge Evaporation (maker_price + live_taker_ask * (1 + fee) >= 0.9950).
          Trigger 3: Micro-Price Spike on Taker Leg (drift >= 0.0030 / 3 ticks).
          Trigger 4: Toxic DOBI Skew against maker position (dobi <= -0.65).
          Trigger 5: Volume Burst Surge (trade surge ratio >= 4.5).

        Returns:
          (True, trigger_name, metrics) if toxicity is detected -> CANCEL Leg 1 immediately!
          (False, "SAFE", metrics) if safe to remain resting.
        """
        taker_quote = self.get_latest_quote(token_taker)
        live_taker_depth = taker_quote.ask_size if taker_quote else initial_taker_depth
        live_taker_ask = taker_quote.best_ask if taker_quote else initial_taker_price

        # Historical depth 500ms ago
        depth_500ms_ago = initial_taker_depth
        q_queue = self.quotes.get(token_taker)
        if q_queue and len(q_queue) > 1:
            target_ts = q_queue[-1].timestamp - 0.500
            for i in range(len(q_queue) - 2, -1, -1):
                if q_queue[i].timestamp <= target_ts:
                    depth_500ms_ago = q_queue[i].ask_size
                    break
            else:
                depth_500ms_ago = q_queue[0].ask_size

        if depth_500ms_ago <= 0:
            depth_500ms_ago = initial_taker_depth if initial_taker_depth > 0 else 1.0

        depth_ratio = live_taker_depth / depth_500ms_ago if depth_500ms_ago > 0 else 1.0
        parity_cost = maker_price + (live_taker_ask * (1.0 + fee_rate))
        micro_drift = self.compute_micro_drift(token_taker, window_ms=500.0)
        dobi = self.compute_dual_obi(token_maker, token_taker)
        trade_surge = self.compute_trade_velocity_surge(token_taker, short_ms=500.0)

        metrics = {
            "live_taker_depth": round(live_taker_depth, 2),
            "depth_500ms_ago": round(depth_500ms_ago, 2),
            "depth_ratio": round(depth_ratio, 4),
            "live_taker_ask": round(live_taker_ask, 4),
            "parity_cost": round(parity_cost, 4),
            "micro_drift": round(micro_drift, 5),
            "dobi": round(dobi, 4),
            "trade_surge": round(trade_surge, 2),
            "maker_price": round(maker_price, 4),
        }

        # Trigger 1: Leg 2 Depth Collapse (liquidity evaporating on taker leg)
        if depth_ratio <= 0.50 or (initial_taker_depth > 0 and live_taker_depth <= 0.50 * initial_taker_depth):
            return True, "LEG2_DEPTH_COLLAPSE", metrics

        # Trigger 2: Parity Edge Evaporation (taker ask pushed total cost >= 0.9950)
        if parity_cost >= 0.9950:
            return True, "PARITY_EDGE_EVAPORATED", metrics

        # Trigger 3: Micro-Price Spike on Taker Leg (taker fair value jumped >= 3 ticks)
        if micro_drift >= 0.0030:
            return True, "TAKER_MICRO_PRICE_SPIKE", metrics

        # Trigger 4: Toxic DOBI Skew against maker position
        if dobi <= -0.65:
            return True, "TOXIC_DOBI_SKEW", metrics

        # Trigger 5: Volume Burst Surge (trade surge ratio >= 4.5)
        if trade_surge >= 4.5:
            return True, "VOLUME_BURST_SURGE", metrics

        return False, "SAFE", metrics
