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
    mock_sdk.orders.create_many.side_effect = [
        {"createdOrderIds": [f"id-{i}" for i in range(20)]},
        {"createdOrderIds": [f"id-{i}" for i in range(20, 25)]},
    ]
    mock_sdk.orders.cancel_many.side_effect = [
        {"canceledOrderIds": [f"id-{i}" for i in range(20)]},
        {"canceledOrderIds": [f"id-{i}" for i in range(20, 25)]},
    ]

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
    mock_sdk.orders.cancel_all.return_value = {"canceledOrderIds": ["slug-1", "slug-2"]}
    client = AuthenticatedClient(sdk=mock_sdk)
    canceled = client.cancel_all_scoped(["slug-1", "slug-2"])
    assert canceled == ["slug-1", "slug-2"]
    mock_sdk.orders.cancel_all.assert_called_once_with({"slugs": ["slug-1", "slug-2"]})

def test_close_position():
    mock_sdk = MagicMock()
    mock_sdk.orders.close_position.return_value = {"id": "exec-1"}
    client = AuthenticatedClient(sdk=mock_sdk)
    client.close_position("slug-1")
    mock_sdk.orders.close_position.assert_called_once_with({
        "marketSlug": "slug-1",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    })

def test_contract_create_orders_batch_payload_types():
    import typing
    from polymarket_us.types.common import Amount
    from polymarket_us.types.orders import CreateOrderParams

    recording_sdk = MagicMock()
    recording_sdk.orders.create_many.return_value = {"createdOrderIds": ["ord-1", "ord-2"]}

    client = AuthenticatedClient(sdk=recording_sdk)
    quotes = [
        Quote(slug="mkt-1", side="BUY_LONG", price=Decimal("0.45"), qty=Decimal("10")),
        Quote(slug="mkt-2", side="SELL_SHORT", price=Decimal("0.55"), qty=Decimal("20")),
    ]
    created = client.create_orders_batch(quotes)
    assert len(created) == 2

    recording_sdk.orders.create_many.assert_called_once()
    call_args = recording_sdk.orders.create_many.call_args[0][0]
    assert "orders" in call_args
    orders_payload = call_args["orders"]
    assert len(orders_payload) == 2

    order_param_hints = typing.get_type_hints(CreateOrderParams)
    allowed_param_keys = set(order_param_hints.keys())
    allowed_intents = set(typing.get_args(order_param_hints["intent"]))
    allowed_tifs = set(typing.get_args(order_param_hints["tif"]))
    allowed_types = set(typing.get_args(order_param_hints["type"]))
    allowed_indicators = set(typing.get_args(order_param_hints["manualOrderIndicator"]))

    amount_hints = typing.get_type_hints(Amount)
    allowed_amount_keys = set(amount_hints.keys())
    allowed_currencies = set(typing.get_args(amount_hints["currency"]))

    for order_dict in orders_payload:
        for k in order_dict.keys():
            assert k in allowed_param_keys, f"Unexpected key {k} in CreateOrderParams"

        assert isinstance(order_dict["marketSlug"], str)
        assert order_dict["intent"] in allowed_intents
        assert order_dict["type"] in allowed_types
        assert order_dict["tif"] in allowed_tifs
        assert order_dict["manualOrderIndicator"] in allowed_indicators
        assert order_dict["participateDontInitiate"] is True
        assert isinstance(order_dict["quantity"], (int, float))

        price_dict = order_dict["price"]
        assert set(price_dict.keys()) == allowed_amount_keys
        assert price_dict["currency"] in allowed_currencies
        assert isinstance(price_dict["value"], str)

def test_contract_cancel_and_close_payload_types():
    import typing
    from polymarket_us.types.orders import CancelAllOrdersParams, CancelOrderListItem, ClosePositionParams

    recording_sdk = MagicMock()
    recording_sdk.orders.cancel_many.return_value = {"canceledOrderIds": ["ord-1"]}
    recording_sdk.orders.cancel_all.return_value = {"canceledOrderIds": ["slug-1", "slug-2"]}
    recording_sdk.orders.close_position.return_value = {"id": "close-1"}

    client = AuthenticatedClient(sdk=recording_sdk)

    # 1. cancel_orders_batch
    orders = [LiveOrder(order_id="ord-1", slug="slug-1", side="BUY_LONG", price=Decimal("0.50"), qty_open=Decimal("10"))]
    canceled = client.cancel_orders_batch(orders)
    assert canceled == ["ord-1"]
    cancel_payload = recording_sdk.orders.cancel_many.call_args[0][0]["orders"]
    cancel_item_hints = typing.get_type_hints(CancelOrderListItem)
    assert set(cancel_payload[0].keys()) == set(cancel_item_hints.keys())
    assert cancel_payload[0]["marketSlug"] == "slug-1"
    assert cancel_payload[0]["orderId"] == "ord-1"

    # 2. cancel_all_scoped
    client.cancel_all_scoped(["slug-1", "slug-2"])
    cancel_all_payload = recording_sdk.orders.cancel_all.call_args[0][0]
    cancel_all_hints = typing.get_type_hints(CancelAllOrdersParams)
    assert set(cancel_all_payload.keys()) == set(cancel_all_hints.keys())
    assert cancel_all_payload["slugs"] == ["slug-1", "slug-2"]

    # 3. close_position
    client.close_position("slug-1")
    close_payload = recording_sdk.orders.close_position.call_args[0][0]
    close_hints = typing.get_type_hints(ClosePositionParams)
    allowed_close_keys = set(close_hints.keys())
    allowed_indicators = set(typing.get_args(close_hints["manualOrderIndicator"]))
    for k in close_payload.keys():
        assert k in allowed_close_keys
    assert close_payload["marketSlug"] == "slug-1"
    assert close_payload["manualOrderIndicator"] in allowed_indicators

