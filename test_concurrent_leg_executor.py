"""
Unit and Integration Test Suite for Simultaneous Dual-Leg Batch Execution Engine.

Verifies:
1. Full success: Both YES and NO legs match simultaneously in single HTTP batch.
2. Full kill zero-loss: CLOB kills both simultaneous FOK legs with $0.00 loss.
3. Asymmetric fill with Stage 1 micro-hedge recovery: YES fills, NO rejected, immediate FOK taker sweeps missing leg.
4. Asymmetric fill with Stage 2 safe rollback: YES fills, NO rejected, hedge unavailable, safely unwinds via RollbackProtector without loss.
5. Minimum size constraint: Orders < 5.0 shares rejected immediately.
6. Insufficient collateral: Rejects execution if available cash < required collateral.
7. Tick size alignment: Correct ceil alignment across ticks.
8. Edge compression: Rejects if Ask_YES + Ask_NO >= 1.00 - min_edge.
9. Delayed sequencer polling: Confirms fill when sequencer returns status="delayed".
"""

import unittest
from unittest.mock import MagicMock, patch, call
import logging

from concurrent_leg_executor import ConcurrentLegExecutor, OrderType, PostOrdersV2Args

logging.getLogger("ConcurrentLegExecutor").setLevel(logging.CRITICAL)


