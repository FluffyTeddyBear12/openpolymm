import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from pmus.clob_venue import ClobVenueClient, parse_trade_fills
from pmus.config import Settings
from pmus.models import Level, Position, Quote
from pmus.venue import AuthenticatedClient, PublicGatewayClient

FIXTURES_DIR = Path(__file__).parent / "fixtures"

def test_parse_markets_fixture():
    with open(FIXTURES_DIR / "markets.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    markets = [PublicGatewayClient.parse_market(m) for m in data["markets"]]
    assert len(markets) > 0
    first = markets[0]
    assert isinstance(first.tick, Decimal)
    assert isinstance(first.min_qty, Decimal)
    assert isinstance(first.slug, str)
    assert isinstance(first.active, bool)

def test_parse_book_fixture():
    with open(FIXTURES_DIR / "book.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    book = PublicGatewayClient.parse_book("tec-mlb-nlchamp-2026-09-27-lad", data["marketData"])
    assert book.slug == "tec-mlb-nlchamp-2026-09-27-lad"
    assert len(book.bids) > 0
    assert len(book.asks) > 0
    for i in range(len(book.bids) - 1):
        assert book.bids[i].price >= book.bids[i + 1].price
    for i in range(len(book.asks) - 1):
        assert book.asks[i].price <= book.asks[i + 1].price
    assert isinstance(book.ts_ms, int)

def test_parse_incentives_fixture():
    with open(FIXTURES_DIR / "incentives.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    windows = PublicGatewayClient.parse_incentives(data)
    assert len(windows) > 0
    first = windows[0]
    assert isinstance(first.pool_usd, Decimal)
    assert isinstance(first.discount, Decimal)
    assert isinstance(first.target_size, Decimal)
    assert first.start is not None

def test_429_backoff_retry():
    mock_session = MagicMock()
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "0"}

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"markets": []}

    mock_session.request.side_effect = [resp_429, resp_200]
    client = PublicGatewayClient(session=mock_session, timeout=1.0, max_retries=2)
    res = client.fetch_markets()
    assert res == []
    assert mock_session.request.call_count == 2

def test_close_position_safe_tight_bid_calls_close_position():
    mock_sdk = MagicMock()
    mock_sdk.orders.close_position.return_value = {"id": "exec-1"}
    client = AuthenticatedClient(sdk=mock_sdk)

    pos = Position(slug="slug-tight", net_qty=Decimal("10"), avg_price=Decimal("0.500"))
    bbo = (Level(Decimal("0.490"), Decimal("100")), Level(Decimal("0.510"), Decimal("100")))

    order = client.close_position_safe("slug-tight", pos, bbo=bbo, max_loss_cents=Decimal("0.02"))
    assert order is None
    mock_sdk.orders.close_position.assert_called_once_with({
        "marketSlug": "slug-tight",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    })
    mock_sdk.orders.create_many.assert_not_called()

def test_close_position_safe_wide_bid_places_passive_limit_order():
    mock_sdk = MagicMock()
    mock_sdk.orders.create_many.return_value = {"createdOrderIds": ["ord-passive-1"]}
    client = AuthenticatedClient(sdk=mock_sdk)

    pos = Position(slug="slug-wide", net_qty=Decimal("10"), avg_price=Decimal("0.500"))
    bbo = (Level(Decimal("0.400"), Decimal("100")), Level(Decimal("0.510"), Decimal("100")))

    order = client.close_position_safe("slug-wide", pos, bbo=bbo, max_loss_cents=Decimal("0.02"))
    assert order is not None
    assert order.order_id == "ord-passive-1"
    assert order.slug == "slug-wide"
    assert order.side == "SELL_LONG"
    assert order.price == Decimal("0.500")
    assert order.qty_open == Decimal("10")
    mock_sdk.orders.close_position.assert_not_called()

def test_close_position_safe_missing_bid_places_passive_limit_order():
    mock_sdk = MagicMock()
    mock_sdk.orders.create_many.return_value = {"createdOrderIds": ["ord-passive-2"]}
    client = AuthenticatedClient(sdk=mock_sdk)

    pos = Position(slug="slug-missing", net_qty=Decimal("10"), avg_price=Decimal("0.500"))
    bbo = (None, Level(Decimal("0.510"), Decimal("100")))

    order = client.close_position_safe("slug-missing", pos, bbo=bbo, max_loss_cents=Decimal("0.02"))
    assert order is not None
    assert order.order_id == "ord-passive-2"
    assert order.slug == "slug-missing"
    assert order.side == "SELL_LONG"
    assert order.price == Decimal("0.500")
    assert order.qty_open == Decimal("10")
    mock_sdk.orders.close_position.assert_not_called()


def test_position_mtm_defaults_and_data_api_hydration():
    default_pos = Position(slug="tok-default", net_qty=Decimal("5"), avg_price=Decimal("0.40"))
    assert default_pos.cur_price is None
    assert default_pos.redeemable is False
    assert default_pos.archived is False

    page = [
        {"asset": "tok-win", "size": 25.0, "avgPrice": 0.42, "curPrice": 1.0, "redeemable": True, "archived": True},
        {"asset": "tok-lose", "size": 10.0, "avgPrice": 0.65, "curPrice": 0, "redeemable": True, "archived": False},
        {"asset": "tok-plain", "size": 8.0, "avgPrice": 0.50},
    ]
    resp = MagicMock()
    resp.json.return_value = page
    client = ClobVenueClient(Settings(venue="global", polymarket_proxy_address="0xproxy"), client=MagicMock())
    client.is_authenticated = True

    with patch("requests.get", return_value=resp):
        positions = client.get_positions()

    assert positions[0] == Position(
        slug="tok-win",
        net_qty=Decimal("25.0"),
        avg_price=Decimal("0.42"),
        cur_price=Decimal("1.0"),
        redeemable=True,
        archived=True,
    )
    assert positions[1] == Position(
        slug="tok-lose",
        net_qty=Decimal("10.0"),
        avg_price=Decimal("0.65"),
        cur_price=Decimal("0"),
        redeemable=True,
        archived=False,
    )
    assert positions[2] == Position(
        slug="tok-plain",
        net_qty=Decimal("8.0"),
        avg_price=Decimal("0.50"),
    )


def test_parse_trade_fills_taker_vwap_sweep_and_fallback():
    sweep_trade = {
        "id": "t-sweep",
        "taker_order_id": "0xtaker-sweep",
        "asset_id": "tok-yes",
        "side": "SELL",
        "size": "59.99",
        "price": "0.37",
        "status": "MATCHED",
        "match_time": "1791596200",
        "fee_rate_bps": "0",
        "trader_side": "TAKER",
        "maker_orders": [
            {"order_id": "m1", "asset_id": "tok-yes", "matched_amount": "20", "price": "0.37"},
            {"order_id": "m2", "asset_id": "tok-yes", "matched_amount": "39.99", "price": "0.3525"},
        ],
    }
    fills = parse_trade_fills(sweep_trade, "0xproxy")
    assert len(fills) == 1
    assert fills[0].is_maker is False
    assert fills[0].qty == Decimal("59.99")
    assert (fills[0].price * fills[0].qty).quantize(Decimal("0.000001")) == Decimal("21.496475")
    assert fills[0].price < Decimal("0.37")

    cross_asset_sweep = {
        **sweep_trade,
        "id": "t-cross",
        "maker_orders": [
            {"order_id": "m1", "asset_id": "tok-yes", "matched_amount": "20", "price": "0.37"},
            {"order_id": "m2", "asset_id": "tok-no", "matched_amount": "39.99", "price": "0.6475"},
        ],
    }
    cross_fills = parse_trade_fills(cross_asset_sweep, "0xproxy")
    assert len(cross_fills) == 1
    assert (cross_fills[0].price * cross_fills[0].qty).quantize(Decimal("0.000001")) == Decimal("21.496475")

    empty_makers = {**sweep_trade, "id": "t-empty", "maker_orders": []}
    fallback_fills = parse_trade_fills(empty_makers, "0xproxy")
    assert len(fallback_fills) == 1
    assert fallback_fills[0].qty == Decimal("59.99")
    assert fallback_fills[0].price == Decimal("0.37")


def test_create_orders_batch_isolates_order_exceptions_and_rounds_floats(caplog):
    mock_clob = MagicMock()
    mock_clob.create_and_post_order.side_effect = [
        {"orderID": "ord-1", "status": "live", "success": True},
        RuntimeError("PolyApiException: invalid tick size"),
        {"orderID": "ord-3", "status": "live", "success": True},
    ]
    client = ClobVenueClient(Settings(venue="global", polygon_private_key="dummy"), client=mock_clob)
    client.is_authenticated = True

    quotes = [
        Quote(slug="tok-1", side="BUY_LONG", price=Decimal("0.30004"), qty=Decimal("19.99999")),
        Quote(slug="tok-2", side="SELL_LONG", price=Decimal("0.5500"), qty=Decimal("10")),
        Quote(slug="tok-3", side="BUY_LONG", price=Decimal("0.4125"), qty=Decimal("25.12344")),
    ]
    with caplog.at_level("WARNING"):
        created = client.create_orders_batch(quotes)

    assert [o.order_id for o in created] == ["ord-1", "ord-3"]
    assert any("Order post failed for tok-2" in rec.message for rec in caplog.records)

    first_args = mock_clob.create_and_post_order.call_args_list[0][0][0]
    third_args = mock_clob.create_and_post_order.call_args_list[2][0][0]
    assert first_args.price == 0.3
    assert first_args.size == 20.0
    assert third_args.price == 0.4125
    assert third_args.size == 25.1234


def test_get_buying_power_accepts_zero_balance_when_resolved_or_probing():
    mock_clob = MagicMock()
    mock_clob.get_balance_allowance.return_value = {"balance": "100000000"}
    client = ClobVenueClient(Settings(venue="global", polygon_private_key="dummy"), client=mock_clob)
    client.is_authenticated = True

    assert client.get_buying_power() == Decimal("100")
    assert client._resolved_bp_sig_type == 3

    client._buying_power_cache_ts -= 10.0
    mock_clob.get_balance_allowance.return_value = {"balance": "0"}
    assert client.get_buying_power() == Decimal("0")
    assert client._buying_power_cache == Decimal("0")

    probe_clob = MagicMock()
    probe_clob.get_balance_allowance.return_value = {"balance": "0"}
    probe_client = ClobVenueClient(Settings(venue="global", polygon_private_key="dummy"), client=probe_clob)
    probe_client.is_authenticated = True
    probe_client._buying_power_cache = Decimal("50")
    probe_client._buying_power_cache_ts = 0.0

    assert probe_client.get_buying_power() == Decimal("0")
    assert probe_client._buying_power_cache == Decimal("0")
    assert probe_client._resolved_bp_sig_type is None


def test_sampling_markets_filters_sports_tags_and_word_bounded_fill_risk():
    now_dt = datetime.now(timezone.utc)
    seasoned_ts = (now_dt - timedelta(hours=72)).isoformat().replace("+00:00", "Z")
    exp_60d = (now_dt + timedelta(days=60)).isoformat().replace("+00:00", "Z")

    def make_item(cid: str, question: str, tags: list[str]) -> dict:
        return {
            "condition_id": cid,
            "market_slug": cid,
            "question": question,
            "active": True,
            "closed": False,
            "archived": False,
            "accepting_orders": True,
            "enable_order_book": True,
            "seconds_delay": 0,
            "accepting_order_timestamp": seasoned_ts,
            "end_date_iso": exp_60d,
            "tags": tags,
            "minimum_tick_size": "0.01",
            "minimum_order_size": "5",
            "volume24hr": 10000,
            "rewards": {"rates": [{"rewards_daily_rate": 100}], "min_size": 50, "max_spread": 3.5},
            "tokens": [
                {"token_id": f"{cid}-yes", "outcome": "Yes"},
                {"token_id": f"{cid}-no", "outcome": "No"},
            ],
        }

    mock_clob = MagicMock()
    mock_clob.get_sampling_markets.return_value = {
        "data": [
            make_item("c-cockpit", "Will commercial cockpit automation certification pass in 2027?", ["Aviation"]),
            make_item("c-sports-tag", "Will Team Alpha claim the championship trophy?", ["Sports", "Soccer"]),
            make_item(
                "c-selic",
                "Will the Bank of Brazil make no change to the target for the Selic rate at the December meeting?",
                [],
            ),
        ],
        "next_cursor": "LTE=",
    }
    client = ClobVenueClient(Settings(venue="global"), client=mock_clob)
    conds = client.fetch_reward_conditions()

    assert [c.condition_id for c in conds] == ["c-cockpit"]
    assert client._last_fill_risk_skipped.get("sports_tag") == 1
    assert client._last_fill_risk_skipped.get("macro_release") == 1
    assert client.get_market("c-sports-tag-yes") is not None


