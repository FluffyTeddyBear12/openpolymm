import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest

from pmus.config import Settings
from pmus.engine import LiveEngine, allocate_markets, reconcile_quotes, update_position_from_fill
from pmus.models import Book, ConditionMarket, IncentiveWindow, Level, LiveOrder, Market, Position, Quote
from pmus.tests.test_condition_selector import make_book, make_condition

def test_reconcile_quotes():
    current = [
        LiveOrder("o-keep", "mkt-1", "BUY_LONG", Decimal("0.490"), Decimal("10")),
        LiveOrder("o-cancel", "mkt-1", "SELL_LONG", Decimal("0.550"), Decimal("10")),
    ]
    desired = [
        Quote("mkt-1", "BUY_LONG", Decimal("0.490"), Decimal("10")),
        Quote("mkt-1", "SELL_LONG", Decimal("0.510"), Decimal("10")),
    ]

    to_create, to_cancel, to_keep = reconcile_quotes(current, desired)

    assert len(to_keep) == 1
    assert to_keep[0].order_id == "o-keep"

    assert len(to_cancel) == 1
    assert to_cancel[0].order_id == "o-cancel"

    assert len(to_create) == 1
    assert to_create[0].side == "SELL_LONG"
    assert to_create[0].price == Decimal("0.510")

def test_update_position_from_fill():
    pos = Position(slug="mkt-1", net_qty=Decimal("0"), avg_price=Decimal("0"))
    pos = update_position_from_fill(pos, "BUY_LONG", Decimal("0.400"), Decimal("10"))
    assert pos.net_qty == Decimal("10")
    assert pos.avg_price == Decimal("0.400")

    pos = update_position_from_fill(pos, "BUY_LONG", Decimal("0.500"), Decimal("10"))
    assert pos.net_qty == Decimal("20")
    assert pos.avg_price == Decimal("0.450")

    pos = update_position_from_fill(pos, "SELL_LONG", Decimal("0.600"), Decimal("10"))
    assert pos.net_qty == Decimal("10")
    assert pos.avg_price == Decimal("0.450")

def test_update_position_short_sides():
    pos = Position(slug="mkt-1", net_qty=Decimal("0"), avg_price=Decimal("0"))
    pos = update_position_from_fill(pos, "SELL_SHORT", Decimal("0.600"), Decimal("10"))
    assert pos.net_qty == Decimal("-10")
    assert pos.avg_price == Decimal("0.600")

    pos = update_position_from_fill(pos, "BUY_SHORT", Decimal("0.500"), Decimal("10"))
    assert pos.net_qty == Decimal("0")


