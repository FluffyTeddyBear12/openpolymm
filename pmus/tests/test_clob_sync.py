from datetime import datetime, timedelta, timezone
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

    with patch("pmus.clob_venue.POSITIONS_PAGE_SIZE", 2), patch("requests.get", side_effect=responses) as mock_get:
        positions = client.get_positions()

    assert {p.slug: p.net_qty for p in positions} == {"1107": Decimal("7.0"), "999": Decimal("12.5")}
    assert positions[0].avg_price == Decimal("0.73")
    first_params = mock_get.call_args_list[0].kwargs["params"]
    assert first_params["user"] == PROXY
    assert first_params["sizeThreshold"] == 0
    assert mock_get.call_args_list[1].kwargs["params"]["offset"] == 2
    assert mock_get.call_args_list[0].kwargs["timeout"] == 10


def test_get_positions_stops_when_under_page_size():
    page1 = [
        {"asset": "1107", "size": 7.0, "avgPrice": 0.73},
        {"asset": "999", "size": 12.5, "avgPrice": 0.1},
    ]
    resp = MagicMock()
    resp.json.return_value = page1
    client = make_client(MagicMock())

    with patch("requests.get", return_value=resp) as mock_get:
        positions = client.get_positions()

    assert len(positions) == 2
    assert mock_get.call_count == 1


def test_get_positions_requires_proxy():
    client = make_client(MagicMock(), proxy="")
    with pytest.raises(RuntimeError, match="POLYMARKET_PROXY_ADDRESS"):
        client.get_positions()


def test_get_positions_returns_cached_within_ttl():
    page = [{"asset": "1107", "size": 7.0, "avgPrice": 0.73}]
    resp = MagicMock()
    resp.json.return_value = page
    client = make_client(MagicMock())

    with patch("requests.get", return_value=resp) as mock_get:
        pos1 = client.get_positions()
        pos2 = client.get_positions()

    assert pos1 == pos2
    assert len(pos1) == 1
    assert mock_get.call_count == 1


def test_invalidate_positions_cache_forces_fresh_fetch():
    page1 = [{"asset": "1107", "size": 7.0, "avgPrice": 0.73}]
    page2 = [{"asset": "1107", "size": 14.0, "avgPrice": 0.73}]
    resp1 = MagicMock()
    resp1.json.return_value = page1
    resp2 = MagicMock()
    resp2.json.return_value = page2
    client = make_client(MagicMock())

    with patch("requests.get", side_effect=[resp1, resp2]) as mock_get:
        pos1 = client.get_positions()
        client.invalidate_positions_cache()
        pos2 = client.get_positions()

    assert mock_get.call_count == 2
    assert pos1[0].net_qty == Decimal("7.0")
    assert pos2[0].net_qty == Decimal("14.0")


def test_get_positions_falls_back_to_stale_cache_on_timeout():
    page = [{"asset": "1107", "size": 7.0, "avgPrice": 0.73}]
    resp = MagicMock()
    resp.json.return_value = page
    client = make_client(MagicMock())

    with patch("requests.get", return_value=resp):
        pos1 = client.get_positions()

    client._positions_cache_ts -= 20.0

    with patch("requests.get", side_effect=TimeoutError("Polymarket data-api timeout")) as mock_get:
        pos2 = client.get_positions()

    assert mock_get.call_count == 1
    assert pos2 == pos1


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


def test_fetch_sampling_markets_filters_status_patterns_and_expiry():
    now = datetime.now(timezone.utc)
    future_valid = (now + timedelta(days=20)).isoformat()
    future_short = (now + timedelta(days=2)).isoformat()
    raw = [
        {
            "condition_id": "c-valid",
            "market_slug": "valid-slug",
            "question": "Will candidate win?",
            "rewards": {"rates": [{"rewards_daily_rate": "100"}], "min_size": 100, "max_spread": 2.5},
            "minimum_tick_size": "0.01",
            "minimum_order_size": "5",
            "tokens": [{"token_id": "tok-y", "outcome": "Yes"}, {"token_id": "tok-n", "outcome": "No"}],
            "end_date_iso": future_valid,
            "game_start_time": future_valid,
            "active": True,
            "closed": False,
        },
        {
            "condition_id": "c-closed",
            "question": "Will candidate win?",
            "rewards": {"rates": [{"rewards_daily_rate": "100"}]},
            "tokens": [{"token_id": "tok-1"}, {"token_id": "tok-2"}],
            "end_date_iso": future_valid,
            "active": True,
            "closed": True,
        },
        {
            "condition_id": "c-inactive",
            "question": "Will candidate win?",
            "rewards": {"rates": [{"rewards_daily_rate": "100"}]},
            "tokens": [{"token_id": "tok-1"}, {"token_id": "tok-2"}],
            "end_date_iso": future_valid,
            "active": False,
            "closed": False,
        },
        {
            "condition_id": "c-weather",
            "question": "Highest temp in London on Friday?",
            "rewards": {"rates": [{"rewards_daily_rate": "100"}]},
            "tokens": [{"token_id": "tok-1"}, {"token_id": "tok-2"}],
            "end_date_iso": future_valid,
            "active": True,
            "closed": False,
        },
        {
            "condition_id": "c-short-expiry",
            "question": "Will team win tonight?",
            "rewards": {"rates": [{"rewards_daily_rate": "100"}]},
            "tokens": [{"token_id": "tok-1"}, {"token_id": "tok-2"}],
            "end_date_iso": future_short,
            "active": True,
            "closed": False,
        },
    ]

    mock_clob = MagicMock()
    mock_clob.get_sampling_markets.return_value = raw
    client = make_client(mock_clob)

    conds = client.fetch_reward_conditions()
    assert len(conds) == 1
    c = conds[0]
    assert c.condition_id == "c-valid"
    assert c.window.end is not None
    assert c.window.event_start_time is not None


