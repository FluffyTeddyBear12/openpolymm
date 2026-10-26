from decimal import Decimal
import threading
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

def test_network_exception_resilience_and_url_redaction(caplog):
    secret_url = "https://hooks.nabu.casa/api/webhook/super_secret_cloudhook_token_999"
    settings = Settings(webhook_url=secret_url)
    notifier = Notifier(settings)

    exc_msg = (
        f"HTTPSConnectionPool(host='hooks.nabu.casa', port=443): "
        f"Max retries exceeded with url: /api/webhook/super_secret_cloudhook_token_999 "
        f"(Caused by RequestException('Failed POST {secret_url} and https://backup.internal/secret_2'))"
    )
    with caplog.at_level("WARNING", logger="pmus.notify"):
        with patch("requests.post", side_effect=requests.RequestException(exc_msg)):
            t = notifier.send(title="Test", message="Testing failure", level="info")
            assert t is not None
            t.join(timeout=2.0)
            assert not t.is_alive()

    assert len(caplog.records) == 1
    logged = caplog.records[0].getMessage()
    assert "Failed to dispatch webhook notification (RequestException):" in logged
    assert "super_secret_cloudhook_token_999" not in logged
    assert secret_url not in logged
    assert "secret_2" not in logged
    assert "[REDACTED_URL]" in logged
    notifier.close()


def test_bounded_dispatch_limits_concurrency():
    settings = Settings(webhook_url="https://example.com/webhook")
    notifier = Notifier(settings)

    lock = threading.Lock()
    active = 0
    peak_active = 0
    release_gate = threading.Event()
    four_entered = threading.Event()

    def slow_post(*args, **kwargs):
        nonlocal active, peak_active
        with lock:
            active += 1
            if active > peak_active:
                peak_active = active
            if active == 4:
                four_entered.set()
        release_gate.wait(timeout=2.0)
        with lock:
            active -= 1

    with patch("requests.post", side_effect=slow_post) as mock_post:
        threads = [notifier.send(title=f"Msg {i}", message="Body") for i in range(8)]
        assert all(t is not None for t in threads)
        assert four_entered.wait(timeout=2.0)
        release_gate.set()
        for t in threads:
            assert t is not None
            t.join(timeout=2.0)

        assert mock_post.call_count == 8
        assert peak_active <= 4
    notifier.close()


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
        assert "State: QUOTING" in payload["message"]
        assert "Active: 3 pairs" in payload["message"]
        assert "1h LIP: $0.1234 (3540s qual)" in payload["message"]
        assert "Today LIP: $1.85 (Proj 24h: $3.20)" in payload["message"]
        assert "Realized PnL: $+0.00" in payload["message"]
        assert "Net Est: $+1.85" in payload["message"]
        assert "Unrealized: $+0.00" in payload["message"]
        assert "Cash: $137.75" in payload["message"]
        assert "Orders:" not in payload["message"]
        assert "Equity:" not in payload["message"]
        assert payload["content"] == f"[INFO] Hourly Status Ping: {payload['message']}"
        assert payload["extra"]["qualifying_sec_1h"] == 3540
        assert payload["extra"]["accrued_usd_1h"] == "0.1234"
        assert payload["extra"]["qualifying_sec_today"] == 25000
        assert payload["extra"]["accrued_usd_today"] == "1.85"
        assert payload["extra"]["projected_payout_today"] == "3.20"
        assert payload["extra"]["open_pairs"] == 3
        assert payload["extra"]["buying_power"] == "137.75"
        assert payload["extra"]["engine_state"] == "QUOTING"
        assert payload["extra"]["open_orders_count"] is None
        assert payload["extra"]["realized_pnl_today"] == "0"
        assert payload["extra"]["net_est_today"] == "1.85"
        assert payload["extra"]["unrealized_pnl"] == "0"
        assert payload["extra"]["mtm_equity"] is None


def test_notify_hourly_status_halted_and_negative_net_est_warns():
    settings = Settings(webhook_url="https://example.com/webhook")
    notifier = Notifier(settings)

    with patch("requests.post") as mock_post:
        t = notifier.notify_hourly_status(
            qualifying_sec_1h=0,
            accrued_usd_1h=Decimal("0"),
            qualifying_sec_today=7200,
            accrued_usd_today=Decimal("2.10"),
            projected_payout_today=Decimal("5.04"),
            open_pairs=0,
            buying_power=Decimal("180.50"),
            engine_state="HALTED",
            open_orders_count=0,
            realized_pnl_today=Decimal("-17.42"),
            unrealized_pnl=Decimal("-1.25"),
            mtm_equity=Decimal("202.02"),
        )
        assert t is not None
        t.join(timeout=2.0)

        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert payload["level"] == "warning"
        msg = payload["message"]
        assert "State: HALTED" in msg
        assert "HALTED" in msg
        assert "Orders: 0" in msg
        assert "-17.42" in msg
        assert "-15.32" in msg
        assert "202.02" in msg
        assert "Equity: $202.02" in msg
        assert payload["extra"]["engine_state"] == "HALTED"
        assert payload["extra"]["open_orders_count"] == 0
        assert payload["extra"]["realized_pnl_today"] == "-17.42"
        assert payload["extra"]["net_est_today"] == "-15.32"
        assert payload["extra"]["unrealized_pnl"] == "-1.25"
        assert payload["extra"]["mtm_equity"] == "202.02"


def test_notify_hourly_status_quoting_negative_net_est_warns():
    settings = Settings(webhook_url="https://example.com/webhook")
    notifier = Notifier(settings)

    with patch("requests.post") as mock_post:
        t = notifier.notify_hourly_status(
            qualifying_sec_1h=1800,
            accrued_usd_1h=Decimal("0.0500"),
            qualifying_sec_today=3600,
            accrued_usd_today=Decimal("1.00"),
            projected_payout_today=Decimal("2.40"),
            open_pairs=2,
            buying_power=Decimal("95.00"),
            engine_state="QUOTING",
            realized_pnl_today=Decimal("-2.50"),
        )
        assert t is not None
        t.join(timeout=2.0)
        payload = mock_post.call_args[1]["json"]
        assert payload["level"] == "warning"
        assert payload["extra"]["net_est_today"] == "-1.50"


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
