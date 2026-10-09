"""
Unit and Integration Test Suite for OrderReaper & Verified Cancellation System.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from maker_taker_engine import MakerTakerExecutor
from order_reaper import OrderReaper


class TestVerifiedCancel(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_dash = MagicMock()
        self.executor = MakerTakerExecutor(client=self.mock_client, dash_state=self.mock_dash)

    def test_clean_confirmed_cancel(self):
        self.mock_client.get_order.return_value = {
            "status": "CANCELED",
            "size_matched": "0.0",
        }
        ok, status, matched = self.executor.cancel_order_verified("order_123", requested_size=10.0)
        self.assertTrue(ok)
        self.assertEqual(status, "CONFIRMED_CANCELED")
        self.assertEqual(matched, 0.0)
        self.mock_client.cancel_orders.assert_called_once_with(["order_123"])

    def test_transient_network_error_retry_success(self):
        self.mock_client.get_order.side_effect = [
            ConnectionResetError("EOF occurred in violation of protocol"),
            {"status": "CANCELED", "size_matched": "0.0"},
        ]
        ok, status, matched = self.executor.cancel_order_verified("order_retry", requested_size=10.0)
        self.assertTrue(ok)
        self.assertEqual(status, "CONFIRMED_CANCELED")
        self.assertEqual(matched, 0.0)
        self.assertGreaterEqual(self.mock_client.cancel_orders.call_count, 1)

    def test_race_condition_filled_in_flight(self):
        self.mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "10.0",
        }
        ok, status, matched = self.executor.cancel_order_verified("order_raced", requested_size=10.0)
        self.assertTrue(ok)
        self.assertEqual(status, "FILLED_IN_FLIGHT")
        self.assertEqual(matched, 10.0)

    def test_partial_fill_race_condition(self):
        self.mock_client.get_order.return_value = {
            "status": "CANCELED",
            "size_matched": "5.0",
        }
        ok, status, matched = self.executor.cancel_order_verified("order_partial", requested_size=10.0)
        self.assertTrue(ok)
        self.assertEqual(status, "PARTIALLY_FILLED_CANCELED")
        self.assertEqual(matched, 5.0)

    def test_order_not_found_404_confirmed_canceled(self):
        self.mock_client.get_order.side_effect = Exception("HTTP 404: Order not found")
        ok, status, matched = self.executor.cancel_order_verified("order_404", requested_size=10.0)
        self.assertTrue(ok)
        self.assertEqual(status, "CONFIRMED_CANCELED")
        self.assertEqual(matched, 0.0)

    def test_unconfirmed_hazard_on_persistent_timeout(self):
        self.mock_client.get_order.return_value = {
            "status": "LIVE",
            "size_matched": "0.0",
        }
        ok, status, matched = self.executor.cancel_order_verified(
            "order_hazard",
            requested_size=10.0,
            max_retries=2,
            verification_timeout=0.1,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "UNCONFIRMED_HAZARD")
        self.assertEqual(matched, 0.0)


class TestOrderReaper(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_dash = MagicMock()
        self.mock_rollback = MagicMock()
        self.reaper = OrderReaper(
            client=self.mock_client,
            poll_interval_sec=1.0,
            max_order_ttl_sec=5.0,
            dash_state=self.mock_dash,
            rollback_protector=self.mock_rollback,
        )

    def test_reaps_unknown_zombie_order(self):
        self.mock_client.get_open_orders.return_value = [
            {"id": "zombie_order_999", "asset_id": "tok_yes", "price": 0.50}
        ]
        self.mock_client.get_order.return_value = {
            "status": "CANCELED",
            "size_matched": "0.0",
        }

        reaped = self.reaper.reconcile_open_orders()
        self.assertEqual(reaped, ["zombie_order_999"])
        self.mock_client.cancel_orders.assert_called_once_with(["zombie_order_999"])
        self.mock_dash.add_activity_log.assert_called()

    def test_reaps_stale_registered_order_exceeding_ttl(self):
        self.reaper.register_order("stale_order_111", token_id="tok_1", side="BUY", size=10.0, price=0.48)
        self.reaper._active_registry["stale_order_111"]["created_at"] = time.time() - 20.0

        self.mock_client.get_open_orders.return_value = [{"id": "stale_order_111"}]
        self.mock_client.get_order.return_value = {"status": "CANCELED", "size_matched": 0.0}

        reaped = self.reaper.reconcile_open_orders()
        self.assertEqual(reaped, ["stale_order_111"])
        self.assertNotIn("stale_order_111", self.reaper._active_registry)
        self.mock_client.cancel_orders.assert_called_once_with(["stale_order_111"])

    def test_active_fresh_order_not_reaped(self):
        self.reaper.register_order("fresh_order_222", token_id="tok_1", side="BUY", size=10.0, price=0.48)
        self.mock_client.get_open_orders.return_value = [{"id": "fresh_order_222"}]

        reaped = self.reaper.reconcile_open_orders()
        self.assertEqual(reaped, [])
        self.mock_client.cancel_orders.assert_not_called()
        self.assertIn("fresh_order_222", self.reaper._active_registry)

    def test_purge_all_orders_clears_clob_and_registry(self):
        self.reaper.register_order("order_a", size=10.0)
        self.reaper.register_order("order_b", size=20.0)
        self.mock_client.get_open_orders.return_value = [{"id": "order_a"}, {"id": "order_b"}]

        res = self.reaper.purge_all_orders()
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(len(self.reaper._active_registry), 0)
        self.mock_client.cancel_all.assert_called_once()

    def test_zombie_with_partial_fill_triggers_rollback_protector(self):
        self.mock_client.get_open_orders.return_value = [
            {"id": "zombie_filled_1", "asset_id": "tok_unwind", "price": 0.45}
        ]
        self.mock_client.get_order.return_value = {
            "status": "CANCELED",
            "size_matched": 7.0,
            "asset_id": "tok_unwind",
            "price": 0.45,
        }

        reaped = self.reaper.reconcile_open_orders()
        self.assertEqual(reaped, ["zombie_filled_1"])
        self.mock_rollback.safe_unwind_or_limit_exit.assert_called_once_with(
            client=self.mock_client,
            token_id="tok_unwind",
            shares=7.0,
            buy_price=0.45,
            label="ZOMBIE_zombie_f",
            target_state=self.mock_dash,
            force_market_exit=False,
        )

    def test_reaper_lifecycle(self):
        self.reaper.start()
        self.assertIsNotNone(self.reaper._thread)
        self.assertTrue(self.reaper._thread.is_alive())
        self.reaper.stop(timeout=1.0)
        self.assertIsNone(self.reaper._thread)

    def test_passive_unwind_order_protected_from_zombie_reap(self):
        self.reaper.register_order(
            "unwind_order_777",
            token_id="tok_unwind",
            side="SELL",
            size=18.0,
            price=0.45,
            is_passive_unwind=True,
        )
        self.reaper._active_registry["unwind_order_777"]["created_at"] = time.time() - 100.0

        self.mock_client.get_open_orders.return_value = [{"id": "unwind_order_777"}]
        self.mock_client.get_order.return_value = {"status": "LIVE", "size_matched": "0.0"}

        reaped = self.reaper.reconcile_open_orders()
        self.assertEqual(reaped, [])
        self.mock_client.cancel_orders.assert_not_called()
        self.assertIn("unwind_order_777", self.reaper._active_registry)

    def test_passive_unwind_order_fill_detection_and_deregistration(self):
        self.reaper.register_order(
            "unwind_order_888",
            token_id="tok_unwind",
            side="SELL",
            size=10.0,
            price=0.52,
            is_passive_unwind=True,
        )
        self.mock_client.get_open_orders.return_value = [{"id": "unwind_order_888"}]
        self.mock_client.get_order.return_value = {"status": "MATCHED", "size_matched": "10.0"}

        reaped = self.reaper.reconcile_open_orders()
        self.assertEqual(reaped, [])
        self.mock_client.cancel_orders.assert_not_called()
        self.assertNotIn("unwind_order_888", self.reaper._active_registry)
        self.mock_dash.add_activity_log.assert_called()

    def test_auto_adopt_unregistered_sell_order(self):
        # Open order on CLOB with side=SELL, not registered in _active_registry
        self.mock_client.get_open_orders.return_value = [
            {"id": "external_unwind_999", "side": "SELL", "asset_id": "tok_bolsonaro", "price": 0.45, "size": 18.0}
        ]
        self.mock_client.get_order.return_value = {"status": "LIVE", "size_matched": "0.0"}

        reaped = self.reaper.reconcile_open_orders()
        self.assertEqual(reaped, [])
        self.mock_client.cancel_orders.assert_not_called()
        # Verify order was adopted into active registry with is_passive_unwind=True
        self.assertIn("external_unwind_999", self.reaper._active_registry)
        self.assertTrue(self.reaper._active_registry["external_unwind_999"]["is_passive_unwind"])


class TestMakerTakerIntegrationWithReaper(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_dash = MagicMock()
        self.mock_guard = MagicMock()
        self.mock_reaper = MagicMock(spec=OrderReaper)
        self.executor = MakerTakerExecutor(
            client=self.mock_client,
            dash_state=self.mock_dash,
            microstructure_guard=self.mock_guard,
            order_reaper=self.mock_reaper,
        )

    @patch("maker_taker_engine._resolve_token_metadata", return_value=(0.001, False))
    def test_step1_registers_order_with_reaper(self, mock_meta):
        order_maker_obj = MagicMock()
        order_taker_obj = MagicMock()
        self.mock_client.create_order.side_effect = [order_maker_obj, order_taker_obj]
        self.mock_client.post_orders.side_effect = [
            [{"orderID": "maker_reg_001"}],
            [{"orderID": "taker_002", "takingAmount": "10.0", "status": "matched"}],
        ]
        self.mock_client.get_order.return_value = {"status": "MATCHED", "size_matched": "10.0"}

        ok, status, _ = self.executor.execute_maker_taker_arbitrage(
            token_maker="tok_m",
            maker_price=0.48,
            token_taker="tok_t",
            taker_price=0.49,
            size=10.0,
            timeout_seconds=1.0,
        )
        self.assertTrue(ok)
        self.mock_reaper.register_order.assert_called_once_with(
            "maker_reg_001",
            token_id="tok_m",
            side="BUY",
            size=10.0,
            price=0.48,
            ttl_sec=6.0,
        )
        self.mock_reaper.deregister_order.assert_called_with("maker_reg_001")

    @patch("maker_taker_engine._resolve_token_metadata", return_value=(0.001, False))
    def test_toxicity_evasion_race_transitions_to_taker(self, mock_meta):
        order_maker_obj = MagicMock()
        order_taker_obj = MagicMock()
        self.mock_client.create_order.side_effect = [order_maker_obj, order_taker_obj]
        self.mock_client.post_orders.side_effect = [
            [{"orderID": "maker_evade_race"}],
            [{"orderID": "taker_002", "takingAmount": "10.0", "status": "matched"}],
        ]

        self.mock_guard.check_toxicity_evasion.return_value = (True, "ADVERSE_FLOW", {})
        self.mock_client.get_order.return_value = {"status": "MATCHED", "size_matched": "10.0"}

        ok, status, meta = self.executor.execute_maker_taker_arbitrage(
            token_maker="tok_m",
            maker_price=0.48,
            token_taker="tok_t",
            taker_price=0.49,
            size=10.0,
            timeout_seconds=1.0,
        )
        self.assertTrue(ok)
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(meta["hedged_size"], 10.0)

    @patch("maker_taker_engine._resolve_token_metadata", return_value=(0.001, False))
    def test_maker_timeout_unconfirmed_hazard_purges_reaper(self, mock_meta):
        order_maker_obj = MagicMock()
        self.mock_client.create_order.return_value = order_maker_obj
        self.mock_client.post_orders.return_value = [{"orderID": "maker_timeout_hazard"}]

        self.mock_client.get_order.return_value = {"status": "LIVE", "size_matched": "0.0"}

        ok, status, meta = self.executor.execute_maker_taker_arbitrage(
            token_maker="tok_m",
            maker_price=0.48,
            token_taker="tok_t",
            taker_price=0.49,
            size=10.0,
            timeout_seconds=0.2,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "MAKER_TIMEOUT_UNCONFIRMED_HAZARD")
        self.mock_reaper.purge_all_orders.assert_called_once()


if __name__ == "__main__":
    unittest.main()
