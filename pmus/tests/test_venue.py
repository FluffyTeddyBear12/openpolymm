import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from pmus.models import Level, Position
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

