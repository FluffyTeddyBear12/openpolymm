from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest
import requests

from pmus.clob_venue import ClobVenueClient, SAMPLING_CACHE_TTL_SEC
from pmus.config import Settings


def _make_sampling_market(
    condition_id: str,
    market_slug: str = "market-test",
    question: str = "Will it happen?",
    daily_rate: int | str = 100,
    min_size: int | str = 200,
    max_spread: float | str = 3.5,
    tokens: list[dict] | None = None,
) -> dict:
    if tokens is None:
        tokens = [
            {"token_id": f"{condition_id}-yes", "outcome": "Yes"},
            {"token_id": f"{condition_id}-no", "outcome": "No"},
        ]
    return {
        "condition_id": condition_id,
        "market_slug": market_slug,
        "question": question,
        "minimum_tick_size": "0.001",
        "minimum_order_size": "5",
        "active": True,
        "closed": False,
        "rewards": {
            "rates": [{"rewards_daily_rate": daily_rate}],
            "min_size": min_size,
            "max_spread": max_spread,
        },
        "tokens": tokens,
    }


def _make_test_client() -> ClobVenueClient:
    return ClobVenueClient(Settings(venue="global"), client=MagicMock(spec=[]))


def test_sampling_pagination_15_pages_chained_by_next_cursor(monkeypatch):
    pages = []
    for i in range(1, 16):
        next_cur = "LTE=" if i == 15 else f"cur_{i}"
        pages.append({
            "data": [_make_sampling_market(f"cond_{i}")],
            "next_cursor": next_cur,
        })

    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if not params or "next_cursor" not in params:
            resp.json.return_value = pages[0]
            return resp
        cursor = params["next_cursor"]
        idx = int(cursor.split("_")[1])
        resp.json.return_value = pages[idx]
        return resp

    monkeypatch.setattr("pmus.clob_venue.requests.get", fake_get)

    client = _make_test_client()
    markets = client._fetch_sampling_markets()

    assert len(markets) == 15
    assert [m.cond_id for m in markets] == [f"cond_{i}" for i in range(1, 16)]
    assert len(calls) == 15


def test_sampling_pagination_repeated_cursor_stops(monkeypatch, caplog):
    responses = [
        {"data": [_make_sampling_market("cond_1")], "next_cursor": "dup_cursor"},
        {"data": [_make_sampling_market("cond_2")], "next_cursor": "dup_cursor"},
    ]
    call_count = 0

    def fake_get(url, params=None, timeout=None):
        nonlocal call_count
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = responses[min(call_count, len(responses) - 1)]
        call_count += 1
        return resp

    monkeypatch.setattr("pmus.clob_venue.requests.get", fake_get)

    client = _make_test_client()
    with caplog.at_level("WARNING"):
        markets = client._fetch_sampling_markets()

    assert len(markets) == 2
    assert [m.cond_id for m in markets] == ["cond_1", "cond_2"]
    assert call_count == 2
    assert any("Repeated cursor dup_cursor" in rec.message for rec in caplog.records)


def test_sampling_pagination_failing_page_keeps_previous_pages(monkeypatch, caplog):
    def fake_get(url, params=None, timeout=None):
        if params and params.get("next_cursor") == "cur_4":
            raise requests.HTTPError("500 Server Error")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if not params or "next_cursor" not in params:
            resp.json.return_value = {
                "data": [_make_sampling_market("cond_1")],
                "next_cursor": "cur_1",
            }
            return resp
        cur_num = int(params["next_cursor"].split("_")[1])
        resp.json.return_value = {
            "data": [_make_sampling_market(f"cond_{cur_num + 1}")],
            "next_cursor": f"cur_{cur_num + 1}",
        }
        return resp

    monkeypatch.setattr("pmus.clob_venue.requests.get", fake_get)

    client = _make_test_client()
    with caplog.at_level("WARNING"):
        markets = client._fetch_sampling_markets()

    assert len(markets) == 4
    assert [m.cond_id for m in markets] == ["cond_1", "cond_2", "cond_3", "cond_4"]
    assert any("4 pages" in rec.message for rec in caplog.records)