class TestConcurrentLegExecutor(unittest.TestCase):
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

        self.token_yes = "0xTOKEN_YES_AAA"
        self.token_no = "0xTOKEN_NO_BBB"
        self.ask_yes = 0.48
        self.ask_no = 0.49
        self.shares = 10.0

    def test_ceil_tick_alignment(self):
        self.assertEqual(self.executor._ceil_to_tick(0.4800, 0.001), 0.48)
        self.assertEqual(self.executor._ceil_to_tick(0.4801, 0.001), 0.481)
        self.assertEqual(self.executor._ceil_to_tick(0.4800001, 0.001), 0.481)
        self.assertEqual(self.executor._ceil_to_tick(0.555, 0.01), 0.56)

    def test_minimum_size_validation(self):
        ok, action, details = self.executor.execute_simultaneous_batch(
            token_yes=self.token_yes,
            ask_yes=self.ask_yes,
            token_no=self.token_no,
            ask_no=self.ask_no,
            shares=4.5,
        )
        self.assertFalse(ok)
        self.assertEqual(action, "BELOW_MINIMUM_SIZE")
        self.assertEqual(details["shares"], 4.5)
        self.mock_client.create_order.assert_not_called()
        self.mock_client.post_orders.assert_not_called()

    def test_insufficient_collateral(self):
        # 10 shares @ (0.48 + 0.49) = $9.70 + fees + $0.10 buffer > $9.80
        ok, action, details = self.executor.execute_simultaneous_batch(
            token_yes=self.token_yes,
            ask_yes=self.ask_yes,
            token_no=self.token_no,
            ask_no=self.ask_no,
            shares=10.0,
            available_cash=5.00,
        )
        self.assertFalse(ok)
        self.assertEqual(action, "INSUFFICIENT_COLLATERAL")
        self.assertGreater(details["required"], 5.00)
        self.mock_client.create_order.assert_not_called()

    def test_edge_compressed_rejection(self):
        # 0.50 + 0.50 = 1.00 >= 1.00 - 0.005 = 0.995
        ok, action, details = self.executor.execute_simultaneous_batch(
            token_yes=self.token_yes,
            ask_yes=0.50,
            token_no=self.token_no,
            ask_no=0.50,
            shares=10.0,
            min_edge=0.005,
        )
        self.assertFalse(ok)
        self.assertEqual(action, "EDGE_COMPRESSED")
        self.mock_client.create_order.assert_not_called()

    def test_execute_simultaneous_batch_full_success(self):
        order_y = MagicMock()
        order_n = MagicMock()
        self.mock_client.create_order.side_effect = [order_y, order_n]

        self.mock_client.post_orders.return_value = [
            {"orderID": "y_001", "status": "MATCHED", "takingAmount": "10.0"},
            {"orderID": "n_001", "status": "MATCHED", "takingAmount": "10.0"},
        ]

        ok, action, details = self.executor.execute_simultaneous_batch(
            token_yes=self.token_yes,
            ask_yes=self.ask_yes,
            token_no=self.token_no,
            ask_no=self.ask_no,
            shares=self.shares,
            available_cash=100.0,
        )

        self.assertTrue(ok)
        self.assertEqual(action, "DUAL_MATCH_SECURED")
        self.assertEqual(details["shares"], 10.0)
        self.assertEqual(details["total_cost"], 9.70)
        self.assertEqual(details["profit"], 0.30)

        # Verify posted payload: exactly 1 batch call containing 2 FOK orders
        self.assertEqual(self.mock_client.post_orders.call_count, 1)
        batch = self.mock_client.post_orders.call_args[0][0]
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch[0].orderType, OrderType.FOK)
        self.assertEqual(batch[1].orderType, OrderType.FOK)

    def test_execute_simultaneous_batch_clean_abort_zero_loss(self):
        order_y = MagicMock()
        order_n = MagicMock()
        self.mock_client.create_order.side_effect = [order_y, order_n]

        self.mock_client.post_orders.return_value = [
            {"orderID": "y_002", "status": "KILLED", "takingAmount": "0.0"},
            {"orderID": "n_002", "status": "KILLED", "takingAmount": "0.0"},
        ]

        ok, action, details = self.executor.execute_simultaneous_batch(
            token_yes=self.token_yes,
            ask_yes=self.ask_yes,
            token_no=self.token_no,
            ask_no=self.ask_no,
            shares=self.shares,
            available_cash=100.0,
        )

        self.assertFalse(ok)
        self.assertEqual(action, "DUAL_KILLED_ZERO_LOSS")
        self.mock_rollback.safe_unwind_or_limit_exit.assert_not_called()

    def test_execute_simultaneous_batch_asymmetric_stage1_hedge_recovery(self):
        order_y = MagicMock()
        order_n = MagicMock()
        order_hedge = MagicMock()
        self.mock_client.create_order.side_effect = [order_y, order_n, order_hedge]

        # First post_orders: YES fills, NO rejected
        # Second post_orders: Stage 1 micro-hedge sweep FOK matches
        self.mock_client.post_orders.side_effect = [
            [
                {"orderID": "y_003", "status": "MATCHED", "takingAmount": "10.0"},
                {"orderID": "n_003", "errorMsg": "Order killed by book", "takingAmount": "0.0"},
            ],
            [
                {"orderID": "h_001", "status": "MATCHED", "takingAmount": "10.0"},
            ],
        ]

        # RollbackProtector order book query returns viable ask for missing NO token
        self.mock_rollback.fetch_order_book.return_value = {
            "asks": [{"price": 0.495, "size": 25.0}]
        }
        self.mock_rollback.extract_best_ask.return_value = 0.495

        ok, action, details = self.executor.execute_simultaneous_batch(
            token_yes=self.token_yes,
            ask_yes=self.ask_yes,
            token_no=self.token_no,
            ask_no=self.ask_no,
            shares=self.shares,
            available_cash=100.0,
            max_hedge_tolerance=0.010,
        )

        self.assertTrue(ok)
        self.assertEqual(action, "HEDGE_RECOVERED")
        self.assertEqual(details["shares"], 10.0)
        self.assertAlmostEqual(details["total_cost"], 10.0 * (0.48 + 0.495), places=4)
        self.assertGreater(details["profit"], 0.0)

        # Verify Stage 2 was NOT needed
        self.mock_rollback.safe_unwind_or_limit_exit.assert_not_called()

    def test_execute_simultaneous_batch_asymmetric_stage2_safe_rollback(self):
        order_y = MagicMock()
        order_n = MagicMock()
        self.mock_client.create_order.side_effect = [order_y, order_n]

        # YES fills, NO rejected
        self.mock_client.post_orders.return_value = [
            {"orderID": "y_004", "status": "MATCHED", "takingAmount": "10.0"},
            {"orderID": "n_004", "errorMsg": "Order killed by book", "takingAmount": "0.0"},
        ]

        # Order book has NO viable ask (ask is 0.65 > max hedge acceptable)
        self.mock_rollback.fetch_order_book.return_value = {
            "asks": [{"price": 0.65, "size": 5.0}]
        }
        self.mock_rollback.extract_best_ask.return_value = 0.65

        # Mock RollbackProtector safe unwind
        self.mock_rollback.safe_unwind_or_limit_exit.return_value = (
            True,
            "LIMIT_ORDER_PLACED",
            {"order_id": "unwind_sell_001", "price": 0.48, "realized_loss": 0.0},
        )

        ok, action, details = self.executor.execute_simultaneous_batch(
            token_yes=self.token_yes,
            ask_yes=self.ask_yes,
            token_no=self.token_no,
            ask_no=self.ask_no,
            shares=self.shares,
            available_cash=100.0,
            max_hedge_tolerance=0.005,
        )

        self.assertFalse(ok)
        self.assertEqual(action, "ROLLBACK_UNWOUND")
        self.assertEqual(details["filled_token"], self.token_yes)
        self.assertEqual(details["filled_size"], 10.0)
        self.assertEqual(details["rollback_action"], "LIMIT_ORDER_PLACED")

        # Verify order was registered with OrderReaper
        self.mock_reaper.register_order.assert_called_once_with(
            order_id="unwind_sell_001",
            token_id=self.token_yes,
            side="SELL",
            size=10.0,
            price=0.48,
            is_passive_unwind=True,
        )

    def test_delayed_sequencer_polling_success(self):
        order_y = MagicMock()
        order_n = MagicMock()
        self.mock_client.create_order.side_effect = [order_y, order_n]

        # Initial post returns status="delayed"
        self.mock_client.post_orders.return_value = [
            {"orderID": "y_delayed", "status": "delayed", "takingAmount": "0.0"},
            {"orderID": "n_delayed", "status": "delayed", "takingAmount": "0.0"},
        ]

        # Polling get_order confirms both filled
        self.mock_client.get_order.side_effect = [
            {"status": "MATCHED", "size_matched": "10.0"},
            {"status": "MATCHED", "size_matched": "10.0"},
        ]

        ok, action, details = self.executor.execute_simultaneous_batch(
            token_yes=self.token_yes,
            ask_yes=self.ask_yes,
            token_no=self.token_no,
            ask_no=self.ask_no,
            shares=self.shares,
            available_cash=100.0,
        )

        self.assertTrue(ok)
        self.assertEqual(action, "DUAL_MATCH_SECURED")
        self.assertEqual(details["shares"], 10.0)


if __name__ == "__main__":
    unittest.main()