def test_authenticated_client_positions_and_buying_power_real_sdk_shapes():
    mock_sdk = MagicMock()
    mock_sdk.account.balances.return_value = {
        "balances": [
            {
                "currency": "USD",
                "currentBalance": 70.0,
                "buyingPower": 65.5,
                "assetNotional": 4.5,
            }
        ]
    }
    mock_sdk.portfolio.positions.return_value = {
        "positions": {
            "mkt-1": {
                "marketMetadata": {"slug": "mkt-1", "title": "Market 1"},
                "netPositionDecimal": "15.0",
                "avgPx": {"value": "0.52", "currency": "USD"},
            },
            "mkt-2": {
                "netPositionDecimal": "-10.0",
                "avgPx": {"value": "0.48", "currency": "USD"},
            },
        }
    }

    client = AuthenticatedClient(sdk=mock_sdk)
    bp = client.get_buying_power()
    assert bp == Decimal("65.5")

    positions = client.get_positions()
    pos_by_slug = {p.slug: p for p in positions}
    assert len(positions) == 2
    assert pos_by_slug["mkt-1"].net_qty == Decimal("15.0")
    assert pos_by_slug["mkt-1"].avg_price == Decimal("0.52")
    assert pos_by_slug["mkt-2"].net_qty == Decimal("-10.0")
    assert pos_by_slug["mkt-2"].avg_price == Decimal("0.48")

def test_create_orders_batch_missing_ids_reconciles_or_raises():
    mock_sdk = MagicMock()
    # Case 1: missing IDs, reconcile succeeds via orders.list
    mock_sdk.orders.create_many.return_value = {"createdOrderIds": []}
    mock_sdk.orders.list.return_value = {
        "orders": [
            {
                "id": "rec-1",
                "marketSlug": "mkt-1",
                "intent": "ORDER_INTENT_BUY_LONG",
                "price": {"value": "0.50", "currency": "USD"},
            }
        ]
    }

    client = AuthenticatedClient(sdk=mock_sdk)
    quotes = [Quote("mkt-1", "BUY_LONG", Decimal("0.50"), Decimal("10"))]
    orders = client.create_orders_batch(quotes)
    assert len(orders) == 1
    assert orders[0].order_id == "rec-1"

    # Case 2: reconciliation fails, raises RuntimeError
    mock_sdk.orders.list.return_value = {"orders": []}
    with pytest.raises(RuntimeError, match="incomplete order IDs"):
        client.create_orders_batch(quotes)

def test_live_engine_websocket_order_and_position_updates(tmp_path):
    import logging
    from pmus.engine import LiveEngine
    from pmus.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.db")
    order = LiveOrder(order_id="order-123", slug="mkt-1", side="BUY_LONG", price=Decimal("0.50"), qty_open=Decimal("10"))
    ledger.record_order(order, status="OPEN")

    engine = LiveEngine(
        settings=Settings(data_dir=tmp_path / "data"),
        ledger=ledger,
        public_client=MagicMock(),
        auth_client=MagicMock(),
        risk_manager=MagicMock(),
        logger=logging.getLogger("test"),
    )

    # 1. Feed real-shaped order_update (fill)
    order_update_event = {
        "requestId": "req-1",
        "subscriptionType": "SUBSCRIPTION_TYPE_ORDER",
        "orderSubscriptionUpdate": {
            "execution": {
                "id": "exec-1",
                "type": "EXECUTION_TYPE_FILL",
                "order": {
                    "id": "order-123",
                    "marketSlug": "mkt-1",
                    "state": "ORDER_STATE_FILLED",
                },
            }
        },
    }
    engine.handle_order_update(order_update_event)
    open_orders = ledger.get_open_orders("mkt-1")
    assert len(open_orders) == 0

    # 2. Feed real-shaped position_update
    pos_update_event = {
        "requestId": "req-2",
        "subscriptionType": "SUBSCRIPTION_TYPE_POSITION",
        "positionSubscription": {
            "afterPosition": {
                "marketMetadata": {"slug": "mkt-1"},
                "netPositionDecimal": "10.0",
                "avgPx": {"value": "0.50", "currency": "USD"},
            }
        },
    }
    engine.handle_position_update(pos_update_event)
    pos = ledger.get_position("mkt-1")
    assert pos.net_qty == Decimal("10.0")
    assert pos.avg_price == Decimal("0.50")

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
    settings = Settings(
        max_contracts_per_market=200,
        max_collateral_per_market=Decimal("500.00"),
        total_budget=Decimal("500.00"),
        max_collateral=Decimal("500.00"),
        data_dir=tmp_path / "data",
    )
    rm = RiskManager(settings)

    pos = {"mkt-1": Position("mkt-1", Decimal("150"), Decimal("0.50"))}
    q_breach = [Quote("mkt-1", "BUY_LONG", Decimal("0.50"), Decimal("60"))]
    res = rm.check_pre_submit(q_breach, [], pos, Decimal("100"), Decimal("0"))
    assert res.allowed is False
    assert "contract cap" in res.reason