def test_sampling_cache_ttl_and_invalidation(monkeypatch):
    call_count = 0

    def fake_get(url, params=None, timeout=None):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "data": [_make_sampling_market(f"cond_call_{call_count}")],
            "next_cursor": "LTE=",
        }
        return resp

    monkeypatch.setattr("pmus.clob_venue.requests.get", fake_get)

    client = _make_test_client()
    m1 = client._fetch_sampling_markets()
    assert call_count == 1
    assert m1[0].cond_id == "cond_call_1"

    m2 = client._fetch_sampling_markets()
    assert call_count == 1
    assert m2 == m1

    client._sampling_markets_cache_ts -= (SAMPLING_CACHE_TTL_SEC + 1.0)
    m3 = client._fetch_sampling_markets()
    assert call_count == 2
    assert m3[0].cond_id == "cond_call_2"

    client.invalidate_sampling_cache()
    m4 = client._fetch_sampling_markets()
    assert call_count == 3
    assert m4[0].cond_id == "cond_call_3"


def test_skipped_fill_risk_question_counted(monkeypatch, caplog):
    raw_data = [
        _make_sampling_market("c_keep", question="Will Team A win the match?"),
        _make_sampling_market("c_risk1", question="Will Company X announce layoffs?"),
        _make_sampling_market("c_risk2", question="Will Company Y launch a token?"),
    ]

    def fake_fill_risk_category(q: str):
        if "layoffs" in q:
            return "layoffs_risk"
        if "token" in q:
            return "crypto_risk"
        return None

    monkeypatch.setattr("pmus.clob_venue.fill_risk_category", fake_fill_risk_category)

    def fake_get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"data": raw_data, "next_cursor": "LTE="}
        return resp

    monkeypatch.setattr("pmus.clob_venue.requests.get", fake_get)

    client = _make_test_client()
    with caplog.at_level("INFO"):
        markets = client._fetch_sampling_markets()

    assert len(markets) == 1
    assert markets[0].cond_id == "c_keep"
    assert client._last_fill_risk_skipped == {
        "layoffs_risk": 1,
        "crypto_risk": 1,
    }
    assert any("Fill-risk skipped markets" in rec.message for rec in caplog.records)


def test_batch_books_chunk_isolation(caplog, monkeypatch):
    client = ClobVenueClient(Settings(venue="global", clob_host="https://clob.polymarket.com"))
    mock_sleep = MagicMock()
    monkeypatch.setattr("pmus.clob_venue.time.sleep", mock_sleep)

    chunk_1_attempts = 0

    def fake_post(url, json=None, timeout=None):
        nonlocal chunk_1_attempts
        requested_ids = [item["token_id"] for item in json]
        if "tok_fail_1" in requested_ids:
            chunk_1_attempts += 1
            raise requests.HTTPError("502 Bad Gateway")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [
            {
                "asset_id": tid,
                "timestamp": 1700000000000,
                "bids": [{"price": "0.50", "size": "100"}],
                "asks": [{"price": "0.55", "size": "100"}],
            }
            for tid in requested_ids
        ]
        return resp

    with patch("pmus.clob_venue.requests.post", side_effect=fake_post):
        with caplog.at_level("INFO"):
            tokens = ["tok_ok_1", "tok_ok_2", "tok_fail_1", "tok_fail_2"]
            books = client.fetch_books_batch(tokens, chunk_size=2)

    assert chunk_1_attempts == 2
    mock_sleep.assert_called_once_with(1.0)
    assert "tok_ok_1" in books
    assert "tok_ok_2" in books
    assert "tok_fail_1" not in books
    assert "tok_fail_2" not in books
    assert books["tok_ok_1"].bids[0].price == Decimal("0.50")

    warning_msgs = [rec.message for rec in caplog.records if rec.levelname == "WARNING"]
    assert any("chunk 1" in msg for msg in warning_msgs)

    info_msgs = [rec.message for rec in caplog.records if rec.levelname == "INFO"]
    assert any("4 tokens requested, 2 returned" in msg for msg in info_msgs)