def test_buying_power_cache_locking_and_stale_fallback():
    mock_clob = MagicMock()
    mock_clob.get_balance_allowance.return_value = {"balance": "100000000"}
    client = make_client(mock_clob)

    bp1 = client.get_buying_power()
    assert bp1 == Decimal("100")
    assert mock_clob.get_balance_allowance.call_count == 1
    assert client._resolved_bp_sig_type == 3

    bp2 = client.get_buying_power()
    assert bp2 == Decimal("100")
    assert mock_clob.get_balance_allowance.call_count == 1

    client._buying_power_cache_ts -= 10.0
    bp3 = client.get_buying_power()
    assert bp3 == Decimal("100")
    assert mock_clob.get_balance_allowance.call_count == 2
    param = mock_clob.get_balance_allowance.call_args[0][0]
    assert param.signature_type == 3

    client._buying_power_cache_ts -= 10.0
    mock_clob.get_balance_allowance.side_effect = RuntimeError("RPC fail")
    bp4 = client.get_buying_power()
    assert bp4 == Decimal("100")

    client.invalidate_buying_power_cache()
    bp5 = client.get_buying_power()
    assert bp5 == Decimal("0")


def test_sampling_metadata_surfacing_and_lifecycle_gates():
    now_dt = datetime.now(timezone.utc)
    seasoned_ts = (now_dt - timedelta(hours=72)).isoformat().replace("+00:00", "Z")
    unseasoned_ts = (now_dt - timedelta(hours=6)).isoformat().replace("+00:00", "Z")
    exp_60d = (now_dt + timedelta(days=60)).isoformat().replace("+00:00", "Z")
    exp_10d = (now_dt + timedelta(days=10)).isoformat().replace("+00:00", "Z")
    exp_20d = (now_dt + timedelta(days=20)).isoformat().replace("+00:00", "Z")

    def item(cid, q, slug, accept_ts=seasoned_ts, end_iso=exp_60d, **overrides):
        base = {
            "condition_id": cid,
            "question": q,
            "market_slug": slug,
            "active": True,
            "closed": False,
            "archived": False,
            "accepting_orders": True,
            "enable_order_book": True,
            "seconds_delay": 0,
            "accepting_order_timestamp": accept_ts,
            "neg_risk": True,
            "neg_risk_market_id": "0xnr1",
            "tags": ["Politics", "Senate"],
            "end_date_iso": end_iso,
            "rewards": {"rates": [{"rewards_daily_rate": 120}], "min_size": 20, "max_spread": 4},
            "tokens": [
                {"token_id": f"{cid}-y", "outcome": "Yes"},
                {"token_id": f"{cid}-n", "outcome": "No"},
            ],
        }
        base.update(overrides)
        return base

    raw = [
        item("c-clean", "Will the US Senate pass the Farm Bill before 2027?", "us-senate-farm-bill"),
        item("c-unseasoned", "Will the US Senate pass Bill B before 2027?", "bill-b", accept_ts=unseasoned_ts),
        item("c-short-tte", "Will the US Senate pass Bill C?", "bill-c", end_iso=exp_10d),
        item("c-series-20d", "Will the Dodgers win the 2026 World Series?", "dodgers-world-series", end_iso=exp_20d),
        item("c-archived", "Will the US Senate pass Bill D?", "bill-d", archived=True),
        item("c-delayed", "Will the US Senate pass Bill E?", "bill-e", seconds_delay=3),
        item("c-not-accepting", "Will the US Senate pass Bill F?", "bill-f", accepting_orders=False),
    ]
    mock_clob = MagicMock()
    mock_clob.get_sampling_markets.return_value = {"data": raw, "next_cursor": "LTE="}
    client = make_client(mock_clob)

    conds = client.fetch_reward_conditions()
    assert [c.condition_id for c in conds] == ["c-clean"]
    c = conds[0]
    assert c.neg_risk_market_id == "0xnr1"
    assert c.tags == ("Politics", "Senate")
    assert c.accepting_order_timestamp is not None
    assert c.seconds_delay == 0
    assert c.accepting_orders is True
    assert c.archived is False
    assert c.window.accepting_order_timestamp == c.accepting_order_timestamp
    assert client._last_fill_risk_skipped.get("unseasoned") == 1
    assert client._last_fill_risk_skipped.get("delayed_matching") == 1
    assert client._last_fill_risk_skipped.get("short_tte") == 2

