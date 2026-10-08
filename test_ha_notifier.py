import os
import json
import unittest
from unittest.mock import patch, MagicMock
import pytest

from ha_notifier import (
    format_trade_message,
    _dispatch_notification_sync,
    send_trade_notification,
    DEFAULT_HA_URL,
    DEFAULT_HA_TOKEN,
)


class TestHANotifier(unittest.TestCase):

    def test_format_trade_message(self):
        msg = format_trade_message(
            question="Will SpaceX reach Mars by 2026?",
            trade_size=25.50,
            expected_profit=1.2345,
            edge_pct=4.84,
            execution_style="maker_taker",
            wallet_balance=150.75,
            market_id="0xabc123456789",
        )
        assert "Will SpaceX reach Mars by 2026?" in msg
        assert "$25.50" in msg
        assert "+$1.2345" in msg
        assert "4.84%" in msg
        assert "maker_taker" in msg
        assert "$150.75" in msg
        assert "456789" in msg

    @patch("ha_notifier.requests.post")
    def test_dispatch_cloudhook_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        success = _dispatch_notification_sync(
            title="🎯 Polymarket Trade Executed",
            message="Test trade executed",
            timeout=6.0,
        )

        assert success is True
        assert mock_post.call_count == 1
        args, kwargs = mock_post.call_args
        assert "hooks.nabu.casa" in args[0]
        assert kwargs["json"] == {
            "title": "🎯 Polymarket Trade Executed",
            "message": "Test trade executed",
        }
        assert kwargs["timeout"] == 6.0

    @patch("ha_notifier.requests.post")
    def test_dispatch_cloudhook_fail_rest_fallback(self, mock_post):
        resp_fail = MagicMock()
        resp_fail.status_code = 500
        resp_fail.text = "Internal Server Error"

        resp_ok = MagicMock()
        resp_ok.status_code = 200

        # 1. Cloudhook fails
        # 2. REST primary succeeds
        mock_post.side_effect = [resp_fail, resp_ok]

        success = _dispatch_notification_sync(
            title="🎯 Polymarket Trade Executed",
            message="Test fallback",
        )

        assert success is True
        assert mock_post.call_count == 2
        first_call = mock_post.call_args_list[0]
        second_call = mock_post.call_args_list[1]
        assert "hooks.nabu.casa" in first_call[0][0]
        assert second_call[0][0] == f"{DEFAULT_HA_URL}/api/services/notify/notify"

    @patch("ha_notifier.requests.post")
    def test_dispatch_all_fail(self, mock_post):
        mock_post.side_effect = Exception("Connection Timeout")

        success = _dispatch_notification_sync(
            title="🎯 Polymarket Trade Executed",
            message="Test fail",
        )

        assert success is False
        assert mock_post.call_count == 3

    @patch("ha_notifier._dispatch_notification_sync")
    def test_send_trade_notification_thread(self, mock_dispatch):
        t = send_trade_notification(
            question="Will Ethereum hit 5k?",
            trade_size=10.0,
            expected_profit=0.5,
            edge_pct=5.0,
            wallet_balance=100.0,
            sync=False,
        )
        assert t is not None
        assert t.daemon is True
        t.join(timeout=2.0)
        assert mock_dispatch.called
