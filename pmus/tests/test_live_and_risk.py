from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo
import pytest

from pmus.config import Settings
from pmus.models import LiveOrder, Position, Quote
from pmus.risk import RiskManager
from pmus.venue import AuthenticatedClient, chunk_list

def test_chunk_list():
    items = list(range(45))
    chunks = list(chunk_list(items, 20))
    assert len(chunks) == 3
    assert len(chunks[0]) == 20
    assert len(chunks[1]) == 20
    assert len(chunks[2]) == 5

def test_authenticated_client_chunked_create_and_cancel():
    mock_sdk = MagicMock()
    mock_sdk.orders.create_many.return_value = {"createdOrderIds": [f"id-{i}" for i in range(25)]}
    mock_sdk.orders.cancel_many.return_value = {}

    client = AuthenticatedClient(sdk=mock_sdk)
    quotes = [
        Quote(slug=f"mkt-{i}", side="BUY_LONG", price=Decimal("0.50"), qty=Decimal("10"))
        for i in range(25)
    ]
    created = client.create_orders_batch(quotes)
    assert len(created) == 25
    assert mock_sdk.orders.create_many.call_count == 2

    orders = [
        LiveOrder(order_id=f"id-{i}", slug=f"mkt-{i}", side="BUY_LONG", price=Decimal("0.50"), qty_open=Decimal("10"))
        for i in range(25)
    ]
    canceled = client.cancel_orders_batch(orders)
    assert len(canceled) == 25
    assert mock_sdk.orders.cancel_many.call_count == 2

def test_startup_cancel_all_scoped_to_slugs():
    mock_sdk = MagicMock()
    client = AuthenticatedClient(sdk=mock_sdk)
    client.cancel_all_scoped(["slug-1", "slug-2"])
    mock_sdk.orders.cancel_all.assert_called_once_with({"slugs": ["slug-1", "slug-2"]})

def test_close_position():
    mock_sdk = MagicMock()
    client = AuthenticatedClient(sdk=mock_sdk)
    client.close_position("slug-1")
    mock_sdk.orders.close_position.assert_called_once_with({
        "marketSlug": "slug-1",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    })

def test_maintenance_window_boundaries_dst_aware():
    settings = Settings()
    rm = RiskManager(settings)

    # Thursday 2026-10-15 (EDT, UTC-4)
    dt_before = datetime(2026, 10, 15, 5, 54, tzinfo=ZoneInfo("America/New_York"))
    assert rm.is_maintenance_active(dt_before) is False

    dt_start = datetime(2026, 10, 15, 5, 55, tzinfo=ZoneInfo("America/New_York"))
    assert rm.is_maintenance_active(dt_start) is True

    dt_mid = datetime(2026, 10, 15, 7, 0, tzinfo=ZoneInfo("America/New_York"))
    assert rm.is_maintenance_active(dt_mid) is True

    dt_end_exact = datetime(2026, 10, 15, 8, 5, tzinfo=ZoneInfo("America/New_York"))
    assert rm.is_maintenance_active(dt_end_exact) is False

    # Winter date 2027-01-14 (EST, UTC-5)
    dt_winter_during = datetime(2027, 1, 14, 6, 30, tzinfo=ZoneInfo("America/New_York"))
    assert rm.is_maintenance_active(dt_winter_during) is True

    # Friday
    dt_friday = datetime(2026, 10, 16, 6, 0, tzinfo=ZoneInfo("America/New_York"))
    assert rm.is_maintenance_active(dt_friday) is False

def test_kill_switch_file(tmp_path):
    stop_file = tmp_path / "STOP"
    settings = Settings(data_dir=tmp_path / "data")
    rm = RiskManager(settings, stop_file_path=stop_file)

    assert rm.is_kill_switch_active() is False
    stop_file.touch()
    assert rm.is_kill_switch_active() is True
    res = rm.check_pre_submit([], [], {}, Decimal("100"), Decimal("0"))
    assert res.allowed is False
    assert "Kill switch" in res.reason

def test_daily_loss_stop(tmp_path):
    settings = Settings(daily_loss_stop=Decimal("10.00"), data_dir=tmp_path / "data")
    rm = RiskManager(settings)

    res_ok = rm.check_pre_submit([], [], {}, Decimal("100"), Decimal("5.00"))
    assert res_ok.allowed is True

    res_loss = rm.check_pre_submit([], [], {}, Decimal("100"), Decimal("10.00"))
    assert res_loss.allowed is False
    assert "Daily realized loss stop" in res_loss.reason

def test_collateral_and_buying_power_limits(tmp_path):
    settings = Settings(total_budget=Decimal("60.00"), data_dir=tmp_path / "data")
    rm = RiskManager(settings)

    q_large = [Quote("mkt-1", "BUY_LONG", Decimal("0.50"), Decimal("150"))]
    res_budget = rm.check_pre_submit(q_large, [], {}, Decimal("100"), Decimal("0"))
    assert res_budget.allowed is False
    assert "total budget collateral" in res_budget.reason

    q_ok = [Quote("mkt-1", "BUY_LONG", Decimal("0.50"), Decimal("50"))]
    res_bp = rm.check_pre_submit(q_ok, [], {}, Decimal("10.00"), Decimal("0"))
    assert res_bp.allowed is False
    assert "buying power" in res_bp.reason

def test_per_market_contract_cap(tmp_path):
    settings = Settings(max_contracts_per_market=200, data_dir=tmp_path / "data")
    rm = RiskManager(settings)

    pos = {"mkt-1": Position("mkt-1", Decimal("150"), Decimal("0.50"))}
    q_breach = [Quote("mkt-1", "BUY_LONG", Decimal("0.50"), Decimal("60"))]
    res = rm.check_pre_submit(q_breach, [], pos, Decimal("100"), Decimal("0"))
    assert res.allowed is False
    assert "contract cap" in res.reason
