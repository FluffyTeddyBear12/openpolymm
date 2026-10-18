from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from pmus.config import Settings
from pmus.clob_venue import ClobVenueClient
from pmus.models import ConditionMarket, Market


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
        "rewards": {
            "rates": [{"rewards_daily_rate": daily_rate}],
            "min_size": min_size,
            "max_spread": max_spread,
        },
        "tokens": tokens,
    }


def test_yes_no_grouping_by_outcome_even_if_no_first():
    mock_clob = MagicMock()
    mock_clob.get_sampling_markets.return_value = {
        "data": [
            _make_sampling_market(
                condition_id="0xreversed",
                tokens=[
                    {"token_id": "tok-no-1", "outcome": "No"},
                    {"token_id": "tok-yes-1", "outcome": "Yes"},
                ],
            ),
            _make_sampling_market(
                condition_id="0xnormal",
                tokens=[
                    {"token_id": "tok-yes-2", "outcome": "Yes"},
                    {"token_id": "tok-no-2", "outcome": "No"},
                ],
            ),
        ]
    }
    client = ClobVenueClient(Settings(venue="global"), client=mock_clob)
    conditions = client.fetch_reward_conditions()

    assert len(conditions) == 2

    c_rev = conditions[0]
    assert c_rev.condition_id == "0xreversed"
    assert c_rev.yes.slug == "tok-yes-1"
    assert c_rev.no.slug == "tok-no-1"

    c_norm = conditions[1]
    assert c_norm.condition_id == "0xnormal"
    assert c_norm.yes.slug == "tok-yes-2"
    assert c_norm.no.slug == "tok-no-2"


def test_named_outcome_fallback_by_order():
    mock_clob = MagicMock()
    mock_clob.get_sampling_markets.return_value = {
        "data": [
            _make_sampling_market(
                condition_id="0xnamed",
                tokens=[
                    {"token_id": "tok-chiefs", "outcome": "Kansas City Chiefs"},
                    {"token_id": "tok-eagles", "outcome": "Philadelphia Eagles"},
                ],
            )
        ]
    }
    client = ClobVenueClient(Settings(venue="global"), client=mock_clob)
    conditions = client.fetch_reward_conditions()

    assert len(conditions) == 1
    c = conditions[0]
    assert c.condition_id == "0xnamed"
    assert c.yes.slug == "tok-chiefs"
    assert c.no.slug == "tok-eagles"


def test_market_with_1_or_3_tokens_skipped():
    mock_clob = MagicMock()
    mock_clob.get_sampling_markets.return_value = {
        "data": [
            _make_sampling_market(
                condition_id="0xone",
                tokens=[{"token_id": "tok-1", "outcome": "Yes"}],
            ),
            _make_sampling_market(
                condition_id="0xthree",
                tokens=[
                    {"token_id": "tok-a", "outcome": "Option A"},
                    {"token_id": "tok-b", "outcome": "Option B"},
                    {"token_id": "tok-c", "outcome": "Option C"},
                ],
            ),
            _make_sampling_market(
                condition_id="0xempty_tok",
                tokens=[
                    {"token_id": "tok-valid", "outcome": "Yes"},
                    {"token_id": "", "outcome": "No"},
                ],
            ),
            _make_sampling_market(
                condition_id="0xvalid_two",
                tokens=[
                    {"token_id": "tok-yes-v", "outcome": "Yes"},
                    {"token_id": "tok-no-v", "outcome": "No"},
                ],
            ),
        ]
    }
    client = ClobVenueClient(Settings(venue="global"), client=mock_clob)
    conditions = client.fetch_reward_conditions()

    assert len(conditions) == 1
    assert conditions[0].condition_id == "0xvalid_two"
    assert conditions[0].yes.slug == "tok-yes-v"
    assert conditions[0].no.slug == "tok-no-v"


def test_zero_pool_skipped():
    mock_clob = MagicMock()
    mock_clob.get_sampling_markets.return_value = {
        "data": [
            _make_sampling_market(condition_id="0xzero", daily_rate=0),
            _make_sampling_market(condition_id="0xneg", daily_rate="-10"),
            _make_sampling_market(condition_id="0xpositive", daily_rate=150),
        ]
    }
    client = ClobVenueClient(Settings(venue="global"), client=mock_clob)
    conditions = client.fetch_reward_conditions()

    assert len(conditions) == 1
    assert conditions[0].condition_id == "0xpositive"
    assert conditions[0].window.pool_usd == Decimal("150")


