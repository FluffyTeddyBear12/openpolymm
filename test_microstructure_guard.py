"""
Unit and Integration Test Suite for Microstructure Guard & Dynamic Hedge Router.
Verifies Phase 1 Adverse Selection Shield & Elastic Edge Compression.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from microstructure_guard import MicrostructureGuard, QuoteRecord, TradeRecord
from dynamic_hedge_router import DynamicHedgeRouter
from maker_taker_engine import MakerTakerExecutor


class TestMicrostructureGuard(unittest.TestCase):
    def setUp(self):
        self.guard = MicrostructureGuard(max_window_seconds=120.0)
        self.token_yes = "0xTOKEN_YES"
        self.token_no = "0xTOKEN_NO"

    def test_compute_obi(self):
        # 1. Balanced book
        self.guard.record_quote(self.token_yes, best_bid=0.48, bid_size=100.0, best_ask=0.50, ask_size=100.0)
        self.assertAlmostEqual(self.guard.compute_obi(self.token_yes), 0.0)

        # 2. Pure bid pressure
        self.guard.record_quote(self.token_yes, best_bid=0.48, bid_size=100.0, best_ask=0.50, ask_size=0.0)
        self.assertAlmostEqual(self.guard.compute_obi(self.token_yes), 1.0)

        # 3. Pure ask pressure
        self.guard.record_quote(self.token_yes, best_bid=0.48, bid_size=0.0, best_ask=0.50, ask_size=100.0)
        self.assertAlmostEqual(self.guard.compute_obi(self.token_yes), -1.0)

    def test_compute_dual_obi_and_toxic_skew(self):
        # Symmetric books -> DOBI = 0.0
        self.guard.record_quote(self.token_yes, best_bid=0.48, bid_size=50.0, best_ask=0.50, ask_size=50.0)
        self.guard.record_quote(self.token_no, best_bid=0.48, bid_size=50.0, best_ask=0.50, ask_size=50.0)
        self.assertAlmostEqual(self.guard.compute_dual_obi(self.token_yes, self.token_no), 0.0)

        # Highly toxic skew against YES maker position:
        # YES book: 10 bid, 90 ask -> (10 - 90) = -80
        # NO book: 90 bid, 10 ask -> (90 - 10) = +80
        # DOBI = (-80 - 80) / 200 = -0.80 <= -0.65
        self.guard.record_quote(self.token_yes, best_bid=0.48, bid_size=10.0, best_ask=0.50, ask_size=90.0)
        self.guard.record_quote(self.token_no, best_bid=0.48, bid_size=90.0, best_ask=0.50, ask_size=10.0)
        dobi = self.guard.compute_dual_obi(self.token_yes, self.token_no)
        self.assertAlmostEqual(dobi, -0.80)
        self.assertLessEqual(dobi, -0.65)

    def test_compute_micro_price_and_drift(self):
        t0 = time.time()
        # Initial quote: Stoikov micro-price = (100*0.50 + 100*0.48) / 200 = 0.4900
        self.guard.record_quote(self.token_no, best_bid=0.48, bid_size=100.0, best_ask=0.50, ask_size=100.0, timestamp=t0 - 0.6)
        self.assertAlmostEqual(self.guard.compute_micro_price(self.token_no), 0.4900)

        # Quote 500ms later: best ask moves up to 0.5040, heavy bid size 300 vs 100 ask
        # Micro-price = (300*0.5040 + 100*0.4840) / 400 = (151.2 + 48.4) / 400 = 0.4990
        # Drift = 0.4990 - 0.4900 = +0.0090 >= 0.0030
        self.guard.record_quote(self.token_no, best_bid=0.4840, bid_size=300.0, best_ask=0.5040, ask_size=100.0, timestamp=t0)
        drift = self.guard.compute_micro_drift(self.token_no, window_ms=500.0)
        self.assertGreaterEqual(drift, 0.0030)

    def test_trade_velocity_surge(self):
        t0 = time.time()
        # Seed 60s baseline with 60 shares (1 share/sec)
        for i in range(60, 5, -5):
            self.guard.record_trade(self.token_no, price=0.49, size=5.0, timestamp=t0 - i)

        # Recent 500ms volume burst: 10 shares in 500ms (20 shares/sec)
        # Surge ratio = 20.0 / 1.0 = 20.0 >= 4.5
        self.guard.record_trade(self.token_no, price=0.495, size=10.0, timestamp=t0 - 0.1)
        surge = self.guard.compute_trade_velocity_surge(self.token_no, short_ms=500.0, baseline_sec=60.0)
        self.assertGreaterEqual(surge, 4.5)

    def test_check_toxicity_evasion_depth_collapse(self):
        t0 = time.time()
        # Initial depth 100 shares
        self.guard.record_quote(self.token_no, best_bid=0.48, bid_size=100.0, best_ask=0.49, ask_size=100.0, timestamp=t0 - 0.6)
        # Depth collapses by 60% down to 40 shares (<= 0.50 ratio)
        self.guard.record_quote(self.token_no, best_bid=0.48, bid_size=100.0, best_ask=0.49, ask_size=40.0, timestamp=t0)

        evade, reason, metrics = self.guard.check_toxicity_evasion(
            token_maker=self.token_yes,
            token_taker=self.token_no,
            maker_price=0.48,
            initial_taker_depth=100.0,
            initial_taker_price=0.49,
            fee_rate=0.0035,
        )
        self.assertTrue(evade)
        self.assertEqual(reason, "LEG2_DEPTH_COLLAPSE")
        self.assertLessEqual(metrics["depth_ratio"], 0.50)

    def test_check_toxicity_evasion_parity_evaporation(self):
        t0 = time.time()
        # Maker price 0.50, taker ask rises to 0.4960 -> Cost = 0.50 + 0.4960*(1.0035) = 0.9977 >= 0.9950
        self.guard.record_quote(self.token_no, best_bid=0.49, bid_size=100.0, best_ask=0.4960, ask_size=100.0, timestamp=t0)

        evade, reason, metrics = self.guard.check_toxicity_evasion(
            token_maker=self.token_yes,
            token_taker=self.token_no,
            maker_price=0.50,
            initial_taker_depth=100.0,
            initial_taker_price=0.49,
            fee_rate=0.0035,
        )
        self.assertTrue(evade)
        self.assertEqual(reason, "PARITY_EDGE_EVAPORATED")
        self.assertGreaterEqual(metrics["parity_cost"], 0.9950)

    def test_dynamic_hedge_router_elastic_price(self):
        # maker_price=0.48, fee_rate=0.0035, buffer=0.0005, tick=0.001
        # raw = (1.0 - 0.48) / 1.0035 - 0.0005 = 0.52 / 1.0035 - 0.0005 = 0.51818 - 0.0005 = 0.51768 -> floor = 0.517
        elastic_price = DynamicHedgeRouter.calculate_elastic_taker_price(
            maker_price=0.48,
            fee_rate=0.0035,
            breakeven_buffer=0.0005,
            tick_size=0.001,
        )
        self.assertEqual(elastic_price, 0.517)
        # Ensure total cost with fees guarantees non-negative profit:
        total_cost = 0.48 + (elastic_price * 1.0035)
        self.assertLess(total_cost, 1.0000)

    def test_dynamic_hedge_router_vwap_sweep(self):
        asks = [
            {"price": 0.50, "size": 10.0},
            {"price": 0.51, "size": 20.0},
        ]
        # Sweep 15 shares: 10 @ 0.50 + 5 @ 0.51 = 5.0 + 2.55 = 7.55 / 15 = 0.5033
        can_fill, vwap, total_cost = DynamicHedgeRouter.calculate_vwap_sweep(
            asks=asks,
            required_size=15.0,
            max_acceptable_cost=0.52,
            fee_rate=0.0,
        )
        self.assertTrue(can_fill)
        self.assertAlmostEqual(vwap, 0.5033, places=3)
        self.assertAlmostEqual(total_cost, 7.55, places=2)

    def test_maker_taker_toxicity_evasion_aborts_with_zero_loss(self):
        mock_client = MagicMock()
        mock_dash = MagicMock()
        executor = MakerTakerExecutor(mock_client, mock_dash, microstructure_guard=self.guard)

        # Seed toxic depth collapse (100 -> 30)
        t0 = time.time()
        self.guard.record_quote(self.token_no, best_bid=0.48, bid_size=100.0, best_ask=0.49, ask_size=100.0, timestamp=t0 - 0.6)
        self.guard.record_quote(self.token_no, best_bid=0.48, bid_size=100.0, best_ask=0.49, ask_size=30.0, timestamp=t0)

        # Mock order posting
        mock_client.create_order.return_value = MagicMock()
        mock_client.post_orders.return_value = [{"orderID": "maker_order_toxic_001"}]
        def _get_order_side_effect(oid):
            if mock_client.cancel_orders.called or mock_client.cancel.called:
                return {"status": "CANCELED", "size_matched": "0.0"}
            return {"status": "LIVE", "size_matched": "0.0"}

        mock_client.get_order.side_effect = _get_order_side_effect

        ok, action, details = executor.execute_maker_taker_arbitrage(
            token_maker=self.token_yes,
            maker_price=0.48,
            token_taker=self.token_no,
            taker_price=0.49,
            size=10.0,
            timeout_seconds=2.0,
            initial_taker_depth=100.0,
        )

        self.assertFalse(ok)
        self.assertEqual(action, "TOXICITY_EVASION_CANCEL")
        self.assertEqual(details["realized_loss"], 0.0)
        self.assertEqual(details["reason"], "LEG2_DEPTH_COLLAPSE")
        mock_client.cancel_orders.assert_called_with(["maker_order_toxic_001"])


if __name__ == "__main__":
    unittest.main()
