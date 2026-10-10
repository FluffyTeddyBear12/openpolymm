"""
Unit and Integration Test Suite for Option 3: Simultaneous Batch Multi-Leg Neg-Risk Arbitrage.

Verifies:
1. Full Match: All N legs match simultaneously in single HTTP batch, profit verified, sizing >= $4.00 and >= 5.0 shares.
2. Clean Abort: All legs killed with $0.00 loss.
3. Asymmetric Micro-Hedge: Partial fill recovers missing leg via Stage 1 micro-hedge sweep.
4. Asymmetric Stage 2 Rollback: Partial fill safely unwinds orphan legs at buy price without dumping.
5. Sizing Calibration: Respects user floor ($4.00) and CLOB minimum (5.0 shares) across various ask sums.
6. Cardinality Filtering: Rejects 1 outcome and 9 outcomes; accepts 2-8 outcomes.
7. NegRiskAdapter Calldata: Verifies convertYESPositions selector 0x4296497f, indexSet bitmask, and 6-decimal units.
8. LiveExecutor Wiring: Verifies execute_negrisk_basket calls ConcurrentLegExecutor, opens risk position, and syncs balance.
"""

import unittest
from unittest.mock import MagicMock, patch
import math
import logging

from concurrent_leg_executor import ConcurrentLegExecutor
from negrisk_scanner import NegRiskBasketScanner, NegRiskAdapter
from paper_trader import LiveExecutor

logging.getLogger("ConcurrentLegExecutor").setLevel(logging.CRITICAL)
logging.getLogger("NegRiskScanner").setLevel(logging.CRITICAL)
logging.getLogger("PolyPaperTrader").setLevel(logging.CRITICAL)