def test_allocate_markets_global_path_selects_both_tokens_and_sets_windows():
    logger = logging.getLogger("test_engine")
    now = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    w = IncentiveWindow(
        slug="cond-123",
        program_id="p-123",
        pool_usd=Decimal("200.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=23),
        status="active",
    )
    yes_m = Market(
        slug="cond-123-yes",
        event_id="ev-123",
        category="sports",
        tick=Decimal("0.01"),
        min_qty=Decimal("5"),
        active=True,
        question="Will X win?",
    )
    no_m = Market(
        slug="cond-123-no",
        event_id="ev-123",
        category="sports",
        tick=Decimal("0.01"),
        min_qty=Decimal("5"),
        active=True,
        question="Will X win?",
    )
    cond = ConditionMarket(
        condition_id="cond-123",
        event_id="ev-123",
        question="Will X win?",
        yes=yes_m,
        no=no_m,
        window=w,
    )

    yes_b = Book(
        slug="cond-123-yes",
        bids=(Level(Decimal("0.48"), Decimal("100")),),
        asks=(Level(Decimal("0.52"), Decimal("100")),),
        ts_ms=1000,
    )
    no_b = Book(
        slug="cond-123-no",
        bids=(Level(Decimal("0.47"), Decimal("100")),),
        asks=(Level(Decimal("0.53"), Decimal("100")),),
        ts_ms=1000,
    )

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond]
    client.fetch_book.side_effect = lambda slug: yes_b if slug == "cond-123-yes" else no_b

    settings = Settings(
        venue="global",
        total_budget=Decimal("200.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=4,
        max_contracts_per_market=100,
    )

    active_windows, alloc_res = allocate_markets(settings, client, logger)

    assert "cond-123-yes" in active_windows
    assert "cond-123-no" in active_windows
    assert active_windows["cond-123-yes"].slug == "cond-123-yes"
    assert active_windows["cond-123-no"].slug == "cond-123-no"
    assert alloc_res.selected_slugs == ["cond-123-yes", "cond-123-no"]
    assert alloc_res.paired_slugs == frozenset({"cond-123-yes", "cond-123-no"})
    assert alloc_res.side_budgets[("cond-123-yes", "bid")] == Decimal("48.00")
    assert alloc_res.side_budgets[("cond-123-no", "bid")] == Decimal("47.00")
    assert alloc_res.side_qtys[("cond-123-yes", "bid")] == Decimal("100")
    assert alloc_res.side_qtys[("cond-123-no", "bid")] == Decimal("100")
    assert alloc_res.condition_pairs == (("cond-123-yes", "cond-123-no"),)


def test_allocate_markets_respects_available_budget():
    logger = logging.getLogger("test_engine_budget")
    cond1 = make_condition(cond_id="c1", pool="200")
    cond2 = make_condition(cond_id="c2", pool="150")

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond1, cond2]
    client.fetch_book.return_value = make_book([("0.48", "100")], [("0.52", "100")])

    settings = Settings(
        venue="global",
        total_budget=Decimal("200.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=4,
        max_contracts_per_market=100,
    )

    _, alloc_full = allocate_markets(settings, client, logger)
    assert alloc_full.selected_slugs == ["c1-yes", "c1-no", "c2-yes", "c2-no"]

    _, alloc_half = allocate_markets(settings, client, logger, available_budget=Decimal("100.00"))
    assert alloc_half.selected_slugs == ["c1-yes", "c1-no"]

    _, alloc_empty = allocate_markets(settings, client, logger, available_budget=Decimal("20.00"))
    assert alloc_empty.selected_slugs == []


def test_live_engine_prepare_stores_paired_and_passes_to_quoter():
    logger = logging.getLogger("test_live_engine")
    now = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    w = IncentiveWindow(
        slug="cond-123",
        program_id="p-123",
        pool_usd=Decimal("200.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=23),
        status="active",
    )
    yes_m = Market(
        slug="cond-123-yes",
        event_id="ev-123",
        category="sports",
        tick=Decimal("0.01"),
        min_qty=Decimal("5"),
        active=True,
    )
    no_m = Market(
        slug="cond-123-no",
        event_id="ev-123",
        category="sports",
        tick=Decimal("0.01"),
        min_qty=Decimal("5"),
        active=True,
    )
    cond = ConditionMarket(
        condition_id="cond-123",
        event_id="ev-123",
        question="Will X win?",
        yes=yes_m,
        no=no_m,
        window=w,
    )

    yes_b = Book(
        slug="cond-123-yes",
        bids=(Level(Decimal("0.48"), Decimal("100")),),
        asks=(Level(Decimal("0.52"), Decimal("100")),),
        ts_ms=1000,
    )
    no_b = Book(
        slug="cond-123-no",
        bids=(Level(Decimal("0.47"), Decimal("100")),),
        asks=(Level(Decimal("0.53"), Decimal("100")),),
        ts_ms=1000,
    )

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond]
    client.fetch_book.side_effect = lambda slug: yes_b if slug == "cond-123-yes" else no_b
    client.fetch_market.side_effect = lambda slug: yes_m if slug == "cond-123-yes" else no_m

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.cancel_all_scoped.return_value = []
    auth_client.get_buying_power.return_value = Decimal("500.00")

    ledger = MagicMock()
    ledger.get_open_orders.return_value = []

    risk_mgr = MagicMock()
    settings = Settings(
        venue="global",
        total_budget=Decimal("200.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=4,
        max_contracts_per_market=100,
    )

    engine = LiveEngine(
        settings=settings,
        ledger=ledger,
        public_client=client,
        auth_client=auth_client,
        risk_manager=risk_mgr,
        logger=logger,
    )
    engine.prepare()

    assert engine.paired_slugs == frozenset({"cond-123-yes", "cond-123-no"})
    assert engine.condition_pairs == (("cond-123-yes", "cond-123-no"),)
    assert engine.side_qtys[("cond-123-yes", "bid")] == Decimal("100")
    assert engine.side_qtys[("cond-123-no", "bid")] == Decimal("100")

    with patch("pmus.engine.desired_quotes") as mock_desired_quotes:
        mock_desired_quotes.return_value = []
        engine._quote_market(
            slug="cond-123-yes",
            now_dt=now,
            now_ts=now.timestamp(),
            now_ms=int(now.timestamp() * 1000),
            exchange_open=[],
            live_positions={},
            buying_power=Decimal("1000"),
            realized_loss_today=Decimal("0"),
        )
        mock_desired_quotes.assert_called_once()
        kws = mock_desired_quotes.call_args.kwargs
        assert kws["paired"] is True
        assert kws["quote_size"] == Decimal("100")
        assert kws["is_global"] is True


def test_live_engine_prepare_uses_buying_power_if_lower_than_budget():
    logger = logging.getLogger("test_live_engine_bp")
    cond1 = make_condition(cond_id="c1", pool="200")
    cond2 = make_condition(cond_id="c2", pool="150")

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond1, cond2]
    client.fetch_book.return_value = make_book([("0.48", "100")], [("0.52", "100")])

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.cancel_all_scoped.return_value = []
    auth_client.get_buying_power.return_value = Decimal("100.00")

    settings = Settings(
        venue="global",
        total_budget=Decimal("200.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=4,
        max_contracts_per_market=100,
    )
    engine = LiveEngine(
        settings=settings,
        ledger=MagicMock(),
        public_client=client,
        auth_client=auth_client,
        risk_manager=MagicMock(),
        logger=logger,
    )
    engine.prepare()
    assert engine.target_slugs == ["c1-yes", "c1-no"]
    assert engine.condition_pairs == (("c1-yes", "c1-no"),)