def test_batch_books_chunk_retry_succeeds(monkeypatch):
    client = ClobVenueClient(Settings(venue="global", clob_host="https://clob.polymarket.com"))
    mock_sleep = MagicMock()
    monkeypatch.setattr("pmus.clob_venue.time.sleep", mock_sleep)

    attempts = 0

    def fake_post(url, json=None, timeout=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise requests.Timeout("Connection timed out")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [
            {
                "asset_id": "tok-1",
                "timestamp": 1700000000000,
                "bids": [{"price": "0.45", "size": "100"}],
                "asks": [{"price": "0.55", "size": "100"}],
            }
        ]
        return resp

    with patch("pmus.clob_venue.requests.post", side_effect=fake_post):
        books = client.fetch_books_batch(["tok-1"])

    assert attempts == 2
    mock_sleep.assert_called_once_with(1.0)
    assert "tok-1" in books
    assert books["tok-1"].bids[0].price == Decimal("0.45")


def test_sampling_mid_crawl_failure_uses_stale_cache_and_avoids_poisoning(monkeypatch, caplog):
    client = _make_test_client()

    # Step 1: initial complete crawl
    complete_page = {
        "data": [_make_sampling_market("cond_c1"), _make_sampling_market("cond_c2")],
        "next_cursor": "LTE=",
    }
    resp1 = MagicMock()
    resp1.raise_for_status.return_value = None
    resp1.json.return_value = complete_page
    monkeypatch.setattr("pmus.clob_venue.requests.get", lambda url, params=None, timeout=None: resp1)

    initial_markets = client._fetch_sampling_markets()
    assert len(initial_markets) == 2
    assert client._sampling_markets_cache is not None
    assert len(client._sampling_markets_cache) == 2

    client._sampling_markets_cache_ts -= (SAMPLING_CACHE_TTL_SEC + 60.0)
    call_count = 0

    def failing_get(url, params=None, timeout=None):
        nonlocal call_count
        call_count += 1
        if params and "next_cursor" in params:
            raise requests.HTTPError("500 Server Error on page 2")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "data": [_make_sampling_market("cond_poison_1")],
            "next_cursor": "cur_2",
        }
        return resp

    monkeypatch.setattr("pmus.clob_venue.requests.get", failing_get)

    with caplog.at_level("WARNING"):
        markets_after_fail = client._fetch_sampling_markets()

    # Returns the stale complete cache, NOT the truncated page 1
    assert len(markets_after_fail) == 2
    assert [m.cond_id for m in markets_after_fail] == ["cond_c1", "cond_c2"]
    assert len(client._sampling_markets_cache) == 2
    assert any("returning stale complete cache" in rec.message for rec in caplog.records)


def test_sampling_mid_crawl_failure_no_cache_returns_partial_without_caching(monkeypatch, caplog):
    client = _make_test_client()

    def failing_get(url, params=None, timeout=None):
        if params and "next_cursor" in params:
            raise requests.HTTPError("500 Server Error on page 2")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "data": [_make_sampling_market("cond_1")],
            "next_cursor": "cur_2",
        }
        return resp

    monkeypatch.setattr("pmus.clob_venue.requests.get", failing_get)

    with caplog.at_level("WARNING"):
        markets = client._fetch_sampling_markets()

    assert len(markets) == 1
    assert markets[0].cond_id == "cond_1"
    assert client._sampling_markets_cache is None
    assert any("without caching" in rec.message for rec in caplog.records)


def test_sampling_first_page_failure_uses_stale_cache_if_present(monkeypatch, caplog):
    client = _make_test_client()

    # Seed complete cache
    complete_page = {
        "data": [_make_sampling_market("cond_cached")],
        "next_cursor": "LTE=",
    }
    resp1 = MagicMock()
    resp1.raise_for_status.return_value = None
    resp1.json.return_value = complete_page
    monkeypatch.setattr("pmus.clob_venue.requests.get", lambda url, params=None, timeout=None: resp1)
    client._fetch_sampling_markets()

    client._sampling_markets_cache_ts -= (SAMPLING_CACHE_TTL_SEC + 60.0)

    def raise_get(url, params=None, timeout=None):
        raise requests.HTTPError("503 Service Unavailable")

    monkeypatch.setattr("pmus.clob_venue.requests.get", raise_get)

    with caplog.at_level("WARNING"):
        markets = client._fetch_sampling_markets()

    assert len(markets) == 1
    assert markets[0].cond_id == "cond_cached"
    assert any("First page of sampling-markets failed" in rec.message for rec in caplog.records)


def test_sampling_first_page_failure_raises_without_cache(monkeypatch):
    client = _make_test_client()

    def raise_get(url, params=None, timeout=None):
        raise requests.HTTPError("503 Service Unavailable")

    monkeypatch.setattr("pmus.clob_venue.requests.get", raise_get)

    with pytest.raises(requests.HTTPError):
        client._fetch_sampling_markets()


def test_sampling_max_pages_below_limit_bypasses_cache_read_and_write(monkeypatch):
    client = _make_test_client()

    # Seed cache with MAX_SAMPLING_PAGES
    resp1 = MagicMock()
    resp1.raise_for_status.return_value = None
    resp1.json.return_value = {"data": [_make_sampling_market("cond_full")], "next_cursor": "LTE="}
    monkeypatch.setattr("pmus.clob_venue.requests.get", lambda url, params=None, timeout=None: resp1)
    client._fetch_sampling_markets()
    assert client._sampling_markets_cache is not None

    # Fetch with max_pages < MAX_SAMPLING_PAGES
    call_count = 0

    def fake_get(url, params=None, timeout=None):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"data": [_make_sampling_market("cond_small")], "next_cursor": "LTE="}
        return resp

    monkeypatch.setattr("pmus.clob_venue.requests.get", fake_get)
    markets = client._fetch_sampling_markets(max_pages=2)

    assert call_count == 1
    assert markets[0].cond_id == "cond_small"
    # Did not overwrite cache
    assert client._sampling_markets_cache[0].cond_id == "cond_full"


