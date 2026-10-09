"""
Unit and Integration Tests for Loss-Free Rollback & Price Protection Engine.

Location: d:\\neststock\\scripts\\polymarket_bot\\test_rollback_protector.py
Run command: .\\venv\\Scripts\\pytest.exe d:\\neststock\\scripts\\polymarket_bot\\test_rollback_protector.py
"""

import unittest
from unittest.mock import MagicMock, patch
import pytest

from rollback_protector import (
    RollbackProtector,
    safe_unwind_or_limit_exit,
    evaluate_position_hold_vs_exit,
)
try:
    from py_clob_client_v2.clob_types import OrderType
except ImportError:
    class OrderType:
        GTC = "GTC"
        FOK = "FOK"


class TestRollbackProtector(unittest.TestCase):

    def test_evaluate_position_hold_vs_exit(self):
        """
        Verify evaluate_position_hold_vs_exit logic:
        - IMMEDIATE_EXIT if best_bid >= buy_price - 0.005
        - POST_LIMIT_SELL if best_bid < buy_price - 0.005
        """
        assert evaluate_position_hold_vs_exit(buy_price=0.50, best_bid=0.495) == "IMMEDIATE_EXIT"
        assert evaluate_position_hold_vs_exit(buy_price=0.50, best_bid=0.498) == "IMMEDIATE_EXIT"
        assert evaluate_position_hold_vs_exit(buy_price=0.50, best_bid=0.500) == "IMMEDIATE_EXIT"
        assert evaluate_position_hold_vs_exit(buy_price=0.50, best_bid=0.494) == "POST_LIMIT_SELL"
        assert evaluate_position_hold_vs_exit(buy_price=0.038, best_bid=0.010) == "POST_LIMIT_SELL"
        assert evaluate_position_hold_vs_exit(buy_price=0.038, best_bid=0.035) == "IMMEDIATE_EXIT"
        assert evaluate_position_hold_vs_exit(buy_price=0.038, best_bid=0.0) == "POST_LIMIT_SELL"

    def test_safe_market_exit_when_bid_close_to_buy_price(self):
        """
        Test case: Best bid close to buy price -> executes market exit safely.
        """
        mock_client = MagicMock()
        mock_client.get_order_book.return_value = {
            "bids": [
                {"price": "0.498", "size": "200.0"},
                {"price": "0.495", "size": "100.0"},
            ]
        }
        mock_client.create_order.return_value = {"order_payload": "valid"}
        mock_client.post_orders.return_value = [{"orderID": "0xmarket_ok", "status": "matched"}]

        success, action, resp = RollbackProtector.safe_unwind_or_limit_exit(
            client=mock_client,
            token_id="token_yes_123",
            shares=50.0,
            buy_price=0.50,
            label="YES",
            max_loss_cents=0.005,
        )

        assert success is True
        assert action == "MARKET_EXIT_SAFE"
        assert resp.get("orderID") == "0xmarket_ok"

        mock_client.create_order.assert_called_once()
        order_arg = mock_client.create_order.call_args[0][0]
        actual_price = getattr(order_arg, "price", order_arg.get("price") if isinstance(order_arg, dict) else None)
        assert actual_price == 0.498
        assert actual_price >= 0.50 - 0.005

        mock_client.post_orders.assert_called_once()
        post_arg = mock_client.post_orders.call_args[0][0][0]
        actual_order_type = getattr(post_arg, "orderType", None)
        assert actual_order_type in (OrderType.FOK, "FOK")

    def test_block_penny_bid_and_post_limit_sell(self):
        """
        Critical Test Case:
        When best bid is $0.01 (penny bid) and buy price was $0.038:
        - Blocks fire-sale dump into $0.01 bid.
        - Creates and posts passive maker limit sell order at buy_price ($0.038).
        - Uses GTC order type.
        """
        mock_client = MagicMock()
        mock_client.get_order_book.return_value = {
            "bids": [
                {"price": "0.010", "size": "1000.0"},
                {"price": "0.008", "size": "500.0"},
            ]
        }
        mock_client.create_order.return_value = {"order_payload": "limit_order"}
        mock_client.post_orders.return_value = [{"orderID": "0xlimit_gtc_456", "status": "live"}]

        target_state = MagicMock()

        success, action, result = RollbackProtector.safe_unwind_or_limit_exit(
            client=mock_client,
            token_id="token_yes_penny",
            shares=100.0,
            buy_price=0.038,
            label="YES",
            max_loss_cents=0.005,
            target_state=target_state,
        )

        assert success is True
        assert action == "LIMIT_ORDER_PLACED"
        assert result["order_id"] == "0xlimit_gtc_456"
        assert result["price"] == 0.038

        mock_client.create_order.assert_called_once()
        order_arg = mock_client.create_order.call_args[0][0]
        actual_price = getattr(order_arg, "price", order_arg.get("price") if isinstance(order_arg, dict) else None)
        assert actual_price == 0.038
        assert actual_price != 0.010

        mock_client.post_orders.assert_called_once()
        post_arg = mock_client.post_orders.call_args[0][0][0]
        actual_order_type = getattr(post_arg, "orderType", None)
        assert actual_order_type in (OrderType.GTC, "GTC")

        target_state.add_activity_log.assert_called_once()
        log_text = target_state.add_activity_log.call_args[0][0]
        assert "LOSS-FREE EXIT" in log_text
        assert "0.0380" in log_text

    def test_negative_slippage_prevention_boundary(self):
        """
        Negative slippage prevention -> asserts sell price never violates floor.
        """
        buy_price = 0.20
        max_loss = 0.005
        floor = buy_price - max_loss

        # Subcase 1: Bid is 0.195 (exactly at floor) -> Market Exit at 0.195
        mock_client_1 = MagicMock()
        mock_client_1.get_order_book.return_value = {"bids": [{"price": 0.195}]}
        mock_client_1.post_orders.return_value = [{"orderID": "0x1"}]
        ok, act, _ = RollbackProtector.safe_unwind_or_limit_exit(
            mock_client_1, "tok1", 10.0, buy_price, max_loss_cents=max_loss
        )
        assert ok is True
        assert act == "MARKET_EXIT_SAFE"
        p1 = mock_client_1.create_order.call_args[0][0]
        price_val1 = getattr(p1, "price", p1.get("price") if isinstance(p1, dict) else None)
        assert price_val1 >= floor

        # Subcase 2: Bid is 0.1949 (just below floor) -> Blocked! Placed limit sell at 0.20
        mock_client_2 = MagicMock()
        mock_client_2.get_order_book.return_value = {"bids": [{"price": 0.1949}]}
        mock_client_2.post_orders.return_value = [{"orderID": "0x2"}]
        ok, act, res = RollbackProtector.safe_unwind_or_limit_exit(
            mock_client_2, "tok2", 10.0, buy_price, max_loss_cents=max_loss
        )
        assert ok is True
        assert act == "LIMIT_ORDER_PLACED"
        assert res["price"] == 0.20
        p2 = mock_client_2.create_order.call_args[0][0]
        price_val2 = getattr(p2, "price", p2.get("price") if isinstance(p2, dict) else None)
        assert price_val2 == 0.20

    def test_empty_order_book_safeguard(self):
        """
        If order book has 0 bids (empty book), NEVER dump at 0.01!
        Must place limit order at buy_price.
        """
        mock_client = MagicMock()
        mock_client.get_order_book.return_value = {"bids": []}
        mock_client.post_orders.return_value = [{"orderID": "0xempty", "status": "live"}]

        ok, action, res = RollbackProtector.safe_unwind_or_limit_exit(
            mock_client, "tok_empty", 25.0, buy_price=0.075
        )
        assert ok is True
        assert action == "LIMIT_ORDER_PLACED"
        assert res["price"] == 0.075

    def test_order_book_extraction_formats(self):
        """
        Verify extract_best_bid correctly handles diverse order book formats.
        """
        assert RollbackProtector.extract_best_bid(None) == 0.0
        assert RollbackProtector.extract_best_bid({}) == 0.0
        assert RollbackProtector.extract_best_bid({"bids": []}) == 0.0

        b1 = {"bids": [{"price": "0.15", "size": "10"}, {"price": "0.22", "size": "5"}]}
        assert RollbackProtector.extract_best_bid(b1) == 0.22

        b2 = {"bids": [["0.18", "100"], [0.25, 50]]}
        assert RollbackProtector.extract_best_bid(b2) == 0.25

    def test_zero_shares_noop(self):
        """
        Zero shares should return cleanly without sending any orders.
        """
        mock_client = MagicMock()
        ok, action, _ = RollbackProtector.safe_unwind_or_limit_exit(
            mock_client, "tok", 0.0, 0.50
        )
        assert ok is True
        assert action == "NO_SHARES"
        mock_client.create_order.assert_not_called()
        mock_client.post_orders.assert_not_called()

    def test_client_none_handling(self):
        """
        client=None should fail gracefully with CLIENT_NONE.
        """
        ok, action, res = RollbackProtector.safe_unwind_or_limit_exit(
            None, "tok", 10.0, 0.50
        )
        assert ok is False
        assert action == "CLIENT_NONE"
        assert "error" in res

    @patch("rollback_protector.time.sleep")
    def test_order_rejection_handling(self, mock_sleep):
        """
        If CLOB returns an error message in response, returns failure tuple.
        """
        mock_client = MagicMock()
        mock_client.get_order_book.return_value = {"bids": [{"price": 0.50}]}
        mock_client.post_orders.return_value = [{"errorMsg": "balance is not enough"}]

        ok, action, res = RollbackProtector.safe_unwind_or_limit_exit(
            mock_client, "tok", 10.0, 0.50
        )
        assert ok is False
        assert action == "MARKET_EXIT_FAILED"
        assert "balance is not enough" in res["error"]
        assert mock_sleep.call_count == 3

    @patch("rollback_protector.time.sleep")
    def test_safe_unwind_or_limit_exit_retries_on_balance_delay_and_succeeds(self, mock_sleep):
        """
        Verify safe_unwind_or_limit_exit retries when CLOB returns balance/allowance error,
        re-fetches book, and succeeds on subsequent attempt.
        """
        mock_client = MagicMock()
        mock_client.get_order_book.side_effect = [
            {"bids": [{"price": 0.498, "size": 100.0}]},
            {"bids": [{"price": 0.499, "size": 100.0}]},
        ]
        mock_client.post_orders.side_effect = [
            [{"errorMsg": "not enough balance: newly matched tokens awaiting CLOB credit"}],
            [{"orderID": "0xretry_success", "status": "matched"}]
        ]

        ok, action, res = RollbackProtector.safe_unwind_or_limit_exit(
            client=mock_client,
            token_id="tok_balance_delay",
            shares=50.0,
            buy_price=0.50,
            label="YES",
            max_loss_cents=0.005,
        )

        assert ok is True
        assert action == "MARKET_EXIT_SAFE"
        assert res.get("orderID") == "0xretry_success"
        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_with(1.0)
        assert mock_client.post_orders.call_count == 2

    def test_safe_unwind_dynamic_tick_size_and_neg_risk(self):
        mock_client = MagicMock()
        mock_client.get_order_book.return_value = {
            "bids": [{"price": "0.485", "size": "100.0"}]
        }
        mock_client.create_order.return_value = {"order_payload": "valid"}
        mock_client.post_orders.return_value = [{"orderID": "0xneg_risk_ok", "status": "matched"}]
        mock_client.get_tick_size.return_value = "0.01"
        mock_client.get_neg_risk.return_value = True

        ok, action, resp = RollbackProtector.safe_unwind_or_limit_exit(
            client=mock_client,
            token_id="tok_neg_risk_123",
            shares=50.0,
            buy_price=0.49,
            label="YES",
            max_loss_cents=0.01,
        )

        assert ok is True
        assert action == "MARKET_EXIT_SAFE"
        mock_client.create_order.assert_called_once()
        args, kwargs = mock_client.create_order.call_args
        opts = kwargs.get("options")
        assert opts is not None
        assert str(opts.tick_size) == "0.01"
        assert opts.neg_risk is True

    def test_post_sell_order_fallback_on_type_error(self):
        mock_client = MagicMock()
        valid_order = {"order_payload": "legacy"}
        def create_order_side_effect(*args, **kwargs):
            if "options" in kwargs:
                raise TypeError("create_order() got an unexpected keyword argument 'options'")
            return valid_order

        mock_client.create_order.side_effect = create_order_side_effect
        mock_client.post_orders.return_value = [{"orderID": "0xlegacy_ok", "status": "matched"}]

        success, resp, err = RollbackProtector._post_sell_order(
            client=mock_client,
            token_id="tok_legacy",
            price=0.50,
            shares=10.0,
            order_type="FOK",
        )

        assert success is True
        assert err is None
        assert mock_client.create_order.call_count == 2


