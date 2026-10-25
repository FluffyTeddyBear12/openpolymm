from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests

from pmus.config import Settings
from pmus.models import ShadowFill
from pmus.notify import Notifier

def test_payload_formatting():
    payload = Notifier.format_payload(
        title="Test Alert",
        message="System functioning normally",
        level="info",
        extra={"key": "val"},
    )
    assert payload["title"] == "Test Alert"
    assert payload["message"] == "System functioning normally"
    assert payload["level"] == "info"
    assert payload["text"] == "[INFO] Test Alert: System functioning normally"
    assert payload["content"] == "[INFO] Test Alert: System functioning normally"
    assert "timestamp" in payload
    assert payload["extra"] == {"key": "val"}

def test_empty_url_is_noop():
    settings = Settings(webhook_url="")
    notifier = Notifier(settings)

    with patch("requests.post") as mock_post:
        res1 = notifier.send("Title", "Message")
        assert res1 is None

        fill = ShadowFill(
            fill_id="f-1",
            order_id="o-1",
            slug="mkt-test",
            side="BUY_LONG",
            price=Decimal("0.50"),
            qty=Decimal("10"),
            fee=Decimal("0.01"),
            is_maker=True,
            ts_ms=1000,
        )
        res2 = notifier.notify_fill(fill)
        assert res2 is None

        res3 = notifier.notify_daily_loss_stop(Decimal("10.00"), Decimal("10.00"))
        assert res3 is None

        res4 = notifier.notify_kill_switch(True)
        assert res4 is None

        mock_post.assert_not_called()

def test_notify_fill():
    settings = Settings(webhook_url="https://example.com/webhook")
    notifier = Notifier(settings)

    fill = ShadowFill(
        fill_id="f-100",
        order_id="o-200",
        slug="nfl-kc-vs-bal",
        side="BUY_LONG",
        price=Decimal("0.550"),
        qty=Decimal("25"),
        fee=Decimal("0.02"),
        is_maker=True,
        ts_ms=1000,
    )

    with patch("requests.post") as mock_post:
        t = notifier.notify_fill(fill)
        assert t is not None
        t.join(timeout=2.0)

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://example.com/webhook"
        payload = kwargs["json"]
        assert payload["title"] == "Order Fill"
        assert "nfl-kc-vs-bal" in payload["message"]
        assert payload["extra"]["fill_id"] == "f-100"
        assert payload["extra"]["order_id"] == "o-200"
        assert payload["extra"]["price"] == "0.550"
        assert payload["extra"]["qty"] == "25"

def test_notify_daily_loss_stop():
    settings = Settings(webhook_url="https://example.com/webhook")
    notifier = Notifier(settings)

    with patch("requests.post") as mock_post:
        t = notifier.notify_daily_loss_stop(Decimal("12.50"), Decimal("10.00"))
        assert t is not None
        t.join(timeout=2.0)

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["level"] == "error"
        assert "Daily Loss Stop Tripped" in payload["title"]
        assert "$12.50 >= $10.00" in payload["message"]
        assert payload["extra"]["loss"] == "12.50"
        assert payload["extra"]["stop"] == "10.00"

def test_notify_kill_switch_active_and_deactivated():
    settings = Settings(webhook_url="https://example.com/webhook")
    notifier = Notifier(settings)

    with patch("requests.post") as mock_post:
        t1 = notifier.notify_kill_switch(active=True)
        assert t1 is not None
        t1.join(timeout=2.0)

        assert mock_post.call_count == 1
        payload1 = mock_post.call_args[1]["json"]
        assert payload1["level"] == "warning"
        assert "Kill Switch Activated" in payload1["title"]
        assert payload1["extra"]["active"] is True

        t2 = notifier.notify_kill_switch(active=False)
        assert t2 is not None
        t2.join(timeout=2.0)

        assert mock_post.call_count == 2
        payload2 = mock_post.call_args[1]["json"]
        assert payload2["level"] == "info"
        assert "Kill Switch Deactivated" in payload2["title"]
        assert payload2["extra"]["active"] is False

def test_network_exception_resilience():
    settings = Settings(webhook_url="https://example.com/webhook")
    notifier = Notifier(settings)

    with patch("requests.post", side_effect=requests.RequestException("Connection refused")):
        # Should not raise exception
        t = notifier.send(title="Test", message="Testing failure", level="info")
        assert t is not None
        t.join(timeout=2.0)
        assert not t.is_alive()


def test_notify_hourly_status():
    settings = Settings(webhook_url="https://example.com/webhook")
    notifier = Notifier(settings)

    with patch("requests.post") as mock_post:
        t = notifier.notify_hourly_status(
            qualifying_sec_1h=3540,
            accrued_usd_1h=Decimal("0.1234"),
            qualifying_sec_today=25000,
            accrued_usd_today=Decimal("1.85"),
            projected_payout_today=Decimal("3.20"),
            open_pairs=3,
            buying_power=Decimal("137.75"),
        )
        assert t is not None
        t.join(timeout=2.0)

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["level"] == "info"
        assert payload["title"] == "Hourly Status Ping"
        assert "1h Accrual: $0.1234 (3540s qual)" in payload["message"]
        assert "Today: $1.85 (Proj 24h: $3.20)" in payload["message"]
        assert "Active: 3 pairs | Cash: $137.75" in payload["message"]
        assert payload["content"] == f"[INFO] Hourly Status Ping: {payload['message']}"
        assert payload["extra"]["qualifying_sec_1h"] == 3540
        assert payload["extra"]["accrued_usd_1h"] == "0.1234"
        assert payload["extra"]["qualifying_sec_today"] == 25000
        assert payload["extra"]["accrued_usd_today"] == "1.85"
        assert payload["extra"]["projected_payout_today"] == "3.20"
        assert payload["extra"]["open_pairs"] == 3
        assert payload["extra"]["buying_power"] == "137.75"


def test_notify_hourly_status_empty_url():
    settings = Settings(webhook_url="")
    notifier = Notifier(settings)

    with patch("requests.post") as mock_post:
        t = notifier.notify_hourly_status(
            qualifying_sec_1h=3600,
            accrued_usd_1h=Decimal("0.20"),
            qualifying_sec_today=10000,
            accrued_usd_today=Decimal("1.00"),
            projected_payout_today=Decimal("2.40"),
            open_pairs=2,
            buying_power=Decimal("100.00"),
        )
        assert t is None
        mock_post.assert_not_called()
