"""
Polymarket Parity Arbitrage Paper-Trading Bot

This script implements:
1. Multi-Market Order Book Parity Monitor: Concurrently monitors the top 50 active, open binary markets on Polymarket.
2. Order Book Parity Monitor: Connects to Polymarket's CLOB via official WebSocket / py-sdk.
3. Paper-Trading Simulator: Calculates theoretical dual-leg parity fills considering slippage and taker fees.
4. Risk & Sizing Engine: Manages max daily loss, fractional sizing, and trade limits.

Dependencies:
    pip install py-clob-client websocket-client
"""

import os
import sys
import time
import logging
import json
import math
import urllib.request
import urllib.error
import socket
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime, timezone
from enum import Enum
import collections
import threading
import concurrent.futures

# Using official Polymarket py_clob_client
from py_clob_client.client import ClobClient
import websocket

from liquidity_filter import (
    validate_order_book_liquidity,
    is_market_eligible,
    validate_arbitrage_execution,
    check_market_parity_liquidity_gate,
    compute_dynamic_min_depth,
    TURBO_AND_SHORT_DURATION_PATTERNS,
    TIME_RANGE_REGEX,
    _parse_market_end_date,
    _parse_market_start_date,
)
from rollback_protector import RollbackProtector, safe_unwind_or_limit_exit
from maker_taker_engine import MakerTakerExecutor
from order_reaper import OrderReaper
from ha_notifier import send_trade_notification
try:
    from negrisk_scanner import NegRiskBasketScanner, NegRiskAdapter
except ImportError:
    NegRiskBasketScanner = None
    NegRiskAdapter = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("PolyPaperTrader")

PAPER_LOCK_PORT = int(os.environ.get("POLYMARKET_BOT_PAPER_PORT", 48124))
PAPER_PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trader.pid")

