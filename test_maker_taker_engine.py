"""
Unit and Integration Test Suite for Maker-Taker Asymmetric Execution Engine.

Verifies:
1. Happy path: Maker leg fills, taker leg fills -> returns True with complete execution.
2. Maker timeout: Maker leg does not fill within timeout -> cancels order, returns False with ZERO loss.
3. Taker failure recovery: Maker leg fills, taker leg fails -> handles rollback safely without loss.
4. Clean interface compatibility with py_clob_client_v2.
"""

import logging
import json
import unittest
from unittest.mock import MagicMock, call, patch

from maker_taker_engine import MakerTakerExecutor, OrderArgsV2, OrderType, PostOrdersV2Args

logging.getLogger("MakerTakerEngine").setLevel(logging.CRITICAL)


class TestMakerTakerEngine(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_dash = MagicMock()
        self.executor = MakerTakerExecutor(self.mock_client, self.mock_dash)

        self.token_maker = "0xTOKEN_YES_1111"
        self.token_taker = "0xTOKEN_NO_2222"
        self.maker_price = 0.48
        self.taker_price = 0.49
        self.size = 10.0

        self.urlopen_patcher = patch("urllib.request.urlopen", side_effect=Exception("Offline test"))
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)

    def test_happy_path_full_execution(self):
        order_maker_obj = MagicMock()
        order_taker_obj = MagicMock()
        self.mock_client.create_order.side_effect = [order_maker_obj, order_taker_obj]

        self.mock_client.post_orders.side_effect = [
            [{"orderID": "maker_order_001"}],
            [{"orderID": "taker_order_002", "takingAmount": "10.0", "status": "matched"}],
        ]

        self.mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "10.0",
        }

        success, reason, meta = self.executor.execute_maker_taker_arbitrage(
            token_maker=self.token_maker,
            maker_price=self.maker_price,
            token_taker=self.token_taker,
            taker_price=self.taker_price,
            size=self.size,
            timeout_seconds=2.0,
        )

        self.assertTrue(success)
        self.assertEqual(reason, "SUCCESS")
        self.assertEqual(meta["hedged_size"], 10.0)
        self.assertEqual(meta["maker_price"], 0.48)
        self.assertEqual(meta["taker_price"], 0.49)
        self.assertEqual(meta["total_cost"], 0.97)
        self.assertEqual(meta["profit_per_share"], 0.03)

        posted_calls = self.mock_client.post_orders.call_args_list
        self.assertEqual(posted_calls[0][0][0][0].orderType, OrderType.GTC)
        self.assertEqual(posted_calls[1][0][0][0].orderType, OrderType.FOK)

        self.mock_dash.add_activity_log.assert_called()

    def test_maker_timeout_cancels_with_zero_loss(self):
        order_maker_obj = MagicMock()
        self.mock_client.create_order.return_value = order_maker_obj
        self.mock_client.post_orders.return_value = [{"orderID": "maker_order_timeout_001"}]

        def _get_order_side_effect(oid):
            if self.mock_client.cancel_orders.called or self.mock_client.cancel.called:
                return {"status": "CANCELED", "size_matched": "0.0"}
            return {"status": "LIVE", "size_matched": "0.0"}

        self.mock_client.get_order.side_effect = _get_order_side_effect

        success, reason, meta = self.executor.execute_maker_taker_arbitrage(
            token_maker=self.token_maker,
            maker_price=self.maker_price,
            token_taker=self.token_taker,
            taker_price=self.taker_price,
            size=self.size,
            timeout_seconds=0.6,
        )

        self.assertFalse(success)
        self.assertEqual(reason, "MAKER_TIMEOUT_ZERO_LOSS")
        self.assertEqual(meta["order_id_maker"], "maker_order_timeout_001")
        self.assertEqual(meta["maker_price"], 0.48)

        self.mock_client.cancel_orders.assert_called_once_with(["maker_order_timeout_001"])
        self.assertEqual(self.mock_client.post_orders.call_count, 1)

    def test_taker_failure_safe_rollback_limit_placed(self):
        order_maker_obj = MagicMock()
        order_taker_obj = MagicMock()
        order_rollback_obj = MagicMock()
        self.mock_client.create_order.side_effect = [
            order_maker_obj,
            order_taker_obj,
            order_rollback_obj,
        ]

        self.mock_client.post_orders.side_effect = [
            [{"orderID": "maker_order_001"}],
            [{"errorMsg": "Order killed by book: Insufficient liquidity", "orderID": "taker_001"}],
            [{"orderID": "rollback_order_001"}],
        ]

        self.mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "10.0",
        }

        success, reason, meta = self.executor.execute_maker_taker_arbitrage(
            token_maker=self.token_maker,
            maker_price=self.maker_price,
            token_taker=self.token_taker,
            taker_price=self.taker_price,
            size=self.size,
            timeout_seconds=1.0,
            rollback_mode="LIMIT_SELL",
        )

        self.assertFalse(success)
        self.assertEqual(reason, "TAKER_FAILED_ROLLBACK_LIMIT_PLACED")
        self.assertEqual(meta["maker_price"], 0.48)
        self.assertEqual(meta["size"], 10.0)
        self.assertEqual(meta["rollback_order_id"], "rollback_order_001")

        posted_calls = self.mock_client.post_orders.call_args_list
        self.assertEqual(len(posted_calls), 3)
        self.assertEqual(posted_calls[0][0][0][0].orderType, OrderType.GTC)
        self.assertEqual(posted_calls[1][0][0][0].orderType, OrderType.FOK)
        self.assertEqual(posted_calls[2][0][0][0].orderType, OrderType.GTC)

        rollback_create_args = self.mock_client.create_order.call_args_list[2][0][0]
        self.assertEqual(rollback_create_args.side, "SELL")
        self.assertEqual(rollback_create_args.price, 0.48)
        self.assertEqual(rollback_create_args.size, 10.0)
        self.assertEqual(rollback_create_args.token_id, self.token_maker)

    def test_maker_post_rejection_returns_error(self):
        self.mock_client.create_order.return_value = MagicMock()
        self.mock_client.post_orders.return_value = [{"errorMsg": "Insufficient collateral"}]

        success, reason, meta = self.executor.execute_maker_taker_arbitrage(
            token_maker=self.token_maker,
            maker_price=self.maker_price,
            token_taker=self.token_taker,
            taker_price=self.taker_price,
            size=self.size,
        )

        self.assertFalse(success)
        self.assertEqual(reason, "MAKER_POST_FAILED")
        self.mock_client.get_order.assert_not_called()

    def test_maker_cancelled_externally(self):
        self.mock_client.create_order.return_value = MagicMock()
        self.mock_client.post_orders.return_value = [{"orderID": "maker_ext_001"}]
        self.mock_client.get_order.return_value = {"status": "CANCELED"}

        success, reason, meta = self.executor.execute_maker_taker_arbitrage(
            token_maker=self.token_maker,
            maker_price=self.maker_price,
            token_taker=self.token_taker,
            taker_price=self.taker_price,
            size=self.size,
            timeout_seconds=2.0,
        )

        self.assertFalse(success)
        self.assertEqual(reason, "MAKER_CANCELLED_EXTERNALLY")
        self.assertEqual(self.mock_client.post_orders.call_count, 1)

    def test_delayed_sequencer_polling_resolution(self):
        order_maker_obj = MagicMock()
        order_taker_obj = MagicMock()
        self.mock_client.create_order.side_effect = [order_maker_obj, order_taker_obj]
        self.mock_client.post_orders.side_effect = [
            [{"orderID": "maker_delayed_001", "status": "delayed"}],
            [{"orderID": "taker_001", "takingAmount": "10.0", "status": "matched"}],
        ]

        self.mock_client.get_order.side_effect = [
            {"status": "delayed", "size_matched": "0.0"},
            {"status": "MATCHED", "size_matched": "10.0"},
        ]

        success, reason, meta = self.executor.execute_maker_taker_arbitrage(
            token_maker=self.token_maker,
            maker_price=self.maker_price,
            token_taker=self.token_taker,
            taker_price=self.taker_price,
            size=self.size,
            timeout_seconds=2.0,
        )

        self.assertTrue(success)
        self.assertEqual(reason, "SUCCESS")

    def test_cancel_order_with_cancel_orders(self):
        client = MagicMock()
        executor = MakerTakerExecutor(client)
        result = executor.cancel_order("order_001")
        self.assertTrue(result)
        client.cancel_orders.assert_called_once_with(["order_001"])

    def test_cancel_order_fallback_to_cancel(self):
        client = MagicMock(spec=["cancel"])
        executor = MakerTakerExecutor(client)
        result = executor.cancel_order("order_002")
        self.assertTrue(result)
        client.cancel.assert_called_once_with("order_002")

    def test_cancel_order_fallback_to_cancel_order(self):
        client = MagicMock(spec=["cancel_order"])
        executor = MakerTakerExecutor(client)
        result = executor.cancel_order("order_003")
        self.assertTrue(result)
        client.cancel_order.assert_called_once_with("order_003")

    def test_cancel_order_no_supported_method(self):
        client = MagicMock(spec=[])
        executor = MakerTakerExecutor(client)
        result = executor.cancel_order("order_004")
        self.assertFalse(result)

    def test_taker_leg_updates_to_live_ask(self):
        order_maker_obj = MagicMock()
        order_taker_obj = MagicMock()
        self.mock_client.create_order.side_effect = [order_maker_obj, order_taker_obj]
        self.mock_client.post_orders.side_effect = [
            [{"orderID": "maker_order_001"}],
            [{"orderID": "taker_order_002", "takingAmount": "10.0", "status": "matched"}],
        ]
        self.mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "10.0",
        }
        in_memory_books = {
            self.token_taker: {
                "asks": [{"price": "0.495", "size": "50.0"}]
            }
        }

        success, reason, meta = self.executor.execute_maker_taker_arbitrage(
            token_maker=self.token_maker,
            maker_price=self.maker_price,   # 0.48
            token_taker=self.token_taker,
            taker_price=self.taker_price,   # 0.49
            size=self.size,
            timeout_seconds=2.0,
            market_books=in_memory_books,
        )

        self.assertTrue(success)
        self.assertEqual(meta["taker_price"], 0.495)
        self.assertEqual(meta["total_cost"], 0.975)
        self.assertEqual(meta["profit_per_share"], 0.025)

        posted_taker_args = self.mock_client.create_order.call_args_list[1][0][0]
        self.assertEqual(posted_taker_args.price, 0.495)

    def test_taker_failure_immediate_unwind_executed(self):
        """Verify that by default, Leg 2 failure executes immediate market exit on Leg 1."""
        order_maker_obj = MagicMock()
        order_taker_obj = MagicMock()
        order_unwind_obj = MagicMock()
        self.mock_client.create_order.side_effect = [
            order_maker_obj,
            order_taker_obj,
            order_unwind_obj,
        ]

        self.mock_client.post_orders.side_effect = [
            [{"orderID": "maker_order_001"}],
            [{"errorMsg": "Order killed by book: Insufficient liquidity", "orderID": "taker_001"}],
            [{"orderID": "unwind_order_001"}],
        ]

        self.mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "10.0",
        }
        self.mock_client.get_order_book.return_value = {
            "bids": [{"price": "0.470", "size": "100.0"}]
        }

        success, reason, meta = self.executor.execute_maker_taker_arbitrage(
            token_maker=self.token_maker,
            maker_price=self.maker_price,   # 0.48
            token_taker=self.token_taker,
            taker_price=self.taker_price,   # 0.49
            size=self.size,
            timeout_seconds=1.0,
            rollback_mode="IMMEDIATE_EXIT",
        )

        self.assertFalse(success)
        self.assertEqual(reason, "TAKER_FAILED_UNWOUND")
        self.assertTrue(meta["unwind_ok"])
        self.assertEqual(meta["unwind_action"], "MARKET_EXIT_SAFE")
        self.assertAlmostEqual(meta["realized_loss"], 10.0 * (0.48 - 0.47), places=4)

    def test_dynamic_tick_size_and_neg_risk_resolution(self):
        order_maker_obj = MagicMock()
        order_taker_obj = MagicMock()
        self.mock_client.create_order.side_effect = [order_maker_obj, order_taker_obj]
        self.mock_client.post_orders.side_effect = [
            [{"orderID": "maker_order_001"}],
            [{"orderID": "taker_order_002", "takingAmount": "10.0", "status": "matched"}],
        ]
        self.mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "10.0",
        }
        self.mock_client.get_tick_size.return_value = "0.01"
        self.mock_client.get_neg_risk.return_value = True

        success, reason, meta = self.executor.execute_maker_taker_arbitrage(
            token_maker=self.token_maker,
            maker_price=0.485,
            token_taker=self.token_taker,
            taker_price=0.495,
            size=10.0,
            timeout_seconds=2.0,
        )

        self.assertTrue(success)
        self.assertEqual(reason, "SUCCESS")
        call_args_list = self.mock_client.create_order.call_args_list
        self.assertEqual(len(call_args_list), 2)
        opts_maker = call_args_list[0][1].get("options")
        self.assertEqual(str(opts_maker.tick_size), "0.01")
        self.assertTrue(opts_maker.neg_risk)


if __name__ == "__main__":
    unittest.main()


