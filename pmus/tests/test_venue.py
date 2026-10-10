import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from pmus.venue import PublicGatewayClient

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