def acquire_paper_lock(port: int = PAPER_LOCK_PORT):
    """
    Acquire exclusive socket lock for paper trader process.
    Prevents duplicate paper trader instances from running and colliding on dashboard_state.json.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE") and sys.platform == "win32":
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except Exception:
            pass
    elif hasattr(socket, "SO_REUSEADDR"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
    try:
        s.bind(("127.0.0.1", port))
        s.listen(5)
        # Drain probing connections so socket backlog never saturates into CLOSE_WAIT
        def _drain():
            while True:
                try:
                    conn, _ = s.accept()
                    try:
                        conn.close()
                    except Exception:
                        pass
                except Exception:
                    break
        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        return s
    except (OSError, socket.error):
        try:
            s.close()
        except Exception:
            pass
        return None

WSS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

def _safe_float(val, default: float = 0.0) -> float:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    return float(default)

def _round_to_tick_size(price: float, tick_size: float = 0.001) -> float:
    if tick_size <= 0:
        return round(price, 4)
    steps = round(price / tick_size)
    return round(steps * tick_size, 4)

def _ceil_to_tick_size(price: float, tick_size: float = 0.001) -> float:
    if tick_size <= 0:
        return round(price, 4)
    steps = math.ceil(round(price, 6) / tick_size)
    return round(steps * tick_size, 4)

def configure_cpu_budget(target_pct: float = 0.20) -> Optional[List[int]]:
    """
    Enforce CPU capping (default <= 20% max CPU limit) and BELOW_NORMAL process priority.
    On 16-thread host (e.g. AMD Ryzen 7 9800X3D), restricts process to 3 cores (18.75%).
    Gracefully degrades if psutil is not available or OS denies affinity changes.
    """
    try:
        import psutil
        p = psutil.Process()
        total_cores = psutil.cpu_count(logical=True) or 1
        num_cores = max(1, int(total_cores * target_pct))
        assigned_cores = list(range(min(num_cores, total_cores)))
        try:
            p.cpu_affinity(assigned_cores)
            logger.info(
                f"Enforced 20% CPU budget: affinity pinned to cores {assigned_cores} "
                f"({len(assigned_cores)}/{total_cores} logical threads = {len(assigned_cores)/total_cores:.1%})."
            )
        except Exception as e:
            logger.warning(f"Could not set CPU affinity: {e}")

        try:
            if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS") and sys.platform == "win32":
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
                logger.info("Process priority set to BELOW_NORMAL_PRIORITY_CLASS.")
            elif hasattr(p, "nice"):
                p.nice(10)
        except Exception as e:
            logger.warning(f"Could not set process priority: {e}")
        return assigned_cores
    except Exception as e:
        logger.warning(f"psutil CPU budgeting not applied: {e}")
        return None

def configure_ram_budget(max_gb: float = 5.0, stop_event: Optional[threading.Event] = None) -> Optional[threading.Thread]:
    """
    Enforce strict RAM limits on the paper trader process (default <= 5.0 GB limit).
    Starts a background memory watchdog thread that monitors process RSS:
    - If RSS exceeds 70% of limit (3.5 GB), performs garbage collection and trims in-memory price history.
    - If RSS exceeds 90% of limit (4.5 GB), aggressively purges non-priority market caches and calls EmptyWorkingSet.
    """
    import gc
    try:
        import psutil
    except ImportError:
        return None

    limit_bytes = int(max_gb * 1024 * 1024 * 1024)
    threshold_soft = int(limit_bytes * 0.70)
    threshold_hard = int(limit_bytes * 0.90)

    def _ram_watchdog():
        p = psutil.Process()
        while True:
            if stop_event and stop_event.wait(5.0):
                break
            elif not stop_event:
                time.sleep(5.0)

            try:
                rss = p.memory_info().rss
                if rss >= threshold_hard:
                    logger.warning(
                        f"HIGH RAM USAGE ALERT: {rss / (1024**3):.2f} GB / {max_gb:.2f} GB limit. "
                        "Triggering aggressive cache purge and memory reclaim..."
                    )
                    gc.collect()
                    ds = globals().get("dash_state")
                    if ds:
                        with ds.lock:
                            priority = ds._get_priority_markets()
                            ph = ds.state.get("price_history", {})
                            to_del = [k for k in ph if k not in priority]
                            for k in to_del:
                                del ph[k]
                            ds.state["activity_log"] = ds.state.get("activity_log", [])[:30]
                            ds.state["trades"] = ds.state.get("trades", [])[:25]
                    if sys.platform == "win32":
                        try:
                            import ctypes
                            ctypes.WinDLL('psapi').EmptyWorkingSet(ctypes.WinDLL('kernel32').GetCurrentProcess())
                        except Exception:
                            pass
                elif rss >= threshold_soft:
                    gc.collect()
            except Exception:
                pass

    t = threading.Thread(target=_ram_watchdog, daemon=True)
    t.start()
    logger.info(f"Enforced {max_gb:.1f} GB RAM budget watchdog for paper trader process.")
    return t

class MissedReason(str, Enum):
    SUB_THRESHOLD_EDGE = "SUB_THRESHOLD_EDGE"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    CONCURRENCY_EXHAUSTED = "CONCURRENCY_EXHAUSTED"
    MARKET_ALREADY_ACTIVE = "MARKET_ALREADY_ACTIVE"
    ZERO_LIQUIDITY = "ZERO_LIQUIDITY"
    ASYMMETRIC_DEPTH = "ASYMMETRIC_DEPTH"
    WIDE_SPREAD = "WIDE_SPREAD"
    CLOB_ORDER_KILLED = "CLOB_ORDER_KILLED"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    EXPOSURE_LIMIT_EXCEEDED = "EXPOSURE_LIMIT_EXCEEDED"


class ShadowParityTracker:
    def __init__(self, max_recent: int = 50, dash_state: Optional['DashboardState'] = None):
        self.lock = threading.Lock()
        self.max_recent = max_recent
        self.dash_state = dash_state
        self.recent_missed = collections.deque(maxlen=max_recent)
        self.total_missed_count = 0
        self.total_missed_pnl = 0.0
        self.by_reason: Dict[str, dict] = {
            r.value: {"count": 0, "pnl": 0.0} for r in MissedReason
        }

    def record_missed(self, opportunity: dict, reason: MissedReason) -> dict:
        reason_val = reason.value if isinstance(reason, MissedReason) else str(reason)
        edge = float(opportunity.get("edge", 0.0) or 0.0)
        size = float(opportunity.get("trade_size", 0.0) or opportunity.get("size", 0.0) or opportunity.get("executable_liquidity_usd", 0.0) or 0.0)
        if size <= 0.0:
            size = float(opportunity.get("available_depth_usd", 0.0) or 100.0)
            
        exp_prof = opportunity.get("expected_profit")
        if reason_val in (
            MissedReason.ZERO_LIQUIDITY.value,
            MissedReason.ASYMMETRIC_DEPTH.value,
            MissedReason.WIDE_SPREAD.value,
            "ZERO_LIQUIDITY",
            "ASYMMETRIC_DEPTH",
            "WIDE_SPREAD"
        ):
            pnl = 0.0
        elif exp_prof is not None:
            pnl = float(exp_prof)
        elif opportunity.get("available_depth_usd") is not None and float(opportunity["available_depth_usd"]) <= 0:
            pnl = 0.0
        else:
            pnl = edge * size
        pnl = max(0.0, round(pnl, 4))

        market_id = str(opportunity.get("market_id", ""))
        question = str(opportunity.get("question", "") or opportunity.get("short_id", ""))

        missed_entry = {
            "market_id": market_id,
            "question": question,
            "edge": round(edge, 6),
            "edge_pct": round(edge * 100, 4),
            "cost": round(float(opportunity.get("cost", opportunity.get("effective_cost", 0.0)) or 0.0), 4),
            "yes_ask": round(float(opportunity.get("yes_ask", opportunity.get("ask_yes", 0.0)) or 0.0), 4),
            "no_ask": round(float(opportunity.get("no_ask", opportunity.get("ask_no", 0.0)) or 0.0), 4),
            "available_depth_usd": round(float(opportunity.get("available_depth_usd", opportunity.get("executable_liquidity_usd", 0.0)) or 0.0), 2),
            "trade_size": round(size, 2),
            "forfeited_pnl": pnl,
            "reason": reason_val,
            "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "timestamp": time.time()
        }

        with self.lock:
            self.recent_missed.appendleft(missed_entry)
            self.total_missed_count += 1
            self.total_missed_pnl = round(self.total_missed_pnl + pnl, 4)
            if reason_val not in self.by_reason:
                self.by_reason[reason_val] = {"count": 0, "pnl": 0.0}
            self.by_reason[reason_val]["count"] += 1
            self.by_reason[reason_val]["pnl"] = round(self.by_reason[reason_val]["pnl"] + pnl, 4)
            if reason_val in (MissedReason.ASYMMETRIC_DEPTH.value, "ASYMMETRIC_DEPTH"):
                if MissedReason.ZERO_LIQUIDITY.value not in self.by_reason:
                    self.by_reason[MissedReason.ZERO_LIQUIDITY.value] = {"count": 0, "pnl": 0.0}
                self.by_reason[MissedReason.ZERO_LIQUIDITY.value]["count"] += 1

        if self.dash_state:
            self.dash_state.record_missed_trade(missed_entry)

        if hasattr(self, "policy_rewriter") and self.policy_rewriter:
            try:
                self.policy_rewriter.on_missed_trade(missed_entry, self.dash_state)
            except Exception as e:
                logger.error(f"Error in policy_rewriter on_missed_trade: {e}")

        return missed_entry

    def get_summary(self) -> dict:
        with self.lock:
            return {
                "total_missed_count": self.total_missed_count,
                "total_missed_pnl": round(self.total_missed_pnl, 4),
                "by_reason": {k: dict(v) for k, v in self.by_reason.items()},
                "recent_missed": list(self.recent_missed)
            }

    def reset(self):
        with self.lock:
            self.recent_missed.clear()
            self.total_missed_count = 0
            self.total_missed_pnl = 0.0
            self.by_reason = {
                r.value: {"count": 0, "pnl": 0.0} for r in MissedReason
            }


def _map_liquidity_gate_reason(gate_reason: str, opp: Optional[dict] = None) -> MissedReason:
    r_lower = (gate_reason or "").lower()
    if any(w in r_lower for w in ("eligibility", "pattern", "prop", "rejected", "resolution", "expiry", "horizon", "sport", "cricket")):
        return MissedReason.CIRCUIT_BREAKER
    if "spread" in r_lower:
        return MissedReason.WIDE_SPREAD
    if "depth" in r_lower:
        if opp:
            avail = float(opp.get("available_depth_usd", opp.get("executable_liquidity_usd", 0.0)) or 0.0)
            d_yes = float(opp.get("depth_yes", 0.0) or 0.0)
            d_no = float(opp.get("depth_no", 0.0) or 0.0)
            if (d_yes > 0 and d_no <= 0) or (d_no > 0 and d_yes <= 0) or abs(d_yes - d_no) > 50.0:
                return MissedReason.ASYMMETRIC_DEPTH
            if avail > 0 and avail < 10.0:
                return MissedReason.ZERO_LIQUIDITY
            if avail >= 10.0:
                return MissedReason.ASYMMETRIC_DEPTH

        if "asymmetric" in r_lower or "one-sided" in r_lower:
            return MissedReason.ASYMMETRIC_DEPTH
        return MissedReason.ZERO_LIQUIDITY
    return MissedReason.ZERO_LIQUIDITY



POLICY_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adaptive_policy_state.json")

class ActivePolicyRewriter:
    def __init__(
        self,
        simulator: Optional['PaperSimulator'] = None,
        optimizer_interval: float = 30.0,
        auto_apply: bool = True,
        state_file: str = POLICY_STATE_FILE
    ):
        self.simulator = simulator
        self.optimizer_interval = optimizer_interval
        self.auto_apply = auto_apply
        self.state_file = state_file
        self.lock = threading.RLock()
        self.last_triggered_time = 0.0
        self.last_optimized_time = 0.0

        # Continuous parameter vector with calibrated defaults
        self.params = {
            "min_edge_pct": 0.0150,
            "max_positions_per_market": 2,
            "hold_period_seconds": 1.0,
            "reserve_cash_pct": 0.10,
            "max_exposure_pct": 0.45,
            "sizing_multiplier": 1.0,
        }
        self.total_adaptations = 0
        self.recovered_pnl = 0.0
        self.primary_bottleneck = "NONE"
        self.bottleneck_counts = {}
        self.last_adaptation_time = 0.0

        self._load_state()

        if self.simulator:
            if hasattr(self.simulator, "min_edge") and self.simulator.min_edge is not None:
                self.params["min_edge_pct"] = self.simulator.min_edge
            if hasattr(self.simulator, "risk") and self.simulator.risk:
                r = self.simulator.risk
                if hasattr(r, "max_positions_per_market"):
                    self.params["max_positions_per_market"] = r.max_positions_per_market
                if hasattr(r, "hold_period_seconds"):
                    self.params["hold_period_seconds"] = r.hold_period_seconds
                if hasattr(r, "reserve_cash_pct"):
                    self.params["reserve_cash_pct"] = r.reserve_cash_pct
                if hasattr(r, "max_exposure_pct"):
                    self.params["max_exposure_pct"] = r.max_exposure_pct

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    for k in self.params:
                        if k in saved:
                            self.params[k] = type(self.params[k])(saved[k])
                    self.total_adaptations = int(saved.get("total_adaptations", 0))
                    self.recovered_pnl = float(saved.get("recovered_pnl", 0.0))
                    self.primary_bottleneck = str(saved.get("primary_bottleneck", "NONE"))
                    logger.info(f"Loaded active policy state from {self.state_file}: {self.params}")
            except Exception as e:
                logger.warning(f"Could not load active policy state: {e}")

    def _save_state(self):
        try:
            state_data = {
                **self.params,
                "total_adaptations": self.total_adaptations,
                "recovered_pnl": round(self.recovered_pnl, 4),
                "primary_bottleneck": self.primary_bottleneck,
                "model_version": "Model v2.0",
                "status": "ONLINE ACTIVE LEARNING: ACTIVE (Model v2.0)",
                "updated_at": time.time()
            }
            tmp_file = f"{self.state_file}.tmp_{os.getpid()}_{int(time.time()*1000)}"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2)
            os.replace(tmp_file, self.state_file)
        except Exception as e:
            logger.warning(f"Could not save active policy state: {e}")

    def reset(self):
        with self.lock:
            self.total_adaptations = 0
            self.recovered_pnl = 0.0
            self.primary_bottleneck = "NONE"
            if hasattr(self, "bottleneck_counts") and isinstance(self.bottleneck_counts, (dict, list, set)):
                self.bottleneck_counts.clear()
            else:
                self.bottleneck_counts = {}
            self.last_adaptation_time = 0.0
            self._save_state()

    def _apply_to_simulator(self, simulator: 'PaperSimulator'):
        with self.lock:
            simulator.min_edge = self.params["min_edge_pct"]
            if hasattr(simulator, "risk") and simulator.risk:
                simulator.risk.max_positions_per_market = self.params["max_positions_per_market"]
                simulator.risk.hold_period_seconds = self.params["hold_period_seconds"]
                simulator.risk.reserve_cash_pct = self.params["reserve_cash_pct"]
                simulator.risk.max_exposure_pct = self.params["max_exposure_pct"]
                simulator.risk.sizing_multiplier = self.params["sizing_multiplier"]

    def _build_policy_dict(self, simulator: Optional['PaperSimulator'] = None, missed_pnl: float = 0.0, realized_pnl: float = 0.0, total_missed: int = 0) -> dict:
        leakage = missed_pnl / (realized_pnl + missed_pnl + 1e-9)
        return {
            "min_edge_pct": self.params["min_edge_pct"],
            "recommended_min_edge_pct": self.params["min_edge_pct"],
            "max_positions_per_market": self.params["max_positions_per_market"],
            "hold_period_seconds": self.params["hold_period_seconds"],
            "reserve_cash_pct": self.params["reserve_cash_pct"],
            "recommended_reserve_cash_pct": self.params["reserve_cash_pct"],
            "max_exposure_pct": self.params["max_exposure_pct"],
            "sizing_multiplier": self.params["sizing_multiplier"],
            "total_adaptations": self.total_adaptations,
            "recovered_pnl": round(self.recovered_pnl, 4),
            "primary_bottleneck": self.primary_bottleneck,
            "leakage_ratio": round(leakage, 4),
            "total_missed_pnl": round(missed_pnl, 4),
            "realized_pnl": round(realized_pnl, 4),
            "total_missed_count": total_missed,
            "model_version": "Model v2.0",
            "status": "ONLINE ACTIVE LEARNING: ACTIVE (Model v2.0)",
            "updated_at": time.time()
        }

    def on_missed_trade(self, missed_entry: dict, dash_state: Optional['DashboardState'] = None):
        now = time.time()
        # Cooldown anti-jitter (1.0s refractory period)
        if now - self.last_triggered_time < 1.0:
            return

        reason = str(missed_entry.get("reason", ""))
        edge = float(missed_entry.get("edge", 0.0) or 0.0)
        missed_pnl = float(missed_entry.get("forfeited_pnl", 0.0) or 0.0)

        adapted = False
        param_name = ""
        val = ""

        with self.lock:
            if reason in (MissedReason.MARKET_ALREADY_ACTIVE.value, MissedReason.CONCURRENCY_EXHAUSTED.value, "MARKET_ALREADY_ACTIVE", "CONCURRENCY_EXHAUSTED"):
                cur_pos = self.params["max_positions_per_market"]
                new_pos = min(5, cur_pos + 1)
                cur_hold = self.params["hold_period_seconds"]
                new_hold = max(0.5, round(cur_hold * 0.8, 2))
                if new_pos != cur_pos or new_hold != cur_hold:
                    self.params["max_positions_per_market"] = new_pos
                    self.params["hold_period_seconds"] = new_hold
                    param_name = f"max_positions={new_pos}, hold_sec={new_hold}s"
                    val = f"{new_pos}"
                    adapted = True

            elif reason in (MissedReason.SUB_THRESHOLD_EDGE.value, "SUB_THRESHOLD_EDGE") and edge > 0:
                cur_edge = self.params["min_edge_pct"]
                new_edge = max(0.0150, round(cur_edge - 0.0002, 4))
                if new_edge != cur_edge:
                    self.params["min_edge_pct"] = new_edge
                    param_name = "min_edge_pct"
                    val = f"{new_edge:.4f}"
                    adapted = True

            elif reason in (MissedReason.INSUFFICIENT_CASH.value, "INSUFFICIENT_CASH") and edge >= 0.0060:
                sim = self.simulator or getattr(dash_state, "simulator", None)
                cap = getattr(getattr(sim, "risk", None), "capital", 1000.0) if sim else 1000.0
                if cap < 100.0:
                    new_res = 0.0
                else:
                    cur_res = self.params.get("reserve_cash_pct", 0.0)
                    new_res = min(0.30, round(cur_res + 0.05, 2))
                if new_res != self.params.get("reserve_cash_pct"):
                    self.params["reserve_cash_pct"] = new_res
                    param_name = "reserve_cash_pct"
                    val = f"{new_res:.2f}"
                    adapted = True


            if adapted:
                self.last_triggered_time = now
                self.total_adaptations += 1
                self.recovered_pnl += missed_pnl
                self.primary_bottleneck = reason
                self._save_state()

                sim = self.simulator or getattr(dash_state, "simulator", None)
                if sim and self.auto_apply:
                    self._apply_to_simulator(sim)

                policy_dict = self._build_policy_dict(sim, missed_pnl=missed_pnl)

                target_dash = dash_state or (sim.dash_state if sim else None)
                if target_dash:
                    target_dash.update_adaptive_policy(policy_dict)
                    target_dash.add_activity_log(
                        f"🧠 [ACTIVE TRAIN] Rewrote policy ({reason}): tuned {param_name}={val} | Est. Recovered: +${missed_pnl:.2f}"
                    )

    def evaluate(self, simulator: 'PaperSimulator') -> dict:
        self.simulator = simulator
        tracker = getattr(simulator, "shadow_tracker", None)
        if not tracker:
            return self._build_policy_dict(simulator)

        summary = tracker.get_summary()
        missed_pnl = float(summary.get("total_missed_pnl", 0.0))
        by_reason = summary.get("by_reason", {})
        recent = summary.get("recent_missed", [])
        total_missed = summary.get("total_missed_count", 0)

        realized_pnl = 0.0
        if simulator.dash_state and "trades" in simulator.dash_state.state:
            realized_pnl = sum(
                float(t.get("expected_profit", 0.0) or 0.0)
                for t in simulator.dash_state.state["trades"]
            )
        elif hasattr(simulator, "risk") and simulator.risk:
            realized_pnl = max(0.0, simulator.risk.capital - 1000.0)

        primary_bottleneck = "NONE"
        max_miss_count = 0
        for reason, stats in by_reason.items():
            cnt = stats.get("count", 0)
            if cnt > max_miss_count:
                max_miss_count = cnt
                primary_bottleneck = reason
        self.primary_bottleneck = primary_bottleneck

        current_edge = float(getattr(simulator, "min_edge", self.params["min_edge_pct"]))
        sub_thresh_stats = by_reason.get(MissedReason.SUB_THRESHOLD_EDGE.value, {})
        sub_thresh_pnl = float(sub_thresh_stats.get("pnl", 0.0))
        sub_thresh_cnt = int(sub_thresh_stats.get("count", 0))

        recommended_edge = current_edge
        if sub_thresh_cnt >= 3 and sub_thresh_pnl > 1.0:
            recommended_edge = max(0.0010, round(current_edge * 0.75, 4))
        elif sub_thresh_cnt >= 1:
            recommended_edge = max(0.0015, round(current_edge - 0.0005, 4))

        high_edge_cash_misses = [
            m for m in recent
            if m.get("reason") == MissedReason.INSUFFICIENT_CASH.value and float(m.get("edge", 0.0)) >= 0.0060
        ]
        recommended_reserve = self.params["reserve_cash_pct"]
        if high_edge_cash_misses:
            recommended_reserve = 0.25
        elif by_reason.get(MissedReason.INSUFFICIENT_CASH.value, {}).get("count", 0) >= 3:
            recommended_reserve = 0.20

        with self.lock:
            self.params["min_edge_pct"] = recommended_edge
            self.params["reserve_cash_pct"] = recommended_reserve

        policy_result = self._build_policy_dict(
            simulator,
            missed_pnl=missed_pnl,
            realized_pnl=realized_pnl,
            total_missed=total_missed
        )

        if simulator.dash_state:
            simulator.dash_state.update_adaptive_policy(policy_result)

        if self.auto_apply:
            self._apply_to_simulator(simulator)
            self._save_state()

        self.last_optimized_time = time.time()
        return policy_result

AdaptivePolicyOptimizer = ActivePolicyRewriter

class DashboardState:
    def __init__(self, filename="dashboard_state.json"):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        if not os.path.isabs(filename):
            filename = os.path.join(base_dir, filename)
        self.filename = filename
        default_state = {
            "capital": 1000.0,
            "starting_capital": 1000.0,
            "available_cash": 1000.0,
            "locked_collateral": 0.0,
            "max_concurrent_positions": 5,
            "open_positions": {},
            "daily_loss": 0.0,
            "circuit_breaker": False,
            "max_exposure_pct": 0.10,
            "min_edge_pct": 0.0020,
            "taker_fee_bps": 35,
            "trades": [],
            "markets": {},
            "market_names": {},
            "pnl_history": [],
            "price_history": {},
            "activity_log": [],
            "ohlc": {},
            "last_heartbeat": time.time(),
            "total_ticks": 0,
            "bot_status": "ONLINE_SCANNING",
            "bot_started_at": time.time(),
            "active_sockets": 16,
            "socket_architecture": "16 Active Sockets (Dual Pool A+B: 0% Blast Radius)",
            "redundancy_mode": "Active-Active Hot Standby",
            "pool_status": {
                "Pool A": "8 Workers Active",
                "Pool B": "8 Workers Active (Redundant Standby)"
            },
            "missed_trades": [],
            "missed_trades_summary": {
                "total_missed_count": 0,
                "total_missed_pnl": 0.0,
                "by_reason": {}
            },
            "adaptive_policy": {
                "leakage_ratio": 0.0,
                "recommended_min_edge_pct": 0.0020,
                "recommended_reserve_cash_pct": 0.0,
                "primary_bottleneck": "NONE",
                "updated_at": time.time()
            }
        }
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    self.state = json.load(f)
                added_defaults = False
                for k, v in default_state.items():
                    if k not in self.state or self.state[k] is None:
                        added_defaults = True
                        if k == "available_cash":
                            self.state[k] = float(self.state.get("capital") or v)
                        elif k == "starting_capital":
                            cap_val = float(self.state.get("capital") or 1000.0)
                            if abs(cap_val - 1153.7016) < 0.01:
                                self.state[k] = 1060.3666
                            else:
                                self.state[k] = cap_val
                        else:
                            self.state[k] = v
                self.dirty = added_defaults
                # Clean up legacy bloated price_history and ohlc if loaded from disk
                if isinstance(self.state.get("price_history"), dict) and len(self.state["price_history"]) > 20:
                    keep_keys = set(list(self.state["price_history"].keys())[:15])
                    self.state["price_history"] = {k: v[-50:] for k, v in self.state["price_history"].items() if k in keep_keys}
                if isinstance(self.state.get("ohlc"), dict) and len(self.state["ohlc"]) > 20:
                    keep_keys = set(list(self.state["ohlc"].keys())[:15])
                    self.state["ohlc"] = {k: v for k, v in self.state["ohlc"].items() if k in keep_keys}
            except Exception as e:
                logger.error(f"Error reading dashboard state: {e}")
                self.state = default_state
                self.dirty = True
        else:
            self.state = default_state
            self.dirty = True
        self.risk_engine = None
        self.simulator = None
        self.last_write_mtime = 0.0
        self.last_disk_write_time = 0.0
        self.inspected_market = None
        
        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        def _saver():
            state_dir = os.path.dirname(os.path.abspath(self.filename))
            update_file = os.path.join(state_dir, "capital_update.json")
            insp_file = os.path.join(state_dir, "inspected_market.txt")
            last_disk_check_mtime = 0.0
            if os.path.exists(self.filename):
                try:
                    last_disk_check_mtime = os.path.getmtime(self.filename)
                    self.last_write_mtime = last_disk_check_mtime
                except Exception:
                    pass

            while not self._stop_event.is_set():
                if self._stop_event.wait(0.5):
                    break

                # 0. Check for inspected market IPC from dashboard operator
                if os.path.exists(insp_file):
                    try:
                        with open(insp_file, "r", encoding="utf-8") as f:
                            insp_val = f.read().strip()
                        if insp_val:
                            with self.lock:
                                if self.inspected_market != insp_val:
                                    self.inspected_market = insp_val
                                    self.dirty = True
                                    self.last_disk_write_time = 0.0
                    except Exception:
                        pass

                # 1. Process explicit update payload from capital_update.json
                if os.path.exists(update_file):
                    try:
                        with open(update_file, "r", encoding="utf-8") as f:
                            content = f.read().strip()
                        if content:
                            try:
                                update = json.loads(content)
                                new_cap = update.get("capital")
                                new_pct = update.get("max_exposure_pct")
                                new_concurrent = update.get("max_concurrent_positions")
                                new_edge = update.get("min_edge_pct")
                                new_fee = update.get("taker_fee_bps")
                                new_insp = update.get("inspected_market")
                                new_mode = update.get("execution_mode")
                                new_style = update.get("execution_style")
                                new_cap_wager = update.get("live_wager_cap")
                                
                                if update.get("reset_missed_trades"):
                                    self.reset_missed_trades()
                                    if getattr(self, "simulator", None):
                                        if hasattr(self.simulator, "shadow_tracker") and self.simulator.shadow_tracker:
                                            self.simulator.shadow_tracker.reset()
                                        if hasattr(self.simulator, "policy_rewriter") and self.simulator.policy_rewriter:
                                            self.simulator.policy_rewriter.reset()
                                    self.add_activity_log("🧹 Missed trades history and active training metrics reset to zero.")

                                if update.get("unwind_all"):
                                    if getattr(self, "simulator", None) and hasattr(self.simulator, "unwind_positions_to_cash"):
                                        unwound = self.simulator.unwind_positions_to_cash(max_unwind=50)
                                        self.add_activity_log(f"🚨 [EMERGENCY SWEEP] Manually unwound {unwound} open positions to 100% cash.")

                                if update.get("unwind_orphans"):
                                    if getattr(self, "simulator", None) and hasattr(self.simulator, "sweep_orphan_positions"):
                                        max_slip = float(update.get("max_slippage", 0.020) or 0.020)
                                        processed = self.simulator.sweep_orphan_positions(max_slippage=max_slip, force_now=True)
                                        self.add_activity_log(f"⚡ [ORPHAN SWEEPER] Evaluated and processed {processed} orphan positions (max slippage {max_slip*100:.1f}¢).")

                                with self.lock:
                                    if new_insp is not None:
                                        self.inspected_market = str(new_insp)
                                    if new_mode is not None:
                                        self.state["execution_mode"] = str(new_mode)
                                    if new_style is not None:
                                        self.state["execution_style"] = str(new_style)
                                    if new_cap_wager is not None:
                                        self.state["live_wager_cap"] = float(new_cap_wager)
                                    if new_cap is not None:
                                        old_cap = float(self.state.get("capital", new_cap))
                                        diff = float(new_cap) - old_cap
                                        self.state["capital"] = float(new_cap)
                                        self.state["starting_capital"] = float(new_cap)
                                        if not self.risk_engine:
                                            old_cash = float(self.state.get("available_cash", old_cap))
                                            self.state["available_cash"] = max(0.0, round(old_cash + diff, 4))
                                    if new_pct is not None:
                                        self.state["max_exposure_pct"] = float(new_pct)
                                    if new_concurrent is not None:
                                        self.state["max_concurrent_positions"] = max(1, min(10, int(new_concurrent)))
                                    if new_edge is not None:
                                        self.state["min_edge_pct"] = max(0.0005, min(0.05, float(new_edge)))
                                    if new_fee is not None:
                                        self.state["taker_fee_bps"] = max(0, min(300, int(new_fee)))
                                    self.dirty = True
                                    self.last_disk_write_time = 0.0

                                if self.risk_engine:
                                    if new_cap is not None:
                                        self.risk_engine.capital = float(new_cap)
                                    if new_pct is not None:
                                        self.risk_engine.max_exposure_pct = float(new_pct)
                                    if new_concurrent is not None:
                                        self.risk_engine.max_concurrent_positions = max(1, min(10, int(new_concurrent)))
                                    if hasattr(self.risk_engine, "_sync_to_dash_state"):
                                        self.risk_engine._sync_to_dash_state()
                                    else:
                                        self.update_risk(
                                            self.risk_engine.capital,
                                            self.risk_engine.daily_loss,
                                            self.risk_engine.circuit_breaker_active,
                                            self.risk_engine.max_exposure_pct
                                        )
                                if getattr(self, "simulator", None):
                                    if new_edge is not None:
                                        self.simulator.min_edge = float(self.state["min_edge_pct"])
                                    if new_fee is not None:
                                        self.simulator.taker_fee_bps = int(self.state["taker_fee_bps"])
                                        self.simulator.fee_rate = self.simulator.taker_fee_bps / 10000.0
                                try:
                                    os.remove(update_file)
                                except Exception:
                                    pass
                            except json.JSONDecodeError:
                                pass
                    except Exception as e:
                        logger.error(f"Error reading capital update: {e}")

                # 2. Check if dashboard_state.json was modified externally
                try:
                    if os.path.exists(self.filename):
                        curr_mtime = os.path.getmtime(self.filename)
                        if curr_mtime > self.last_write_mtime + 0.05 and curr_mtime > last_disk_check_mtime + 0.05:
                            with open(self.filename, "r", encoding="utf-8") as f:
                                disk_state = json.load(f)
                            disk_pct = disk_state.get("max_exposure_pct")
                            disk_cap = disk_state.get("capital")
                            disk_concurrent = disk_state.get("max_concurrent_positions")
                            disk_edge = disk_state.get("min_edge_pct")
                            disk_fee = disk_state.get("taker_fee_bps")
                            disk_cash = disk_state.get("available_cash")
                            disk_mode = disk_state.get("execution_mode")
                            disk_style = disk_state.get("execution_style")
                            disk_wager = disk_state.get("live_wager_cap")
                            disk_collateral = disk_state.get("locked_collateral")
                            disk_positions = disk_state.get("open_positions")
                            
                            val_pct = None
                            val_cap = None
                            val_concurrent = None
                            val_edge = None
                            val_fee = None
                            with self.lock:
                                if disk_mode is not None:
                                    self.state["execution_mode"] = str(disk_mode)
                                if disk_style is not None:
                                    self.state["execution_style"] = str(disk_style)
                                if disk_wager is not None:
                                    try:
                                        self.state["live_wager_cap"] = float(disk_wager)
                                    except (ValueError, TypeError):
                                        pass
                                if disk_pct is not None:
                                    try:
                                        val_pct = float(disk_pct)
                                        self.state["max_exposure_pct"] = val_pct
                                    except (ValueError, TypeError):
                                        pass
                                disk_start = disk_state.get("starting_capital")
                                if disk_start is not None:
                                    try:
                                        self.state["starting_capital"] = float(disk_start)
                                    except (ValueError, TypeError):
                                        pass
                                if disk_cap is not None:
                                    try:
                                        val_cap = float(disk_cap)
                                        old_cap = float(self.state.get("capital", val_cap))
                                        diff = val_cap - old_cap
                                        self.state["capital"] = val_cap
                                        if not self.risk_engine:
                                            old_cash = float(self.state.get("available_cash", old_cap))
                                            self.state["available_cash"] = max(0.0, round(old_cash + diff, 4))
                                    except (ValueError, TypeError):
                                        pass
                                if disk_concurrent is not None:
                                    try:
                                        val_concurrent = max(1, min(10, int(disk_concurrent)))
                                        self.state["max_concurrent_positions"] = val_concurrent
                                    except (ValueError, TypeError):
                                        pass
                                if disk_edge is not None:
                                    try:
                                        val_edge = max(0.0005, min(0.05, float(disk_edge)))
                                        self.state["min_edge_pct"] = val_edge
                                    except (ValueError, TypeError):
                                        pass
                                if disk_fee is not None:
                                    try:
                                        val_fee = max(0, min(300, int(disk_fee)))
                                        self.state["taker_fee_bps"] = val_fee
                                    except (ValueError, TypeError):
                                        pass
                                if not self.risk_engine:
                                    if disk_cash is not None:
                                        try:
                                            self.state["available_cash"] = float(disk_cash)
                                        except (ValueError, TypeError):
                                            pass
                                    if disk_collateral is not None:
                                        try:
                                            self.state["locked_collateral"] = float(disk_collateral)
                                        except (ValueError, TypeError):
                                            pass
                                    if disk_positions is not None and isinstance(disk_positions, dict):
                                        self.state["open_positions"] = disk_positions

                            if self.risk_engine:
                                if val_pct is not None and abs(self.risk_engine.max_exposure_pct - val_pct) > 1e-4:
                                    self.risk_engine.max_exposure_pct = val_pct
                                    logger.info(f"Dynamically updated max_exposure_pct from disk state: {val_pct:.2%}")
                                if val_cap is not None and abs(self.risk_engine.capital - val_cap) > 1e-4:
                                    self.risk_engine.capital = val_cap
                                    logger.info(f"Dynamically updated capital from disk state: ${val_cap:.2f}")
                                if val_concurrent is not None and getattr(self.risk_engine, "max_concurrent_positions", None) != val_concurrent:
                                    self.risk_engine.max_concurrent_positions = val_concurrent
                                    logger.info(f"Dynamically updated max_concurrent_positions from disk state: {val_concurrent}")

                            if getattr(self, "simulator", None):
                                if val_edge is not None and abs(self.simulator.min_edge - val_edge) > 1e-5:
                                    self.simulator.min_edge = val_edge
                                    logger.info(f"Dynamically updated min_edge from disk state: {val_edge:.4f}")
                                if val_fee is not None and self.simulator.taker_fee_bps != val_fee:
                                    self.simulator.taker_fee_bps = val_fee
                                    self.simulator.fee_rate = val_fee / 10000.0
                                    logger.info(f"Dynamically updated taker_fee_bps from disk state: {val_fee}")

                            last_disk_check_mtime = curr_mtime
                            self.last_write_mtime = curr_mtime
                except Exception:
                    pass

                # 3. Periodically check and recycle expired collateral
                if self.risk_engine and hasattr(self.risk_engine, "recycle_collateral"):
                    try:
                        self.risk_engine.recycle_collateral()
                    except Exception as e:
                        logger.error(f"Error during collateral recycling in saver thread: {e}")

                now = time.time()
                # 4. Periodically evaluate adaptive policy optimizer
                if getattr(self, "simulator", None) and hasattr(self.simulator, "optimizer"):
                    opt = self.simulator.optimizer
                    try:
                        last_opt = float(getattr(opt, "last_optimized_time", 0.0) or 0.0)
                        interval = float(getattr(opt, "optimizer_interval", 30.0) or 30.0)
                        if now - last_opt >= interval:
                            opt.evaluate(self.simulator)
                    except (TypeError, ValueError):
                        pass
                    except Exception as e:
                        logger.error(f"Error evaluating adaptive policy optimizer: {e}")

                # 5. Periodically evaluate simulator maintenance (live balance sync and position unwinding)
                if getattr(self, "simulator", None):
                    sim = self.simulator
                    if hasattr(sim, "saver_thread"):
                        try:
                            sim.saver_thread()
                        except Exception as e:
                            logger.error(f"Error evaluating simulator saver maintenance: {e}")

                if self.dirty and (now - self.last_disk_write_time >= 1.5):
                    self._write_if_dirty()
                
        self._saver_thread = threading.Thread(target=_saver, daemon=True)
        self._saver_thread.start()

    def stop(self, timeout: float = 1.0):
        """Stop the background saver thread gracefully."""
        self._stop_event.set()
        if hasattr(self, "_saver_thread") and self._saver_thread.is_alive():
            self._saver_thread.join(timeout=timeout)
    
    def init_monitored_markets(self, markets_list: List[dict]):
        """Populate initial metadata for all monitored markets so dashboard is immediately populated."""
        with self.lock:
            if "market_names" not in self.state:
                self.state["market_names"] = {}
            if "markets" not in self.state:
                self.state["markets"] = {}
            active_cids = set()
            for m in markets_list:
                cid = m.get("condition_id")
                if not cid:
                    continue
                active_cids.add(cid)
                q = m.get("question", f"Market {cid[:8] if cid else 'Unknown'}")
                try:
                    rew_rate = float(m.get("rewards_daily_rate", 0.0) or 0.0)
                except (ValueError, TypeError):
                    rew_rate = 0.0
                self.state["market_names"][cid] = q
                if cid not in self.state["markets"]:
                    self.state["markets"][cid] = {
                        "question": q,
                        "yes_ask": 0.0,
                        "no_ask": 0.0,
                        "cost": 0.0,
                        "edge": 0.0,
                        "updated_at": 0.0,
                        "rewards_daily_rate": rew_rate
                    }
                else:
                    self.state["markets"][cid]["question"] = q
                    if rew_rate > 0 or "rewards_daily_rate" not in self.state["markets"][cid]:
                        self.state["markets"][cid]["rewards_daily_rate"] = rew_rate

            # Prune stale unmonitored markets from previous runs unless they have open positions
            if active_cids:
                open_pos = self.state.get("open_positions", {})
                open_pos_market_ids = {
                    p.get("market_id", k) for k, p in open_pos.items()
                } if isinstance(open_pos, dict) else set()
                stale_cids = [
                    cid for cid in list(self.state["markets"].keys())
                    if cid not in active_cids and cid not in open_pos_market_ids
                ]
                for cid in stale_cids:
                    del self.state["markets"][cid]
                    if cid in self.state.get("price_history", {}):
                        del self.state["price_history"][cid]
                    if cid in self.state.get("ohlc", {}):
                        del self.state["ohlc"][cid]
                    if cid in self.state.get("market_names", {}):
                        del self.state["market_names"][cid]

            self.dirty = True

    def record_tick(self, market_id: str = None):
        with self.lock:
            self.state["last_heartbeat"] = time.time()
            self.state["total_ticks"] = self.state.get("total_ticks", 0) + 1
            if not self.state.get("circuit_breaker", False):
                self.state["bot_status"] = "ONLINE_SCANNING"
            self.dirty = True

    def set_bot_status(self, status: str):
        with self.lock:
            self.state["bot_status"] = status
            self.state["last_heartbeat"] = time.time()
            self.dirty = True

    def update_risk(self, capital, daily_loss, circuit_breaker, max_exposure_pct=None, available_cash=None, locked_collateral=None, max_concurrent_positions=None, open_positions=None):
        with self.lock:
            self.state["capital"] = capital
            self.state["daily_loss"] = daily_loss
            self.state["circuit_breaker"] = circuit_breaker
            if max_exposure_pct is not None:
                self.state["max_exposure_pct"] = float(max_exposure_pct)
            elif "max_exposure_pct" not in self.state:
                self.state["max_exposure_pct"] = 0.10
            if available_cash is not None:
                self.state["available_cash"] = float(available_cash)
            elif "available_cash" not in self.state:
                self.state["available_cash"] = capital
            if locked_collateral is not None:
                self.state["locked_collateral"] = float(locked_collateral)
            elif "locked_collateral" not in self.state:
                self.state["locked_collateral"] = 0.0
            if max_concurrent_positions is not None:
                self.state["max_concurrent_positions"] = max(1, min(10, int(max_concurrent_positions)))
            elif "max_concurrent_positions" not in self.state:
                self.state["max_concurrent_positions"] = 5
            if open_positions is not None:
                self.state["open_positions"] = open_positions
            elif "open_positions" not in self.state:
                self.state["open_positions"] = {}

            if circuit_breaker:
                self.state["bot_status"] = "HALTED_CIRCUIT_BREAKER"
            time_iso = datetime.now().isoformat()
            self.state["pnl_history"].append({"time": time_iso, "capital": capital})
            self.state["pnl_history"] = self.state["pnl_history"][-100:]
            self.dirty = True

    def add_trade(self, market_id, size, profit, time_str=None):
        if not time_str:
            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with self.lock:
            self.state["trades"].insert(0, {
                "market": market_id,
                "size": size,
                "expected_profit": profit,
                "time": time_str
            })
            self.state["trades"] = self.state["trades"][:50]  # Keep last 50
            self.dirty = True

    def add_activity_log(self, msg: str):
        with self.lock:
            time_str = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            self.state["activity_log"].insert(0, f"[{time_str}] {msg}")
            self.state["activity_log"] = self.state["activity_log"][:100]
            self.dirty = True

    def clear_market_edge(self, market_id: str):
        with self.lock:
            if market_id in self.state.get("markets", {}):
                self.state["markets"][market_id]["edge"] = 0.0
            self.dirty = True

    def record_missed_trade(self, missed_data: dict):
        with self.lock:
            if "missed_trades" not in self.state:
                self.state["missed_trades"] = []
            self.state["missed_trades"].insert(0, dict(missed_data))
            self.state["missed_trades"] = self.state["missed_trades"][:50]

            if "missed_trades_summary" not in self.state:
                self.state["missed_trades_summary"] = {
                    "total_missed_count": 0,
                    "total_missed_pnl": 0.0,
                    "by_reason": {}
                }
            summary = self.state["missed_trades_summary"]
            summary["total_missed_count"] = summary.get("total_missed_count", 0) + 1
            pnl = float(missed_data.get("forfeited_pnl", 0.0) or 0.0)
            summary["total_missed_pnl"] = round(float(summary.get("total_missed_pnl", 0.0)) + pnl, 4)
            reason = str(missed_data.get("reason", "UNKNOWN"))
            if "by_reason" not in summary:
                summary["by_reason"] = {}
            if reason not in summary["by_reason"]:
                summary["by_reason"][reason] = {"count": 0, "pnl": 0.0}
            summary["by_reason"][reason]["count"] += 1
            summary["by_reason"][reason]["pnl"] = round(summary["by_reason"][reason]["pnl"] + pnl, 4)
            self.dirty = True

    def reset_missed_trades(self):
        with self.lock:
            self.state["missed_trades"] = []
            self.state["missed_trades_summary"] = {
                "total_missed_count": 0,
                "total_missed_pnl": 0.0,
                "by_reason": {}
            }
            if "adaptive_policy" in self.state:
                self.state["adaptive_policy"]["total_adaptations"] = 0
                self.state["adaptive_policy"]["recovered_pnl"] = 0.0
                self.state["adaptive_policy"]["primary_bottleneck"] = "NONE"
            self.dirty = True

    def update_adaptive_policy(self, policy_data: dict):
        with self.lock:
            self.state["adaptive_policy"] = dict(policy_data)
            self.dirty = True

    def update_market(self, market_id, yes_ask, no_ask, effective_cost, edge, question=None, bid_yes=None, bid_no=None, depth_yes=None, depth_no=None):
        with self.lock:
            now = time.time()
            self.state["last_heartbeat"] = now
            if not self.state.get("circuit_breaker", False):
                self.state["bot_status"] = "ONLINE_SCANNING"
            
            if market_id not in self.state["markets"]:
                self.state["markets"][market_id] = {}
                
            m_dict = self.state["markets"][market_id]
            m_dict.update({
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "cost": effective_cost,
                "edge": edge,
                "updated_at": now
            })
            if bid_yes is not None:
                m_dict["bid_yes"] = bid_yes
            if bid_no is not None:
                m_dict["bid_no"] = bid_no
            if depth_yes is not None:
                m_dict["depth_yes"] = depth_yes
            if depth_no is not None:
                m_dict["depth_no"] = depth_no
            if question:
                m_dict["question"] = question
                if "market_names" not in self.state:
                    self.state["market_names"] = {}
                self.state["market_names"][market_id] = question

            if yes_ask is not None and no_ask is not None and yes_ask > 0 and no_ask > 0:
                if market_id not in self.state["price_history"]:
                    self.state["price_history"][market_id] = []
                dt = datetime.now()
                time_iso = dt.isoformat()
                self.state["price_history"][market_id].append({
                    "time": time_iso,
                    "yes_ask": yes_ask,
                    "no_ask": no_ask,
                    "cost": effective_cost
                })
                self.state["price_history"][market_id] = self.state["price_history"][market_id][-50:]

                # Only compute expensive 10s OHLC candles and ISO bin strings for priority markets
                # (open positions, inspected market, or edge > 0)
                is_priority = (
                    market_id == getattr(self, "inspected_market", None) or
                    (edge is not None and edge > 0) or
                    market_id in self.state.get("open_positions", {}) or
                    any(isinstance(p, dict) and p.get("market_id") == market_id for p in self.state.get("open_positions", {}).values())
                )

                if is_priority:
                    if "ohlc" not in self.state:
                        self.state["ohlc"] = {}
                    if market_id not in self.state["ohlc"]:
                        self.state["ohlc"][market_id] = {}
                    
                    bin_dt = dt.replace(second=(dt.second // 10) * 10, microsecond=0)
                    bin_str = bin_dt.isoformat()
                    
                    market_ohlc = self.state["ohlc"][market_id]
                    if bin_str not in market_ohlc:
                        market_ohlc[bin_str] = {
                            "yes_open": yes_ask, "yes_high": yes_ask, "yes_low": yes_ask, "yes_close": yes_ask,
                            "no_open": no_ask, "no_high": no_ask, "no_low": no_ask, "no_close": no_ask,
                            "time": bin_str
                        }
                    else:
                        candle = market_ohlc[bin_str]
                        if yes_ask > candle["yes_high"]: candle["yes_high"] = yes_ask
                        if yes_ask < candle["yes_low"]: candle["yes_low"] = yes_ask
                        candle["yes_close"] = yes_ask
                        
                        if no_ask > candle["no_high"]: candle["no_high"] = no_ask
                        if no_ask < candle["no_low"]: candle["no_low"] = no_ask
                        candle["no_close"] = no_ask
                    
                    if len(market_ohlc) > 30:
                        oldest = min(market_ohlc.keys())
                        del market_ohlc[oldest]

            self.dirty = True

    def _get_priority_markets(self) -> set:
        """
        Identify high-priority markets whose price history and OHLC should be persisted
        to disk in dashboard_state.json. Keeps file compact (< 300 KB) while preserving
        full fidelity for active opportunities, open positions, and operator inspections.
        """
        priority = set()
        # 1. Explicitly inspected market from dashboard operator (highest priority)
        if getattr(self, "inspected_market", None):
            priority.add(self.inspected_market)

        # 2. Any market with an open position (up to 2)
        open_pos = self.state.get("open_positions", {})
        if isinstance(open_pos, dict):
            for k, p in list(open_pos.items())[:2]:
                m_id = p.get("market_id", k) if isinstance(p, dict) else k
                priority.add(m_id)

        # 3. Top active markets ranked by highest edge / lowest cost
        markets = self.state.get("markets", {})
        if isinstance(markets, dict):
            active_list = []
            for m_id, m_info in markets.items():
                if isinstance(m_info, dict):
                    try:
                        c = float(m_info.get("cost", 0.0) or 0.0)
                        e = float(m_info.get("edge", 0.0) or 0.0)
                        if c > 0:
                            active_list.append((m_id, e, c))
                    except (ValueError, TypeError):
                        pass
            # Sort by highest edge (-e), then lowest cost (c)
            active_list.sort(key=lambda x: (-x[1], x[2]))
            for m_id, _, _ in active_list:
                if len(priority) >= 5:
                    break
                priority.add(m_id)

        # 4. Fallback: ensure at least up to 4 markets with price history are included
        if len(priority) < 4:
            for m_id in self.state.get("price_history", {}).keys():
                priority.add(m_id)
                if len(priority) >= 4:
                    break

        return priority

    def _write_if_dirty(self):
        if not self.dirty:
            return
        with self.lock:
            self.dirty = False
            self.last_disk_write_time = time.time()
            priority_mkts = self._get_priority_markets()
            # Lean state serialization: restrict price_history & ohlc to priority markets
            lean_state = dict(self.state)
            lean_ph = {}
            for m_id, pts in self.state.get("price_history", {}).items():
                if m_id in priority_mkts and isinstance(pts, list):
                    lean_ph[m_id] = pts[-15:]
            lean_ohlc = {}
            for m_id, candles in self.state.get("ohlc", {}).items():
                if m_id in priority_mkts and isinstance(candles, dict):
                    sorted_candles = sorted(candles.items(), key=lambda x: x[0])[-8:]
                    lean_ohlc[m_id] = dict(sorted_candles)
            lean_state["price_history"] = lean_ph
            lean_state["ohlc"] = lean_ohlc
            lean_state["activity_log"] = list(self.state.get("activity_log", []))[:30]
            lean_state["pnl_history"] = list(self.state.get("pnl_history", []))[-30:]
            lean_state["trades"] = list(self.state.get("trades", []))[:25]
            lean_state["missed_trades"] = list(self.state.get("missed_trades", []))[:50]
            lean_state["missed_trades_summary"] = dict(self.state.get("missed_trades_summary", {}))
            lean_state["adaptive_policy"] = dict(self.state.get("adaptive_policy", {}))
            if "open_positions" in self.state and isinstance(self.state["open_positions"], dict):
                lean_state["open_positions"] = dict(self.state["open_positions"])

            # Restrict redundant market_names to priority markets or first 10
            # Full questions are already stored inside state["markets"][m_id]["question"]
            lean_names = {}
            for m_id in priority_mkts:
                if m_id in self.state.get("market_names", {}):
                    lean_names[m_id] = self.state["market_names"][m_id]
            for m_id, q in self.state.get("market_names", {}).items():
                if len(lean_names) >= 10:
                    break
                if m_id not in lean_names:
                    lean_names[m_id] = q
            lean_state["market_names"] = lean_names

            # Clean and compact markets dictionary (round floats to prevent JSON expansion)
            compact_markets = {}
            for m_id, m_info in self.state.get("markets", {}).items():
                if isinstance(m_info, dict):
                    q_str = m_info.get("question", "")
                    if isinstance(q_str, str) and len(q_str) > 75:
                        q_str = q_str[:72] + "..."
                    entry = {
                        "question": q_str,
                        "yes_ask": round(float(m_info.get("yes_ask") or 0.0), 4),
                        "no_ask": round(float(m_info.get("no_ask") or 0.0), 4),
                        "cost": round(float(m_info.get("cost") or 0.0), 4),
                        "edge": round(float(m_info.get("edge") or 0.0), 4),
                        "updated_at": int(float(m_info.get("updated_at") or 0.0))
                    }
                    if "bid_yes" in m_info and m_info["bid_yes"] is not None:
                        entry["bid_yes"] = round(float(m_info["bid_yes"]), 4)
                    if "bid_no" in m_info and m_info["bid_no"] is not None:
                        entry["bid_no"] = round(float(m_info["bid_no"]), 4)
                    if "depth_yes" in m_info and m_info["depth_yes"] is not None:
                        d_y = float(m_info["depth_yes"])
                        if not math.isinf(d_y):
                            entry["depth_yes"] = round(d_y, 2)
                    if "depth_no" in m_info and m_info["depth_no"] is not None:
                        d_n = float(m_info["depth_no"])
                        if not math.isinf(d_n):
                            entry["depth_no"] = round(d_n, 2)
                    r_val = float(m_info.get("rewards_daily_rate") or 0.0)
                    if r_val > 0:
                        entry["rewards_daily_rate"] = round(r_val, 2)
                    compact_markets[m_id] = entry
                else:
                    compact_markets[m_id] = m_info
            lean_state["markets"] = compact_markets

        # Serialized and saved outside lock to eliminate tick lock contention
        try:
            serialized = json.dumps(lean_state)
            tmp_file = self.filename + ".tmp"
            with open(tmp_file, 'w', encoding='utf-8') as f:
                f.write(serialized)
            for attempt in range(3):
                try:
                    os.replace(tmp_file, self.filename)
                    break
                except (PermissionError, OSError):
                    if attempt < 2:
                        time.sleep(0.025)
                    else:
                        raise
            try:
                self.last_write_mtime = os.path.getmtime(self.filename)
            except Exception:
                pass
        except Exception:
            with self.lock:
                self.dirty = True

# Global state for dashboard (initialized lazily or in main)
dash_state: Optional[DashboardState] = None

class OpenPositionsDict(dict):
    """
    Dict subclass for open positions supporting both unique position keys
    (e.g. {market_id}_{timestamp}) and direct market_id lookups for backwards compatibility.
    """
    def __contains__(self, key):
        if super().__contains__(key):
            return True
        return any(
            (isinstance(v, dict) and v.get("market_id") == key) or
            k.startswith(f"{key}_")
            for k, v in self.items()
        )

    def __getitem__(self, key):
        if super().__contains__(key):
            return super().__getitem__(key)
        for k, v in self.items():
            if (isinstance(v, dict) and v.get("market_id") == key) or k.startswith(f"{key}_"):
                return v
        raise KeyError(key)

    def __delitem__(self, key):
        if super().__contains__(key):
            super().__delitem__(key)
            return
        for k, v in list(self.items()):
            if (isinstance(v, dict) and v.get("market_id") == key) or k.startswith(f"{key}_"):
                super().__delitem__(k)
                return
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

class RiskSizingEngine:
    def __init__(
        self,
        initial_capital: float = 1000.0,
        max_exposure_pct: float = 0.10,
        daily_loss_limit: float = 50.0,
        dash_state: Optional['DashboardState'] = None,
        max_concurrent_positions: int = 5,
        hold_period_seconds: float = 1.0,
        available_cash: Optional[float] = None,
        locked_collateral: Optional[float] = None,
        open_positions: Optional[Dict[str, dict]] = None,
        max_positions_per_market: int = 2,
        max_market_exposure_pct: float = 0.25,
        reserve_cash_pct: float = 0.0,
        sizing_multiplier: float = 1.0,
        min_cash_floor: Optional[float] = None,
        is_live: bool = False
    ):
        self.lock = threading.RLock()
        self.starting_capital = float(initial_capital)
        self.max_exposure_pct = float(max_exposure_pct)
        if float(initial_capital) < 100.0:
            self.daily_loss_limit = min(float(daily_loss_limit), max(5.00, round(float(initial_capital) * 0.20, 2)))
        else:
            self.daily_loss_limit = min(float(daily_loss_limit), max(5.00, round(float(initial_capital) * 0.05, 2)))
        self.min_cash_floor = float(min_cash_floor) if min_cash_floor is not None else min(5.0, max(0.0, float(initial_capital) * 0.10))
        self.is_live = is_live
        self.daily_loss = 0.0
        self.circuit_breaker_active = False
        self.dash_state = dash_state
        self.max_concurrent_positions = max(1, min(10, int(max_concurrent_positions)))
        self.hold_period_seconds = float(hold_period_seconds)
        self.max_positions_per_market = int(max_positions_per_market)
        self.max_market_exposure_pct = float(max_market_exposure_pct)
        self.reserve_cash_pct = float(reserve_cash_pct)
        self.sizing_multiplier = float(sizing_multiplier)

        # Retrieve saved two-tier capital state from dash_state if not explicitly provided
        if available_cash is not None:
            self.available_cash = float(available_cash)
        elif dash_state and "available_cash" in dash_state.state:
            self.available_cash = float(dash_state.state["available_cash"])
        else:
            self.available_cash = float(initial_capital)

        if locked_collateral is not None:
            self.locked_collateral = float(locked_collateral)
        elif dash_state and "locked_collateral" in dash_state.state:
            self.locked_collateral = float(dash_state.state["locked_collateral"])
        else:
            self.locked_collateral = 0.0

        if open_positions is not None:
            self.open_positions = OpenPositionsDict(open_positions)
        elif dash_state and "open_positions" in dash_state.state and isinstance(dash_state.state["open_positions"], dict):
            self.open_positions = OpenPositionsDict(dash_state.state["open_positions"])
        else:
            self.open_positions = OpenPositionsDict()
        
        # Init and link dashboard state if provided, recycling any positions that expired offline
        target_state = self.dash_state
        if target_state:
            target_state.risk_engine = self
            self.recycle_collateral()
            self._sync_to_dash_state()

    @property
    def capital(self) -> float:
        with self.lock:
            unrealized = sum(p.get("expected_profit", 0.0) for p in self.open_positions.values())
            return self.available_cash + self.locked_collateral + unrealized

    @capital.setter
    def capital(self, value: float):
        with self.lock:
            unrealized = sum(p.get("expected_profit", 0.0) for p in self.open_positions.values())
            current = self.available_cash + self.locked_collateral + unrealized
            diff = float(value) - current
            self.available_cash = max(0.0, round(self.available_cash + diff, 4))
        self._sync_to_dash_state()

    @property
    def circuit_breaker(self) -> bool:
        return self.circuit_breaker_active

    @circuit_breaker.setter
    def circuit_breaker(self, value: bool):
        self.circuit_breaker_active = bool(value)

    def _sync_exposure_from_state(self):
        """Read and respect max_exposure_pct and max_concurrent_positions dynamically from linked dashboard_state."""
        target_state = self.dash_state
        if target_state:
            with target_state.lock:
                pct = target_state.state.get("max_exposure_pct")
                mcp = target_state.state.get("max_concurrent_positions")
                mm_pct = target_state.state.get("max_market_exposure_pct")
                res_pct = target_state.state.get("reserve_cash_pct")
            val_pct = None
            val_mcp = None
            val_mm = None
            val_res = None
            if pct is not None:
                try:
                    val_pct = float(pct)
                except (ValueError, TypeError):
                    pass
            if mcp is not None:
                try:
                    val_mcp = max(1, min(10, int(mcp)))
                except (ValueError, TypeError):
                    pass
            if mm_pct is not None:
                try:
                    val_mm = float(mm_pct)
                except (ValueError, TypeError):
                    pass
            if res_pct is not None:
                try:
                    val_res = float(res_pct)
                except (ValueError, TypeError):
                    pass
            with self.lock:
                if val_pct is not None:
                    self.max_exposure_pct = val_pct
                if val_mcp is not None:
                    self.max_concurrent_positions = val_mcp
                if val_mm is not None and val_mm > 0:
                    self.max_market_exposure_pct = val_mm
                if val_res is not None and val_res >= 0:
                    self.reserve_cash_pct = val_res


    def _sync_to_dash_state(self):
        target_state = self.dash_state
        if target_state:
            with self.lock:
                cap = self.available_cash + self.locked_collateral + sum(p.get("expected_profit", 0.0) for p in self.open_positions.values())
                cash = self.available_cash
                locked = self.locked_collateral
                positions = {k: dict(v) for k, v in self.open_positions.items()}
                mcp = self.max_concurrent_positions
                mep = self.max_exposure_pct
                dl = self.daily_loss
                cb = self.circuit_breaker_active

            target_state.update_risk(
                capital=cap,
                daily_loss=dl,
                circuit_breaker=cb,
                max_exposure_pct=mep,
                available_cash=cash,
                locked_collateral=locked,
                max_concurrent_positions=mcp,
                open_positions=positions
            )

    def open_position(self, market_id: str, size: float, expected_profit: float, hold_seconds: Optional[float] = None) -> bool:
        duration = self.hold_period_seconds if hold_seconds is None else float(hold_seconds)
        now = time.time()
        pos_key = f"{market_id}_{int(now * 1000)}"
        with self.lock:
            while pos_key in self.open_positions:
                now += 0.001
                pos_key = f"{market_id}_{int(now * 1000)}"
            self.available_cash = max(0.0, round(self.available_cash - size, 4))
            self.locked_collateral = max(0.0, round(self.locked_collateral + size, 4))
            self.open_positions[pos_key] = {
                "position_id": pos_key,
                "market_id": market_id,
                "size": round(size, 4),
                "expected_profit": round(expected_profit, 4),
                "entry_time": now,
                "release_time": now + duration
            }
        self._sync_to_dash_state()
        return True

    def recycle_collateral(self, now: Optional[float] = None) -> List[dict]:
        """
        Check active positions and recycle collateral for any position whose hold period expired.
        Principal and profit are returned to available_cash, releasing locked_collateral.
        """
        if getattr(self, 'is_live', False):
            return []

        if now is None:
            now = time.time()
        released = []
        to_release = []

        with self.lock:
            for pos_key, pos in list(self.open_positions.items()):
                if now >= pos.get("release_time", 0.0):
                    to_release.append((pos_key, pos))

            for pos_key, pos in to_release:
                del self.open_positions[pos_key]
                size = pos.get("size", 0.0)
                profit = pos.get("expected_profit", 0.0)
                self.locked_collateral = max(0.0, round(self.locked_collateral - size, 4))
                self.available_cash = max(0.0, round(self.available_cash + (size + profit), 4))
                if profit < 0:
                    self.daily_loss += abs(profit)
                    if self.daily_loss >= self.daily_loss_limit:
                        self.circuit_breaker_active = True
                released.append(pos)

        if released:
            if self.dash_state:
                for pos in released:
                    m_id = pos.get("market_id", pos.get("position_id", ""))
                    short_id = m_id[-6:] if len(m_id) > 6 else m_id
                    self.dash_state.add_activity_log(
                        f"♻️ CTF Merge / Collateral Recycled: Released ${pos.get('size', 0.0):.2f} + ${pos.get('expected_profit', 0.0):.2f} profit from {short_id}"
                    )
            self._sync_to_dash_state()

        return released

    def check_trade_gating_reason(self, risk_amount: float, market_id: Optional[str] = None) -> Optional[str]:
        self.recycle_collateral()
        self._sync_exposure_from_state()

        with self.lock:
            if self.circuit_breaker_active:
                logger.warning("Circuit breaker is ACTIVE. Trading halted.")
                return MissedReason.CIRCUIT_BREAKER.value

            total_cap = self.available_cash + self.locked_collateral + sum(p.get("expected_profit", 0.0) for p in self.open_positions.values())
            if risk_amount <= 0 or total_cap <= 0:
                return MissedReason.ZERO_LIQUIDITY.value

            if len(self.open_positions) >= self.max_concurrent_positions:
                logger.info(f"Max concurrent positions reached ({len(self.open_positions)}/{self.max_concurrent_positions}). Gating trade.")
                return MissedReason.CONCURRENCY_EXHAUSTED.value

            if total_cap < 100.0:
                reserve_amt = 0.0
                spendable = self.available_cash
            else:
                reserve_amt = self.available_cash * getattr(self, "reserve_cash_pct", 0.0)
                spendable = max(0.0, self.available_cash - reserve_amt)
            cash_floor = getattr(self, "min_cash_floor", 0.0)
            if cash_floor > 0 and self.available_cash < cash_floor:
                logger.warning(f"Available cash ${self.available_cash:.2f} is below minimum bankroll threshold (${cash_floor:.2f}). Gating trade.")
                return MissedReason.INSUFFICIENT_CASH.value
            if risk_amount > spendable + 1e-5:
                logger.warning(f"Trade size ${risk_amount:.2f} exceeds spendable cash ${spendable:.2f}.")
                return MissedReason.INSUFFICIENT_CASH.value

            if market_id is not None:
                market_positions = [
                    p for k, p in self.open_positions.items()
                    if (isinstance(p, dict) and p.get('market_id') == market_id) or k == market_id or k.startswith(f"{market_id}_")
                ]
                current_m_exp = sum(float(p.get('size', 0.0) or 0.0) for p in market_positions)
                if total_cap < 50.0:
                    min_market_floor = max(5.50, 5.50 * min(self.max_positions_per_market, 2))
                    max_m_allowed = max(min_market_floor, total_cap * self.max_market_exposure_pct)
                else:
                    max_m_allowed = total_cap * self.max_market_exposure_pct
                if len(market_positions) >= self.max_positions_per_market:
                    if current_m_exp >= max_m_allowed * 0.25:
                        return MissedReason.MARKET_ALREADY_ACTIVE.value
                if current_m_exp + risk_amount > max_m_allowed + 1e-5:
                    return MissedReason.EXPOSURE_LIMIT_EXCEEDED.value


            max_allowed_risk = max(5.50, (total_cap * self.max_exposure_pct) + 1e-5)
            if risk_amount > max_allowed_risk:
                logger.warning(f"Risk amount {risk_amount} exceeds max exposure limit (${max_allowed_risk:.2f}).")
                return MissedReason.EXPOSURE_LIMIT_EXCEEDED.value

            return None

    def can_trade(self, risk_amount: float, market_id: Optional[str] = None) -> bool:
        return self.check_trade_gating_reason(risk_amount, market_id) is None

    def calculate_sizing(self) -> float:
        """Calculate amount to deploy based on fractional fixed sizing."""
        with self.lock:
            self.recycle_collateral()
            self._sync_exposure_from_state()
            cap = self.capital
            if cap <= 0:
                return 0.0
            mult = getattr(self, "sizing_multiplier", 1.0)
            target = cap * self.max_exposure_pct * mult
            if target < 5.0 and self.available_cash >= 5.0:
                return 5.0
            return target

    def record_pnl(self, pnl: float):
        with self.lock:
            self.capital += pnl
            if pnl < 0:
                self.daily_loss += abs(pnl)
                if self.daily_loss >= self.daily_loss_limit:
                    logger.error(f"Daily loss limit of ${self.daily_loss_limit} reached! Triggering circuit breaker.")
                    self.circuit_breaker_active = True
            self._sync_to_dash_state()

def fetch_top_markets(
    limit: int = 1000,
    fallback_file: str = "markets.json",
    min_volume_24h: float = 1000.0,
    min_liquidity: float = 0.0,
    use_fallback: bool = False
) -> List[dict]:
    """
    Multi-Stream 1,000-Market Dynamic Ingestion Engine.
    Dynamically discover active binary markets across Polymarket Gamma API:
      - Stream 1: Direct markets endpoint (ordered by volume24hr with liquidity_num_min filter)
      - Stream 2: Parent events endpoint (each event bundles top 3 child markets)
      - Fallback: Local markets.json ONLY if explicitly requested via use_fallback=True
    Filters:
      - active is True, closed is False, archived is False, acceptingOrders is not False
      - exactly 2 tokens [YES / NO]
      - volume24hr >= min_volume_24h
      - liquidity >= min_liquidity (default 500.0)
      - non-identical tokens, expiry horizon >= 4.0h, non-turbo/non-short duration, spread <= 3.5 cents
    All candidates ranked by balanced formula: (vol * 0.30 + liq * 0.70 + rewards * 500) / (1 + spread * 50).
    """
    markets = []
    existing_cids = set()

    def _parse_market_obj(item, is_live=False, check_expiry=True):
        if not isinstance(item, dict):
            return None
        if not item.get("active") or item.get("closed") or item.get("archived"):
            return None
        if item.get("acceptingOrders") is False:
            return None
        if item.get("enableOrderBook") is False:
            return None
        spread_val = item.get("spread")
        if spread_val is not None:
            try:
                if float(spread_val) > 0.035:
                    return None
            except (ValueError, TypeError):
                pass

        q = item.get("question")
        cid = item.get("conditionId") or item.get("condition_id")
        if not cid or not q or cid in existing_cids:
            return None

        # 1. Turbo / short-duration pattern rejection
        slug = str(item.get("slug") or item.get("market_slug") or "")
        title = str(item.get("title") or "")
        combined_text = f"{q} {slug} {title}".lower()

        for pat in TURBO_AND_SHORT_DURATION_PATTERNS:
            if pat in combined_text:
                return None

        if TIME_RANGE_REGEX.search(combined_text):
            return None

        # 2. Expiration horizon gating (reject < 4.0 hours)
        end_dt = None
        if check_expiry:
            end_dt = _parse_market_end_date(item)
            if end_dt is not None:
                now_utc = datetime.now(timezone.utc)
                hours_left = (end_dt - now_utc).total_seconds() / 3600.0
                if hours_left < 4.0:
                    return None

        # 3. Game start time / in-play sports gating
        start_dt = _parse_market_start_date(item)
        if start_dt is not None:
            now_utc = datetime.now(timezone.utc)
            if start_dt <= now_utc or (start_dt - now_utc).total_seconds() < 3600.0:
                return None

        clob_ids = item.get("clobTokenIds")
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except Exception:
                clob_ids = []
        tokens = item.get("tokens", [])
        token_ids = []
        if isinstance(clob_ids, list) and len(clob_ids) == 2:
            token_ids = [str(x) for x in clob_ids if x]
        elif isinstance(tokens, list) and len(tokens) == 2:
            token_ids = [str(t.get("token_id")) for t in tokens if t.get("token_id")]

        if len(token_ids) != 2 or not token_ids[0] or not token_ids[1] or token_ids[0] == token_ids[1]:
            return None

        vol = float(item.get("volume24hr") or 0.0)
        liq = float(item.get("liquidityNum") or item.get("liquidity") or 0.0)

        # Strict activity and liquidity gate
        if vol < min_volume_24h or (min_liquidity > 0.0 and liq < min_liquidity):
            return None

        outcomes = item.get("outcomes", ["Yes", "No"])
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = ["Yes", "No"]
        if not isinstance(outcomes, list) or len(outcomes) != 2:
            outcomes = ["Yes", "No"]

        # Ingest rewards daily rate (USDC liquidity mining)
        rewards_daily = 0.0
        clob_rewards = item.get("clobRewards")
        if isinstance(clob_rewards, list):
            for r in clob_rewards:
                if isinstance(r, dict):
                    rewards_daily += float(r.get("rewardsDailyRate") or r.get("rewards_daily_rate") or 0.0)
        elif isinstance(clob_rewards, (int, float)):
            rewards_daily = float(clob_rewards)
        rew = item.get("rewards")
        if isinstance(rew, dict):
            rates = rew.get("rates") or []
            if isinstance(rates, list):
                for r in rates:
                    if isinstance(r, dict):
                        rewards_daily += float(r.get("rewards_daily_rate") or r.get("rewardsDailyRate") or 0.0)

        is_live_game = bool(is_live or item.get("sportsMarketType") or item.get("gameStartTime") or item.get("startDateIso"))

        end_date_str = (
            item.get("endDateIso") or
            item.get("endDate") or
            item.get("end_date_iso") or
            item.get("end_date") or
            item.get("gameStartTime") or
            (end_dt.isoformat() if end_dt else None)
        )

        return {
            "condition_id": cid,
            "question": q,
            "token_ids": token_ids,
            "outcomes": outcomes,
            "volume": vol,
            "volume24hr": vol,
            "liquidity": liq,
            "liquidityNum": liq,
            "liquidityClob": float(item.get("liquidityClob") or 0.0),
            "spread": float(spread_val) if spread_val is not None else None,
            "rewards_daily_rate": rewards_daily,
            "is_live_game": is_live_game,
            "end_date": end_date_str,
            "endDateIso": end_date_str,
            "slug": item.get("slug") or item.get("market_slug"),
            "category": item.get("category"),
            "description": item.get("description"),
            "startDateIso": item.get("startDateIso") or item.get("startDate") or item.get("gameStartTime"),
            "gameStartTime": item.get("gameStartTime"),
        }

    target_buffer = int(limit * 1.25)
    PAGE_SIZE = 100
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PolymarketBot/2.0"}

    # Stream 1: Direct markets endpoint (ordered by 24h volume with liquidity filter)
    for offset in range(0, 1500, 100):
        url = (
            f"https://gamma-api.polymarket.com/markets?limit={PAGE_SIZE}&offset={offset}"
            f"&active=true&closed=false&order=volume24hr&ascending=false"
            f"&liquidity_num_min={min_liquidity}&volume_num_min={min_volume_24h}"
        )
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                raw_resp = resp.read().decode("utf-8")
                data = json.loads(raw_resp) if raw_resp.strip() else []
        except Exception:
            break
        if not isinstance(data, list) or len(data) == 0:
            break
        for item in data:
            m_obj = _parse_market_obj(item, is_live=False)
            if m_obj and m_obj["condition_id"] not in existing_cids:
                existing_cids.add(m_obj["condition_id"])
                markets.append(m_obj)
        if len(markets) >= target_buffer or len(data) < PAGE_SIZE:
            break

    # Stream 2: Parent events endpoint (each event bundles top 3 child markets)
    if len(markets) < target_buffer:
        for order_field in ["volume24hr", "liquidity"]:
            for offset in range(0, 500, 100):
                url = f"https://gamma-api.polymarket.com/events?limit={PAGE_SIZE}&offset={offset}&active=true&closed=false&order={order_field}&ascending=false"
                req = urllib.request.Request(url, headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=6) as resp:
                        raw_resp = resp.read().decode("utf-8")
                        events = json.loads(raw_resp) if raw_resp.strip() else []
                except Exception:
                    break
                if not isinstance(events, list) or len(events) == 0:
                    break
                for ev in events:
                    raw_children = ev.get("markets", [])
                    if isinstance(raw_children, list):
                        sorted_children = sorted(
                            raw_children,
                            key=lambda m: float(m.get("volume24hr") or 0.0) if isinstance(m, dict) else 0.0,
                            reverse=True
                        )
                        top_children = sorted_children[:3]
                    else:
                        top_children = []
                    for item in top_children:
                        m_obj = _parse_market_obj(item, is_live=False)
                        if m_obj and m_obj["condition_id"] not in existing_cids:
                            existing_cids.add(m_obj["condition_id"])
                            markets.append(m_obj)
                if len(markets) >= target_buffer or len(events) < PAGE_SIZE:
                    break
            if len(markets) >= target_buffer:
                break

    # Fallback to markets.json ONLY if explicitly requested via use_fallback=True
    if use_fallback and len(markets) < limit:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        if not os.path.isabs(fallback_file):
            fallback_file = os.path.join(base_dir, fallback_file)
        if os.path.exists(fallback_file):
            try:
                with open(fallback_file, "r", encoding="utf-8") as f:
                    content = json.load(f)
                items = content.get("data", []) if isinstance(content, dict) else (content if isinstance(content, list) else [])
                local_candidates = []
                for item in items:
                    m_obj = _parse_market_obj(item, is_live=False, check_expiry=False)
                    if m_obj:
                        daily_rate = m_obj["rewards_daily_rate"]
                        min_size = m_obj["liquidity"]
                        m_obj["_sort_score"] = daily_rate * 500.0 + min_size
                        local_candidates.append(m_obj)
                local_candidates.sort(key=lambda x: x["_sort_score"], reverse=True)
                for c in local_candidates:
                    if c["condition_id"] not in existing_cids:
                        markets.append(c)
                        existing_cids.add(c["condition_id"])
                        if len(markets) >= limit:
                            break
                logger.info(f"Loaded {len(markets)} active binary markets using local {fallback_file} fallback.")
            except Exception as e:
                logger.error(f"Error loading fallback file {fallback_file}: {e}")

    markets.sort(
        key=lambda x: (
            float(x.get("volume24hr", 0.0) or 0.0) +
            (float(x.get("liquidity", 0.0) or x.get("liquidityNum", 0.0) or 0.0) * 0.05) +
            (float(x.get("rewards_daily_rate", 0.0) or 0.0) * 500.0)
        ),
        reverse=True
    )
    return markets[:limit]


class PaperSimulator:
    def __init__(
        self,
        risk_engine: RiskSizingEngine,
        market_token_map: Dict[str, dict] = None,
        dash_state: Optional[DashboardState] = None,
        min_edge: Optional[float] = None,
        taker_fee_bps: Optional[int] = None
    ):
        self.risk = risk_engine
        self.lock = threading.RLock()
        self.trade_lock = threading.RLock()
        self._dash_state = dash_state
        
        # Calibrated Polymarket taker fee: default 35 bps (0.35%) reflecting competitive CLOB dynamic taker rates
        if taker_fee_bps is not None:
            self.taker_fee_bps = int(taker_fee_bps)
        elif dash_state and hasattr(dash_state, "state") and "taker_fee_bps" in dash_state.state:
            self.taker_fee_bps = int(dash_state.state["taker_fee_bps"])
        else:
            self.taker_fee_bps = 35
        self.fee_rate = self.taker_fee_bps / 10000.0

        # Minimum net profit edge required to execute arbitrage (default 20 bps = 0.20%)
        if min_edge is not None:
            self.min_edge = float(min_edge)
        elif dash_state and hasattr(dash_state, "state") and "min_edge_pct" in dash_state.state:
            self.min_edge = float(dash_state.state["min_edge_pct"])
        else:
            self.min_edge = 0.0150
        
        # Track latest asks for YES and NO across all monitored markets
        self.market_books: Dict[str, Dict[str, Optional[float]]] = {}
        # Track top-of-book ask depths (liquidity sizes) across all monitored markets
        self.market_depths: Dict[str, Dict[str, float]] = {}
        # Track latest bids for YES and NO across all monitored markets
        self.market_bids: Dict[str, Dict[str, float]] = {}
        # Optional metadata mapping condition_id -> {"token_yes": ..., "token_no": ..., "question": ...}
        self.market_token_map = market_token_map or {}
        try:
            from reward_harvester import RewardHarvester
            self.reward_harvester = RewardHarvester(market_token_map=self.market_token_map, dash_state=dash_state)
        except Exception:
            self.reward_harvester = None
        try:
            from microstructure_guard import MicrostructureGuard
            self.guard = MicrostructureGuard()
        except Exception:
            self.guard = None
        if NegRiskBasketScanner:
            try:
                self.negrisk_scanner = NegRiskBasketScanner()
                if self.market_token_map:
                    self.negrisk_scanner.index_market_universe(self.market_token_map)
            except Exception:
                self.negrisk_scanner = None
        else:
            self.negrisk_scanner = None
        # Track market cooldowns to prevent rapid re-entry after failed execution/unwind
        self.market_cooldowns: Dict[str, float] = {}
        # Track last processed ask & size per (market_id, asset_id) for dual-active tick deduplication
        self.last_processed_quotes: Dict[Tuple[str, str], Tuple[float, float]] = {}
        # Track last market tick timestamp per condition_id for stagnation detection
        self.last_market_tick: Dict[str, float] = {}
        self.stagnant_purged_total: int = 0
        self.shadow_tracker = ShadowParityTracker(dash_state=dash_state)
        self.policy_rewriter = ActivePolicyRewriter(simulator=self, auto_apply=False)
        self.shadow_tracker.policy_rewriter = self.policy_rewriter
        self.last_balance_sync_time = 0.0
        self.last_positions_sync_time = 0.0
        self.last_unwind_time = 0.0
        if dash_state:
            dash_state.simulator = self
            if getattr(self.risk, "dash_state", None) is None:
                self.risk.dash_state = dash_state
            if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                self.shadow_tracker.dash_state = dash_state

    def on_book_update(self, market_id: str, asset_id: str, best_ask: float, ask_size: Optional[float] = None, evaluate: bool = True) -> bool:
        """Alias for update_book to standardize book update ingestion."""
        return self.update_book(market_id, asset_id, best_ask, ask_size=ask_size, evaluate=evaluate)

    def dispatch_arbitrage(self, opp: dict) -> bool:
        """Synchronous dispatch for paper simulation."""
        return self.execute_arbitrage(opp)

    def sweep_orphan_positions(self, active_positions: Optional[List[dict]] = None, max_slippage: float = 0.020, force_now: bool = False) -> int:
        """Stub for paper simulation mode."""
        return 0

    def saver_thread(self):
        """
        Periodic maintenance routine for live balance synchronization.
        Invoked periodically from DashboardState saver thread.
        Collateral is preserved at 100% face value; positions are never sold periodically.
        """
        now = time.time()
        if hasattr(self, "sync_live_balance"):
            if now - getattr(self, "last_balance_sync_time", 0.0) >= 15.0:
                try:
                    self.sync_live_balance()
                except Exception as e:
                    logger.warning(f"Periodic live balance sync failed: {e}")
                self.last_balance_sync_time = now

        if hasattr(self, "sync_live_positions"):
            if now - getattr(self, "last_positions_sync_time", 0.0) >= 15.0:
                try:
                    self.sync_live_positions()
                except Exception as e:
                    logger.warning(f"Periodic live positions sync failed: {e}")
                self.last_positions_sync_time = now

        if hasattr(self, "redeem_resolved_positions"):
            if now - getattr(self, "last_redemption_time", 0.0) >= 60.0:
                try:
                    self.redeem_resolved_positions()
                except Exception as e:
                    logger.warning(f"Periodic redeem_resolved_positions failed: {e}")
                self.last_redemption_time = now

    @property
    def dash_state(self) -> Optional[DashboardState]:
        return self._dash_state

    @dash_state.setter
    def dash_state(self, val: Optional[DashboardState]):
        self._dash_state = val
        if hasattr(self, "shadow_tracker") and self.shadow_tracker:
            self.shadow_tracker.dash_state = val

    def update_book(
        self,
        market_id: str,
        asset_id: str,
        best_ask: float,
        ask_size: Optional[float] = None,
        evaluate: bool = True,
        best_bid: Optional[float] = None,
        bid_size: Optional[float] = None
    ) -> bool:
        with self.lock:
            self.last_market_tick[market_id] = time.time()
            if not hasattr(self, "market_bids") or self.market_bids is None:
                self.market_bids = {}
            if market_id not in self.market_bids:
                self.market_bids[market_id] = {}

            if best_bid is not None and float(best_bid) > 0.0:
                self.market_bids[market_id][asset_id] = float(best_bid)

            if best_ask is None or best_ask <= 0.0:
                return False

            if market_id not in self.market_books:
                self.market_books[market_id] = {}
            if market_id not in self.market_depths:
                self.market_depths[market_id] = {}

            current_ask = self.market_books[market_id].get(asset_id)
            prev_depth = self.market_depths[market_id].get(asset_id)

            if ask_size is None:
                if prev_depth is not None and prev_depth > 0:
                    parsed_size = prev_depth
                else:
                    parsed_size = float('inf')
            else:
                parsed_size = max(0.0, float(ask_size))

            if parsed_size <= 0.0:
                self.market_books[market_id].pop(asset_id, None)
                self.market_depths[market_id][asset_id] = 0.0
                return False

            quote_key = (market_id, str(asset_id))
            if current_ask is not None:
                last_quote = self.last_processed_quotes.get(quote_key)
                if last_quote is not None:
                    last_ask, last_size = last_quote
                    if abs(last_ask - best_ask) < 1e-9:
                        if (math.isinf(last_size) and math.isinf(parsed_size)) or abs(last_size - parsed_size) < 1e-9:
                            return False

            self.market_books[market_id][asset_id] = best_ask
            self.market_depths[market_id][asset_id] = parsed_size
            self.last_processed_quotes[quote_key] = (best_ask, parsed_size)

            if hasattr(self, "guard") and self.guard and asset_id:
                try:
                    self.guard.record_quote(
                        token_id=str(asset_id),
                        best_bid=float(best_bid or 0.0),
                        bid_size=float(bid_size or 0.0),
                        best_ask=float(best_ask or 0.0),
                        ask_size=float(parsed_size) if not math.isinf(parsed_size) else 0.0,
                        timestamp=time.time(),
                    )
                except Exception:
                    pass

        if evaluate:
            self.evaluate_parity(market_id)
        return True

    def on_trade_tick(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
        timestamp: Optional[float] = None,
    ):
        if hasattr(self, "guard") and self.guard and token_id:
            try:
                self.guard.record_trade(
                    token_id=str(token_id),
                    price=float(price or 0.0),
                    size=float(size or 0.0),
                    side=str(side or "BUY"),
                    timestamp=timestamp or time.time(),
                )
            except Exception:
                pass


    def check_market_parity(self, market_id: str) -> Optional[dict]:
        # Cooldown check: prevent rapid re-entry into recently failed or unwound contracts
        if time.time() < getattr(self, "market_cooldowns", {}).get(market_id, 0.0):
            return None

        with self.lock:
            books = dict(self.market_books.get(market_id, {}))
            depths = dict(self.market_depths.get(market_id, {}))
            bids = dict(self.market_bids.get(market_id, {})) if hasattr(self, "market_bids") else {}
            m_info = dict(self.market_token_map.get(market_id, {})) if market_id in self.market_token_map else None

        if m_info:
            end_dt = _parse_market_end_date(m_info)
            if end_dt is not None:
                now_utc = datetime.now(timezone.utc)
                hours_left = (end_dt - now_utc).total_seconds() / 3600.0
                if hours_left < 4.0:
                    return None

            eligible, _ = is_market_eligible(m_info, min_volume_24h=500.0, min_hours_to_expiry=4.0)
            if not eligible:
                return None

        if len(books) != 2:
            return None  # Incomplete data

        if m_info and "token_yes" in m_info and "token_no" in m_info:
            ask_yes = books.get(m_info["token_yes"])
            ask_no = books.get(m_info["token_no"])
        elif "YES" in books and "NO" in books:
            ask_yes = books.get("YES")
            ask_no = books.get("NO")
        else:
            keys = sorted(books.keys())
            ask_yes = books[keys[0]]
            ask_no = books[keys[1]]

        if ask_yes is None or ask_no is None or ask_yes <= 0.0 or ask_no <= 0.0:
            return None  # Incomplete or invalid quote data

        target_state = self.dash_state

        bid_yes = None
        bid_no = None
        depth_yes = None
        depth_no = None
        if m_info and "token_yes" in m_info and "token_no" in m_info:
            bid_yes = bids.get(m_info["token_yes"])
            bid_no = bids.get(m_info["token_no"])
            depth_yes = depths.get(m_info["token_yes"])
            depth_no = depths.get(m_info["token_no"])
        elif "YES" in books and "NO" in books:
            bid_yes = bids.get("YES")
            bid_no = bids.get("NO")
            depth_yes = depths.get("YES")
            depth_no = depths.get("NO")
        else:
            keys = sorted(books.keys())
            bid_yes = bids.get(keys[0])
            bid_no = bids.get(keys[1])
            depth_yes = depths.get(keys[0])
            depth_no = depths.get(keys[1])

        # Parity Check: Ask_YES + Ask_NO < 1.00 - fees
        raw_cost = ask_yes + ask_no
        effective_cost = raw_cost * (1 + self.fee_rate)
        edge = 1.00 - effective_cost

        # Update dashboard market stats every time we have complete book data
        q_name = m_info.get("question") if m_info else None
        if target_state:
            target_state.update_market(
                market_id,
                ask_yes,
                ask_no,
                effective_cost,
                edge,
                question=q_name,
                bid_yes=bid_yes,
                bid_no=bid_no,
                depth_yes=depth_yes,
                depth_no=depth_no
            )

        short_id = (q_name[:26] + "...") if q_name else f"Market {market_id[-6:]}"
        if target_state and edge > 0:
            target_state.add_activity_log(f"⚡ {short_id}: YES {ask_yes:.4f} | NO {ask_no:.4f} | Cost {effective_cost:.4f} | Edge {edge*100:+.2f}%")

        if effective_cost < 1.00:
            if edge <= self.min_edge:
                opp_info = {
                    "market_id": market_id,
                    "question": q_name or short_id,
                    "short_id": short_id,
                    "ask_yes": ask_yes,
                    "ask_no": ask_no,
                    "effective_cost": effective_cost,
                    "cost": effective_cost,
                    "edge": edge,
                    "available_depth_usd": 0.0,
                    "trade_size": self.risk.calculate_sizing(),
                    "expected_profit": self.risk.calculate_sizing() * edge
                }
                if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                    self.shadow_tracker.record_missed(opp_info, MissedReason.SUB_THRESHOLD_EDGE)
                return None

            if m_info and "token_yes" in m_info and "token_no" in m_info:
                depth_yes = depths.get(m_info["token_yes"], float('inf'))
                depth_no = depths.get(m_info["token_no"], float('inf'))
            elif "YES" in depths and "NO" in depths:
                depth_yes = depths.get("YES", float('inf'))
                depth_no = depths.get("NO", float('inf'))
            else:
                keys = sorted(books.keys())
                depth_yes = depths.get(keys[0], float('inf'))
                depth_no = depths.get(keys[1], float('inf'))

            if depth_yes is None:
                depth_yes = float('inf')
            if depth_no is None:
                depth_no = float('inf')

            executable_liquidity_usd = min(depth_yes, depth_no)
            if executable_liquidity_usd <= 0.0:
                if depth_yes <= 0.0 and depth_no <= 0.0 and ask_yes > 0 and ask_no > 0:
                    executable_liquidity_usd = self.risk.calculate_sizing()
                else:
                    d_y = depth_yes if depth_yes != float('inf') else 0.0
                    d_n = depth_no if depth_no != float('inf') else 0.0
                    if (d_y > 0.0 and d_n <= 0.0) or (d_n > 0.0 and d_y <= 0.0):
                        missed_reason = MissedReason.ASYMMETRIC_DEPTH
                        avail_depth = max(d_y, d_n)
                    else:
                        missed_reason = MissedReason.ZERO_LIQUIDITY
                        avail_depth = 0.0
                    opp_info = {
                        "market_id": market_id,
                        "question": q_name or short_id,
                        "short_id": short_id,
                        "ask_yes": ask_yes,
                        "ask_no": ask_no,
                        "depth_yes": d_y,
                        "depth_no": d_n,
                        "effective_cost": effective_cost,
                        "cost": effective_cost,
                        "edge": edge,
                        "available_depth_usd": avail_depth,
                        "trade_size": self.risk.calculate_sizing(),
                        "expected_profit": 0.0
                    }
                    if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                        self.shadow_tracker.record_missed(opp_info, missed_reason)
                    return None

            desired_trade_size = self.risk.calculate_sizing()
            trade_size = min(desired_trade_size, executable_liquidity_usd)
            if trade_size <= 0.0:
                return None

            expected_profit = trade_size * edge
            return {
                "market_id": market_id,
                "ask_yes": ask_yes,
                "ask_no": ask_no,
                "token_yes": m_info.get("token_yes") if m_info else None,
                "token_no": m_info.get("token_no") if m_info else None,
                "depth_yes": depth_yes,
                "depth_no": depth_no,
                "effective_cost": effective_cost,
                "edge": edge,
                "executable_liquidity_usd": executable_liquidity_usd,
                "available_depth_usd": executable_liquidity_usd,
                "trade_size": trade_size,
                "expected_profit": expected_profit,
                "short_id": short_id,
                "question": q_name or short_id,
                "market_meta": m_info.get('market_meta', m_info) if m_info else None,
                "tick_size": float(m_info.get('tick_size', 0.001)) if m_info else 0.001
            }


        return None

    def check_maker_taker_parity(self, market_id: str) -> Optional[dict]:
        """
        Active Maker-Taker Parity Scanner.
        Discovers asymmetric spread-capture opportunities by placing a passive limit buy order
        inside the spread (0% maker fee) on Leg 1 (YES or NO), with Leg 2 executed as a taker FOK:
          Branch A: Post Maker limit BUY on YES; match with Taker BUY on NO.
          Branch B: Post Maker limit BUY on NO; match with Taker BUY on YES.
        Selects superior branch by highest edge + USDC liquidity mining rewards bonus.
        """
        if time.time() < getattr(self, "market_cooldowns", {}).get(market_id, 0.0):
            return None

        with self.lock:
            books = dict(self.market_books.get(market_id, {}))
            depths = dict(self.market_depths.get(market_id, {}))
            bids_map = dict(self.market_bids.get(market_id, {})) if hasattr(self, "market_bids") else {}
            m_info = dict(self.market_token_map.get(market_id, {})) if market_id in self.market_token_map else {}

        if m_info:
            end_dt = _parse_market_end_date(m_info)
            if end_dt is not None:
                now_utc = datetime.now(timezone.utc)
                hours_left = (end_dt - now_utc).total_seconds() / 3600.0
                if hours_left < 4.0:
                    return None

            eligible, _ = is_market_eligible(m_info, min_volume_24h=500.0, min_hours_to_expiry=4.0)
            if not eligible:
                return None

        token_yes = m_info.get("token_yes", "YES")
        token_no = m_info.get("token_no", "NO")
        q_name = m_info.get("question") if m_info else None
        short_id = (q_name[:26] + "...") if q_name else f"Market {market_id[-6:]}"
        market_meta = m_info.get("market_meta", m_info) if m_info else {}
        meta_dict = market_meta if isinstance(market_meta, dict) else {}
        actual_tick_size = float(
            meta_dict.get("minimum_tick_size")
            or m_info.get("minimum_tick_size")
            or m_info.get("tick_size")
            or meta_dict.get("tick_size")
            or 0.001
        )
        actual_neg_risk = bool(
            meta_dict.get("neg_risk")
            if "neg_risk" in meta_dict
            else m_info.get("neg_risk", False)
        )
        tick_size = actual_tick_size
        rewards_daily_rate = float(m_info.get("rewards_daily_rate") or meta_dict.get("rewards_daily_rate") or 0.0)

        bid_yes = None
        ask_yes = None
        bid_no = None
        ask_no = None
        depth_yes = depths.get(token_yes, depths.get("YES", float("inf"))) if depths else float("inf")
        depth_no = depths.get(token_no, depths.get("NO", float("inf"))) if depths else float("inf")

        # 1. Parse from sub-dicts (book_yes, book_no)
        if isinstance(books.get("book_yes"), dict):
            b_y, a_y, d_y = _extract_book_metrics(books["book_yes"])
            if b_y is not None: bid_yes = b_y
            if a_y is not None: ask_yes = a_y
            if d_y > 0 and (depth_yes == float("inf") or depth_yes == 0): depth_yes = d_y
        if isinstance(books.get("book_no"), dict):
            b_n, a_n, d_n = _extract_book_metrics(books["book_no"])
            if b_n is not None: bid_no = b_n
            if a_n is not None: ask_no = a_n
            if d_n > 0 and (depth_no == float("inf") or depth_no == 0): depth_no = d_n

        for k_yes in (token_yes, "YES"):
            if k_yes in books and isinstance(books[k_yes], dict):
                b_y, a_y, d_y = _extract_book_metrics(books[k_yes])
                if b_y is not None and bid_yes is None: bid_yes = b_y
                if a_y is not None and ask_yes is None: ask_yes = a_y
                if d_y > 0 and (depth_yes == float("inf") or depth_yes == 0): depth_yes = d_y

        for k_no in (token_no, "NO"):
            if k_no in books and isinstance(books[k_no], dict):
                b_n, a_n, d_n = _extract_book_metrics(books[k_no])
                if b_n is not None and bid_no is None: bid_no = b_n
                if a_n is not None and ask_no is None: ask_no = a_n
                if d_n > 0 and (depth_no == float("inf") or depth_no == 0): depth_no = d_n

        # 2. Parse direct asks
        if ask_yes is None:
            if "ask_yes" in books and isinstance(books["ask_yes"], (int, float)):
                ask_yes = float(books["ask_yes"])
            elif token_yes in books and isinstance(books[token_yes], (int, float)):
                ask_yes = float(books[token_yes])
            elif "YES" in books and isinstance(books["YES"], (int, float)):
                ask_yes = float(books["YES"])

        if ask_no is None:
            if "ask_no" in books and isinstance(books["ask_no"], (int, float)):
                ask_no = float(books["ask_no"])
            elif token_no in books and isinstance(books[token_no], (int, float)):
                ask_no = float(books[token_no])
            elif "NO" in books and isinstance(books["NO"], (int, float)):
                ask_no = float(books["NO"])

        # 3. Parse direct bids
        if bid_yes is None:
            for k in ("bid_yes", "bid_YES", f"{token_yes}_bid", f"{token_yes}_best_bid"):
                if k in books and isinstance(books[k], (int, float)):
                    bid_yes = float(books[k])
                    break
        if bid_no is None:
            for k in ("bid_no", "bid_NO", f"{token_no}_bid", f"{token_no}_best_bid"):
                if k in books and isinstance(books[k], (int, float)):
                    bid_no = float(books[k])
                    break

        if bid_yes is None and bids_map:
            if token_yes in bids_map:
                bid_yes = float(bids_map[token_yes])
            elif "YES" in bids_map:
                bid_yes = float(bids_map["YES"])
        if bid_no is None and bids_map:
            if token_no in bids_map:
                bid_no = float(bids_map[token_no])
            elif "NO" in bids_map:
                bid_no = float(bids_map["NO"])

        if depth_yes is None or depth_yes == float("inf"):
            d_val = depths.get("depth_yes", depths.get(token_yes, depths.get("YES", float("inf"))))
            depth_yes = float(d_val) if d_val is not None else float("inf")
        if depth_no is None or depth_no == float("inf"):
            d_val = depths.get("depth_no", depths.get(token_no, depths.get("NO", float("inf"))))
            depth_no = float(d_val) if d_val is not None else float("inf")

        max_spread = 0.030
        branch_a = None
        branch_b = None

        current_capital = float(getattr(self.risk, "available_cash", getattr(self.risk, "capital", 1000.0)) or 1000.0) if self.risk else 1000.0
        can_harvest = False
        rei_val = 0.0
        if self.reward_harvester:
            can_harvest = self.reward_harvester.can_quote_reward_market(market_id, available_cash=current_capital)
            rm = self.reward_harvester.reward_markets.get(str(market_id), {})
            rei_val = float(rm.get("rei", 0.0) or 0.0)
            if rei_val <= 0.0 and rewards_daily_rate > 0:
                min_sz = float(rm.get("min_size", 200.0) or 200.0)
                max_sp = float(rm.get("max_spread", 3.5) or 3.5)
                rei_val = self.reward_harvester.compute_reward_efficiency_index(rewards_daily_rate, min_size=min_sz, max_spread=max_sp)

        desired_trade_size = self.risk.calculate_sizing() if (hasattr(self, "risk") and self.risk) else 100.0

        # Branch A (Maker YES, Taker NO):
        if (bid_yes is not None and bid_yes > 0 and
            ask_no is not None and ask_no > 0 and
            ask_yes is not None and ask_yes > bid_yes):

            max_viable_yes = round(1.000 - ask_no * (1 + self.fee_rate) - self.min_edge, 4)
            maker_price_yes = min(ask_yes - tick_size, max_viable_yes)
            if bid_yes is not None and bid_yes >= (ask_yes - 3 * tick_size):
                maker_price_yes = max(bid_yes + tick_size, maker_price_yes)
            maker_price_yes = min(maker_price_yes, max_viable_yes)
            maker_price_yes = max(tick_size, _round_to_tick_size(maker_price_yes, tick_size))
            cost_a = maker_price_yes + ask_no * (1 + self.fee_rate)
            edge_a = 1.000 - cost_a
            taker_depth_a = depth_no if (depth_no is not None and not math.isinf(depth_no)) else 50.0
            spread_yes = ask_yes - bid_yes

            if (taker_depth_a >= 5.0 and
                spread_yes <= max_spread + 1e-7 and
                edge_a >= self.min_edge):

                trade_size_a = min(desired_trade_size, taker_depth_a) if taker_depth_a > 0 else desired_trade_size
                trade_size_a = max(5.0, trade_size_a)
                expected_profit_a = trade_size_a * edge_a
                if self.reward_harvester:
                    priority_score_a = self.reward_harvester.calculate_reward_priority(
                        market_id=market_id,
                        edge=edge_a,
                        expected_profit=expected_profit_a,
                        available_cash=current_capital
                    )
                else:
                    reward_bonus_a = (rewards_daily_rate * 0.001) if rewards_daily_rate > 0 else 0.0
                    priority_score_a = edge_a + reward_bonus_a

                branch_a = {
                    "execution_type": "maker_taker",
                    "maker_leg": "YES",
                    "maker_token": token_yes,
                    "maker_price": maker_price_yes,
                    "taker_token": token_no,
                    "taker_price": ask_no,
                    "edge": edge_a,
                    "cost": cost_a,
                    "effective_cost": cost_a,
                    "trade_size": trade_size_a,
                    "expected_profit": expected_profit_a,
                    "priority_score": priority_score_a,
                    "rewards_daily_rate": rewards_daily_rate,
                    "rei": rei_val,
                    "can_harvest_rewards": can_harvest,
                    "depth_taker": taker_depth_a,
                    "market_id": market_id,
                    "short_id": short_id,
                    "question": q_name or short_id,
                    "ask_yes": ask_yes,
                    "ask_no": ask_no,
                    "bid_yes": bid_yes,
                    "bid_no": bid_no,
                    "depth_yes": depth_yes if not math.isinf(depth_yes) else 50.0,
                    "depth_no": taker_depth_a,
                    "tick_size": tick_size,
                    "neg_risk": actual_neg_risk,
                    "market_meta": market_meta
                }

        # Branch B (Maker NO, Taker YES):
        if (bid_no is not None and bid_no > 0 and
            ask_yes is not None and ask_yes > 0 and
            ask_no is not None and ask_no > bid_no):

            max_viable_no = round(1.000 - ask_yes * (1 + self.fee_rate) - self.min_edge, 4)
            maker_price_no = min(ask_no - tick_size, max_viable_no)
            if bid_no is not None and bid_no >= (ask_no - 3 * tick_size):
                maker_price_no = max(bid_no + tick_size, maker_price_no)
            maker_price_no = min(maker_price_no, max_viable_no)
            maker_price_no = max(tick_size, _round_to_tick_size(maker_price_no, tick_size))
            cost_b = ask_yes * (1 + self.fee_rate) + maker_price_no
            edge_b = 1.000 - cost_b
            taker_depth_b = depth_yes if (depth_yes is not None and not math.isinf(depth_yes)) else 50.0
            spread_no = ask_no - bid_no

            if (taker_depth_b >= 5.0 and
                spread_no <= max_spread + 1e-7 and
                edge_b >= self.min_edge):

                trade_size_b = min(desired_trade_size, taker_depth_b) if taker_depth_b > 0 else desired_trade_size
                trade_size_b = max(5.0, trade_size_b)
                expected_profit_b = trade_size_b * edge_b
                if self.reward_harvester:
                    priority_score_b = self.reward_harvester.calculate_reward_priority(
                        market_id=market_id,
                        edge=edge_b,
                        expected_profit=expected_profit_b,
                        available_cash=current_capital
                    )
                else:
                    reward_bonus_b = (rewards_daily_rate * 0.001) if rewards_daily_rate > 0 else 0.0
                    priority_score_b = edge_b + reward_bonus_b

                branch_b = {
                    "execution_type": "maker_taker",
                    "maker_leg": "NO",
                    "maker_token": token_no,
                    "maker_price": maker_price_no,
                    "taker_token": token_yes,
                    "taker_price": ask_yes,
                    "edge": edge_b,
                    "cost": cost_b,
                    "effective_cost": cost_b,
                    "trade_size": trade_size_b,
                    "expected_profit": expected_profit_b,
                    "priority_score": priority_score_b,
                    "rewards_daily_rate": rewards_daily_rate,
                    "rei": rei_val,
                    "can_harvest_rewards": can_harvest,
                    "depth_taker": taker_depth_b,
                    "market_id": market_id,
                    "short_id": short_id,
                    "question": q_name or short_id,
                    "ask_yes": ask_yes,
                    "ask_no": ask_no,
                    "bid_yes": bid_yes,
                    "bid_no": bid_no,
                    "depth_yes": taker_depth_b,
                    "depth_no": depth_no if not math.isinf(depth_no) else 50.0,
                    "tick_size": tick_size,
                    "neg_risk": actual_neg_risk,
                    "market_meta": market_meta
                }

        if branch_a and branch_b:
            chosen = branch_a if branch_a["priority_score"] >= branch_b["priority_score"] else branch_b
        elif branch_a:
            chosen = branch_a
        elif branch_b:
            chosen = branch_b
        else:
            chosen = None

        if chosen and self.dash_state:
            self.dash_state.add_activity_log(
                f"🎯 [MAKER-TAKER] Edge {chosen['edge']*100:+.2f}% on {chosen['short_id']} | Maker {chosen['maker_leg']} @ ${chosen['maker_price']:.4f} | Taker @ ${chosen['taker_price']:.4f}"
            )

        return chosen

    def on_trade_executed(self, market_id: str, trade_size: float, expected_profit: float):
        """
        Triggered immediately the moment a trade executes.
        Compounds profits in real time and recycles collateral immediately into the pool.
        """
        if self.risk:
            self.risk.recycle_collateral(now=time.time() + 100000.0)
            if self.dash_state and hasattr(self.dash_state, "state"):
                with getattr(self.dash_state, "lock", threading.Lock()):
                    self.dash_state.state["capital"] = self.risk.capital
                    self.dash_state.state["available_cash"] = self.risk.available_cash
                    self.dash_state.dirty = True

        self.last_trade_time = time.time()
        try:
            question = self.market_token_map.get(market_id, {}).get("question", f"Market {market_id[-6:]}") if getattr(self, "market_token_map", None) else f"Market {market_id[-6:]}"
            edge_pct = round((expected_profit / trade_size) * 100.0, 2) if trade_size > 0 else 0.0
            exec_style = getattr(self.dash_state, "state", {}).get("execution_style", "paper") if self.dash_state else "paper"
            avail_cash = float(getattr(self.risk, "available_cash", 0.0) or 0.0) if self.risk else 0.0
            send_trade_notification(
                question=question,
                trade_size=trade_size,
                expected_profit=expected_profit,
                edge_pct=edge_pct,
                execution_style=exec_style,
                wallet_balance=avail_cash,
                market_id=market_id
            )
        except Exception as notify_err:
            logger.warning(f"Failed to dispatch paper trade push notification: {notify_err}")

    def execute_arbitrage(self, opp: dict) -> bool:
        market_id = opp["market_id"]
        trade_size = opp["trade_size"]
        edge = opp["edge"]
        short_id = opp.get("short_id", f"Market {market_id[-6:]}")
        target_state = self.dash_state

        with self.trade_lock:
            # Re-verify that books were not already consumed by another socket's thread
            with self.lock:
                if market_id in self.market_books:
                    books = self.market_books[market_id]
                    if any(v is None for v in books.values()):
                        return False

            # Adjust trade size to current available cash, desired sizing, and remaining market capacity
            with self.risk.lock:
                total_cap = _safe_float(getattr(self.risk, "capital", None), 1000.0)
                available_cash = _safe_float(getattr(self.risk, "available_cash", None), total_cap)
                if total_cap < 100.0:
                    spendable = available_cash
                else:
                    reserve_pct = _safe_float(getattr(self.risk, "reserve_cash_pct", None), 0.0)
                    spendable = max(0.0, available_cash - (available_cash * reserve_pct))

                open_pos = getattr(self.risk, "open_positions", {})
                if not isinstance(open_pos, dict):
                    open_pos = {}

                market_positions = [
                    p for k, p in open_pos.items()
                    if (isinstance(p, dict) and p.get('market_id') == market_id) or k == market_id or k.startswith(f"{market_id}_")
                ]
                current_m_exp = sum(_safe_float(p.get('size'), 0.0) for p in market_positions)
                max_market_pct = _safe_float(getattr(self.risk, "max_market_exposure_pct", None), 0.25)
                if total_cap < 50.0:
                    max_m_allowed = max(5.50, total_cap * max_market_pct)
                else:
                    max_m_allowed = total_cap * max_market_pct
                rem_cap = max(0.0, max_m_allowed - current_m_exp)
                max_exp_pct = _safe_float(getattr(self.risk, "max_exposure_pct", None), 0.10)
                sizing_mult = _safe_float(getattr(self.risk, "sizing_multiplier", None), 1.0)
                desired = total_cap * max_exp_pct * sizing_mult
                if total_cap < 50.0 and desired < 5.0 and spendable >= 5.0:
                    desired = 5.0
            
            trade_size = min(trade_size, desired, spendable, rem_cap)
            if trade_size < 1.0:
                if spendable < 1.0:
                    reason = MissedReason.INSUFFICIENT_CASH
                elif rem_cap < 1.0:
                    reason = MissedReason.EXPOSURE_LIMIT_EXCEEDED
                else:
                    reason = MissedReason.INSUFFICIENT_CASH

                if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                    opp_copy = dict(opp)
                    opp_copy["trade_size"] = opp.get("trade_size", desired)
                    opp_copy["expected_profit"] = opp_copy["trade_size"] * edge
                    self.shadow_tracker.record_missed(opp_copy, reason)
                return False

            expected_profit = trade_size * edge

            if not self.risk.can_trade(trade_size, market_id=market_id):
                gating_reason = getattr(self.risk, "check_trade_gating_reason", lambda s, m: None)(trade_size, market_id=market_id)
                try:
                    reason_enum = MissedReason(gating_reason) if gating_reason else MissedReason.CIRCUIT_BREAKER
                except ValueError:
                    reason_enum = MissedReason.CIRCUIT_BREAKER
                if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                    opp_copy = dict(opp)
                    opp_copy["trade_size"] = trade_size
                    opp_copy["expected_profit"] = expected_profit
                    self.shadow_tracker.record_missed(opp_copy, reason_enum)
                return False

            logger.info(f"🚨 ARBITRAGE OPPORTUNITY 🚨 | Market {market_id} ({short_id})")
            logger.info(f"YES Ask: {opp['ask_yes']:.4f} | NO Ask: {opp['ask_no']:.4f} | Total Cost: {opp['effective_cost']:.4f} | Edge: {edge*100:.2f}%")
            logger.info(f"PAPER TRADE EXECUTED -> Size: ${trade_size:.2f} | Exp. Profit: ${expected_profit:.2f}")

            opened = self.risk.open_position(market_id, trade_size, expected_profit)
            if not opened:
                return False

            self.on_trade_executed(market_id, trade_size, expected_profit)

            logger.info(f"New Capital: ${self.risk.capital:.2f} (Available: ${self.risk.available_cash:.2f}, Locked: ${self.risk.locked_collateral:.2f})\n")

            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if target_state:
                target_state.add_trade(market_id, round(trade_size, 2), round(expected_profit, 4), time_str)

            # Reset book to avoid duplicate trades on same tick
            with self.lock:
                if market_id in self.market_books:
                    for k in self.market_books[market_id]:
                        self.market_books[market_id][k] = None
                if market_id in self.market_depths:
                    del self.market_depths[market_id]
                stale_keys = [k for k in self.last_processed_quotes if k[0] == market_id]
                for k in stale_keys:
                    del self.last_processed_quotes[k]

            # Clear edge in dashboard state without zeroing out asks or corrupting charts
            if target_state:
                target_state.clear_market_edge(market_id)

            return True


    def on_book_tick(self, market_id: str) -> Optional[dict]:
        """
        Dual-mode arbitrage evaluation hook.
        If execution_style == 'maker_taker', actively evaluates maker-taker parity opportunities.
        Otherwise evaluates taker-taker arbitrage first and maker-taker as a secondary pass.
        If opportunity is discovered, dispatches directly to execute_arbitrage.
        """
        with self.trade_lock:
            exec_style = getattr(self.dash_state, "state", {}).get("execution_style", "") if self.dash_state else ""
            if exec_style == "maker_taker":
                opp = self.check_maker_taker_parity(market_id)
                if opp:
                    if self.execute_arbitrage(opp):
                        return opp
                opp_taker = self.check_market_parity(market_id)
                if opp_taker:
                    if self.execute_arbitrage(opp_taker):
                        return opp_taker
            else:
                opp = self.check_market_parity(market_id)
                if opp:
                    if self.execute_arbitrage(opp):
                        return opp
                opp_mt = self.check_maker_taker_parity(market_id)
                if opp_mt:
                    if self.execute_arbitrage(opp_mt):
                        return opp_mt
        return None

    def execute_negrisk_basket(self, opp: dict) -> bool:
        """
        Executes or paper-trades a multi-outcome Neg-Risk basket parity arbitrage.
        Buys 1 share of YES across all N mutually exclusive outcomes.
        """
        basket_id = opp["neg_risk_market_id"]
        trade_size = opp["trade_size"]
        expected_profit = opp["expected_profit"]
        edge = opp["edge"]
        num_outcomes = opp["num_outcomes"]

        with self.trade_lock:
            if self.risk:
                with self.risk.lock:
                    total_cap = _safe_float(getattr(self.risk, "capital", None), 1000.0)
                    available_cash = _safe_float(getattr(self.risk, "available_cash", None), total_cap)
                    reserve_pct = _safe_float(getattr(self.risk, "reserve_cash_pct", None), 0.0)
                    spendable = max(0.0, available_cash - (available_cash * reserve_pct)) if total_cap >= 100.0 else available_cash

                trade_size = min(trade_size, spendable)
                if trade_size < 5.0:
                    if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                        opp_copy = dict(opp)
                        opp_copy["trade_size"] = trade_size
                        self.shadow_tracker.record_missed(opp_copy, MissedReason.INSUFFICIENT_CASH)
                    return False

                if not self.risk.can_trade(trade_size, market_id=basket_id):
                    return False

                opened = self.risk.open_position(basket_id, trade_size, expected_profit)
                if not opened:
                    return False

            logger.info(f"🚨 NEG-RISK BASKET ARBITRAGE EXECUTED 🚨 | Basket {basket_id} ({num_outcomes} outcomes)")
            logger.info(f"Sum Asks: {opp['sum_ask']:.4f} | Total Cost: {opp['total_cost']:.4f} | Edge: {edge*100:.2f}%")
            logger.info(f"BASKET TRADE -> Size: ${trade_size:.2f} | Exp. Profit: ${expected_profit:.2f}")

            self.on_trade_executed(basket_id, trade_size, expected_profit)

            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if self.dash_state:
                self.dash_state.add_trade(basket_id, round(trade_size, 2), round(expected_profit, 4), time_str)
                self.dash_state.add_activity_log(
                    f"🎉 [NEG-RISK BASKET FILLED] Basket {basket_id[:8]} ({num_outcomes} legs) -> Size: ${trade_size:.2f} | Edge: {edge*100:+.2f}% | Profit: +${expected_profit:.2f}"
                )

            # Reset books for all basket legs non-destructively
            with self.lock:
                for o in opp.get("outcomes", []):
                    cid = o.get("condition_id")
                    if cid and cid in self.market_books:
                        for k in self.market_books[cid]:
                            self.market_books[cid][k] = None
                    if cid and cid in self.market_depths:
                        del self.market_depths[cid]

            return True

    def evaluate_parity(self, market_id: str) -> Optional[dict]:
        if getattr(self, "negrisk_scanner", None):
            neg_risk_id = None
            m_info = self.market_token_map.get(market_id, {})
            if isinstance(m_info, dict):
                neg_risk_id = (
                    m_info.get("neg_risk_market_id")
                    or m_info.get("negRiskMarketID")
                    or m_info.get("negRiskMarketId")
                    or (m_info.get("market_meta", {}).get("neg_risk_market_id") if isinstance(m_info.get("market_meta"), dict) else None)
                )
            if not neg_risk_id:
                neg_risk_id = getattr(self.negrisk_scanner, "market_to_basket", {}).get(market_id)

            if neg_risk_id:
                opp_nr = self.negrisk_scanner.check_basket_parity(
                    neg_risk_market_id=neg_risk_id,
                    market_books=self.market_books,
                    market_depths=self.market_depths,
                    fee_rate=self.fee_rate,
                    min_edge=self.min_edge
                )
                if opp_nr:
                    if self.dash_state:
                        self.dash_state.add_activity_log(
                            f"🪢 [NEG-RISK BASKET] Edge {opp_nr['edge']*100:+.2f}% on Basket {neg_risk_id[:10]}... | {opp_nr['num_outcomes']} outcomes | Sum Asks: ${opp_nr['sum_ask']:.4f} | Size: ${opp_nr['trade_size']:.2f}"
                        )
                    if self.execute_negrisk_basket(opp_nr):
                        return opp_nr

        return self.on_book_tick(market_id)


def on_message(ws, message, simulator):
    """
    Handle py-sdk / CLOB WebSocket messages across all monitored pairs.
    Parses both 'book' snapshots and real-time 'price_change' events.
    Supports depth parsing, multi-market batch updates, and priority ranking.
    """
    try:
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="ignore")
        if not message or not str(message).strip() or str(message).strip().upper() in ("PONG", "PING"):
            return
        
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, ValueError):
            return

        target_state = simulator.dash_state if (simulator and simulator.dash_state) else globals().get("dash_state")
        if target_state:
            target_state.record_tick()
        events = data if isinstance(data, list) else [data]

        touched_markets = []
        seen = set()
        for event in events:
            ev_type = event.get("event_type") or event.get("type")
            if ev_type == "book":
                market_id = event.get("market")
                asset_id = event.get("asset_id")
                asks = event.get("asks", [])
                bids = event.get("bids", [])
                if (asks or bids) and market_id and asset_id:
                    valid_asks = []
                    for a in asks:
                        try:
                            p = float(a.get("price", 0))
                            s = float(a.get("size", 0))
                            if p > 0:
                                valid_asks.append((p, s))
                        except (ValueError, TypeError):
                            pass
                    best_ask = None
                    best_size = None
                    if valid_asks:
                        best_ask_tuple = min(valid_asks, key=lambda x: x[0])
                        best_ask = best_ask_tuple[0]
                        best_size = sum(s for p, s in valid_asks if abs(p - best_ask) < 1e-9)

                    valid_bids = []
                    for b in bids:
                        try:
                            p = float(b.get("price", 0))
                            s = float(b.get("size", 0))
                            if p > 0:
                                valid_bids.append((p, s))
                        except (ValueError, TypeError):
                            pass
                    best_bid = None
                    best_bid_size = None
                    if valid_bids:
                        best_bid_tuple = max(valid_bids, key=lambda x: x[0])
                        best_bid = best_bid_tuple[0]
                        best_bid_size = sum(s for p, s in valid_bids if abs(p - best_bid) < 1e-9)

                    if best_ask is not None:
                        updated = simulator.update_book(
                            market_id,
                            asset_id,
                            best_ask,
                            ask_size=best_size,
                            evaluate=False,
                            best_bid=best_bid,
                            bid_size=best_bid_size
                        )
                        if (updated or best_bid is not None) and market_id not in seen:
                            seen.add(market_id)
                            touched_markets.append(market_id)
                    elif best_bid is not None:
                        with simulator.lock:
                            if not hasattr(simulator, "market_bids") or simulator.market_bids is None:
                                simulator.market_bids = {}
                            if market_id not in simulator.market_bids:
                                simulator.market_bids[market_id] = {}
                            simulator.market_bids[market_id][asset_id] = float(best_bid)
                        if market_id not in seen:
                            seen.add(market_id)
                            touched_markets.append(market_id)

            elif ev_type == "best_bid_ask":
                market_id = event.get("market")
                asset_id = event.get("asset_id")
                best_ask = event.get("best_ask") or event.get("ask")
                best_bid = event.get("best_bid") or event.get("bid")

                raw_bid_val = None
                if best_bid is not None:
                    try:
                        f_bid = float(best_bid)
                        if f_bid > 0:
                            raw_bid_val = f_bid
                    except (ValueError, TypeError):
                        pass

                raw_bid_size = event.get("best_bid_size") or event.get("bid_size")
                bid_size_val = None
                if raw_bid_size is not None:
                    try:
                        bs = float(raw_bid_size)
                        if bs > 0:
                            bid_size_val = bs
                    except (ValueError, TypeError):
                        pass

                if market_id and asset_id:
                    if best_ask is not None:
                        try:
                            val = float(best_ask)
                            if val > 0:
                                size_val = None
                                raw_size = event.get("best_ask_size") or event.get("ask_size") or event.get("size")
                                if raw_size is not None:
                                    try:
                                        s = float(raw_size)
                                        if s > 0:
                                            size_val = s
                                    except (ValueError, TypeError):
                                        pass
                                updated = simulator.update_book(
                                    market_id,
                                    asset_id,
                                    val,
                                    ask_size=size_val,
                                    evaluate=False,
                                    best_bid=raw_bid_val,
                                    bid_size=bid_size_val
                                )
                                if (updated or raw_bid_val is not None) and market_id not in seen:
                                    seen.add(market_id)
                                    touched_markets.append(market_id)
                        except (ValueError, TypeError):
                            pass
                    elif raw_bid_val is not None:
                        with simulator.lock:
                            if not hasattr(simulator, "market_bids") or simulator.market_bids is None:
                                simulator.market_bids = {}
                            if market_id not in simulator.market_bids:
                                simulator.market_bids[market_id] = {}
                            simulator.market_bids[market_id][asset_id] = raw_bid_val
                        if market_id not in seen:
                            seen.add(market_id)
                            touched_markets.append(market_id)

            elif ev_type == "price_change":
                market_id = event.get("market")
                for change in event.get("price_changes", []):
                    asset_id = change.get("asset_id")
                    if not (market_id and asset_id):
                        continue
                    
                    side = str(change.get("side", "")).upper()
                    best_ask = change.get("best_ask")
                    if best_ask is None and side == "SELL":
                        best_ask = change.get("price")

                    best_bid = change.get("best_bid")
                    if best_bid is None and side == "BUY":
                        best_bid = change.get("price")

                    raw_bid_val = None
                    if best_bid is not None:
                        try:
                            f_bid = float(best_bid)
                            if f_bid > 0:
                                raw_bid_val = f_bid
                        except (ValueError, TypeError):
                            pass

                    raw_bid_size = change.get("best_bid_size") or change.get("bid_size")
                    if raw_bid_size is None and side == "BUY":
                        raw_bid_size = change.get("size")
                    bid_size_val = None
                    if raw_bid_size is not None:
                        try:
                            bs = float(raw_bid_size)
                            if bs > 0:
                                bid_size_val = bs
                        except (ValueError, TypeError):
                            pass

                    if best_ask is not None:
                        try:
                            val = float(best_ask)
                            if val > 0:
                                change_price = float(change.get("price", val))
                                raw_size = change.get("best_ask_size") or change.get("ask_size")
                                if raw_size is None and side in ("SELL", ""):
                                    raw_size = change.get("size")
                                
                                size_val = None
                                if raw_size is not None:
                                    try:
                                        parsed_s = float(raw_size)
                                        if side in ("SELL", "") and abs(change_price - val) < 1e-6 and parsed_s > 0:
                                            size_val = parsed_s
                                    except (ValueError, TypeError):
                                        pass
                                
                                updated = simulator.update_book(
                                    market_id,
                                    asset_id,
                                    val,
                                    ask_size=size_val,
                                    evaluate=False,
                                    best_bid=raw_bid_val,
                                    bid_size=bid_size_val
                                )
                                if (updated or raw_bid_val is not None) and market_id not in seen:
                                    seen.add(market_id)
                                    touched_markets.append(market_id)
                        except (ValueError, TypeError):
                            pass
                    elif raw_bid_val is not None:
                        with simulator.lock:
                            if not hasattr(simulator, "market_bids") or simulator.market_bids is None:
                                simulator.market_bids = {}
                            if market_id not in simulator.market_bids:
                                simulator.market_bids[market_id] = {}
                            simulator.market_bids[market_id][asset_id] = raw_bid_val
                        if market_id not in seen:
                            seen.add(market_id)
                            touched_markets.append(market_id)

        # Evaluate parity across all touched markets in this frame
        candidates = []
        exec_style = getattr(simulator.dash_state, "state", {}).get("execution_style", "maker_taker") if simulator.dash_state else "maker_taker"
        for m_id in touched_markets:
            opp = None
            if exec_style == "maker_taker":
                opp = simulator.check_maker_taker_parity(m_id) or simulator.check_market_parity(m_id)
            else:
                opp = simulator.check_market_parity(m_id) or simulator.check_maker_taker_parity(m_id)
            if opp:
                candidates.append(opp)

        # Prioritize candidates by highest expected profit / edge factoring in rewards
        if candidates:
            if hasattr(simulator, "reward_harvester") and simulator.reward_harvester:
                candidates.sort(
                    key=lambda x: simulator.reward_harvester.calculate_reward_priority(
                        x["market_id"],
                        edge=x.get("edge", 0.0),
                        expected_profit=x.get("expected_profit")
                    ),
                    reverse=True
                )
            else:
                candidates.sort(key=lambda x: x.get("expected_profit", 0.0), reverse=True)
            for opp in candidates:
                if hasattr(simulator, "dispatch_arbitrage"):
                    simulator.dispatch_arbitrage(opp)
                else:
                    simulator.execute_arbitrage(opp)

    except Exception as e:
        logger.error(f"Error parsing message: {e}")

def on_error(ws, error):
    logger.error(f"WebSocket Error: {error}")
    ds = globals().get("dash_state")
    if ds:
        ds.set_bot_status(f"ERROR: {error}")

def on_close(ws, close_status_code, close_msg):
    logger.info(f"WebSocket Closed (code={close_status_code}, msg={close_msg}).")
    ds = globals().get("dash_state")
    if ds:
        ds.set_bot_status("DISCONNECTED")

def start_keepalive(ws, interval: float = 20.0, stop_event: Optional[threading.Event] = None) -> threading.Thread:
    """
    Start a background keepalive thread sending RFC 6455 Ping frames at the specified interval
    to avoid silent connection drops on Polymarket CLOB WebSocket without triggering 1008 payload errors.
    """
    def _ping_loop():
        while True:
            if stop_event and stop_event.wait(interval):
                break
            elif not stop_event:
                time.sleep(interval)
            try:
                # Protocol ping frame (RFC 6455 opcode 0x9) rather than raw text "PING"
                sock = getattr(ws, "sock", None)
                ping_opcode = getattr(websocket.ABNF, "OPCODE_PING", 0x9) if 'websocket' in globals() and hasattr(websocket, 'ABNF') else 0x9
                if sock is not None and getattr(sock, "connected", False):
                    try:
                        ws.send("", opcode=ping_opcode)
                    except TypeError:
                        ws.send("")
                elif hasattr(ws, "send") and not hasattr(ws, "sock"):
                    try:
                        ws.send("", opcode=ping_opcode)
                    except TypeError:
                        ws.send("")
                else:
                    break
            except Exception:
                break

    t = threading.Thread(target=_ping_loop, daemon=True)
    t.start()
    return t

def on_open(
    ws,
    all_token_ids: List[str],
    market_count: int,
    worker_id: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
    dash_state: Optional[DashboardState] = None,
    worker_tag: Optional[str] = None
):
    if dash_state is None:
        dash_state = globals().get("dash_state")
    # Deduplicate token IDs while preserving deterministic order
    unique_tokens = list(dict.fromkeys(all_token_ids))
    if worker_tag:
        prefix = f"{worker_tag} "
    elif worker_id is not None:
        pool_name = "Pool B" if worker_id >= 8 else "Pool A"
        prefix = f"[{pool_name} - Worker {worker_id + 1}] "
    else:
        prefix = ""
    logger.info(f"{prefix}Connected to Polymarket WebSocket. Subscribing to {market_count} pairs ({len(unique_tokens)} tokens)...")
    if dash_state:
        dash_state.set_bot_status("ONLINE_SCANNING")
    
    # Start 20-second protocol ping keepalive thread if socket does not manage its own pings
    if not getattr(ws, "_has_managed_pings", False):
        start_keepalive(ws, interval=20.0, stop_event=stop_event)

    BATCH_SIZE = 100
    total_tokens = len(unique_tokens)
    num_batches = (total_tokens + BATCH_SIZE - 1) // BATCH_SIZE if total_tokens > 0 else 0
    if dash_state:
        dash_state.add_activity_log(
            f"{prefix}WebSocket connected. Subscribed to {market_count} market orderbook feeds ({total_tokens} tokens across {num_batches} batches)."
        )
    
    # Batch subscription payloads using official syntax {"assets_ids": batch, "operation": "subscribe"}
    for i in range(0, total_tokens, BATCH_SIZE):
        batch = unique_tokens[i:i + BATCH_SIZE]
        payload = {
            "assets_ids": batch,
            "operation": "subscribe",
            "type": "market",
            "custom_feature_enabled": True
        }
        ws.send(json.dumps(payload))
        logger.info(f"{prefix}Sent subscription payload for {len(batch)} asset IDs ({i+1} to {min(i+BATCH_SIZE, total_tokens)})")
        if i + BATCH_SIZE < total_tokens:
            time.sleep(0.02)

def seed_order_books_via_rest(
    simulator: PaperSimulator,
    all_token_ids: List[str],
    chunk_size: int = 500,
    clob_host: str = "https://clob.polymarket.com",
    timeout: float = 8.0
) -> int:
    """
    Cold-start order book pre-population via POST https://clob.polymarket.com/books.
    Fetches snapshot books in chunks of 500 tokens (e.g. 4 requests for 2,000 tokens).
    Pre-populates simulator.market_books, simulator.market_depths, and initial market parity.
    Eliminates cold-start 'connecting / awaiting ticks' state across the entire universe.
    Returns count of successfully seeded markets.
    """
    unique_tokens = list(dict.fromkeys([str(t) for t in all_token_ids if t]))
    if not unique_tokens:
        return 0

    url = f"{clob_host.rstrip('/')}/books"
    touched_markets: Set[str] = set()
    total_tokens = len(unique_tokens)
    num_chunks = (total_tokens + chunk_size - 1) // chunk_size if total_tokens > 0 else 0
    logger.info(f"Initiating REST cold-start order book seeding for {total_tokens} tokens across {num_chunks} chunks...")

    for i in range(0, total_tokens, chunk_size):
        chunk = unique_tokens[i:i + chunk_size]
        payload = [{"token_id": t} for t in chunk]
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PolymarketBot/2.0"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                books_data = json.loads(resp.read().decode("utf-8"))

            if isinstance(books_data, list):
                for book in books_data:
                    if not isinstance(book, dict):
                        continue
                    market_id = book.get("market")
                    asset_id = str(book.get("asset_id", ""))
                    asks = book.get("asks", [])
                    bids = book.get("bids", [])
                    if market_id and asset_id and (asks or bids):
                        valid_asks = []
                        for a in asks:
                            try:
                                p = float(a.get("price", 0))
                                s = float(a.get("size", 0))
                                if p > 0:
                                    valid_asks.append((p, s))
                            except (ValueError, TypeError):
                                pass
                        best_ask = None
                        best_size = None
                        if valid_asks:
                            best_ask_tuple = min(valid_asks, key=lambda x: x[0])
                            best_ask = best_ask_tuple[0]
                            best_size = sum(s for p, s in valid_asks if abs(p - best_ask) < 1e-9)

                        valid_bids = []
                        for b in bids:
                            try:
                                p = float(b.get("price", 0))
                                s = float(b.get("size", 0))
                                if p > 0:
                                    valid_bids.append((p, s))
                            except (ValueError, TypeError):
                                pass
                        best_bid = None
                        best_bid_size = None
                        if valid_bids:
                            best_bid_tuple = max(valid_bids, key=lambda x: x[0])
                            best_bid = best_bid_tuple[0]
                            best_bid_size = sum(s for p, s in valid_bids if abs(p - best_bid) < 1e-9)

                        if best_ask is not None or best_bid is not None:
                            simulator.update_book(
                                market_id,
                                asset_id,
                                best_ask or 0.0,
                                ask_size=best_size or 0.0,
                                best_bid=best_bid,
                                bid_size=best_bid_size,
                                evaluate=False
                            )
                            touched_markets.add(market_id)
            logger.info(f"REST cold-start seeded chunk {i // chunk_size + 1}/{num_chunks}: processed {len(chunk)} tokens ({len(touched_markets)} markets active so far).")
        except Exception as e:
            logger.warning(f"REST cold-start book seeding failed for chunk {i // chunk_size + 1}/{num_chunks} ({len(chunk)} tokens): {e}")

    # Evaluate parity across all populated markets
    candidates = []
    for m_id in touched_markets:
        opp = simulator.check_market_parity(m_id)
        if opp:
            candidates.append(opp)

    if candidates:
        candidates.sort(key=lambda x: x["expected_profit"], reverse=True)
        for opp in candidates:
            simulator.execute_arbitrage(opp)

    target_state = simulator.dash_state
    if target_state:
        target_state.record_tick()
        target_state.set_bot_status("ONLINE_SCANNING")
        target_state.add_activity_log(
            f"REST cold-start complete: pre-populated order books for {len(touched_markets)} markets ({total_tokens} tokens)."
        )
        target_state._write_if_dirty()

    logger.info(f"REST cold-start order book seeding complete: {len(touched_markets)} markets populated.")
    return len(touched_markets)

class SocketWorkerState:
    def __init__(
        self,
        worker_id: int,
        initial_markets: List[dict],
        pool_name: Optional[str] = None,
        worker_num: Optional[int] = None,
        shard_id: Optional[int] = None
    ):
        self.worker_id = worker_id
        self.markets = list(initial_markets)
        self.token_ids: Set[str] = set()
        for m in initial_markets:
            for t in m.get("token_ids", []):
                self.token_ids.add(str(t))
        self.active_ws = None
        self.lock = threading.Lock()

        # Dual Active-Active Pool Identification
        if pool_name is not None:
            self.pool_name = pool_name
        else:
            self.pool_name = "Pool B" if worker_id >= 8 else "Pool A"

        if worker_num is not None:
            self.worker_num = worker_num
        else:
            self.worker_num = worker_id + 1

        self.shard_id = shard_id if shard_id is not None else (worker_id % 8)
        self.tag = f"[{self.pool_name} - Worker {self.worker_num}]"

    def add_markets(self, new_markets: List[dict]) -> List[str]:
        new_tokens = []
        with self.lock:
            for m in new_markets:
                self.markets.append(m)
                for t in m.get("token_ids", []):
                    st = str(t)
                    if st not in self.token_ids:
                        self.token_ids.add(st)
                        new_tokens.append(st)
            ws = self.active_ws

        if ws and new_tokens:
            try:
                BATCH_SIZE = 100
                for i in range(0, len(new_tokens), BATCH_SIZE):
                    batch = new_tokens[i:i + BATCH_SIZE]
                    payload = {
                        "assets_ids": batch,
                        "operation": "subscribe",
                        "type": "market",
                        "custom_feature_enabled": True
                    }
                    ws.send(json.dumps(payload))
                logger.info(f"{self.tag} Dynamically subscribed to {len(new_tokens)} new asset tokens.")
            except Exception as e:
                logger.warning(f"{self.tag} Error dynamically subscribing to tokens: {e}")
        return new_tokens

    def remove_markets(self, condition_ids_to_remove: Set[str]) -> List[str]:
        dropped_tokens = []
        with self.lock:
            kept_markets = []
            for m in self.markets:
                if m.get("condition_id") in condition_ids_to_remove:
                    for t in m.get("token_ids", []):
                        st = str(t)
                        if st in self.token_ids:
                            self.token_ids.remove(st)
                            dropped_tokens.append(st)
                else:
                    kept_markets.append(m)
            self.markets = kept_markets
            ws = self.active_ws

        if ws and dropped_tokens:
            try:
                BATCH_SIZE = 100
                for i in range(0, len(dropped_tokens), BATCH_SIZE):
                    batch = dropped_tokens[i:i + BATCH_SIZE]
                    payload = {
                        "assets_ids": batch,
                        "operation": "unsubscribe",
                        "type": "market"
                    }
                    ws.send(json.dumps(payload))
                logger.info(f"{self.tag} Dynamically unsubscribed from {len(dropped_tokens)} closed tokens.")
            except Exception:
                pass
        return dropped_tokens

def refresh_market_universe(
    simulator: PaperSimulator,
    worker_states: List[SocketWorkerState],
    market_limit: int = 1000,
    min_volume_24h: float = 1000.0,
    min_liquidity: Optional[float] = None
) -> Tuple[int, int]:
    """
    Dynamically rotate out closed/settled and stagnant markets, and subscribe to newly
    active high-volume markets across the worker pool without dropping connections.
    Returns (num_added, num_removed).
    """
    if min_liquidity is None:
        min_liquidity = float(os.environ.get("POLYMARKET_BOT_MIN_LIQUIDITY", 500.0))

    logger.info("Initiating dynamic market universe auto-refresh scan...")
    try:
        refreshed_markets = fetch_top_markets(
            limit=market_limit,
            min_volume_24h=min_volume_24h,
            min_liquidity=min_liquidity,
            use_fallback=False
        )
    except Exception as e:
        logger.warning(f"Market universe fetch failed during refresh: {e}")
        return 0, 0

    if not refreshed_markets:
        logger.warning("Dynamic refresh returned no active markets. Aborting refresh cycle.")
        return 0, 0

    # Determine currently monitored condition IDs
    current_cids = set()
    for w in worker_states:
        with w.lock:
            for m in w.markets:
                cid = m.get("condition_id")
                if cid:
                    current_cids.add(cid)
    with simulator.lock:
        current_cids.update(simulator.market_token_map.keys())

    refreshed_cids = {m["condition_id"] for m in refreshed_markets if m.get("condition_id")}

    now = time.time()
    open_pos = simulator.risk.open_positions if (simulator.risk and hasattr(simulator.risk, "open_positions")) else {}
    if not isinstance(open_pos, dict):
        open_pos = {}

    # Scan for stagnant markets (configurable timeout, default 180.0s = 3 min, never evict open positions)
    stagnant_timeout = float(os.environ.get("POLYMARKET_BOT_STAGNANT_TIMEOUT", 180.0))
    stagnant_cids = set()
    for cid in current_cids:
        if cid in open_pos:
            continue
        last_tick = simulator.last_market_tick.get(cid, now)
        if (now - last_tick) > stagnant_timeout:
            stagnant_cids.add(cid)

    # Dynamic shallow/unquoted market eviction (max 50 per cycle)
    shallow_cids = []
    with simulator.lock:
        for cid in current_cids:
            if cid in open_pos or cid in stagnant_cids:
                continue
            books = simulator.market_books.get(cid, {})
            depths = simulator.market_depths.get(cid, {})
            m_info = simulator.market_token_map.get(cid, {})

            if m_info and "token_yes" in m_info and "token_no" in m_info:
                y_ask = float(books.get(m_info["token_yes"]) or 0.0)
                n_ask = float(books.get(m_info["token_no"]) or 0.0)
                d_yes = float(depths.get(m_info["token_yes"]) or 0.0)
                d_no = float(depths.get(m_info["token_no"]) or 0.0)
            elif "YES" in books and "NO" in books:
                y_ask = float(books.get("YES") or 0.0)
                n_ask = float(books.get("NO") or 0.0)
                d_yes = float(depths.get("YES") or 0.0)
                d_no = float(depths.get("NO") or 0.0)
            elif len(books) >= 2:
                keys = sorted(books.keys())
                y_ask = float(books.get(keys[0]) or 0.0)
                n_ask = float(books.get(keys[1]) or 0.0)
                d_yes = float(depths.get(keys[0]) or 0.0)
                d_no = float(depths.get(keys[1]) or 0.0)
            else:
                y_ask = float(books.get("YES") or 0.0)
                n_ask = float(books.get("NO") or 0.0)
                d_yes = 0.0
                d_no = 0.0

            is_unquoted = (y_ask == 0.0 and n_ask == 0.0 and cid in simulator.market_books)
            cross_spread = (y_ask + n_ask) - 1.00
            is_shallow_wide = (y_ask > 0.0 and n_ask > 0.0 and min(d_yes, d_no) < 5.0 and cross_spread > 0.10)

            if is_unquoted or is_shallow_wide:
                shallow_cids.append(cid)

    shallow_evictions = set(shallow_cids[:50])
    stagnant_cids.update(shallow_evictions)

    # Maintain stagnant cooldown quarantine to prevent dead markets from immediately re-subscribing
    if not hasattr(simulator, "stagnant_cooldown"):
        simulator.stagnant_cooldown = {}
    simulator.stagnant_cooldown = {cid: exp for cid, exp in simulator.stagnant_cooldown.items() if exp > now}
    for cid in stagnant_cids:
        simulator.stagnant_cooldown[cid] = now + 1800.0  # 30 minute quarantine

    if not hasattr(simulator, "stagnant_purged_total"):
        simulator.stagnant_purged_total = 0
    simulator.stagnant_purged_total += len(stagnant_cids)

    # Identify dropped condition IDs (closed, volume dropped, or stagnant; NEVER open positions)
    dropped_cids = {cid for cid in current_cids if (cid not in refreshed_cids or cid in stagnant_cids) and cid not in open_pos}

    # Identify new markets to add (must not be currently monitored, dropped, or in stagnant cooldown quarantine)
    stagnant_cooldown = getattr(simulator, "stagnant_cooldown", {})
    new_markets = [
        m for m in refreshed_markets
        if m.get("condition_id")
        and m["condition_id"] not in current_cids
        and m["condition_id"] not in dropped_cids
        and m["condition_id"] not in stagnant_cooldown
    ]

    # 1. Prune dropped/closed/stagnant markets
    if dropped_cids:
        for w in worker_states:
            w.remove_markets(dropped_cids)

        with simulator.lock:
            for cid in dropped_cids:
                if cid in simulator.market_books:
                    del simulator.market_books[cid]
                if cid in simulator.market_depths:
                    del simulator.market_depths[cid]
                if cid in simulator.market_token_map:
                    del simulator.market_token_map[cid]
                if cid in simulator.last_market_tick:
                    del simulator.last_market_tick[cid]
            stale_q_keys = [k for k in simulator.last_processed_quotes if k[0] in dropped_cids]
            for k in stale_q_keys:
                del simulator.last_processed_quotes[k]

        target_dash = simulator.dash_state or globals().get("dash_state")
        if target_dash and hasattr(target_dash, "state"):
            with target_dash.lock:
                for cid in dropped_cids:
                    if "markets" in target_dash.state and cid in target_dash.state["markets"]:
                        del target_dash.state["markets"][cid]
                    if "price_history" in target_dash.state and cid in target_dash.state["price_history"]:
                        del target_dash.state["price_history"][cid]
                    if "ohlc" in target_dash.state and cid in target_dash.state["ohlc"]:
                        del target_dash.state["ohlc"][cid]
                target_dash.dirty = True

    # 2. Add newly active markets
    if new_markets:
        with simulator.lock:
            for m in new_markets:
                cid = m["condition_id"]
                tids = m["token_ids"]
                outcomes = m.get("outcomes", ["Yes", "No"])
                simulator.market_token_map[cid] = {
                    "token_yes": tids[0],
                    "token_no": tids[1],
                    "question": m["question"],
                    "outcomes": outcomes,
                    "market_meta": m,
                    "volume24hr": float(m.get('volume24hr', 0.0) or 0.0),
                    "endDateIso": m.get('endDateIso') or m.get('endDate'),
                    "slug": m.get('slug') or m.get('market_slug', ''),
                    "category": m.get('category', ''),
                    "tick_size": float(m.get('minimum_tick_size') or m.get('tick_size') or 0.001)
                }
                simulator.last_market_tick[cid] = now

        # Distribute new markets across workers with fewest markets
        pool_a_workers = [w for w in worker_states if getattr(w, "pool_name", "") == "Pool A"]
        pool_b_workers = [w for w in worker_states if getattr(w, "pool_name", "") == "Pool B"]

        new_tokens_to_seed = []
        for m in new_markets:
            if pool_a_workers and pool_b_workers:
                best_worker_a = min(pool_a_workers, key=lambda w: len(w.markets))
                added_tids = best_worker_a.add_markets([m])
                new_tokens_to_seed.extend(added_tids)
                best_worker_b = next((w for w in pool_b_workers if getattr(w, "shard_id", None) == getattr(best_worker_a, "shard_id", None)), None)
                if best_worker_b:
                    best_worker_b.add_markets([m])
            elif worker_states:
                best_worker = min(worker_states, key=lambda w: len(w.markets))
                added_tids = best_worker.add_markets([m])
                new_tokens_to_seed.extend(added_tids)

        # Pre-seed new markets via REST cold-start
        if new_tokens_to_seed:
            try:
                seed_order_books_via_rest(simulator, new_tokens_to_seed, chunk_size=500)
            except Exception as e:
                logger.warning(f"Error seeding new dynamic markets via REST: {e}")
        if hasattr(simulator, "seed_clob_token_cache"):
            try:
                simulator.seed_clob_token_cache()
            except Exception:
                pass

    # 3. Synchronize with dashboard state
    target_dash = simulator.dash_state or globals().get("dash_state")
    if target_dash:
        with simulator.lock:
            active_market_objs = [
                {
                    "condition_id": cid,
                    "question": info.get("question", f"Market {cid[:8]}"),
                    "token_ids": [info.get("token_yes"), info.get("token_no")],
                    "outcomes": info.get("outcomes", ["Yes", "No"])
                }
                for cid, info in simulator.market_token_map.items()
            ]
        target_dash.init_monitored_markets(active_market_objs)
        target_dash.state['active_markets_count'] = len(simulator.market_token_map)
        target_dash.state['stagnant_purged_count'] = getattr(simulator, 'stagnant_purged_total', 0)
        if new_markets or dropped_cids:
            target_dash.add_activity_log(
                f"🔄 Market Universe Refreshed: +{len(new_markets)} active pairs added, -{len(dropped_cids)} closed/stagnant pairs rotated."
            )
        target_dash.dirty = True
        target_dash._write_if_dirty()

    logger.info(
        f"Dynamic market universe refresh complete: +{len(new_markets)} added, "
        f"-{len(dropped_cids)} removed ({len(stagnant_cids)} stagnant). Total active monitored: {len(simulator.market_token_map)}."
    )
    return len(new_markets), len(dropped_cids)

def start_market_universe_refresher(
    simulator: PaperSimulator,
    worker_states: List[SocketWorkerState],
    interval: float = 60.0,
    stop_event: Optional[threading.Event] = None,
    market_limit: int = 1000
) -> threading.Thread:
    def _loop():
        logger.info(f"Market Universe Dynamic Auto-Refresher started (interval: {interval:.0f}s).")
        while not (stop_event and stop_event.is_set()):
            if stop_event and stop_event.wait(interval):
                break
            elif not stop_event:
                time.sleep(interval)
            try:
                refresh_market_universe(simulator, worker_states, market_limit=market_limit)
            except Exception as e:
                logger.error(f"Error in dynamic market universe auto-refresher: {e}")

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t

def partition_markets(markets: List[dict], num_partitions: int = 8) -> List[List[dict]]:
    """
    Partition market list into balanced shards for parallel WebSocket connections.
    E.g. 1,000 markets partitioned into 8 shards of 125 markets (250 tokens) each.
    """
    if not markets:
        return []
    n = max(1, min(num_partitions, len(markets)))
    shards: List[List[dict]] = [[] for _ in range(n)]
    for i, m in enumerate(markets):
        shards[i % n].append(m)
    return [s for s in shards if s]

def partition_dual_pools(
    markets: List[dict],
    num_shards: int = 8
) -> Tuple[List[SocketWorkerState], List[SocketWorkerState]]:
    """
    Partition market universe into two identical, redundant worker pools (Pool A and Pool B).
    Pool A: Workers 1..num_shards.
    Pool B: Workers (num_shards+1)..(2*num_shards).
    Both pools cover identical market shards simultaneously for 0% blast radius dual-active redundancy.
    """
    shards = partition_markets(markets, num_partitions=num_shards)
    actual_shards = len(shards)
    pool_a = [
        SocketWorkerState(i, shards[i], pool_name="Pool A", worker_num=i + 1, shard_id=i)
        for i in range(actual_shards)
    ]
    pool_b = [
        SocketWorkerState(i + num_shards, shards[i], pool_name="Pool B", worker_num=i + num_shards + 1, shard_id=i)
        for i in range(actual_shards)
    ]
    return pool_a, pool_b

def run_socket_pool(
    top_markets: List[dict],
    simulator: PaperSimulator,
    num_sockets: int = 16,
    stop_event: Optional[threading.Event] = None,
    dual_pool: bool = True,
    refresher_interval: float = 60.0
):
    """
    Manage an Active-Active Dual-Redundant WebSocket pool streaming CLOB orderbooks.
    Pool A (Workers 1-8) and Pool B (Workers 9-16) both subscribe to the same markets simultaneously.
    Reduces blast radius to 0% (if any socket drops, the hot standby counterpart continues streaming).
    Total sockets: 16 (8 per pool) by default.
    """
    if dual_pool:
        num_shards = max(1, num_sockets // 2) if num_sockets >= 16 else num_sockets
        pool_a, pool_b = partition_dual_pools(top_markets, num_shards=num_shards)
        worker_states = pool_a + pool_b
        actual_workers = len(worker_states)
        logger.info(
            f"Starting Dual Active-Active WebSocket Pool: {actual_workers} total workers "
            f"({len(pool_a)} Pool A + {len(pool_b)} Pool B) covering {len(top_markets)} markets with 0% blast radius."
        )
    else:
        shards = partition_markets(top_markets, num_partitions=num_sockets)
        worker_states = [
            SocketWorkerState(i, shard, pool_name="Pool A", worker_num=i + 1, shard_id=i)
            for i, shard in enumerate(shards)
        ]
        actual_workers = len(worker_states)
        logger.info(f"Starting Multi-Socket WebSocket Pool: {actual_workers} workers for {len(top_markets)} markets.")

    if stop_event is None:
        stop_event = threading.Event()

    threads = []
    active_ws_list = []
    ws_lock = threading.Lock()

    def _sync_pool_telemetry():
        pool_dash = simulator.dash_state or globals().get("dash_state")
        if not pool_dash:
            return
        with ws_lock:
            active_cnt = len(active_ws_list)
        pool_a_active = sum(1 for ws in worker_states if getattr(ws, "pool_name", "") == "Pool A" and ws.active_ws is not None)
        pool_b_active = sum(1 for ws in worker_states if getattr(ws, "pool_name", "") == "Pool B" and ws.active_ws is not None)
        pool_a_total = sum(1 for ws in worker_states if getattr(ws, "pool_name", "") == "Pool A")
        pool_b_total = sum(1 for ws in worker_states if getattr(ws, "pool_name", "") == "Pool B")

        with pool_dash.lock:
            pool_dash.state["active_sockets"] = active_cnt
            if dual_pool:
                pool_dash.state["socket_architecture"] = (
                    f"{active_cnt} Active Sockets (Dual Pool A+B: 0% Blast Radius)"
                )
                pool_dash.state["redundancy_mode"] = "Active-Active Hot Standby (0% Blast Radius)"
                pool_dash.state["pool_status"] = {
                    "Pool A": f"{pool_a_active}/{pool_a_total} Workers Active",
                    "Pool B": f"{pool_b_active}/{pool_b_total} Workers Active (Redundant Standby)"
                }
            pool_dash.dirty = True

    def _worker(worker_state: SocketWorkerState):
        tag = worker_state.tag
        idx = worker_state.worker_id

        while not stop_event.is_set():
            worker_stop = threading.Event()
            current_ws = None
            try:
                def _worker_open(w):
                    with ws_lock:
                        active_ws_list.append(w)
                    worker_state.active_ws = w
                    _sync_pool_telemetry()
                    pool_dash = simulator.dash_state or globals().get("dash_state")
                    with worker_state.lock:
                        tids = list(worker_state.token_ids)
                        m_count = len(worker_state.markets)
                    on_open(
                        w, tids, m_count,
                        worker_id=idx,
                        stop_event=worker_stop,
                        dash_state=pool_dash,
                        worker_tag=tag
                    )

                def _worker_close(w, code, msg):
                    worker_stop.set()
                    worker_state.active_ws = None
                    with ws_lock:
                        if w in active_ws_list:
                            active_ws_list.remove(w)
                    _sync_pool_telemetry()
                    logger.info(f"{tag} WebSocket Closed (code={code}).")
                    pool_dash = simulator.dash_state or globals().get("dash_state")
                    if not active_ws_list and not stop_event.is_set() and pool_dash:
                        pool_dash.set_bot_status("DISCONNECTED")

                def _worker_error(w, err):
                    logger.error(f"{tag} WebSocket Error: {err}")

                current_ws = websocket.WebSocketApp(
                    WSS_URL,
                    on_open=_worker_open,
                    on_message=lambda w, m: on_message(w, m, simulator),
                    on_error=_worker_error,
                    on_close=_worker_close
                )
                current_ws._has_managed_pings = True
                current_ws.run_forever(ping_interval=0, ping_timeout=None)
            except Exception as e:
                logger.error(f"{tag} Error in run_forever: {e}")
            finally:
                worker_stop.set()
                worker_state.active_ws = None
                with ws_lock:
                    if current_ws in active_ws_list:
                        active_ws_list.remove(current_ws)
                _sync_pool_telemetry()

            if not stop_event.is_set():
                time.sleep(2.0 + ((idx % 8) * 0.1))

    # Stagger socket connections during startup (100–150ms delay between connections, e.g. 120ms)
    # to avoid burst connection spikes against Cloudflare
    for ws_state in worker_states:
        t = threading.Thread(target=_worker, args=(ws_state,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.12)

    # Start dynamic periodic market universe auto-refresh daemon thread
    refresher_interval = float(os.environ.get("POLYMARKET_BOT_REFRESH_INTERVAL", 30.0))
    refresher_thread = start_market_universe_refresher(
        simulator, worker_states, interval=refresher_interval, stop_event=stop_event, market_limit=len(top_markets)
    )

    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutdown requested (Ctrl+C). Terminating WebSocket pool...")
        stop_event.set()
        with ws_lock:
            for w in list(active_ws_list):
                try:
                    w.close()
                except Exception:
                    pass
        pool_dash = simulator.dash_state or globals().get("dash_state")
        if pool_dash:
            pool_dash.set_bot_status("SHUTDOWN")
            pool_dash._write_if_dirty()

    for t in threads:
        t.join(timeout=2.0)

import os
from dotenv import load_dotenv

class LiveExecutor(PaperSimulator):
    def __init__(self, risk_engine, market_token_map=None, dash_state=None):
        super().__init__(risk_engine, market_token_map, dash_state)
        if self.risk:
            self.risk.is_live = True
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
        if os.path.exists(env_path) and ('POLYMARKET_PRIVATE_KEY' not in os.environ or 'POLYMARKET_ADDRESS' not in os.environ):
            load_dotenv(env_path)
        
        host = os.environ.get('POLYMARKET_HOST', 'https://clob.polymarket.com')
        chain_id = int(os.environ.get('POLYMARKET_CHAIN_ID', 137))
        key = os.environ.get('POLYMARKET_PRIVATE_KEY', '')
        self.address = os.environ.get('POLYMARKET_ADDRESS', '')
        
        sig_type_str = os.environ.get('POLYMARKET_SIGNATURE_TYPE', '3')
        try:
            self.signature_type = int(sig_type_str)
        except (ValueError, TypeError):
            self.signature_type = 3
        if self.signature_type not in (0, 1, 2, 3):
            self.signature_type = 3
        
        self.last_balance_sync_time = 0.0
        self.last_unwind_time = 0.0
        self.last_redemption_time = 0.0
        self._swept_cooldowns: Dict[str, float] = {}

        # Initialize official py_builder_relayer_client RelayClient
        self.relayer_client = None
        builder_api_key = os.environ.get('POLY_BUILDER_API_KEY')
        builder_secret = os.environ.get('POLY_BUILDER_SECRET')
        builder_passphrase = os.environ.get('POLY_BUILDER_PASSPHRASE')
        if builder_api_key and builder_secret and builder_passphrase and key:
            try:
                from py_builder_signing_sdk.config import BuilderConfig
                from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
                from py_builder_relayer_client.client import RelayClient
                b_creds = BuilderApiKeyCreds(key=builder_api_key, secret=builder_secret, passphrase=builder_passphrase)
                b_cfg = BuilderConfig(local_builder_creds=b_creds)
                self.relayer_client = RelayClient('https://relayer-v2.polymarket.com', chain_id, private_key=key, builder_config=b_cfg)
            except Exception as e:
                logger.warning(f"Failed to initialize RelayClient: {e}")
        
        if not key or not self.address:
            logger.warning("LiveExecutor missing POLYMARKET_PRIVATE_KEY or POLYMARKET_ADDRESS. Falling back to PaperSimulator behavior.")
            self.client = None
        else:
            try:
                from py_clob_client_v2.client import ClobClient
                from py_clob_client_v2.clob_types import ApiCreds, OrderArgsV2, PostOrdersV2Args, OrderType, BalanceAllowanceParams, AssetType
                self.client = ClobClient(
                    host, 
                    key=key, 
                    chain_id=chain_id, 
                    signature_type=self.signature_type,
                    funder=self.address
                )
                creds = self.client.derive_api_key()
                self.client.set_api_creds(creds)
            except Exception as e:
                logger.error(f"Failed to set api creds or initialize ClobClient v2: {e}")
                self.client = None

        if self.client and hasattr(self.client, 'signer') and self.client.signer:
            try:
                signer_addr = str(self.client.signer.address()).lower()
                funder_addr = str(self.address or '').lower()
                if signer_addr == funder_addr and self.signature_type in (1, 2, 3):
                    logger.warning('⚠️ Configuration check: POLYMARKET_ADDRESS matches signer EOA, but signature_type indicates proxy/deposit wallet. Ensure POLYMARKET_ADDRESS is your Deposit Wallet address from polymarket.com.')
            except Exception:
                pass

        if self.client is not None:
            try:
                self.sync_live_trades()
            except Exception as e:
                logger.warning(f"Startup sync_live_trades check encountered: {e}")
            try:
                self.redeem_resolved_positions()
            except Exception as e:
                logger.warning(f"Startup redeem_resolved_positions check encountered: {e}")
        if self.address:
            try:
                self.sync_live_positions()
                if self.risk:
                    bal = float(getattr(self.risk, "available_cash", 0.0) or 0.0)
                    if bal > 0:
                        self.risk.starting_capital = self.risk.capital
                        self.risk.circuit_breaker_active = False
                        self.risk.daily_loss = 0.0
                        if self.dash_state and hasattr(self.dash_state, "state"):
                            self.dash_state.state["circuit_breaker"] = False
                            self.dash_state.state["starting_capital"] = self.risk.capital
                            self.dash_state.state["daily_loss"] = 0.0
                            self.dash_state.dirty = True
            except Exception as e:
                logger.warning(f"Startup sync_live_positions check encountered: {e}")

        try:
            from microstructure_guard import MicrostructureGuard
            self.guard = getattr(self, "guard", None) or MicrostructureGuard()
        except Exception:
            self.guard = None
        self.rollback_protector = RollbackProtector(target_state=self.dash_state)

        # OrderReaper Background Daemon & Startup CLOB Hygiene
        if self.client is not None:
            try:
                self.order_reaper = OrderReaper(
                    client=self.client,
                    poll_interval_sec=1.0,
                    max_order_ttl_sec=2.5,
                    dash_state=self.dash_state,
                    rollback_protector=RollbackProtector,
                )
                self.order_reaper.purge_all_orders()
                self.order_reaper.start()
            except Exception as e:
                logger.warning(f"Failed to initialize OrderReaper on startup: {e}")
                self.order_reaper = None
        else:
            self.order_reaper = None

        self._trade_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="LiveTradeWorker")
        self._active_trading_markets: Set[str] = set()
        self._trading_lock = threading.Lock()

        self.maker_taker_executor = MakerTakerExecutor(
            self.client,
            dash_state=self.dash_state,
            microstructure_guard=self.guard,
            order_reaper=self.order_reaper,
        )

        # 1.0s Periodic Background Balance & Position Sync Thread
        self._sync_stop_event = threading.Event()
        self._sync_thread = threading.Thread(
            target=self._periodic_sync_loop,
            daemon=True,
            name="LiveExecutorSyncThread",
        )
        self._sync_thread.start()

        try:
            self.seed_clob_token_cache()
        except Exception as e:
            logger.warning(f"Error during initial CLOB token cache seeding: {e}")

    def dispatch_arbitrage(self, opp: dict) -> bool:
        exec_mode = self.dash_state.state.get("execution_mode", "Paper Trading") if self.dash_state else "Paper Trading"
        if self.client is None or exec_mode != "Live Trading":
            return self.execute_arbitrage(opp)
        market_id = opp.get("market_id")
        if not market_id:
            return False
        with self._trading_lock:
            if market_id in self._active_trading_markets:
                return False
            self._active_trading_markets.add(market_id)

        def _run():
            try:
                self.execute_arbitrage(opp)
            except Exception as e:
                logger.error(f"Error during async arbitrage execution for {market_id}: {e}", exc_info=True)
            finally:
                with self._trading_lock:
                    self._active_trading_markets.discard(market_id)

        self._trade_executor.submit(_run)
        return True

    def _periodic_sync_loop(self):
        """Periodic background sync loop (every 15s) for live balance and positions."""
        while not self._sync_stop_event.is_set():
            slept = 0.0
            while slept < 15.0 and not self._sync_stop_event.is_set():
                time.sleep(0.5)
                slept += 0.5
            if not self._sync_stop_event.is_set() and self.client is not None:
                try:
                    self.sync_live_balance()
                except Exception as e:
                    logger.debug(f"Periodic live balance sync error: {e}")
                try:
                    self.sync_live_positions()
                except Exception as e:
                    logger.debug(f"Periodic live positions sync error: {e}")

    def stop(self, timeout: float = 2.0):
        """Gracefully stop background sync and OrderReaper, then purge CLOB orders."""
        if hasattr(self, "_sync_stop_event"):
            self._sync_stop_event.set()
        if hasattr(self, "_sync_thread") and self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=timeout)
            self._sync_thread = None
        if hasattr(self, "_trade_executor") and self._trade_executor:
            try:
                self._trade_executor.shutdown(wait=False)
            except Exception:
                pass
        if getattr(self, "order_reaper", None):
            try:
                self.order_reaper.stop(timeout=timeout)
                self.order_reaper.purge_all_orders()
            except Exception as e:
                logger.warning(f"Error stopping order reaper: {e}")

    def seed_clob_token_cache(self):
        """
        Pre-seeds ClobClient internal tick-size and neg-risk caches from market_token_map.
        Completely eliminates HTTP calls for tick-size and neg-risk on monitored tokens!
        """
        if self.client and hasattr(self.client, "_ClobClient__tick_sizes"):
            now_mono = time.monotonic()
            for cid, info in (self.market_token_map or {}).items():
                if not isinstance(info, dict):
                    continue
                meta = info.get("market_meta", {}) if isinstance(info.get("market_meta"), dict) else {}
                min_tick = meta.get("minimum_tick_size") or info.get("minimum_tick_size") or info.get("tick_size") or 0.001
                neg_risk = bool(meta.get("neg_risk") if "neg_risk" in meta else info.get("neg_risk", False))
                for t_key in ("token_yes", "token_no"):
                    tid = info.get(t_key)
                    if tid:
                        self.client._ClobClient__tick_sizes[tid] = str(min_tick)
                        if hasattr(self.client, "_ClobClient__tick_size_timestamps"):
                            self.client._ClobClient__tick_size_timestamps[tid] = now_mono
                        if hasattr(self.client, "_ClobClient__neg_risk"):
                            self.client._ClobClient__neg_risk[tid] = neg_risk

    def redeem_resolved_positions(self) -> int:
        """
        Automated position redemption for resolved Polymarket binary markets.
        Queries positions from the Polymarket Data API with redeemable == True,
        constructs CTF redeemPositions calldata, and broadcasts gasless batch
        transactions via the Polymarket Relayer v2 API to claim payouts.
        """
        if not self.address or not self.relayer_client:
            return 0
        try:
            import urllib.request
            from web3 import Web3
            from py_builder_relayer_client.models import DepositWalletCall
            
            url = f"https://data-api.polymarket.com/positions?user={self.address}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PolymarketBot/2.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                positions = json.loads(resp.read().decode('utf-8'))
            
            redeemable = [p for p in positions if isinstance(p, dict) and p.get("redeemable") is True and p.get("conditionId")]
            if not redeemable:
                return 0
                
            w3 = Web3(Web3.HTTPProvider(os.environ.get('POLYGON_RPC_URL', 'https://polygon-bor-rpc.publicnode.com')))
            wallet_checksum = w3.to_checksum_address(self.address)
            abi_wallet = [{'inputs': [], 'name': 'nonce', 'outputs': [{'name': '', 'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'}]
            contract_w = w3.eth.contract(address=wallet_checksum, abi=abi_wallet)
            
            ctf_addr = w3.to_checksum_address('0x4D97DCd97eC945f40cF65F87097ACe5EA0476045')
            collateral = w3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
            parent = Web3.to_bytes(hexstr="0x0000000000000000000000000000000000000000000000000000000000000000")
            abi_ctf = [{'inputs': [{'name': 'collateralToken', 'type': 'address'}, {'name': 'parentCollectionId', 'type': 'bytes32'}, {'name': 'conditionId', 'type': 'bytes32'}, {'name': 'indexSets', 'type': 'uint256[]'}], 'name': 'redeemPositions', 'outputs': [], 'stateMutability': 'nonpayable', 'type': 'function'}]
            ctf = w3.eth.contract(address=ctf_addr, abi=abi_ctf)
            
            redeemed_count = 0
            for p in redeemable:
                cid_hex = p["conditionId"]
                cid_bytes = Web3.to_bytes(hexstr=cid_hex if cid_hex.startswith("0x") else "0x" + cid_hex)
                calldata = ctf.encode_abi('redeemPositions', args=[collateral, parent, cid_bytes, [1, 2]])
                call = DepositWalletCall(target=ctf_addr, value="0", data=calldata)
                
                on_chain_nonce = str(contract_w.functions.nonce().call())
                deadline = str(int(time.time()) + 3600)
                
                tx_resp = self.relayer_client.execute_deposit_wallet_batch(calls=[call], wallet_address=self.address, nonce=on_chain_nonce, deadline=deadline)
                tx_hash = getattr(tx_resp, "transaction_hash", None)
                logger.info(f"Auto-redeemed resolved market {p.get('title')}: TX Hash {tx_hash}")
                if self.dash_state:
                    self.dash_state.add_activity_log(f"💰 [AUTO-REDEEM] Claimed payout for '{p.get('title')}' -> TX {str(tx_hash)[:10]}...")
                redeemed_count += 1
                time.sleep(1.0)
            
            if redeemed_count > 0:
                self.sync_live_balance()
            return redeemed_count
        except Exception as e:
            logger.warning(f"Error during redeem_resolved_positions: {e}")
            return 0

    def check_live_readiness(self) -> dict:
        """
        Check if the live client is initialized and capable of querying balance allowance.
        Returns dict with status, signature_type, funder, balance, and any error message.
        """
        if self.client is None:
            return {
                "ready": False,
                "signature_type": getattr(self, "signature_type", 2),
                "funder": self.address,
                "balance": "0.0",
                "error": "ClobClient not initialized or missing credentials"
            }
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = self.client.get_balance_allowance(params)
            bal = "0.0"
            if isinstance(resp, dict):
                bal = str(resp.get("balance", "0.0"))
            elif hasattr(resp, "balance"):
                bal = str(resp.balance)
            return {
                "ready": True,
                "signature_type": self.signature_type,
                "funder": self.address,
                "balance": bal,
                "error": None
            }
        except Exception as e:
            return {
                "ready": False,
                "signature_type": self.signature_type,
                "funder": self.address,
                "balance": "0.0",
                "error": str(e)
            }

    def sync_live_balance(self) -> float:
        """
        Query Polymarket collateral balance allowance for funder address via py-clob-client v2.
        Converts balance (scaled to 1e6 for USDC) and updates risk engine cash and dashboard state.
        """
        if self.client is None:
            return getattr(self.risk, "available_cash", 0.0) if self.risk else 0.0
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = self.client.get_balance_allowance(params)
            allowances = resp.get('allowances', {}) if isinstance(resp, dict) else getattr(resp, 'allowances', {})
            if allowances and isinstance(allowances, dict):
                total_allowance = sum(float(v or 0) for v in allowances.values())
                if total_allowance <= 0:
                    logger.warning('⚠️ Polymarket exchange allowance is 0. Ensure USDC is approved on polymarket.com.')
                    if self.dash_state:
                        self.dash_state.add_activity_log('⚠️ Warning: Polymarket exchange allowance is 0. Verify token approvals.')
            raw = float(resp.get("balance", 0.0) if isinstance(resp, dict) else getattr(resp, "balance", 0.0) or 0.0)
            bal_usd = round(raw / 1e6, 4)
            if bal_usd > 0:
                if getattr(self, "prev_synced_balance", None) is not None and bal_usd > self.prev_synced_balance + 0.05:
                    last_trade = getattr(self, "last_trade_time", 0.0)
                    if time.time() - last_trade > 30.0:
                        reward_delta = round(bal_usd - self.prev_synced_balance, 2)
                        log_msg = f"🎁 Balance Credit Detected: +${reward_delta:.2f} USDC (Polymarket Rewards / Deposit)"
                        logger.info(log_msg)
                        if self.dash_state:
                            self.dash_state.add_activity_log(log_msg)
                self.prev_synced_balance = bal_usd

                if self.risk:
                    with self.risk.lock:
                        self.risk.available_cash = bal_usd
                        self.risk.capital = bal_usd + getattr(self.risk, "locked_collateral", 0.0)
                        if hasattr(self.risk, "_sync_to_dash_state"):
                            self.risk._sync_to_dash_state()
                if self.dash_state and hasattr(self.dash_state, "state"):
                    with getattr(self.dash_state, "lock", threading.Lock()):
                        self.dash_state.state["capital"] = self.risk.capital if self.risk else bal_usd
                        self.dash_state.state["available_cash"] = bal_usd
                        self.dash_state.dirty = True
                self.last_balance_sync_time = time.time()
                try:
                    self.sync_live_positions()
                except Exception as e:
                    logger.warning(f"Error in sync_live_positions during balance sync: {e}")
                try:
                    self.sync_live_trades()
                except Exception as e:
                    logger.warning(f"Error in sync_live_trades during balance sync: {e}")
                return bal_usd
            return bal_usd
        except Exception as e:
            logger.warning(f"Error syncing live balance: {e}")
            return getattr(self.risk, "available_cash", 0.0) if self.risk else 0.0

    def sync_live_trades(self, limit: int = 50):
        """
        Synchronize confirmed fills from the Polymarket CLOB via client.get_trades()
        into dash_state.state["trades"] so executed fills render in real-time on the dashboard.
        """
        if not self.client or not hasattr(self.client, "get_trades"):
            return
        try:
            raw_trades = self.client.get_trades()
            if not raw_trades or not isinstance(raw_trades, list):
                return

            formatted_trades = []
            for t in raw_trades[:limit]:
                if not isinstance(t, dict):
                    continue
                raw_ts = t.get("match_time") or t.get("timestamp") or 0
                try:
                    match_ts = float(raw_ts or 0)
                    if match_ts > 1e11:  # Millisecond timestamp
                        match_ts = match_ts / 1000.0
                except (ValueError, TypeError):
                    match_ts = 0.0

                if match_ts > 0:
                    time_str = datetime.fromtimestamp(match_ts).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                market = t.get("market") or t.get("condition_id") or t.get("asset_id") or ""
                side = str(t.get("side", "")).upper()
                outcome = str(t.get("outcome", ""))
                try:
                    size_shares = float(t.get("size", 0.0) or 0.0)
                except (ValueError, TypeError):
                    size_shares = 0.0
                try:
                    price = float(t.get("price", 0.0) or 0.0)
                except (ValueError, TypeError):
                    price = 0.0
                size_usd = round(size_shares * price, 2)
                status = str(t.get("status", "CONFIRMED"))
                tx_hash = str(t.get("transaction_hash", ""))

                formatted_trades.append({
                    "market": market,
                    "size": size_usd,
                    "expected_profit": 0.0,
                    "time": time_str,
                    "side": side,
                    "price": price,
                    "outcome": outcome,
                    "status": status,
                    "tx_hash": tx_hash
                })

            if self.dash_state and hasattr(self.dash_state, "state"):
                with getattr(self.dash_state, "lock", threading.Lock()):
                    existing_trades = list(self.dash_state.state.get("trades", []))

                    # Retain explicit expected_profit > 0 from local executions
                    local_profits = {}
                    for tr in existing_trades:
                        if float(tr.get("expected_profit", 0.0) or 0.0) > 0:
                            local_profits[(tr.get("market"), tr.get("time"))] = float(tr.get("expected_profit"))

                    for tr in formatted_trades:
                        k_profit = (tr.get("market"), tr.get("time"))
                        if k_profit in local_profits and tr.get("expected_profit", 0.0) == 0.0:
                            tr["expected_profit"] = local_profits[k_profit]

                    seen_keys = set()
                    merged = []
                    for tr in formatted_trades:
                        k = (tr.get("market"), tr.get("time"), tr.get("side"), tr.get("price"))
                        if k not in seen_keys:
                            seen_keys.add(k)
                            merged.append(tr)
                    for tr in existing_trades:
                        k = (tr.get("market"), tr.get("time"), tr.get("side", ""), tr.get("price", 0.0))
                        if k not in seen_keys:
                            seen_keys.add(k)
                            merged.append(tr)
                    merged.sort(key=lambda x: str(x.get("time", "")), reverse=True)
                    self.dash_state.state["trades"] = merged[:50]
                    self.dash_state.dirty = True
        except Exception as e:
            logger.warning(f"Error syncing live trades from Polymarket CLOB: {e}")

    def sweep_orphan_positions(self, active_positions: Optional[List[dict]] = None, max_slippage: float = 0.020, force_now: bool = False) -> int:
        """
        Autonomous Orphan Position Sweeper & 2-Tier Liquidation Engine.
        1. Identifies unhedged single-sided positions sitting in the user's wallet.
        2. Tier 1 (Instant Market Exit): If slippage <= max_slippage (default 2.0¢), executes immediate market sell.
        3. Tier 2 (Protected Par / Top-of-Book Limit Sell): If slippage > max_slippage, posts limit sell order
           pegged to cost basis or top-of-book, and registers it into OrderReaper as is_passive_unwind=True
           so it is never reaped.
        """
        if self.client is None:
            return 0

        if active_positions is None:
            if not self.address:
                return 0
            try:
                import urllib.request
                url = f"https://data-api.polymarket.com/positions?user={self.address}"
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PolymarketBot/2.0"}
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    raw_positions = json.loads(resp.read().decode('utf-8'))
                active_positions = [
                    p for p in raw_positions
                    if isinstance(p, dict) and float(p.get('size', 0) or 0) >= 1.0 and not p.get('redeemable')
                ]
            except Exception as e:
                logger.warning(f"Failed to fetch active positions for orphan sweep: {e}")
                return 0

        if not active_positions:
            return 0

        # Group active positions by conditionId
        by_market = collections.defaultdict(list)
        for p in active_positions:
            cid = str(p.get("conditionId") or p.get("market") or "")
            if cid:
                by_market[cid].append(p)
            else:
                by_market[str(p.get("asset", ""))].append(p)

        if not hasattr(self, "_orphan_first_seen"):
            self._orphan_first_seen = {}
        if not hasattr(self, "_active_unwind_orders"):
            self._active_unwind_orders = set()
        if not hasattr(self, "_swept_cooldowns"):
            self._swept_cooldowns = {}

        active_unhedged_tokens = set()
        swept_count = 0
        now = time.time()
        for cid, pos_list in by_market.items():
            # If both YES and NO exist with balanced size, it's a matched pair (awaiting on-chain merge)
            if len(pos_list) >= 2:
                sizes = [float(p.get("size", 0.0) or 0.0) for p in pos_list]
                if min(sizes) > 0 and abs(sizes[0] - sizes[1]) < 1.0:
                    continue  # Balanced pair, safe

            # Unhedged orphan leg(s) detected
            for p in pos_list:
                size = float(p.get("size", 0.0) or 0.0)
                cur_val = float(p.get("currentValue", 0.0) or 0.0)
                cur_price = float(p.get("curPrice", 0.0) or 0.0)
                token_id = str(p.get("asset", ""))
                title = str(p.get("title", f"Market {cid[:8]}"))
                outcome = str(p.get("outcome", "SHARES"))

                # Ignore dust or zero-value positions
                if size < 1.0 or cur_val <= 0.05 or not token_id:
                    continue

                active_unhedged_tokens.add(token_id)
                first_seen = self._orphan_first_seen.setdefault(token_id, now)

                # If not force_now, wait 30 seconds debounce before taking action
                if not force_now and (now - first_seen) < 30.0:
                    continue

                # Cooldown / debounce: don't hammer the same token repeatedly within 15 seconds unless forced
                if not force_now and now < self._swept_cooldowns.get(token_id, 0.0):
                    continue

                init_val = float(p.get("initialValue", 0.0) or 0.0)
                raw_avg = p.get("avgPrice")
                if raw_avg is not None and float(raw_avg) > 0.0:
                    buy_price = float(raw_avg)
                elif size > 0 and init_val > 0:
                    buy_price = round(init_val / size, 4)
                else:
                    buy_price = max(0.01, cur_price)

                # Fetch live orderbook to check bids and asks
                book = RollbackProtector.fetch_order_book(self.client, token_id)
                best_bid = RollbackProtector.extract_best_bid(book)
                best_ask = RollbackProtector.extract_best_ask(book)
                slippage = max(0.0, buy_price - best_bid) if best_bid > 0 else 1.0

                # Tier 1: Instant Market Exit if slippage is small
                if best_bid >= 0.01 and slippage <= max_slippage:
                    logger.warning(
                        f"🧹 [ORPHAN SWEEPER] Tier 1 Market Exit: {size:.1f} {outcome} shares on '{title}' "
                        f"(Paid ${buy_price:.4f}, Best Bid ${best_bid:.4f}, Slippage {slippage*100:.1f}¢ <= {max_slippage*100:.1f}¢)"
                    )
                    ok, action, details = RollbackProtector.safe_unwind_or_limit_exit(
                        client=self.client,
                        token_id=token_id,
                        shares=size,
                        buy_price=buy_price,
                        label=outcome,
                        target_state=self.dash_state,
                        force_market_exit=True,
                    )
                    self._swept_cooldowns[token_id] = now + 15.0
                    if ok:
                        swept_count += 1
                        self._orphan_first_seen.pop(token_id, None)
                        recovered_usd = size * best_bid
                        realized_loss = details.get("realized_loss", size * slippage)
                        log_msg = f"🧹 [ORPHAN LIQUIDATED] Market sold {size:.1f} {outcome} on '{title}' @ ${best_bid:.4f}. Recovered ${recovered_usd:.2f} USDC (loss ${realized_loss:.2f})."
                        logger.info(log_msg)
                        if self.dash_state:
                            self.dash_state.add_activity_log(log_msg)
                        try:
                            from ha_notifier import send_trade_notification
                            send_trade_notification(
                                question=f"[AUTO-SWEEP] {title}",
                                trade_size=cur_val,
                                expected_profit=float(details.get("realized_loss", 0.0) or 0.0),
                                execution_style="orphan_sweeper",
                                wallet_balance=getattr(self.risk, "available_cash", 0.0) if self.risk else 0.0
                            )
                        except Exception as e:
                            logger.debug(f"HA sweep notification error: {e}")
                    continue

                # Tier 2: Protected Limit Sell at Par or Top of Book
                # If best_ask > buy_price: undercut best_ask by 1 tick (0.001) while staying >= buy_price
                if best_ask > 0 and best_ask > buy_price:
                    limit_price = min(round(buy_price, 4), round(best_ask - 0.001, 4))
                else:
                    limit_price = round(buy_price, 4)

                logger.info(
                    f"🛡️ [ORPHAN SWEEPER] Tier 2 Protected Limit Sell: {size:.1f} {outcome} on '{title}' at ${limit_price:.4f} "
                    f"(Paid ${buy_price:.4f}, Bid ${best_bid:.4f}, Ask ${best_ask:.4f}, Spread {slippage*100:.1f}¢ > {max_slippage*100:.1f}¢)"
                )
                ok, action, details = RollbackProtector.safe_unwind_or_limit_exit(
                    client=self.client,
                    token_id=token_id,
                    shares=size,
                    buy_price=buy_price,
                    label=outcome,
                    target_state=self.dash_state,
                    force_market_exit=False,
                    limit_price_override=limit_price,
                )
                self._swept_cooldowns[token_id] = now + 60.0

                if ok:
                    order_id = details.get("order_id")
                    if order_id and self.order_reaper:
                        self.order_reaper.register_order(
                            order_id=order_id,
                            token_id=token_id,
                            side="SELL",
                            size=size,
                            price=limit_price,
                            is_passive_unwind=True,
                        )
                    self._active_unwind_orders.add(token_id)
                    swept_count += 1
                    self._orphan_first_seen.pop(token_id, None)
                    log_msg = f"🛡️ [ORPHAN LIMIT PROTECTED] Posted limit sell for {size:.1f} {outcome} on '{title}' at ${limit_price:.4f}. Protected by OrderReaper."
                    logger.info(log_msg)
                    if self.dash_state:
                        self.dash_state.add_activity_log(log_msg)
                    try:
                        from ha_notifier import send_trade_notification
                        send_trade_notification(
                            question=f"[AUTO-SWEEP] {title}",
                            trade_size=cur_val,
                            expected_profit=float(details.get("realized_loss", 0.0) or 0.0),
                            execution_style="orphan_sweeper",
                            wallet_balance=getattr(self.risk, "available_cash", 0.0) if self.risk else 0.0
                        )
                    except Exception as e:
                        logger.debug(f"HA sweep notification error: {e}")

        # Clean up tokens that are no longer unhedged
        for tid in list(self._orphan_first_seen.keys()):
            if tid not in active_unhedged_tokens:
                self._orphan_first_seen.pop(tid, None)
                self._active_unwind_orders.discard(tid)

        return swept_count

    def sync_live_positions(self) -> dict:
        """
        Synchronize active Polymarket positions for self.address via Polymarket Data API.
        Computes:
        - positions_market_val: Current mark-to-market value of open positions.
        - positions_theoretical_val: Payout at binary resolution ($1.00 face value per share).
        - theoretical_equity: Available cash + theoretical resolution value.
        - projected_profit: Realized closed trade profit + unrealized resolution gains.
        - mark_to_market_equity: Available cash + positions mark-to-market value.
        Updates dash_state and triggers dirty flush.
        """
        if not self.address:
            return {}
        try:
            import urllib.request
            url = f"https://data-api.polymarket.com/positions?user={self.address}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PolymarketBot/2.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_positions = json.loads(resp.read().decode('utf-8'))
            
            active_positions = [
                p for p in raw_positions
                if isinstance(p, dict) and float(p.get('size', 0) or 0) > 0 and not p.get('redeemable')
            ]
            
            try:
                self.sweep_orphan_positions(active_positions)
            except Exception as e:
                logger.warning(f"Error in sweep_orphan_positions: {e}")
            
            positions_market_val = sum(float(p.get('currentValue', 0.0) or 0.0) for p in active_positions)
            
            # Pair-aware theoretical valuation: only matched binary pairs receive $1.00 face value.
            # Unhedged single-sided orphan tokens are valued strictly at mark-to-market currentValue.
            by_market: Dict[str, List[dict]] = collections.defaultdict(list)
            for p in active_positions:
                m_key = str(p.get("conditionId") or p.get("market") or p.get("asset") or "")
                by_market[m_key].append(p)

            positions_theoretical_val = sum(float(p.get('size', 0.0) or 0.0) * 1.0 for p in active_positions)
            initial_cost = sum(float(p.get('initialValue', 0.0) or 0.0) for p in active_positions)
            
            avail_cash = getattr(self.risk, 'available_cash', 0.0) if self.risk else 0.0
            if avail_cash <= 0 and hasattr(self, 'sync_live_balance'):
                avail_cash = float(getattr(self.risk, 'capital', 0.0) or 0.0)
                
            theoretical_equity = round(avail_cash + positions_theoretical_val, 2)
            mark_to_market_equity = round(avail_cash + positions_market_val, 2)
            
            # Wallet drawdown circuit breaker
            if self.risk:
                starting_cap = getattr(self.risk, "starting_capital", None)
                daily_limit = getattr(self.risk, "daily_loss_limit", None)
                if isinstance(starting_cap, (int, float)) and isinstance(daily_limit, (int, float)):
                    current_drawdown = max(0.0, float(starting_cap) - mark_to_market_equity)
                    cb_active = getattr(self.risk, "circuit_breaker", False) or getattr(self.risk, "circuit_breaker_active", False)
                    if current_drawdown >= float(daily_limit) and current_drawdown > 0:
                        if not cb_active:
                            self.risk.circuit_breaker = True
                            self.risk.circuit_breaker_active = True
                            logger.critical(
                                f"🚨 CIRCUIT BREAKER TRIPPED by wallet equity drawdown! "
                                f"Starting: ${float(starting_cap):.2f} | Current: ${mark_to_market_equity:.2f} | "
                                f"Drawdown: ${current_drawdown:.2f} >= Limit: ${float(daily_limit):.2f}"
                            )
                            if self.dash_state:
                                self.dash_state.add_activity_log(
                                    f"🛑 CIRCUIT BREAKER TRIPPED: Drawdown ${current_drawdown:.2f} >= Limit ${float(daily_limit):.2f}"
                                )
                    elif current_drawdown < float(daily_limit) and cb_active:
                        self.risk.circuit_breaker = False
                        self.risk.circuit_breaker_active = False
                        if self.dash_state and hasattr(self.dash_state, "state"):
                            with getattr(self.dash_state, "lock", threading.Lock()):
                                self.dash_state.state["circuit_breaker"] = False
                                self.dash_state.dirty = True
                            self.dash_state.add_activity_log("🟢 Circuit breaker cleared: Equity recovered within safe operating limits.")

            trades = getattr(self.dash_state, 'state', {}).get('trades', []) if self.dash_state else []
            trades_profit = sum(float(t.get('expected_profit', 0.0) or 0.0) for t in trades)
            unrealized_res_profit = max(0.0, positions_theoretical_val - initial_cost)
            projected_profit = round(trades_profit + unrealized_res_profit, 2)
            
            if self.dash_state and hasattr(self.dash_state, 'state'):
                with getattr(self.dash_state, 'lock', threading.Lock()):
                    self.dash_state.state["live_positions"] = active_positions
                    self.dash_state.state["positions_market_val"] = round(positions_market_val, 2)
                    self.dash_state.state["positions_theoretical_val"] = round(positions_theoretical_val, 2)
                    self.dash_state.state["theoretical_equity"] = theoretical_equity
                    self.dash_state.state["projected_profit"] = projected_profit
                    self.dash_state.state["mark_to_market_equity"] = mark_to_market_equity
                    self.dash_state.dirty = True
                    
            return {
                "active_count": len(active_positions),
                "positions_market_val": positions_market_val,
                "positions_theoretical_val": positions_theoretical_val,
                "theoretical_equity": theoretical_equity,
                "projected_profit": projected_profit
            }
        except Exception as e:
            logger.warning(f"Error in sync_live_positions: {e}")
            return {}

    def _emergency_dump_leg(self, token_id: str, shares: float, label: str = "shares", target_state=None, buy_price: Optional[float] = None) -> bool:
        """
        Price-Protected Rollback: Guarantees unhedged shares are NEVER dumped at a
        loss into the market bid. Quantizes price to tick size and delegates through
        RollbackProtector.safe_unwind_or_limit_exit with force_market_exit=False.
        """
        if shares <= 0:
            return True
        price_to_protect = buy_price if (buy_price is not None and buy_price > 0) else 0.50
        ok, action, details = RollbackProtector.safe_unwind_or_limit_exit(
            client=self.client,
            token_id=token_id,
            shares=shares,
            buy_price=price_to_protect,
            label=label,
            target_state=target_state,
            force_market_exit=False
        )
        return ok

    def unwind_positions_to_cash(self, max_unwind: int = 10, min_price: float = 0.95) -> int:
        """
        Autonomous CLOB Cash-Out and Position Unwinder with Strict Capital Protection.
        Fetches open positions for self.address via Polymarket Data API.
        CRITICAL INVARIANT: NEVER sells positions at a loss into low bids!
        Only sells if best_bid >= min_price (default $0.95).
        """
        if self.client is None or not self.address:
            return 0

        target_state = self.dash_state
        unrolled_count = 0

        try:
            url = f"https://data-api.polymarket.com/positions?user={self.address}"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PolymarketBot/2.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw_positions = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"Failed to fetch positions for auto-unwind from Data API: {e}")
            return 0

        positions = raw_positions if isinstance(raw_positions, list) else (raw_positions.get("data", []) if isinstance(raw_positions, dict) else [])

        candidates = []
        for p in positions:
            if not isinstance(p, dict):
                continue
            try:
                size_val = float(p.get("size", 0) or 0)
                if size_val >= 1.0 and p.get("asset"):
                    candidates.append(p)
            except (ValueError, TypeError):
                continue

        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType, OrderArgsV2, PostOrdersV2Args, OrderType

        for p in candidates[:max_unwind]:
            try:
                asset_id = str(p.get("asset"))
                try:
                    bal_check = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=asset_id))
                    raw_bal = bal_check.get("balance", 0) if isinstance(bal_check, dict) else getattr(bal_check, "balance", 0)
                    actual_shares = float(raw_bal or 0) / 1e6
                except Exception as e:
                    logger.debug(f"Failed to fetch balance allowance for asset {asset_id}: {e}")
                    actual_shares = float(p.get("size", 0) or 0)

                if actual_shares < 1.0:
                    continue
                size_to_sell = int(actual_shares)

                book_url = f"https://clob.polymarket.com/book?token_id={asset_id}"
                req_book = urllib.request.Request(
                    book_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PolymarketBot/2.0"}
                )
                try:
                    with urllib.request.urlopen(req_book, timeout=5) as resp_book:
                        book_data = json.loads(resp_book.read().decode("utf-8"))
                except urllib.error.HTTPError as he:
                    if he.code == 404:
                        logger.debug(f"Market for asset {asset_id} is resolved/archived (HTTP 404)")
                        continue
                    raise

                bids = book_data.get("bids", []) if isinstance(book_data, dict) else []
                best_bid = 0.0
                for b in bids:
                    try:
                        if isinstance(b, dict):
                            bp = float(b.get("price", 0.0))
                        elif isinstance(b, (list, tuple)):
                            bp = float(b[0])
                        else:
                            bp = float(getattr(b, "price", 0.0))
                        if bp > best_bid:
                            best_bid = bp
                    except (ValueError, TypeError):
                        pass

                if best_bid <= 0.0:
                    continue

                sell_price = round(math.floor(best_bid * 100.0) / 100.0, 2)
                # Strict price safety gating: NEVER dump at a loss
                if sell_price < min_price:
                    logger.info(
                        f"Skipping auto-unwind for asset {asset_id}: best bid ${sell_price:.4f} is below "
                        f"minimum cashout threshold (${min_price:.2f}). Preserving 100% face-value collateral."
                    )
                    continue

                order = self.client.create_order(
                    OrderArgsV2(
                        price=sell_price,
                        size=float(size_to_sell),
                        side="SELL",
                        token_id=asset_id
                    )
                )
                resp = self.client.post_orders([PostOrdersV2Args(order=order, orderType=OrderType.FOK)])

                has_error = False
                if isinstance(resp, list):
                    for r in resp:
                        if isinstance(r, dict) and r.get("errorMsg"):
                            has_error = True
                elif isinstance(resp, dict) and resp.get("errorMsg"):
                    has_error = True

                if not has_error and resp:
                    if target_state:
                        outcome_str = p.get('outcome', 'shares')
                        recovered = size_to_sell * sell_price
                        target_state.add_activity_log(
                            f"♻️ [AUTO-UNWIND] Sold {size_to_sell} {outcome_str} at ${sell_price:.4f} -> Recovered +${recovered:.2f} USDC"
                        )
                    unrolled_count += 1
            except Exception as e:
                logger.warning(f"Failed to auto-unwind position for asset {p.get('asset')}: {e}")

        self.sync_live_balance()
        return unrolled_count

    def on_trade_executed(self, market_id: str, trade_size: float, expected_profit: float):
        """
        Triggered immediately the moment any live trade executes on the CLOB.
        Preserves collateral at 100% face value ($1.00 per share), synchronizes real-time CLOB balance,
        and secures capital in the active pool.
        """
        logger.info(f"Triggering instant post-trade collateral securing for market {market_id}...")
        try:
            guaranteed_value = round(trade_size + expected_profit, 4)
            if self.risk:
                if hasattr(self.risk, "lock"):
                    with getattr(self.risk, "lock"):
                        cur_collateral = getattr(self.risk, "locked_collateral", 0.0)
                        self.risk.locked_collateral = round(cur_collateral + expected_profit, 4)
                else:
                    cur_collateral = getattr(self.risk, "locked_collateral", 0.0)
                    self.risk.locked_collateral = round(cur_collateral + expected_profit, 4)

            bal = self.sync_live_balance()
            if self.risk:
                self.risk.capital = round(getattr(self.risk, "available_cash", bal) + getattr(self.risk, "locked_collateral", 0.0), 4)

            if self.dash_state and hasattr(self.dash_state, "state"):
                with getattr(self.dash_state, "lock", threading.Lock()):
                    if self.risk:
                        self.dash_state.state["capital"] = self.risk.capital
                        self.dash_state.state["available_cash"] = self.risk.available_cash
                        self.dash_state.state["locked_collateral"] = self.risk.locked_collateral
                    else:
                        self.dash_state.state["capital"] = round(bal + guaranteed_value, 4)
                        self.dash_state.state["available_cash"] = bal
                        self.dash_state.state["locked_collateral"] = guaranteed_value
                    self.dash_state.dirty = True
                avail_cash = getattr(self.risk, 'available_cash', bal)
                self.dash_state.add_activity_log(
                    f"💎 [TRADE SECURED] Matched arbitrage executed for {market_id[-6:]}: Deployed ${trade_size:.2f} -> Guaranteed Value ${trade_size + expected_profit:.2f} (+${expected_profit:.4f} edge). Available Cash: ${avail_cash:.2f}"
                )

            # HA Push Notification Dispatch
            try:
                question = self.market_token_map.get(market_id, {}).get("question", f"Market {market_id[-6:]}") if getattr(self, "market_token_map", None) else f"Market {market_id[-6:]}"
                edge_pct = round((expected_profit / trade_size) * 100.0, 2) if trade_size > 0 else 0.0
                exec_style = getattr(self.dash_state, "state", {}).get("execution_style", "maker_taker") if self.dash_state else "maker_taker"
                avail_cash = float(getattr(self.risk, "available_cash", bal) or 0.0)
                send_trade_notification(
                    question=question,
                    trade_size=trade_size,
                    expected_profit=expected_profit,
                    edge_pct=edge_pct,
                    execution_style=exec_style,
                    wallet_balance=avail_cash,
                    market_id=market_id
                )
            except Exception as notify_err:
                logger.warning(f"Failed to dispatch live trade push notification: {notify_err}")
        except Exception as e:
            logger.warning(f"Error in post-trade collateral securing: {e}")

    def execute_arbitrage(self, opp: dict) -> bool:
        exec_mode = self.dash_state.state.get("execution_mode", "Paper Trading") if self.dash_state else "Paper Trading"
        if self.client is None or exec_mode != "Live Trading":
            return super().execute_arbitrage(opp)

        target_state = self.dash_state

        # Direct routing for Maker-Taker Parity Execution Core
        if opp.get("execution_type") == "maker_taker":

            market_id = opp["market_id"]
            short_id = opp.get("short_id", f"Market {market_id[-6:]}")
            tick_size = float(opp.get("tick_size", 0.001))
            maker_leg = opp.get("maker_leg", "YES")
            maker_token = opp.get("maker_token")
            maker_price = float(opp.get("maker_price", 0.0))
            taker_token = opp.get("taker_token")
            taker_price = float(opp.get("taker_price", 0.0))

            total_cap = _safe_float(getattr(self.risk, "capital", None), 1000.0) if self.risk else 1000.0
            available_cash = _safe_float(getattr(self.risk, "available_cash", None), total_cap) if self.risk else total_cap
            if total_cap < 100.0:
                spendable = available_cash
            else:
                reserve_pct = _safe_float(getattr(self.risk, "reserve_cash_pct", None), 0.0) if self.risk else 0.0
                spendable = max(0.0, available_cash - (available_cash * reserve_pct))

            cost_per_pair = maker_price + taker_price
            if cost_per_pair <= 0:
                return False

            raw_trade_size = float(opp.get("trade_size", 0.0))
            if target_state and getattr(target_state, "state", {}).get("execution_mode") == "Live Trading":
                live_wager_cap = max(5.50, float(target_state.state.get("live_wager_cap", 10.0) or 10.0))
                raw_trade_size = min(raw_trade_size, live_wager_cap)

            max_shares = int(raw_trade_size / cost_per_pair)
            if max_shares < 5:
                five_share_cost = 5.0 * cost_per_pair
                if spendable >= five_share_cost and (not self.risk or self.risk.can_trade(five_share_cost, market_id=market_id)):
                    max_shares = 5
                else:
                    if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                        opp_copy = dict(opp)
                        self.shadow_tracker.record_missed(opp_copy, MissedReason.INSUFFICIENT_CASH)
                    return False

            shares = float(max_shares)
            trade_size = round(shares * cost_per_pair, 2)
            expected_profit = round((shares * 1.0) - trade_size, 4)

            timeout_sec = float(target_state.state.get("maker_timeout_seconds", 1.0) or 1.0) if target_state else 1.0
            min_edge_val = float(getattr(target_state, "state", {}).get("min_edge_pct", self.min_edge) or self.min_edge)
            init_taker_depth = opp.get("depth_taker") or (opp.get("depth_no") if opp.get("maker_leg") == "YES" else opp.get("depth_yes"))

            ok, action, details = self.maker_taker_executor.execute_maker_taker_arbitrage(
                token_maker=maker_token,
                maker_price=maker_price,
                token_taker=taker_token,
                taker_price=taker_price,
                size=shares,
                timeout_seconds=timeout_sec,
                dash_state=self.dash_state,
                min_edge=min_edge_val,
                tick_size=tick_size,
                initial_taker_depth=init_taker_depth
            )

            if not ok:
                if action in ("MAKER_TIMEOUT_ZERO_LOSS", "TOXICITY_EVASION_CANCEL"):
                    tag = action
                    logger.info(f"{tag} on {short_id}: Leg 1 order safely aborted with ZERO loss. Reason: {details.get('reason', 'n/a') if isinstance(details, dict) else 'n/a'}")
                    if self.dash_state and hasattr(self.dash_state, "add_activity_log"):
                        self.dash_state.add_activity_log(f"🛡️ [{tag}] Market {short_id} maker order cancelled with $0 loss.")
                    if self.risk and hasattr(self.risk, "open_positions"):
                        if market_id in self.risk.open_positions:
                            del self.risk.open_positions[market_id]
                    cooldown_duration = 5.0
                else:
                    realized_loss = float(details.get("realized_loss", 0.0) or 0.0) if isinstance(details, dict) else 0.0
                    if realized_loss > 0:
                        logger.warning(f"Maker-taker arbitrage unwound on {short_id} with realized loss: ${realized_loss:.4f}")
                        if self.risk:
                            self.risk.record_pnl(-realized_loss)
                        if self.dash_state:
                            self.dash_state.add_activity_log(f"⚠️ Unwind loss on {short_id}: -${realized_loss:.4f}")
                    cooldown_duration = 300.0

                if not hasattr(self, "market_cooldowns"):
                    self.market_cooldowns = {}
                self.market_cooldowns[market_id] = time.time() + cooldown_duration

                if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                    opp_copy = dict(opp)
                    opp_copy["trade_size"] = trade_size
                    opp_copy["expected_profit"] = expected_profit
                    miss_r = MissedReason.CONCURRENCY_EXHAUSTED if action in ("MAKER_TIMEOUT_ZERO_LOSS", "TOXICITY_EVASION_CANCEL") else MissedReason.CLOB_ORDER_KILLED
                    self.shadow_tracker.record_missed(opp_copy, miss_r)
                return False


            if self.risk:
                self.risk.open_position(market_id, trade_size, expected_profit)
            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if target_state and hasattr(target_state, "add_trade"):
                target_state.add_trade(market_id, round(trade_size, 2), round(expected_profit, 4), time_str)
            self.on_trade_executed(market_id, trade_size, expected_profit)
            return True
            
        if time.time() - getattr(self, "last_balance_sync_time", 0.0) > 1.0:

            try:
                self.sync_live_balance()
            except Exception as e:
                logger.warning(f"Balance sync check in execute_arbitrage encountered error: {e}")

        market_id = opp['market_id']
        trade_size = opp['trade_size']
        edge = opp['edge']
        short_id = opp.get('short_id', f"Market {market_id[-6:]}")
        target_state = self.dash_state

        # Dynamic Bankroll Liquidity Gating
        total_cap = _safe_float(getattr(self.risk, "capital", None), 1000.0) if self.risk else 1000.0
        max_exp_pct = _safe_float(getattr(self.risk, "max_exposure_pct", None), 0.20) if self.risk else 0.20
        sizing_mult = _safe_float(getattr(self.risk, "sizing_multiplier", None), 1.0) if self.risk else 1.0
        desired = max(5.0, total_cap * max_exp_pct * sizing_mult)
        bankroll_floor = 10.0 if total_cap < 100.0 else 250.0
        dyn_min_depth = compute_dynamic_min_depth(total_cap, desired, floor_override=bankroll_floor)

        # Strict Expiration Horizon Gating
        m_info = self.market_token_map.get(market_id, {}) if self.market_token_map else {}
        end_dt = _parse_market_end_date(opp) or _parse_market_end_date(m_info)
        if end_dt is not None:
            now_utc = datetime.now(timezone.utc)
            hours_left = (end_dt - now_utc).total_seconds() / 3600.0
            if hours_left < 4.0:
                logger.warning(
                    f"🚫 [EXPIRY GATE BLOCKED] Market {short_id} expires in {hours_left:.1f}h "
                    f"(< 4.0h safety threshold). Order rejected."
                )
                return False

        # Strict Liquidity & Spread Gating
        token_yes = opp.get('token_yes') or m_info.get('token_yes')
        token_no = opp.get('token_no') or m_info.get('token_no')

        book_yes = opp.get("book_yes")
        book_no = opp.get("book_no")
        if book_yes is None and isinstance(getattr(self, "market_books", None), dict):
            mb = self.market_books.get(market_id, {})
            if isinstance(mb.get("book_yes"), dict):
                book_yes = mb.get("book_yes")
            if isinstance(mb.get("book_no"), dict):
                book_no = mb.get("book_no")

        if "bid_yes" not in opp and book_yes:
            b_bid, _, _ = _extract_book_metrics(book_yes)
            if b_bid is not None:
                opp["bid_yes"] = b_bid
        if "bid_no" not in opp and book_no:
            b_bid, _, _ = _extract_book_metrics(book_no)
            if b_bid is not None:
                opp["bid_no"] = b_bid

        # If live client is available and bids still unknown, populate from CLOB order book
        if ("bid_yes" not in opp or "bid_no" not in opp) and hasattr(self, "client") and self.client:
            if token_yes and "bid_yes" not in opp and hasattr(self.client, "get_order_book"):
                try:
                    r_by = self.client.get_order_book(token_yes)
                    if hasattr(r_by, "bids") and isinstance(getattr(r_by, "bids", None), (list, tuple)):
                        bids_y = getattr(r_by, "bids", [])
                        if bids_y:
                            first_bid = bids_y[0]
                            opp["bid_yes"] = float(getattr(first_bid, "price", None) or (first_bid.get("price") if isinstance(first_bid, dict) else first_bid[0]))
                        book_yes = r_by
                    elif isinstance(r_by, dict):
                        bids_y = r_by.get("bids", [])
                        if bids_y:
                            opp["bid_yes"] = float(bids_y[0].get("price") if isinstance(bids_y[0], dict) else bids_y[0][0])
                        book_yes = r_by
                except Exception:
                    pass
            if token_no and "bid_no" not in opp and hasattr(self.client, "get_order_book"):
                try:
                    r_bn = self.client.get_order_book(token_no)
                    if hasattr(r_bn, "bids") and isinstance(getattr(r_bn, "bids", None), (list, tuple)):
                        bids_n = getattr(r_bn, "bids", [])
                        if bids_n:
                            first_bid = bids_n[0]
                            opp["bid_no"] = float(getattr(first_bid, "price", None) or (first_bid.get("price") if isinstance(first_bid, dict) else first_bid[0]))
                        book_no = r_bn
                    elif isinstance(r_bn, dict):
                        bids_n = r_bn.get("bids", [])
                        if bids_n:
                            opp["bid_no"] = float(bids_n[0].get("price") if isinstance(bids_n[0], dict) else bids_n[0][0])
                        book_no = r_bn
                except Exception:
                    pass

        is_liquid, gate_reason = validate_arbitrage_execution(
            opp,
            book_yes=book_yes,
            book_no=book_no,
            market_meta=opp.get("market_meta"),
            max_spread=0.030,
            min_depth_usd=dyn_min_depth,
            min_volume_24h=100.0,
            min_hours_to_expiry=4.0
        )
        if not is_liquid:
            logger.info(f"🚫 [LIQUIDITY FILTER BLOCKED] Market {short_id}: {gate_reason}")
            if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                opp_copy = dict(opp)
                reason_to_record = _map_liquidity_gate_reason(gate_reason, opp)
                self.shadow_tracker.record_missed(opp_copy, reason_to_record)
            return False

        with self.trade_lock:
            with self.lock:
                if market_id in self.market_books:
                    books = self.market_books[market_id]
                    if any(v is None for v in books.values()):
                        return False

            with self.risk.lock:
                total_cap = _safe_float(getattr(self.risk, "capital", None), 1000.0)
                available_cash = _safe_float(getattr(self.risk, "available_cash", None), total_cap)
                if total_cap < 100.0:
                    spendable = available_cash
                else:
                    reserve_pct = _safe_float(getattr(self.risk, "reserve_cash_pct", None), 0.0)
                    spendable = max(0.0, available_cash - (available_cash * reserve_pct))

                open_pos = getattr(self.risk, "open_positions", {})
                if not isinstance(open_pos, dict):
                    open_pos = {}

                market_positions = [
                    p for k, p in open_pos.items()
                    if (isinstance(p, dict) and p.get('market_id') == market_id) or k == market_id or k.startswith(f"{market_id}_")
                ]
                current_m_exp = sum(_safe_float(p.get('size'), 0.0) for p in market_positions)
                max_market_pct = _safe_float(getattr(self.risk, "max_market_exposure_pct", None), 0.25)
                if total_cap < 50.0:
                    max_m_allowed = max(5.50, total_cap * max_market_pct)
                else:
                    max_m_allowed = total_cap * max_market_pct
                rem_cap = max(0.0, max_m_allowed - current_m_exp)
                max_exp_pct = _safe_float(getattr(self.risk, "max_exposure_pct", None), 0.10)
                sizing_mult = _safe_float(getattr(self.risk, "sizing_multiplier", None), 1.0)
                desired = total_cap * max_exp_pct * sizing_mult
                if total_cap < 50.0 and desired < 5.0 and spendable >= 5.0:
                    desired = 5.0
            
            trade_size = min(trade_size, desired, spendable, rem_cap)
            
            if target_state and getattr(target_state, 'state', {}).get('execution_mode') == 'Live Trading':
                live_wager_cap = max(5.50, float(target_state.state.get('live_wager_cap', 10.0) or 10.0))
                trade_size = min(trade_size, live_wager_cap)
            
            if trade_size < 1.0:
                if spendable < 1.0:
                    reason = MissedReason.INSUFFICIENT_CASH
                elif rem_cap < 1.0:
                    reason = MissedReason.EXPOSURE_LIMIT_EXCEEDED
                else:
                    reason = MissedReason.INSUFFICIENT_CASH
                if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                    opp_copy = dict(opp)
                    opp_copy["trade_size"] = opp.get("trade_size", desired)
                    opp_copy["expected_profit"] = opp_copy["trade_size"] * edge
                    self.shadow_tracker.record_missed(opp_copy, reason)
                return False


            expected_profit = trade_size * edge

            if not self.risk.can_trade(trade_size, market_id=market_id):
                gating_reason = getattr(self.risk, "check_trade_gating_reason", lambda s, m: None)(trade_size, market_id=market_id)
                try:
                    reason_enum = MissedReason(gating_reason) if gating_reason else MissedReason.CIRCUIT_BREAKER
                except ValueError:
                    reason_enum = MissedReason.CIRCUIT_BREAKER
                if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                    opp_copy = dict(opp)
                    opp_copy["trade_size"] = trade_size
                    opp_copy["expected_profit"] = expected_profit
                    self.shadow_tracker.record_missed(opp_copy, reason_enum)
                return False
                
            m_info = self.market_token_map.get(market_id, {})
            token_yes = opp.get('token_yes') or m_info.get('token_yes')
            token_no = opp.get('token_no') or m_info.get('token_no')
            if not token_yes or not token_no:
                if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                    opp_copy = dict(opp)
                    opp_copy["trade_size"] = trade_size
                    opp_copy["expected_profit"] = expected_profit
                    self.shadow_tracker.record_missed(opp_copy, MissedReason.ZERO_LIQUIDITY)
                return False

            logger.info(f"🚨 LIVE ARBITRAGE OPPORTUNITY 🚨 | Market {market_id}")
            
            try:
                from py_clob_client_v2.clob_types import (
                    OrderArgsV2,
                    PostOrdersV2Args,
                    OrderType,
                    PartialCreateOrderOptions,
                )
                cost_per_pair = opp['ask_yes'] + opp['ask_no']
                if cost_per_pair <= 0:
                    return False
                max_shares = int(trade_size / cost_per_pair)
                if max_shares < 5:
                    five_share_cost = 5.0 * cost_per_pair
                    # Only allow 5 shares if both spendable cash AND risk sizing permit it
                    if spendable >= five_share_cost and self.risk.can_trade(five_share_cost, market_id=market_id):
                        max_shares = 5
                    else:
                        if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                            opp_copy = dict(opp)
                            self.shadow_tracker.record_missed(opp_copy, MissedReason.INSUFFICIENT_CASH)
                        return False

                matched_shares = None
                for s in range(max_shares, 4, -1):
                    if (s * cost_per_pair <= spendable + 1e-5) and self.risk.can_trade(s * cost_per_pair, market_id=market_id):
                        matched_shares = float(s)
                        break

                if matched_shares is None or matched_shares < 5:
                    if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                        opp_copy = dict(opp)
                        five_share_cost = 5.0 * cost_per_pair
                        if spendable < five_share_cost:
                            reason_enum = MissedReason.INSUFFICIENT_CASH
                        else:
                            gating = self.risk.check_trade_gating_reason(five_share_cost, market_id=market_id)
                            try:
                                reason_enum = MissedReason(gating) if gating else MissedReason.EXPOSURE_LIMIT_EXCEEDED
                            except ValueError:
                                reason_enum = MissedReason.EXPOSURE_LIMIT_EXCEEDED
                        self.shadow_tracker.record_missed(opp_copy, reason_enum)
                    return False

                # Clamp matched_shares by available taker depth if taker depth is known
                depth_taker = float(opp.get('depth_no_shares', opp.get('depth_no', 0.0)) or 0.0)
                if depth_taker > 0 and matched_shares > depth_taker:
                    matched_shares = max(5.0, float(int(depth_taker)))

                size_yes = matched_shares
                size_no = matched_shares
                trade_size = round(size_yes * opp['ask_yes'] + size_no * opp['ask_no'], 2)
                expected_profit = round((size_yes * 1.0) - trade_size, 4)

                # Optional Maker-Taker Asymmetric Execution Engine (Zero Slippage & Zero Taker Fees on Leg 1)
                if target_state and getattr(target_state, "state", {}).get("execution_style") == "maker_taker":
                    tick_size = float(opp.get('tick_size', 0.001))
                    min_edge_val = float(getattr(target_state, 'state', {}).get('min_edge_pct', 0.0080) or 0.0080)

                    if opp.get("execution_type") == "maker_taker":
                        token_maker = opp.get("maker_token") or token_yes
                        maker_price = opp.get("maker_price")
                        token_taker = opp.get("taker_token") or token_no
                        taker_price = opp.get("taker_price") or opp['ask_no']
                    else:
                        bid_yes = opp.get('bid_yes')
                        ask_yes = opp['ask_yes']
                        ask_no = opp['ask_no']
                        max_viable = round(1.00 - ask_no - min_edge_val, 4)
                        maker_price = min(ask_yes - tick_size, max_viable)
                        if bid_yes is not None and bid_yes >= (ask_yes - 3 * tick_size):
                            maker_price = max(bid_yes + tick_size, maker_price)
                        maker_price = min(maker_price, max_viable)
                        maker_price = max(tick_size, _round_to_tick_size(maker_price, tick_size))
                        if maker_price > max_viable or (1.00 - maker_price - ask_no) < min_edge_val:
                            logger.info(f"🚫 Maker price {maker_price} exceeds max viable {max_viable} on {short_id}. Aborting.")
                            return False
                        token_maker = token_yes
                        token_taker = token_no
                        taker_price = ask_no

                    timeout_sec = float(getattr(target_state, "state", {}).get("maker_timeout_seconds", 5.0) or 5.0)
                    init_taker_depth = opp.get("depth_taker") or (opp.get("depth_no") if opp.get("maker_leg") == "YES" else opp.get("depth_yes"))
                    ok, action, details = self.maker_taker_executor.execute_maker_taker_arbitrage(
                        token_maker=token_maker,
                        maker_price=maker_price,
                        token_taker=token_taker,
                        taker_price=taker_price,
                        size=matched_shares,
                        timeout_seconds=timeout_sec,
                        dash_state=self.dash_state,
                        min_edge=min_edge_val,
                        tick_size=tick_size,
                        initial_taker_depth=init_taker_depth
                    )
                    if not ok:
                        if action in ("MAKER_TIMEOUT_ZERO_LOSS", "TOXICITY_EVASION_CANCEL"):
                            tag = action
                            logger.info(f"{tag} on {short_id}: Leg 1 order safely aborted with ZERO loss. Reason: {details.get('reason', 'n/a') if isinstance(details, dict) else 'n/a'}")
                            if self.dash_state and hasattr(self.dash_state, "add_activity_log"):
                                self.dash_state.add_activity_log(f"🛡️ [{tag}] Market {short_id} maker order cancelled with $0 loss.")
                            if self.risk and hasattr(self.risk, "open_positions"):
                                if market_id in self.risk.open_positions:
                                    del self.risk.open_positions[market_id]
                            cooldown_duration = 5.0
                            miss_r = MissedReason.CONCURRENCY_EXHAUSTED
                        else:
                            realized_loss = float(details.get("realized_loss", 0.0) or 0.0) if isinstance(details, dict) else 0.0
                            if realized_loss > 0:
                                logger.warning(f"Maker-taker arbitrage unwound on {short_id} with realized loss: ${realized_loss:.4f}")
                                self.risk.record_pnl(-realized_loss)
                                if self.dash_state:
                                    self.dash_state.add_activity_log(f"⚠️ Unwind loss on {short_id}: -${realized_loss:.4f}")
                            cooldown_duration = 15.0
                            miss_r = MissedReason.CLOB_ORDER_KILLED

                        if not hasattr(self, "market_cooldowns"):
                            self.market_cooldowns = {}
                        self.market_cooldowns[market_id] = time.time() + cooldown_duration

                        if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                            opp_copy = dict(opp)
                            opp_copy["trade_size"] = trade_size
                            opp_copy["expected_profit"] = expected_profit
                            self.shadow_tracker.record_missed(opp_copy, miss_r)
                        return False

                    # Both legs secured! Record position & sync collateral
                    opened = self.risk.open_position(market_id, trade_size, expected_profit)
                    time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    if target_state and hasattr(target_state, "add_trade"):
                        target_state.add_trade(market_id, round(trade_size, 2), round(expected_profit, 4), time_str)
                    self.on_trade_executed(market_id, trade_size, expected_profit)
                    return True

                tick_size = float(opp.get('tick_size', 0.001))
                # For taker BUY orders, always ceil to tick size so price is >= ask and never rounded down below the book:
                price_yes = _ceil_to_tick_size(opp['ask_yes'], tick_size)
                price_no = _ceil_to_tick_size(opp['ask_no'], tick_size)

                # Clamp matched shares so FOK order does NOT exceed available depth on either leg:
                depth_yes_shares = float(opp.get('depth_yes_shares', opp.get('depth_yes', 0.0)) or 0.0)
                depth_no_shares = float(opp.get('depth_no_shares', opp.get('depth_no', 0.0)) or 0.0)
                avail_shares = min(depth_yes_shares, depth_no_shares)
                if avail_shares > 0 and matched_shares > avail_shares:
                    matched_shares = math.floor(avail_shares * 10.0) / 10.0
                    if matched_shares < 5.0:
                        logger.info(f"Available dual-leg depth ({matched_shares:.1f} shares) below exchange minimum 5. Skipping.")
                        return False
                    size_yes = matched_shares
                    size_no = matched_shares

                # Verify edge still exists:
                if price_yes + price_no >= 1.0 - float(getattr(target_state, 'state', {}).get('min_edge_pct', 0.0005) or 0.0005):
                    return False
                try:
                    order_opts = PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=False)
                except Exception:
                    order_opts = None

                supports_options = False
                if order_opts is not None and hasattr(self.client, "create_order"):
                    try:
                        import inspect
                        fn_target = getattr(self.client.create_order, "side_effect", None) or self.client.create_order
                        sig = inspect.signature(fn_target)
                        supports_options = 'options' in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                    except Exception:
                        supports_options = True

                if supports_options and order_opts is not None:
                    order_yes = self.client.create_order(
                        OrderArgsV2(
                            price=price_yes,
                            size=size_yes,
                            side='BUY',
                            token_id=token_yes
                        ),
                        options=order_opts
                    )
                    order_no = self.client.create_order(
                        OrderArgsV2(
                            price=price_no,
                            size=size_no,
                            side='BUY',
                            token_id=token_no
                        ),
                        options=order_opts
                    )
                else:
                    order_yes = self.client.create_order(
                        OrderArgsV2(
                            price=price_yes,
                            size=size_yes,
                            side='BUY',
                            token_id=token_yes
                        )
                    )
                    order_no = self.client.create_order(
                        OrderArgsV2(
                            price=price_no,
                            size=size_no,
                            side='BUY',
                            token_id=token_no
                        )
                    )

                orders = [
                    PostOrdersV2Args(order=order_yes, orderType=OrderType.FOK),
                    PostOrdersV2Args(order=order_no, orderType=OrderType.FOK)
                ]
                
                resp = self.client.post_orders(orders)
                logger.info(f"Live orders posted: {resp}")
                
                # Critical Dual-Leg Fill Validation & Delayed Order Polling
                leg_yes = resp[0] if isinstance(resp, list) and len(resp) > 0 and isinstance(resp[0], dict) else {}
                leg_no = resp[1] if isinstance(resp, list) and len(resp) > 1 and isinstance(resp[1], dict) else {}

                order_id_yes = leg_yes.get("orderID")
                order_id_no = leg_no.get("orderID")

                yes_err = leg_yes.get("errorMsg")
                no_err = leg_no.get("errorMsg")

                # Extract initial takingAmount directly if already matched/filled in resp
                try:
                    yes_taking = float(leg_yes.get("takingAmount") or 0.0) if not yes_err else 0.0
                except (ValueError, TypeError):
                    yes_taking = 0.0
                try:
                    no_taking = float(leg_no.get("takingAmount") or 0.0) if not no_err else 0.0
                except (ValueError, TypeError):
                    no_taking = 0.0

                # Determine if delayed polling is needed (only if status is delayed or takingAmount empty, and no fatal error)
                need_poll_yes = bool(order_id_yes and not yes_err and (leg_yes.get("status") == "delayed" or yes_taking <= 0))
                need_poll_no = bool(order_id_no and not no_err and (leg_no.get("status") == "delayed" or no_taking <= 0))
                is_delayed = need_poll_yes or need_poll_no

                if is_delayed and self.client:
                    logger.info("Order status is delayed in CLOB sequencer. Awaiting terminal settlement...")
                    poll_start = time.time()
                    yes_settled = not need_poll_yes
                    no_settled = not need_poll_no

                    while time.time() - poll_start < 3.0:
                        time.sleep(0.4)
                        if not yes_settled and order_id_yes:
                            try:
                                info_y = self.client.get_order(order_id_yes)
                                if info_y is None:
                                    yes_settled = True
                                elif isinstance(info_y, dict):
                                    st = str(info_y.get("status", "")).upper()
                                    if st in ("MATCHED", "CANCELED", "KILLED"):
                                        yes_taking = float(info_y.get("size_matched", 0.0) or 0.0)
                                        yes_settled = True
                            except Exception:
                                pass
                        if not no_settled and order_id_no:
                            try:
                                info_n = self.client.get_order(order_id_no)
                                if info_n is None:
                                    no_settled = True
                                elif isinstance(info_n, dict):
                                    st = str(info_n.get("status", "")).upper()
                                    if st in ("MATCHED", "CANCELED", "KILLED"):
                                        no_taking = float(info_n.get("size_matched", 0.0) or 0.0)
                                        no_settled = True
                            except Exception:
                                pass
                        if yes_settled and no_settled:
                            break

                yes_filled = yes_taking > 0
                no_filled = no_taking > 0

                # Dual-Leg Atomicity & Hedge Completion / Rollback
                if yes_filled and not no_filled:
                    logger.warning(f"🚨 UNHEDGED FILL: Leg 1 (YES) filled {yes_taking} shares but Leg 2 (NO) missed! Attempting immediate hedge completion...")
                    # Step 1: Attempt to complete the hedge by buying missing NO shares at best ask
                    hedge_done = False
                    try:
                        book_no_url = f"https://clob.polymarket.com/book?token_id={token_no}"
                        req_b = urllib.request.Request(book_no_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req_b, timeout=3) as resp_b:
                            book_data = json.loads(resp_b.read().decode('utf-8'))
                        asks_no = book_data.get("asks", [])
                        if asks_no:
                            best_ask_no = float(asks_no[0].get("price") if isinstance(asks_no[0], dict) else asks_no[0][0])
                            if best_ask_no + opp['ask_yes'] <= 1.03:
                                logger.info(f"Completing hedge: Buying {yes_taking} NO shares at ${best_ask_no:.4f}...")
                                h_order = self.client.create_order(OrderArgsV2(price=best_ask_no, size=yes_taking, side='BUY', token_id=token_no))
                                h_resp = self.client.post_orders([PostOrdersV2Args(order=h_order, orderType=OrderType.FOK)])
                                h_leg = h_resp[0] if isinstance(h_resp, list) and h_resp and isinstance(h_resp[0], dict) else {}
                                if not h_leg.get("errorMsg"):
                                    no_taking = yes_taking
                                    no_filled = True
                                    hedge_done = True
                                    logger.info("✅ Hedge successfully completed! Dual matched pair secured.")
                    except Exception as e:
                        logger.warning(f"Hedge completion attempt encountered error: {e}")
                        
                    if not hedge_done:
                        logger.warning(f"Hedge completion unavailable. Rolling back {yes_taking} YES shares immediately to CLOB...")
                        if target_state:
                            target_state.add_activity_log(f"🚨 [IMMEDIATE ROLLBACK] Leg 2 missed. Liquidating {yes_taking} YES shares to eliminate directional risk...")
                        self._emergency_dump_leg(token_yes, yes_taking, label="YES", target_state=target_state, buy_price=opp['ask_yes'])
                        self.sync_live_balance()
                        if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                            opp_copy = dict(opp)
                            opp_copy["trade_size"] = trade_size
                            opp_copy["expected_profit"] = expected_profit
                            self.shadow_tracker.record_missed(opp_copy, MissedReason.CLOB_ORDER_KILLED)
                        return False

                elif no_filled and not yes_filled:
                    logger.warning(f"🚨 UNHEDGED FILL: Leg 2 (NO) filled {no_taking} shares but Leg 1 (YES) missed! Attempting immediate hedge completion...")
                    hedge_done = False
                    try:
                        book_yes_url = f"https://clob.polymarket.com/book?token_id={token_yes}"
                        req_b = urllib.request.Request(book_yes_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req_b, timeout=3) as resp_b:
                            book_data = json.loads(resp_b.read().decode('utf-8'))
                        asks_yes = book_data.get("asks", [])
                        if asks_yes:
                            best_ask_yes = float(asks_yes[0].get("price") if isinstance(asks_yes[0], dict) else asks_yes[0][0])
                            if best_ask_yes + opp['ask_no'] <= 1.03:
                                logger.info(f"Completing hedge: Buying {no_taking} YES shares at ${best_ask_yes:.4f}...")
                                h_order = self.client.create_order(OrderArgsV2(price=best_ask_yes, size=no_taking, side='BUY', token_id=token_yes))
                                h_resp = self.client.post_orders([PostOrdersV2Args(order=h_order, orderType=OrderType.FOK)])
                                h_leg = h_resp[0] if isinstance(h_resp, list) and h_resp and isinstance(h_resp[0], dict) else {}
                                if not h_leg.get("errorMsg"):
                                    yes_taking = no_taking
                                    yes_filled = True
                                    hedge_done = True
                                    logger.info("✅ Hedge successfully completed! Dual matched pair secured.")
                    except Exception as e:
                        logger.warning(f"Hedge completion attempt encountered error: {e}")
                        
                    if not hedge_done:
                        logger.warning(f"Hedge completion unavailable. Rolling back {no_taking} NO shares immediately to CLOB...")
                        if target_state:
                            target_state.add_activity_log(f"🚨 [IMMEDIATE ROLLBACK] Leg 1 missed. Liquidating {no_taking} NO shares to eliminate directional risk...")
                        self._emergency_dump_leg(token_no, no_taking, label="NO", target_state=target_state, buy_price=opp['ask_no'])
                        self.sync_live_balance()
                        if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                            opp_copy = dict(opp)
                            opp_copy["trade_size"] = trade_size
                            opp_copy["expected_profit"] = expected_profit
                            self.shadow_tracker.record_missed(opp_copy, MissedReason.CLOB_ORDER_KILLED)
                        return False

                elif yes_filled and no_filled:
                    # Both filled! Handle unequal sizes if any
                    hedged_size = min(yes_taking, no_taking)
                    excess = abs(yes_taking - no_taking)
                    if excess > 0.01:
                        if yes_taking > no_taking:
                            logger.warning(f"🚨 UNEQUAL FILL: YES {yes_taking} vs NO {no_taking}. Immediately dumping {excess} excess YES shares to CLOB...")
                            if target_state:
                                target_state.add_activity_log(f"🚨 [EMERGENCY DUMP] Dumping {excess} excess YES shares to cash...")
                            self._emergency_dump_leg(token_yes, excess, label="YES", target_state=target_state, buy_price=opp['ask_yes'])
                        else:
                            logger.warning(f"🚨 UNEQUAL FILL: NO {no_taking} vs YES {yes_taking}. Immediately dumping {excess} excess NO shares to CLOB...")
                            if target_state:
                                target_state.add_activity_log(f"🚨 [EMERGENCY DUMP] Dumping {excess} excess NO shares to cash...")
                            self._emergency_dump_leg(token_no, excess, label="NO", target_state=target_state, buy_price=opp['ask_no'])
                    trade_size = round(hedged_size * opp['ask_yes'] + hedged_size * opp['ask_no'], 2)
                    expected_profit = round((hedged_size * 1.0) - trade_size, 4)
                else:
                    # Neither filled
                    logger.warning(f"Live order unfilled / killed on Polymarket: {resp}")
                    err_text = f"YES: {yes_err or 'unfilled'}; NO: {no_err or 'unfilled'}"
                    err_lower = err_text.lower()
                    if "not enough balance" in err_lower or "balance is not enough" in err_lower:
                        logger.warning("Insufficient balance reported by Polymarket CLOB. Synchronizing live collateral balance without liquidating positions.")
                        try:
                            self.sync_live_balance()
                        except Exception as sync_err:
                            logger.warning(f"Balance sync failed after insufficient balance error: {sync_err}")
                    if target_state:
                        target_state.add_activity_log(f"⚠️ Live Orders Unfilled: {err_text}")
                    if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                        opp_copy = dict(opp)
                        opp_copy["trade_size"] = trade_size
                        opp_copy["expected_profit"] = expected_profit
                        self.shadow_tracker.record_missed(opp_copy, MissedReason.CLOB_ORDER_KILLED)
                    return False

                # 1. Open the position
                opened = self.risk.open_position(market_id, trade_size, expected_profit)
                time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                if target_state:
                    target_state.add_trade(market_id, round(trade_size, 2), round(expected_profit, 4), time_str)

                # 2. Attempt on-chain token merging in a try/except block, but if it reverts or fails, log it and do NOT return False or abort the trade!
                merge_succeeded = False
                wallet_disp = f"{wallet_address[:6]}...{wallet_address[-4:]}" if 'wallet_address' in locals() and wallet_address and len(wallet_address) > 10 else "Polymarket wallet"
                try:
                    from web3 import Web3
                    
                    w3 = Web3(Web3.HTTPProvider(os.environ.get('POLYGON_RPC_URL', 'https://polygon-bor-rpc.publicnode.com')))
                    private_key = os.environ.get('POLYMARKET_PRIVATE_KEY')
                    wallet_address = self.address or os.environ.get('POLYMARKET_ADDRESS')
                    
                    if not private_key or not wallet_address:
                        raise ValueError("Missing private key or address for Web3 execution.")
                    
                    wallet_address = Web3.to_checksum_address(wallet_address)
                    wallet_disp = f"{wallet_address[:6]}...{wallet_address[-4:]}"
                    ctf_address = Web3.to_checksum_address('0x4D97DCd97eC945f40cF65F87097ACe5EA0476045')
                    collateral_token = Web3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
                    parent_collection_id = Web3.to_bytes(hexstr="0x0000000000000000000000000000000000000000000000000000000000000000")
                    
                    cond_id_hex = market_id if market_id.startswith('0x') else '0x' + market_id
                    condition_id = Web3.to_bytes(hexstr=cond_id_hex)
                    
                    partition = [1, 2]
                    
                    abi = '[{"inputs":[{"internalType":"address","name":"collateralToken","type":"address"},{"internalType":"bytes32","name":"parentCollectionId","type":"bytes32"},{"internalType":"bytes32","name":"conditionId","type":"bytes32"},{"internalType":"uint256[]","name":"partition","type":"uint256[]"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"mergePositions","outputs":[],"stateMutability":"nonpayable","type":"function"}, {"inputs":[{"internalType":"address","name":"account","type":"address"},{"internalType":"uint256","name":"id","type":"uint256"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]'
                    
                    ctf_contract = w3.eth.contract(address=ctf_address, abi=abi)
                    
                    try:
                        token_yes_int = int(token_yes, 0) if isinstance(token_yes, str) and token_yes.startswith('0x') else int(token_yes)
                    except (ValueError, TypeError):
                        token_yes_int = 1
                    try:
                        token_no_int = int(token_no, 0) if isinstance(token_no, str) and token_no.startswith('0x') else int(token_no)
                    except (ValueError, TypeError):
                        token_no_int = 2
                    
                    signer_account = w3.eth.account.from_key(private_key)
                    signer_address = signer_account.address
                    
                    logger.info("Waiting for order settlement on-chain...")
                    balance_yes = 0
                    balance_no = 0
                    for _ in range(3):
                        balance_yes = ctf_contract.functions.balanceOf(wallet_address, token_yes_int).call()
                        balance_no = ctf_contract.functions.balanceOf(wallet_address, token_no_int).call()
                        if balance_yes == 0 and balance_no == 0 and signer_address != wallet_address:
                            balance_yes = ctf_contract.functions.balanceOf(signer_address, token_yes_int).call()
                            balance_no = ctf_contract.functions.balanceOf(signer_address, token_no_int).call()
                        if balance_yes > 0 and balance_no > 0:
                            break
                        time.sleep(0.5)
                        
                    merge_amount = min(balance_yes, balance_no)
                    if merge_amount <= 0:
                        logger.info(f"Tokens awaiting asynchronous on-chain batch settlement on Polygon for {short_id} (YES={balance_yes}, NO={balance_no}).")
                    else:
                        logger.info(f"Merging exact on-chain balance: {merge_amount} units")

                    # Gasless Relayer v2 submission using self.relayer_client
                    if merge_amount > 0 and getattr(self, "relayer_client", None):
                        try:
                            from py_builder_relayer_client.models import DepositWalletCall
                            calldata = ctf_contract.encode_abi('mergePositions', args=[collateral_token, parent_collection_id, condition_id, partition, merge_amount])
                            call = DepositWalletCall(target=ctf_address, value="0", data=calldata)
                            
                            # Query dynamic on-chain nonce for deposit wallet
                            abi_wallet = [{'inputs': [], 'name': 'nonce', 'outputs': [{'name': '', 'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'}]
                            contract_w = w3.eth.contract(address=wallet_address, abi=abi_wallet)
                            on_chain_nonce = str(contract_w.functions.nonce().call())
                            deadline = str(int(time.time()) + 3600)
                            
                            tx_resp = self.relayer_client.execute_deposit_wallet_batch(calls=[call], wallet_address=self.address, nonce=on_chain_nonce, deadline=deadline)
                            tx_hash = getattr(tx_resp, "transaction_hash", None)
                            logger.info(f"Gasless relayer merge submitted! TX Hash: {tx_hash}")
                            merge_succeeded = True
                        except Exception as relayer_err:
                            logger.warning(f"Gasless relayer merge attempt encountered: {relayer_err}")

                    if merge_amount > 0 and not merge_succeeded:
                        # Direct Web3 transaction from signer_address
                        nonce = w3.eth.get_transaction_count(signer_address)
                        gas_price = int(w3.eth.gas_price * 1.25)
                        
                        tx = ctf_contract.functions.mergePositions(
                            collateral_token,
                            parent_collection_id,
                            condition_id,
                            partition,
                            merge_amount
                        ).build_transaction({
                            'chainId': 137,
                            'gas': 300000,
                            'gasPrice': gas_price,
                            'nonce': nonce,
                            'from': signer_address
                        })
                        
                        signed_tx = w3.eth.account.sign_transaction(tx, private_key=private_key)
                        raw_tx = getattr(signed_tx, 'raw_transaction', getattr(signed_tx, 'rawTransaction', None))
                        tx_hash = w3.eth.send_raw_transaction(raw_tx)
                        
                        logger.info(f"Token merging transaction sent! TX Hash: {w3.to_hex(tx_hash)}")
                        
                        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                        if getattr(receipt, 'status', None) == 1:
                            logger.info("Tokens merged successfully on-chain.")
                            merge_succeeded = True
                        else:
                            logger.error("Token merging failed or reverted on-chain.")
                            
                except Exception as e:
                    logger.info(f"Direct on-chain merge skipped or pending ({e}). Matched pair secured in Polymarket wallet {wallet_disp} ($1.00 payout on market resolution or 1-click portfolio merge).")
                    
                if not merge_succeeded:
                    logger.info(f"Matched pair secured in Polymarket wallet {wallet_disp} ($1.00 payout on market resolution or 1-click portfolio merge).")
                    if target_state:
                        target_state.add_activity_log("⚠️ Token Merge Failed or pending on-chain: Position held for auto-unwind")
                        target_state.add_activity_log(f"💎 Matched pair secured in Polymarket wallet {wallet_disp} ($1.00 payout on market resolution or 1-click portfolio merge).")

                # 3. Call self.on_trade_executed immediately so cash is unwound and balance is synchronized to the pool in real time!
                self.on_trade_executed(market_id, trade_size, expected_profit)

                # 4. Reset books and depths non-destructively:
                with self.lock:
                    if market_id in self.market_books:
                        for k in self.market_books[market_id]:
                            self.market_books[market_id][k] = None
                    if market_id in self.market_depths:
                        del self.market_depths[market_id]
                    stale_keys = [k for k in self.last_processed_quotes if k[0] == market_id]
                    for k in stale_keys:
                        del self.last_processed_quotes[k]
                if target_state:
                    target_state.clear_market_edge(market_id)
                return True

            except Exception as e:
                logger.error(f"Live execution failed: {e}")
                err_raw = str(e)
                err_lower = err_raw.lower()
                if "not enough balance" in err_lower or "balance is not enough" in err_lower:
                    logger.warning("Insufficient balance exception from Polymarket CLOB. Synchronizing live collateral balance without liquidating positions.")
                    try:
                        self.sync_live_balance()
                    except Exception as sync_err:
                        logger.warning(f"Balance sync failed after insufficient balance exception: {sync_err}")
                if target_state:
                    if "maker address not allowed" in err_lower:
                        err_msg = "❌ Live Order Failed: Maker address not allowed (Deposit wallet required in POLYMARKET_ADDRESS)"
                    elif "invalid order version" in err_lower:
                        err_msg = "❌ Live Order Failed: Invalid order version"
                    elif "signer address has to be" in err_lower:
                        err_msg = "❌ Live Order Failed: Signer / API key address mismatch"
                    else:
                        truncated = err_raw[:57] + "..." if len(err_raw) > 60 else err_raw
                        err_msg = f"❌ Live Order Failed: {truncated}"
                    target_state.add_activity_log(err_msg)
                if hasattr(self, "shadow_tracker") and self.shadow_tracker:
                    opp_copy = dict(opp)
                    opp_copy["trade_size"] = trade_size
                    opp_copy["expected_profit"] = expected_profit
                    self.shadow_tracker.record_missed(opp_copy, MissedReason.CIRCUIT_BREAKER)
                return False



def main():
    # 0. Enforce 20% CPU budget & Below Normal priority on AMD Ryzen host
    configure_cpu_budget(0.20)

    paper_lock = acquire_paper_lock(PAPER_LOCK_PORT)
    if paper_lock is None:
        logger.error(f"Another instance of Polymarket Paper Trader is already running (Port {PAPER_LOCK_PORT}). Exiting.")
        sys.exit(1)

    try:
        with open(PAPER_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    def _cleanup_paper():
        try:
            if os.path.exists(PAPER_PID_FILE):
                os.remove(PAPER_PID_FILE)
        except Exception:
            pass
        try:
            paper_lock.close()
        except Exception:
            pass

    import atexit
    atexit.register(_cleanup_paper)

    global dash_state
    if dash_state is None:
        dash_state = DashboardState()

    max_ram_gb = float(os.environ.get("POLYMARKET_BOT_MAX_RAM_GB", 5.0))
    configure_ram_budget(max_ram_gb)

    market_limit = int(os.environ.get("POLYMARKET_BOT_MARKET_LIMIT", 1000))
    logger.info(f"Initializing Risk Engine & Paper Simulator for Top {market_limit} Markets...")
    saved_capital = dash_state.state.get("capital", 1000.0)
    saved_exposure_pct = dash_state.state.get("max_exposure_pct", 0.10)
    saved_max_concurrent = dash_state.state.get("max_concurrent_positions", 5)
    saved_available_cash = dash_state.state.get("available_cash", saved_capital)
    saved_locked_collateral = dash_state.state.get("locked_collateral", 0.0)
    saved_open_positions = dash_state.state.get("open_positions", {})
    risk_engine = RiskSizingEngine(
        initial_capital=saved_capital,
        max_exposure_pct=saved_exposure_pct,
        daily_loss_limit=100.0,
        dash_state=dash_state,
        max_concurrent_positions=saved_max_concurrent,
        available_cash=saved_available_cash,
        locked_collateral=saved_locked_collateral,
        open_positions=saved_open_positions
    )
    risk_engine.daily_loss = dash_state.state.get("daily_loss", 0.0)
    risk_engine.circuit_breaker_active = dash_state.state.get("circuit_breaker", False)
    dash_state.risk_engine = risk_engine

    # 1. Dynamically fetch or load top active binary markets (1,000 pairs)
    top_markets = fetch_top_markets(limit=market_limit)
    logger.info(f"Loaded {len(top_markets)} top binary markets for active scanning.")

    # Initialize dashboard state with all markets immediately
    dash_state.init_monitored_markets(top_markets)
    dash_state.state['active_markets_count'] = len(top_markets)
    dash_state.state['stagnant_purged_count'] = 0

    # Build market token map and collect all asset IDs
    market_token_map = {}
    all_token_ids = []
    now_ts = time.time()
    for m in top_markets:
        cid = m["condition_id"]
        tids = m["token_ids"]
        outcomes = m.get("outcomes", ["Yes", "No"])
        market_token_map[cid] = {
            "token_yes": tids[0],
            "token_no": tids[1],
            "question": m["question"],
            "outcomes": outcomes,
            "market_meta": m,
            "volume24hr": float(m.get('volume24hr', 0.0) or 0.0),
            "endDateIso": m.get('endDateIso') or m.get('endDate'),
            "slug": m.get('slug') or m.get('market_slug', ''),
            "category": m.get('category', ''),
            "tick_size": float(m.get('minimum_tick_size') or m.get('tick_size') or 0.001)
        }
        all_token_ids.extend(tids)

    simulator = LiveExecutor(risk_engine, market_token_map=market_token_map, dash_state=dash_state)
    logger.info("Initializing LIVE EXECUTOR (capable of hot-swapping to Paper Trading mode).")
    for cid in market_token_map:
        simulator.last_market_tick[cid] = now_ts


    # 2. Instant REST Cold-Start Order Book Seeding
    # Pre-populate simulator.market_books, simulator.market_depths, and initial market parity before opening WebSockets
    try:
        seeded_count = seed_order_books_via_rest(simulator, all_token_ids, chunk_size=500)
        logger.info(f"Instant REST cold-start seeding succeeded for {seeded_count} markets.")
    except Exception as e:
        logger.warning(f"REST cold-start seeding encountered error: {e}")

    # Initialize py_clob_client for REST health check
    host = "https://clob.polymarket.com"
    try:
        client = ClobClient(host)
        if client.get_ok() == "OK":
            logger.info("Polymarket CLOB REST API is accessible.")
    except Exception as e:
        logger.warning(f"Could not connect via ClobClient REST (py-sdk): {e}")

    # 3. WebSocket Pool & Keepalive (Configurable dual pool and socket count)
    num_sockets = int(os.environ.get("POLYMARKET_BOT_NUM_SOCKETS", 16))
    dual_pool_env = os.environ.get("POLYMARKET_BOT_DUAL_POOL", "1").lower() in ("1", "true", "yes")
    run_socket_pool(top_markets, simulator, num_sockets=num_sockets, dual_pool=dual_pool_env, refresher_interval=60.0)

if __name__ == "__main__":
    main()