def test_window_max_spread_divided_by_100_and_target_size_min_size():
    mock_clob = MagicMock()
    mock_clob.get_sampling_markets.return_value = {
        "data": [
            _make_sampling_market(
                condition_id="0xspread_calc",
                market_slug="slug-calc",
                question="Will the home team win?",
                daily_rate="300",
                min_size="250",
                max_spread="4.5",
            )
        ]
    }
    client = ClobVenueClient(Settings(venue="global"), client=mock_clob)
    conditions = client.fetch_reward_conditions()

    assert len(conditions) == 1
    c = conditions[0]
    assert isinstance(c, ConditionMarket)
    assert c.condition_id == "0xspread_calc"
    assert c.event_id == "slug-calc"
    assert c.question == "Will the home team win?"
    assert c.window.slug == "0xspread_calc"
    assert c.window.program_id == "0xspread_calc"
    assert c.window.pool_usd == Decimal("300")
    assert c.window.target_size == Decimal("250")
    assert c.window.max_spread == Decimal("0.045")
    assert c.window.discount == Decimal("0.30")
    assert c.window.status == "active"


def test_limit_counts_conditions():
    mock_clob = MagicMock()
    mock_clob.get_sampling_markets.return_value = {
        "data": [
            _make_sampling_market(condition_id=f"0xcond_{i}")
            for i in range(5)
        ]
    }
    client = ClobVenueClient(Settings(venue="global"), client=mock_clob)

    limited = client.fetch_reward_conditions(limit=2)
    assert len(limited) == 2
    assert [c.condition_id for c in limited] == ["0xcond_0", "0xcond_1"]

    all_conds = client.fetch_reward_conditions(limit=0)
    assert len(all_conds) == 5


def test_market_cache_populated_for_both_tokens():
    mock_clob = MagicMock()
    mock_clob.get_sampling_markets.return_value = {
        "data": [
            _make_sampling_market(
                condition_id="0xcached",
                market_slug="cached-slug",
                question="Cached question?",
                tokens=[
                    {"token_id": "tok-y-cache", "outcome": "Yes"},
                    {"token_id": "tok-n-cache", "outcome": "No"},
                ],
            )
        ]
    }
    client = ClobVenueClient(Settings(venue="global"), client=mock_clob)
    assert len(client._market_cache) == 0

    conditions = client.fetch_reward_conditions()
    assert len(conditions) == 1
    assert "tok-y-cache" in client._market_cache
    assert "tok-n-cache" in client._market_cache

    cached_yes = client._market_cache["tok-y-cache"]
    cached_no = client._market_cache["tok-n-cache"]
    assert cached_yes == conditions[0].yes
    assert cached_no == conditions[0].no
    assert cached_yes.event_id == "cached-slug"
    assert cached_yes.question == "Cached question?"
    assert cached_yes.category == "global"
    assert cached_yes.active is True


def test_fetch_reward_markets_output_unchanged():
    mock_clob = MagicMock()
    mock_clob.get_sampling_markets.return_value = {
        "data": [
            _make_sampling_market(
                condition_id="0xunchanged",
                market_slug="slug-unchanged",
                question="Unchanged?",
                daily_rate=100,
                min_size=200,
                max_spread=3.5,
                tokens=[
                    {"token_id": "tok-yes-u", "outcome": "Yes"},
                    {"token_id": "tok-no-u", "outcome": "No"},
                ],
            )
        ]
    }
    client = ClobVenueClient(Settings(venue="global"), client=mock_clob)
    pairs = client.fetch_reward_markets()

    assert len(pairs) == 2

    m_yes, w_yes = pairs[0]
    assert m_yes.slug == "tok-yes-u"
    assert m_yes.event_id == "slug-unchanged"
    assert m_yes.question == "Unchanged?"
    assert m_yes.tick == Decimal("0.001")
    assert m_yes.min_qty == Decimal("5")
    assert m_yes.category == "global"
    assert m_yes.active is True

    assert w_yes.slug == "tok-yes-u"
    assert w_yes.program_id == "0xunchanged"
    assert w_yes.pool_usd == Decimal("100")
    assert w_yes.discount == Decimal("0.30")
    assert w_yes.target_size == Decimal("200")
    assert w_yes.max_spread == Decimal("0.035")
    assert w_yes.status == "active"

    m_no, w_no = pairs[1]
    assert m_no.slug == "tok-no-u"
    assert w_no.slug == "tok-no-u"


def test_fetch_sampling_markets_paginates_next_cursor_and_deduplicates(monkeypatch):
    mock_clob = MagicMock()
    mock_clob.get_sampling_markets.return_value = {
        "data": [_make_sampling_market("0xp1", daily_rate=100)],
        "next_cursor": "MTAwMA==",
    }

    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params, timeout))
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "data": [
                _make_sampling_market("0xp1", daily_rate=100),
                _make_sampling_market("0xp2", daily_rate=250),
            ],
            "next_cursor": "LTE=",
        }
        return resp

    monkeypatch.setattr("pmus.clob_venue.requests.get", fake_get)

    client = ClobVenueClient(Settings(venue="global"), client=mock_clob)
    conditions = client.fetch_reward_conditions()

    assert len(calls) == 1
    assert calls[0][1] == {"next_cursor": "MTAwMA=="}
    assert [c.condition_id for c in conditions] == ["0xp1", "0xp2"]
    assert [c.window.pool_usd for c in conditions] == [Decimal("100"), Decimal("250")]

