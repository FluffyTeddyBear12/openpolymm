from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from pmus.clob_venue import ClobVenueClient, parse_trade_fills
from pmus.config import Settings
from pmus.models import Quote

PROXY = "0xe7d565d58c61e2b96adb0e4518e7c33978402e7f"

MAKER_TRADE = {
    "id": "0f7d",
    "taker_order_id": "0x6e80",
    "asset_id": "1062",
    "side": "BUY",
    "size": "7",
    "price": "0.27",
    "status": "CONFIRMED",
    "match_time": "1791596070",
    "maker_orders": [
        {
            "order_id": "0xa092",
            "maker_address": "0xe7D565D58c61E2B96adb0e4518E7c33978402e7f",
            "matched_amount": "7",
            "price": "0.73",
            "fee_rate_bps": "",
            "asset_id": "1107",
            "side": "BUY",
        },
        {
            "order_id": "0xother",
            "maker_address": "0x0000000000000000000000000000000000000001",
            "matched_amount": "3",
            "price": "0.73",
            "fee_rate_bps": "",
            "asset_id": "1107",
            "side": "BUY",
        },
    ],
    "trader_side": "MAKER",
}

TAKER_TRADE = {
    "id": "t-1",
    "taker_order_id": "0xtaker",
    "asset_id": "2001",
    "side": "SELL",
    "size": "10",
    "price": "0.40",
    "status": "MATCHED",
    "match_time": "1791596100",
    "fee_rate_bps": "100",
    "maker_orders": [],
    "trader_side": "TAKER",
}


def make_client(mock_clob: MagicMock, proxy: str = PROXY) -> ClobVenueClient:
    client = ClobVenueClient(Settings(venue="global", polymarket_proxy_address=proxy), client=mock_clob)
    client.is_authenticated = True
    return client


def test_maker_fill_uses_our_maker_leg_not_taker_fields():
    fills = parse_trade_fills(MAKER_TRADE, PROXY)
    assert len(fills) == 1
    f = fills[0]
    assert f.fill_id == "0f7d:0xa092"
    assert f.order_id == "0xa092"
    assert f.slug == "1107"
    assert f.side == "BUY_LONG"
    assert f.price == Decimal("0.73")
    assert f.qty == Decimal("7")
    assert f.fee == Decimal("0")
    assert f.is_maker is True
    assert f.ts_ms == 1791596070000


def test_taker_fill_uses_top_level_fields_and_fee_rate():
    fills = parse_trade_fills(TAKER_TRADE, PROXY)
    assert len(fills) == 1
    f = fills[0]
    assert f.fill_id == "t-1:0xtaker"
    assert f.order_id == "0xtaker"
    assert f.slug == "2001"
    assert f.side == "SELL_LONG"
    assert f.qty == Decimal("10")
    assert f.is_maker is False
    assert f.fee == Decimal("0.01") * Decimal("0.40") * Decimal("10")


def test_failed_trade_yields_no_fill():
    assert parse_trade_fills({**MAKER_TRADE, "status": "FAILED"}, PROXY) == []


def test_get_fills_queries_trades_after_since():
    mock_clob = MagicMock()
    mock_clob.get_trades.return_value = [MAKER_TRADE, TAKER_TRADE]
    client = make_client(mock_clob)
    since = datetime(2026, 10, 10, tzinfo=timezone.utc)

    fills = client.get_fills(since=since)

    assert [f.fill_id for f in fills] == ["0f7d:0xa092", "t-1:0xtaker"]
    params = mock_clob.get_trades.call_args[0][0]
    assert params.after == int(since.timestamp())


def test_exchange_reads_raise_instead_of_returning_empty():
    mock_clob = MagicMock()
    mock_clob.get_open_orders.side_effect = RuntimeError("502")
    mock_clob.get_trades.side_effect = RuntimeError("502")
    client = make_client(mock_clob)

    with pytest.raises(RuntimeError):
        client.get_open_orders()
    with pytest.raises(RuntimeError):
        client.get_fills()
    with patch("requests.get", side_effect=ConnectionError("reset")):
        with pytest.raises(ConnectionError):
            client.get_positions()


def test_get_positions_pages_data_api():
    page1 = [
        {"asset": "1107", "size": 7.0, "avgPrice": 0.73},
        {"asset": "999", "size": 12.5, "avgPrice": 0.1},
    ]
    responses = [MagicMock(), MagicMock()]
    responses[0].json.return_value = page1
    responses[1].json.return_value = []
    client = make_client(MagicMock())

    with patch("requests.get", side_effect=responses) as mock_get:
        positions = client.get_positions()

    assert {p.slug: p.net_qty for p in positions} == {"1107": Decimal("7.0"), "999": Decimal("12.5")}
    assert positions[0].avg_price == Decimal("0.73")
    first_params = mock_get.call_args_list[0].kwargs["params"]
    assert first_params["user"] == PROXY
    assert first_params["sizeThreshold"] == 0
    assert mock_get.call_args_list[1].kwargs["params"]["offset"] == 2
    assert mock_get.call_args_list[0].kwargs["timeout"] == 10


def test_get_positions_requires_proxy():
    client = make_client(MagicMock(), proxy="")
    with pytest.raises(RuntimeError, match="POLYMARKET_PROXY_ADDRESS"):
        client.get_positions()


def test_scoped_cancel_falls_back_to_per_market_cancel():
    mock_clob = MagicMock()
    mock_clob.get_open_orders.side_effect = RuntimeError("502")
    client = make_client(mock_clob)

    client.cancel_all_scoped(["tok-a", "tok-b"])

    assets = [c[0][0].asset_id for c in mock_clob.cancel_market_orders.call_args_list]
    assert assets == ["tok-a", "tok-b"]
    mock_clob.cancel_orders.assert_not_called()
    mock_clob.cancel_all.assert_not_called()


def test_scoped_cancel_falls_back_to_cancel_all_when_market_cancel_fails():
    mock_clob = MagicMock()
    mock_clob.get_open_orders.side_effect = RuntimeError("502")
    mock_clob.cancel_market_orders.side_effect = RuntimeError("400")
    client = make_client(mock_clob)

    client.cancel_all_scoped(["tok-a"])

    mock_clob.cancel_all.assert_called_once_with()


def test_empty_cancels_never_reach_the_exchange():
    mock_clob = MagicMock()
    mock_clob.get_open_orders.return_value = [
        {"id": "ord-x", "asset_id": "tok-other", "side": "BUY", "price": "0.5", "size": "10"},
    ]
    client = make_client(mock_clob)

    assert client.cancel_orders([]) is True
    assert client.cancel_all_scoped(["tok-a"]) == []
    assert client.cancel_all_scoped([]) == []
    mock_clob.cancel_orders.assert_not_called()


def test_unacknowledged_order_is_not_reported_as_created():
    mock_clob = MagicMock()
    mock_clob.create_and_post_order.side_effect = [
        {"success": False, "errorMsg": "not enough balance"},
        {"orderID": "0xok"},
    ]
    client = make_client(mock_clob)
    quotes = [
        Quote(slug="tok-a", side="BUY_LONG", price=Decimal("0.40"), qty=Decimal("50")),
        Quote(slug="tok-a", side="BUY_LONG", price=Decimal("0.41"), qty=Decimal("50")),
    ]

    created = client.create_orders_batch(quotes)

    assert [o.order_id for o in created] == ["0xok"]
