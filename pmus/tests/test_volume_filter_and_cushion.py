from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from pmus.clob_venue import ClobVenueClient
from pmus.config import Settings
from pmus.models import Book, ConditionMarket, IncentiveWindow, Level, Market
from pmus.strategy import condition_selector, global_rewards
from pmus.strategy.condition_selector import ConditionTarget, risk_adjustment_multiplier

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def test_settings_min_24h_volume_config(monkeypatch):
    monkeypatch.delenv("PMUS_MIN_24H_VOLUME", raising=False)
    s_default = Settings.from_env()
    assert s_default.min_24h_volume == Decimal("5000.00")

    monkeypatch.setenv("PMUS_MIN_24H_VOLUME", "12500.50")
    s_custom = Settings.from_env()
    assert s_custom.min_24h_volume == Decimal("12500.50")


def test_models_store_volume_24h():
    win = IncentiveWindow(
        slug="mkt-1",
        program_id="p-1",
        pool_usd=Decimal("100"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=NOW,
        end=None,
        status="active",
        volume_24h=Decimal("18500.25"),
    )
    assert win.volume_24h == Decimal("18500.25")

    mkt_y = Market(slug="mkt-1-y", event_id="ev-1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)
    mkt_n = Market(slug="mkt-1-n", event_id="ev-1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)
    cond = ConditionMarket(
        condition_id="c-1",
        event_id="ev-1",
        question="Will X occur?",
        yes=mkt_y,
        no=mkt_n,
        window=win,
        volume_24h=Decimal("18500.25"),
    )
    assert cond.volume_24h == Decimal("18500.25")


def test_clob_venue_enriches_volume_from_gamma_and_filters_low_volume(monkeypatch):
    settings = Settings(venue="global", min_24h_volume=Decimal("5000.00"))
    mock_clob = MagicMock()
    client = ClobVenueClient(settings, client=mock_clob)

    sampling_payload = [
        {
            "condition_id": "c-low-vol",
            "question": "Will candidate A win in 2026?",
            "minimum_tick_size": "0.001",
            "minimum_order_size": "5",
            "active": True,
            "closed": False,
            "rewards": {
                "rates": [{"rewards_daily_rate": 100}],
                "min_size": 100,
                "max_spread": 4.0,
            },
            "tokens": [
                {"token_id": "tok-low-y", "outcome": "Yes"},
                {"token_id": "tok-low-n", "outcome": "No"},
            ],
            "end_date_iso": (NOW + timedelta(days=30)).isoformat(),
        },
        {
            "condition_id": "c-high-vol",
            "question": "Will candidate B win in 2026?",
            "minimum_tick_size": "0.001",
            "minimum_order_size": "5",
            "active": True,
            "closed": False,
            "rewards": {
                "rates": [{"rewards_daily_rate": 200}],
                "min_size": 100,
                "max_spread": 4.0,
            },
            "tokens": [
                {"token_id": "tok-high-y", "outcome": "Yes"},
                {"token_id": "tok-high-n", "outcome": "No"},
            ],
            "end_date_iso": (NOW + timedelta(days=30)).isoformat(),
        },
    ]

    gamma_payload = [
        {"condition_id": "c-low-vol", "volume24hr": 1200.50},
        {"condition_id": "c-high-vol", "volume24hr": 25000.00},
    ]

    mock_resp = MagicMock()
    mock_resp.json.return_value = gamma_payload
    mock_clob.get_sampling_markets.return_value = {"data": sampling_payload}

    with patch("requests.get", return_value=mock_resp) as mock_get:
        conditions = client.fetch_reward_conditions()

        assert mock_get.called
        assert len(conditions) == 1
        assert conditions[0].condition_id == "c-high-vol"
        assert conditions[0].volume_24h == Decimal("25000.00")
        assert conditions[0].window.volume_24h == Decimal("25000.00")


def test_clob_venue_gamma_fallback_on_network_error():
    settings = Settings(venue="global", min_24h_volume=Decimal("5000.00"))
    mock_clob = MagicMock()
    client = ClobVenueClient(settings, client=mock_clob)

    sampling_payload = [
        {
            "condition_id": "c-fallback",
            "question": "Will fallback succeed in 2026?",
            "minimum_tick_size": "0.001",
            "minimum_order_size": "5",
            "active": True,
            "closed": False,
            "volume24hr": 8000.0,
            "rewards": {
                "rates": [{"rewards_daily_rate": 100}],
                "min_size": 100,
                "max_spread": 4.0,
            },
            "tokens": [
                {"token_id": "tok-f-y", "outcome": "Yes"},
                {"token_id": "tok-f-n", "outcome": "No"},
            ],
            "end_date_iso": (NOW + timedelta(days=30)).isoformat(),
        }
    ]
    mock_clob.get_sampling_markets.return_value = {"data": sampling_payload}

    with patch("requests.get", side_effect=RuntimeError("Timeout")):
        conditions = client.fetch_reward_conditions()
        assert len(conditions) == 1
        assert conditions[0].condition_id == "c-fallback"
        assert conditions[0].volume_24h == Decimal("8000.0")


def test_risk_adjustment_multiplier_volume_factor():
    mkt_y = Market(slug="mkt-y", event_id="ev-1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)
    mkt_n = Market(slug="mkt-n", event_id="ev-1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)
    book_y = Book("mkt-y", (Level(Decimal("0.50"), Decimal("200")),), (Level(Decimal("0.52"), Decimal("200")),), 1000)
    book_n = Book("mkt-n", (Level(Decimal("0.48"), Decimal("200")),), (Level(Decimal("0.50"), Decimal("200")),), 1000)

    win_zero = IncentiveWindow(
        slug="mkt-zero",
        program_id="p-zero",
        pool_usd=Decimal("100"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=NOW - timedelta(days=1),
        end=NOW + timedelta(days=20),
        status="active",
        volume_24h=Decimal("0"),
    )
    cond_zero = ConditionMarket("c-zero", "ev-1", "Q?", mkt_y, mkt_n, win_zero, volume_24h=Decimal("0"))
    target_zero = ConditionTarget(
        condition=cond_zero,
        mode="two_sided",
        size=Decimal("50"),
        yes_price=Decimal("0.50"),
        no_price=Decimal("0.48"),
        cost=Decimal("49"),
        expected_reward_sec=Decimal("0.001"),
    )
    mult_zero = risk_adjustment_multiplier(target_zero, book_y, book_n)

    win_high = IncentiveWindow(
        slug="mkt-high",
        program_id="p-high",
        pool_usd=Decimal("100"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=NOW - timedelta(days=1),
        end=NOW + timedelta(days=20),
        status="active",
        volume_24h=Decimal("100000"),
    )
    cond_high = ConditionMarket("c-high", "ev-1", "Q?", mkt_y, mkt_n, win_high, volume_24h=Decimal("100000"))
    target_high = ConditionTarget(
        condition=cond_high,
        mode="two_sided",
        size=Decimal("50"),
        yes_price=Decimal("0.50"),
        no_price=Decimal("0.48"),
        cost=Decimal("49"),
        expected_reward_sec=Decimal("0.001"),
    )
    mult_high = risk_adjustment_multiplier(target_high, book_y, book_n)

    assert mult_high > mult_zero
    assert abs((mult_high / mult_zero) - Decimal("1.25")) < Decimal("0.05")


def test_configurable_max_cushion_spread_ratio(monkeypatch):
    b_wide_cent = Book(
        slug="tok",
        bids=(Level(Decimal("0.59"), Decimal("100")), Level(Decimal("0.58"), Decimal("100"))),
        asks=(Level(Decimal("0.62"), Decimal("100")),),
        ts_ms=0,
    )
    w_wide_cent = IncentiveWindow(
        slug="tok",
        program_id="p-tok",
        pool_usd=Decimal("100"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.03"),
        start=NOW,
        end=None,
        status="active",
    )

    with patch.object(global_rewards, "MAX_CUSHION_SPREAD_RATIO", Decimal("0.625")):
        price_625 = global_rewards.bid_price(b_wide_cent, w_wide_cent, Decimal("0.01"))
        assert price_625 == Decimal("0.59")

    with patch.object(global_rewards, "MAX_CUSHION_SPREAD_RATIO", Decimal("0.85")):
        price_85 = global_rewards.bid_price(b_wide_cent, w_wide_cent, Decimal("0.01"))
        assert price_85 == Decimal("0.58")