def test_sampling_tokens_stored_as_immutable_tuple(monkeypatch):
    raw_tokens = [
        {"token_id": "tok_y", "outcome": "Yes"},
        {"token_id": "tok_n", "outcome": "No"},
    ]
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "data": [_make_sampling_market("c1", tokens=raw_tokens)],
        "next_cursor": "LTE=",
    }
    monkeypatch.setattr("pmus.clob_venue.requests.get", lambda url, params=None, timeout=None: resp)

    client = _make_test_client()
    markets = client._fetch_sampling_markets()

    m = markets[0]
    assert isinstance(m.tokens, tuple)
    with pytest.raises(TypeError):
        m.tokens[0] = {"token_id": "hacked"}

    # Mutating raw_tokens dict does not mutate m.tokens dict copy
    raw_tokens[0]["outcome"] = "Changed"
    assert m.tokens[0]["outcome"] == "Yes"


def test_sampling_raw_resp_data_none_handled_cleanly(monkeypatch):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"data": None, "next_cursor": None}
    monkeypatch.setattr("pmus.clob_venue.requests.get", lambda url, params=None, timeout=None: resp)

    client = _make_test_client()
    markets = client._fetch_sampling_markets()
    assert markets == []


def test_sampling_filtered_market_metadata_recorded_for_resolve_market(monkeypatch):
    from pmus.engine import resolve_market

    raw_data = [
        {
            "condition_id": "c_weather",
            "market_slug": "weather-market",
            "question": "What is the temperature in Chicago?",
            "minimum_tick_size": "0.01",
            "minimum_order_size": "10",
            "active": True,
            "closed": False,
            "rewards": {
                "rates": [{"rewards_daily_rate": 100}],
                "min_size": 200,
                "max_spread": 3.5,
            },
            "tokens": [
                {"token_id": "tok_weather_y", "outcome": "Yes"},
                {"token_id": "tok_weather_n", "outcome": "No"},
            ],
        },
        {
            "condition_id": "c_layoffs",
            "market_slug": "layoffs-market",
            "question": "Will BigCorp announce layoffs this quarter?",
            "minimum_tick_size": "0.05",
            "minimum_order_size": "25",
            "active": True,
            "closed": False,
            "rewards": {
                "rates": [{"rewards_daily_rate": 100}],
                "min_size": 200,
                "max_spread": 3.5,
            },
            "tokens": [
                {"token_id": "tok_layoffs_y", "outcome": "Yes"},
                {"token_id": "tok_layoffs_n", "outcome": "No"},
            ],
        },
    ]

    monkeypatch.setattr("pmus.clob_venue.fill_risk_category", lambda q: "layoffs_risk" if "layoffs" in q else None)

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"data": raw_data, "next_cursor": "LTE="}
    monkeypatch.setattr("pmus.clob_venue.requests.get", lambda url, params=None, timeout=None: resp)

    client = _make_test_client()
    markets = client._fetch_sampling_markets()

    assert len(markets) == 0

    m_weather = resolve_market(client, "tok_weather_y")
    assert m_weather.tick == Decimal("0.01")
    assert m_weather.min_qty == Decimal("10")
    assert m_weather.slug == "tok_weather_y"

    m_layoffs = resolve_market(client, "tok_layoffs_n")
    assert m_layoffs.tick == Decimal("0.05")
    assert m_layoffs.min_qty == Decimal("25")
    assert m_layoffs.slug == "tok_layoffs_n"


def test_sampling_cache_serves_fresh_within_300s_ttl(monkeypatch):
    client = _make_test_client()
    calls = 0

    def counting_get(url, params=None, timeout=None):
        nonlocal calls
        calls += 1
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"data": [_make_sampling_market("cond_ttl")], "next_cursor": "LTE="}
        return resp

    monkeypatch.setattr("pmus.clob_venue.requests.get", counting_get)
    first = client._fetch_sampling_markets()
    assert len(first) == 1
    assert calls == 1

    client._sampling_markets_cache_ts -= 240.0
    second = client._fetch_sampling_markets()
    assert len(second) == 1
    assert calls == 1