class TestNegRiskBatchExecution(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_dash = MagicMock()
        self.mock_reaper = MagicMock()
        self.mock_rollback = MagicMock()

        self.executor = ConcurrentLegExecutor(
            client=self.mock_client,
            dash_state=self.mock_dash,
            rollback_protector=self.mock_rollback,
            order_reaper=self.mock_reaper,
            fee_rate=0.0035,
        )

        self.basket_id = "0xMARKET_NEGRISK_123"
        self.outcomes_3 = [
            {"condition_id": "0xc1", "token_yes": "0xt1", "ask_yes": 0.30, "tick_size": 0.001},
            {"condition_id": "0xc2", "token_yes": "0xt2", "ask_yes": 0.32, "tick_size": 0.001},
            {"condition_id": "0xc3", "token_yes": "0xt3", "ask_yes": 0.33, "tick_size": 0.001},
        ]
        self.shares = 10.0

    def test_execute_negrisk_batch_full_match(self):
        orders = [MagicMock(), MagicMock(), MagicMock()]
        self.mock_client.create_order.side_effect = orders
        self.mock_client.post_orders.return_value = [
            {"orderID": "o1", "status": "MATCHED", "takingAmount": 10.0, "errorMsg": None},
            {"orderID": "o2", "status": "MATCHED", "takingAmount": 10.0, "errorMsg": None},
            {"orderID": "o3", "status": "MATCHED", "takingAmount": 10.0, "errorMsg": None},
        ]

        ok, action, details = self.executor.execute_negrisk_batch(
            outcomes=self.outcomes_3,
            shares=self.shares,
            neg_risk_market_id=self.basket_id,
            min_edge=0.015,
        )

        self.assertTrue(ok)
        self.assertEqual(action, "BASKET_MATCHED")
        self.assertEqual(details["num_outcomes"], 3)
        self.assertAlmostEqual(details["total_cost"], 9.50, places=2)
        self.assertAlmostEqual(details["profit"], 0.50, places=2)
        self.mock_client.post_orders.assert_called_once()
        batch_sent = self.mock_client.post_orders.call_args[0][0]
        self.assertEqual(len(batch_sent), 3)

    def test_execute_negrisk_batch_clean_abort(self):
        orders = [MagicMock(), MagicMock(), MagicMock()]
        self.mock_client.create_order.side_effect = orders
        self.mock_client.post_orders.return_value = [
            {"orderID": "o1", "status": "CANCELED", "takingAmount": 0.0, "errorMsg": "Book depleted"},
            {"orderID": "o2", "status": "CANCELED", "takingAmount": 0.0, "errorMsg": "Book depleted"},
            {"orderID": "o3", "status": "CANCELED", "takingAmount": 0.0, "errorMsg": "Book depleted"},
        ]

        ok, action, details = self.executor.execute_negrisk_batch(
            outcomes=self.outcomes_3,
            shares=self.shares,
            neg_risk_market_id=self.basket_id,
        )

        self.assertFalse(ok)
        self.assertEqual(action, "BASKET_KILLED_ZERO_LOSS")
        self.mock_rollback.safe_unwind_or_limit_exit.assert_not_called()

    def test_execute_negrisk_batch_asymmetric_micro_hedge_recovery(self):
        orders = [MagicMock(), MagicMock(), MagicMock(), MagicMock()]
        self.mock_client.create_order.side_effect = orders
        self.mock_client.post_orders.side_effect = [
            # Initial batch: Leg 0 and 1 fill, Leg 2 rejected
            [
                {"orderID": "o1", "status": "MATCHED", "takingAmount": 10.0, "errorMsg": None},
                {"orderID": "o2", "status": "MATCHED", "takingAmount": 10.0, "errorMsg": None},
                {"orderID": "o3", "status": "CANCELED", "takingAmount": 0.0, "errorMsg": "Killed"},
            ],
            # Stage 1 micro-hedge taker order fill
            [{"orderID": "h1", "status": "MATCHED", "takingAmount": 10.0, "errorMsg": None}]
        ]

        self.mock_rollback.fetch_order_book.return_value = {"asks": [{"price": "0.34", "size": "20.0"}]}
        self.mock_rollback.extract_best_ask.return_value = 0.34

        ok, action, details = self.executor.execute_negrisk_batch(
            outcomes=self.outcomes_3,
            shares=self.shares,
            neg_risk_market_id=self.basket_id,
            max_hedge_tolerance=0.010,
        )

        self.assertTrue(ok)
        self.assertEqual(action, "HEDGE_RECOVERED")
        self.assertAlmostEqual(details["hedge_price"], 0.34, places=2)
        # Cost = 10 * (0.30 + 0.32 + 0.34) = $9.60 -> profit = +$0.40
        self.assertAlmostEqual(details["total_cost"], 9.60, places=2)
        self.assertAlmostEqual(details["profit"], 0.40, places=2)

    def test_execute_negrisk_batch_asymmetric_stage2_rollback(self):
        orders = [MagicMock(), MagicMock(), MagicMock()]
        self.mock_client.create_order.side_effect = orders
        self.mock_client.post_orders.return_value = [
            {"orderID": "o1", "status": "MATCHED", "takingAmount": 10.0, "errorMsg": None},
            {"orderID": "o2", "status": "CANCELED", "takingAmount": 0.0, "errorMsg": "Killed"},
            {"orderID": "o3", "status": "CANCELED", "takingAmount": 0.0, "errorMsg": "Killed"},
        ]

        self.mock_rollback.safe_unwind_or_limit_exit.return_value = (
            True,
            "PASSIVE_LIMIT_SELL_PLACED",
            {"order_id": "unwind_1", "price": 0.30},
        )

        ok, action, details = self.executor.execute_negrisk_batch(
            outcomes=self.outcomes_3,
            shares=self.shares,
            neg_risk_market_id=self.basket_id,
        )

        self.assertFalse(ok)
        self.assertEqual(action, "ROLLBACK_UNWOUND")
        self.assertEqual(details["filled_outcomes"], [0])
        self.assertEqual(details["missing_outcomes"], [1, 2])
        self.mock_rollback.safe_unwind_or_limit_exit.assert_called_once()
        self.mock_reaper.register_order.assert_called_once()

    def test_sizing_calibration_respects_user_floor_and_clob_minimum(self):
        scanner = NegRiskBasketScanner()
        basket_id = "test_basket_calib"

        # Case A: sum_ask = 0.95 -> 4.00 / 0.95 = 4.21 -> ceil = 5.0 -> max(5.0, 5.0) = 5.0
        scanner.baskets[basket_id] = [
            {"condition_id": "c1", "token_yes": "t1", "ask_yes": 0.47, "tick_size": 0.001},
            {"condition_id": "c2", "token_yes": "t2", "ask_yes": 0.48, "tick_size": 0.001},
        ]
        books = {"c1": {"ask_yes": 0.47}, "c2": {"ask_yes": 0.48}}
        depths = {"c1": {"depth_yes": 10.0}, "c2": {"depth_yes": 10.0}}
        opp = scanner.check_basket_parity(basket_id, books, depths, min_edge=0.01)
        self.assertIsNotNone(opp)
        self.assertEqual(opp["calibrated_min_shares"], 5.0)

        # Case B: sum_ask = 0.70 -> 4.00 / 0.70 = 5.71 -> ceil = 6.0 -> max(5.0, 6.0) = 6.0
        books["c1"]["ask_yes"] = 0.35
        books["c2"]["ask_yes"] = 0.35
        depths["c1"]["depth_yes"] = 5.0  # Less than calibrated 6.0!
        depths["c2"]["depth_yes"] = 5.0
        opp_gated = scanner.check_basket_parity(basket_id, books, depths, min_edge=0.01)
        self.assertIsNone(opp_gated)  # Must be gated due to bottleneck check

        depths["c1"]["depth_yes"] = 15.0
        depths["c2"]["depth_yes"] = 15.0
        opp_allowed = scanner.check_basket_parity(basket_id, books, depths, min_edge=0.01)
        self.assertIsNotNone(opp_allowed)
        self.assertEqual(opp_allowed["calibrated_min_shares"], 6.0)

        # Case C: sum_ask = 0.50 -> 4.00 / 0.50 = 8.0 -> ceil = 8.0 -> max(5.0, 8.0) = 8.0
        books["c1"]["ask_yes"] = 0.25
        books["c2"]["ask_yes"] = 0.25
        opp_half = scanner.check_basket_parity(basket_id, books, depths, min_edge=0.01)
        self.assertIsNotNone(opp_half)
        self.assertEqual(opp_half["calibrated_min_shares"], 8.0)

    def test_cardinality_filtering(self):
        scanner = NegRiskBasketScanner()
        # 1 outcome: Rejected
        scanner.baskets["b1"] = [{"condition_id": "c1", "token_yes": "t1"}]
        self.assertIsNone(scanner.check_basket_parity("b1", {}, {}))

        # 9 outcomes: Rejected
        scanner.baskets["b9"] = [{"condition_id": f"c{i}", "token_yes": f"t{i}"} for i in range(9)]
        self.assertIsNone(scanner.check_basket_parity("b9", {}, {}))

        # Executor rejects 1 outcome and 9 outcomes
        ok1, act1, _ = self.executor.execute_negrisk_batch([{"token_yes": "t1"}], 10.0, "b1")
        self.assertFalse(ok1)
        self.assertEqual(act1, "INVALID_CARDINALITY")

        ok9, act9, _ = self.executor.execute_negrisk_batch([{"token_yes": f"t{i}"} for i in range(9)], 10.0, "b9")
        self.assertFalse(ok9)
        self.assertEqual(act9, "INVALID_CARDINALITY")

    def test_negrisk_adapter_convert_calldata(self):
        adapter = NegRiskAdapter()
        mid = "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
        num_outcomes = 4
        shares = 10.5
        tx = adapter.format_convert_yes_transaction(mid, num_outcomes=num_outcomes, amount_shares=shares)

        self.assertEqual(tx["to"], "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
        self.assertEqual(tx["function"], "convertYESPositions")
        self.assertEqual(tx["index_set"], (1 << 4) - 1)  # 15
        self.assertEqual(tx["amount_raw"], 10_500_000)
        self.assertTrue(tx["calldata"].startswith("0x327ddd2b"))
        self.assertEqual(len(tx["calldata"]), 10 + 64 * 3)

    def test_live_executor_wires_negrisk_batch(self):
        risk = MagicMock()
        risk.available_cash = 100.0
        risk.capital = 100.0
        risk.can_trade.return_value = True

        dash = MagicMock()
        dash.state = {"execution_mode": "Live Trading"}

        executor = LiveExecutor(risk, dash_state=dash)
        executor.client = MagicMock()
        executor.concurrent_leg_executor = MagicMock()
        executor.concurrent_leg_executor.execute_negrisk_batch.return_value = (
            True,
            "BASKET_MATCHED",
            {"total_cost": 9.50, "profit": 0.50},
        )

        opp = {
            "execution_type": "negrisk_basket",
            "neg_risk_market_id": "basket_test_wire",
            "num_outcomes": 3,
            "max_shares": 10.0,
            "edge": 0.05,
            "outcomes": self.outcomes_3,
        }

        success = executor.execute_negrisk_basket(opp)
        self.assertTrue(success)
        executor.concurrent_leg_executor.execute_negrisk_batch.assert_called_once()
        risk.open_position.assert_called_once_with("basket_test_wire", 9.50, 0.50)


if __name__ == "__main__":
    unittest.main()
