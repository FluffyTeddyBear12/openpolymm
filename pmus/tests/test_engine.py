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


def test_allocate_markets_sorts_conditions_by_pool_size_descending():
    logger = logging.getLogger("test_engine_pool_sort")
    cond_small = make_condition(cond_id="c_small", pool="50")
    cond_large = make_condition(cond_id="c_large", pool="300")
    cond_medium = make_condition(cond_id="c_medium", pool="100")

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond_small, cond_large, cond_medium]
    client.fetch_book.return_value = make_book([("0.48", "100")], [("0.52", "100")])

    settings = Settings(
        venue="global",
        total_budget=Decimal("100.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=2,
        max_contracts_per_market=100,
    )

    _, alloc = allocate_markets(settings, client, logger)
    assert alloc.selected_slugs == ["c_large-yes", "c_large-no"]


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


def test_live_engine_tick_records_lip_snapshots_single_market():
    logger = logging.getLogger("test_live_engine_single_snapshots")
    now = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    w = IncentiveWindow(
        slug="mkt-test",
        program_id="p-test",
        pool_usd=Decimal("200.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=23),
        status="active",
    )
    market = Market(
        slug="mkt-test",
        event_id="ev-test",
        category="sports",
        tick=Decimal("0.01"),
        min_qty=Decimal("5"),
        active=True,
        question="Will test pass?",
    )
    book = Book(
        slug="mkt-test",
        bids=(Level(Decimal("0.48"), Decimal("100")),),
        asks=(Level(Decimal("0.52"), Decimal("100")),),
        ts_ms=1000,
    )

    public_client = MagicMock()
    public_client.fetch_book.return_value = book
    public_client.get_market.return_value = market

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_open_orders.return_value = []
    auth_client.get_fills.return_value = []
    auth_client.get_positions.return_value = []
    auth_client.get_buying_power.return_value = Decimal("500.00")
    auth_client.create_orders_batch.return_value = []

    ledger = MagicMock()
    existing_order = LiveOrder("o-1", "mkt-test", "BUY_LONG", Decimal("0.48"), Decimal("10"))
    ledger.get_open_orders.return_value = [existing_order]
    ledger.get_realized_loss_today.return_value = Decimal("0")

    risk_mgr = MagicMock()
    risk_mgr.is_kill_switch_active.return_value = False
    risk_mgr.is_maintenance_active.return_value = False
    risk_mgr.check_pre_submit.return_value = MagicMock(allowed=True)

    settings = Settings(
        venue="us",
        total_budget=Decimal("200.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=4,
        max_contracts_per_market=100,
    )

    engine = LiveEngine(
        settings=settings,
        ledger=ledger,
        public_client=public_client,
        auth_client=auth_client,
        risk_manager=risk_mgr,
        logger=logger,
    )
    engine.target_slugs = ["mkt-test"]
    engine.active_windows = {"mkt-test": w}

    period = engine.tick(now.timestamp())
    assert period == 1.0
    ledger.record_lip_snapshot.assert_called_once()
    snapshot = ledger.record_lip_snapshot.call_args[0][0]
    assert snapshot.slug == "mkt-test"


def test_live_engine_tick_records_lip_snapshots_condition_pairs():
    logger = logging.getLogger("test_live_engine_pair_snapshots")
    now = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    w = IncentiveWindow(
        slug="c1-yes",
        program_id="p-c1",
        pool_usd=Decimal("200.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=23),
        status="active",
    )
    yes_b = Book(slug="c1-yes", bids=(Level(Decimal("0.48"), Decimal("100")),), asks=(Level(Decimal("0.52"), Decimal("100")),), ts_ms=1000)
    no_b = Book(slug="c1-no", bids=(Level(Decimal("0.48"), Decimal("100")),), asks=(Level(Decimal("0.52"), Decimal("100")),), ts_ms=1000)

    market_y = Market(slug="c1-yes", event_id="c1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True, question="Yes?")
    market_n = Market(slug="c1-no", event_id="c1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True, question="No?")
    public_client = MagicMock()
    public_client.fetch_book.side_effect = lambda slug: yes_b if slug == "c1-yes" else no_b
    public_client.get_market.side_effect = lambda slug: market_y if slug == "c1-yes" else market_n

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_open_orders.return_value = []
    auth_client.get_fills.return_value = []
    auth_client.get_positions.return_value = []
    auth_client.get_buying_power.return_value = Decimal("500.00")
    auth_client.create_orders_batch.return_value = []

    ledger = MagicMock()
    orders_yes = [LiveOrder("o-yes", "c1-yes", "BUY_LONG", Decimal("0.48"), Decimal("10"))]
    orders_no = [LiveOrder("o-no", "c1-no", "BUY_LONG", Decimal("0.48"), Decimal("10"))]
    ledger.get_open_orders.side_effect = lambda slug=None: (
        orders_yes if slug == "c1-yes" else (orders_no if slug == "c1-no" else (orders_yes + orders_no))
    )
    ledger.get_realized_loss_today.return_value = Decimal("0")

    risk_mgr = MagicMock()
    risk_mgr.is_kill_switch_active.return_value = False
    risk_mgr.is_maintenance_active.return_value = False
    risk_mgr.check_pre_submit.return_value = MagicMock(allowed=True)

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
        public_client=public_client,
        auth_client=auth_client,
        risk_manager=risk_mgr,
        logger=logger,
    )
    engine.target_slugs = ["c1-yes", "c1-no"]
    engine.paired_slugs = frozenset({"c1-yes", "c1-no"})
    engine.condition_pairs = (("c1-yes", "c1-no"),)
    engine.active_windows = {"c1-yes": w, "c1-no": w}

    period = engine.tick(now.timestamp())
    assert period == 1.0
    assert ledger.record_lip_snapshot.call_count == 2


def test_allocate_markets_tiebreaks_by_smaller_target_size_and_uses_fetch_books_batch():
    logger = logging.getLogger("test_engine_tiebreak")
    cond_large_target = make_condition(cond_id="c_large_tgt", pool="100", target="200")
    cond_small_target = make_condition(cond_id="c_small_tgt", pool="100", target="50")

    book_yes_small = make_book([("0.48", "100")], [("0.52", "100")], slug="c_small_tgt-yes")
    book_no_small = make_book([("0.48", "100")], [("0.52", "100")], slug="c_small_tgt-no")
    book_yes_large = make_book([("0.48", "200")], [("0.52", "200")], slug="c_large_tgt-yes")
    book_no_large = make_book([("0.48", "200")], [("0.52", "200")], slug="c_large_tgt-no")

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond_large_target, cond_small_target]
    client.fetch_books_batch.return_value = {
        "c_small_tgt-yes": book_yes_small,
        "c_small_tgt-no": book_no_small,
        "c_large_tgt-yes": book_yes_large,
        "c_large_tgt-no": book_no_large,
    }

    settings = Settings(
        venue="global",
        total_budget=Decimal("100.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=2,
        max_contracts_per_market=100,
    )

    _, alloc = allocate_markets(settings, client, logger)
    client.fetch_books_batch.assert_called_once()
    assert alloc.selected_slugs == ["c_small_tgt-yes", "c_small_tgt-no"]


def test_live_engine_prepare_reallocation_cancels_only_dropped_slugs():
    logger = logging.getLogger("test_engine_realloc")
    cond1 = make_condition(cond_id="c1", pool="200")
    cond2 = make_condition(cond_id="c2", pool="150")

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond1, cond2]
    client.fetch_book.return_value = make_book([("0.48", "100")], [("0.52", "100")])

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.cancel_all_scoped.return_value = []
    auth_client.get_buying_power.return_value = Decimal("500.00")

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
    engine.prepare(is_realloc=False)
    assert set(engine.target_slugs) == {"c1-yes", "c1-no", "c2-yes", "c2-no"}
    auth_client.cancel_all_scoped.assert_called_once()
    auth_client.cancel_all_scoped.reset_mock()

    cond2_small = make_condition(cond_id="c2", pool="50")
    client.fetch_reward_conditions.return_value = [cond1, cond2_small]
    import dataclasses
    engine.settings = dataclasses.replace(settings, total_budget=Decimal("100.00"))

    engine.prepare(is_realloc=True)
    assert set(engine.target_slugs) == {"c1-yes", "c1-no"}
    auth_client.cancel_all_scoped.assert_called_once()
    canceled_call_slugs = auth_client.cancel_all_scoped.call_args[0][0]
    assert set(canceled_call_slugs) == {"c2-yes", "c2-no"}


def test_allocate_markets_deprioritizes_conditions_exceeding_max_contracts():
    logger = logging.getLogger("test_engine_feasible_sort")
    cond_infeasible = make_condition(cond_id="c_infeasible", pool="500", target="500")
    cond_feasible = make_condition(cond_id="c_feasible", pool="100", target="50")

    book_yes = make_book([("0.48", "100")], [("0.52", "100")], slug="c_feasible-yes")
    book_no = make_book([("0.48", "100")], [("0.52", "100")], slug="c_feasible-no")

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond_infeasible, cond_feasible]
    client.fetch_books_batch.return_value = {
        "c_feasible-yes": book_yes,
        "c_feasible-no": book_no,
    }

    settings = Settings(
        venue="global",
        total_budget=Decimal("100.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=2,
        max_contracts_per_market=200,
    )

    _, alloc = allocate_markets(settings, client, logger)
    client.fetch_books_batch.assert_called_once_with(
        ["c_feasible-yes", "c_feasible-no", "c_infeasible-yes", "c_infeasible-no"]
    )
    assert alloc.selected_slugs == ["c_feasible-yes", "c_feasible-no"]


def test_live_engine_tick_uses_fetch_books_batch_when_dict_returned():
    logger = logging.getLogger("test_live_engine_batch_tick")
    now = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    w = IncentiveWindow(
        slug="mkt-batch",
        program_id="p-batch",
        pool_usd=Decimal("200.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=23),
        status="active",
    )
    market = Market(
        slug="mkt-batch",
        event_id="ev-batch",
        category="sports",
        tick=Decimal("0.01"),
        min_qty=Decimal("5"),
        active=True,
        question="Will batch work?",
    )
    book = Book(
        slug="mkt-batch",
        bids=(Level(Decimal("0.48"), Decimal("100")),),
        asks=(Level(Decimal("0.52"), Decimal("100")),),
        ts_ms=1000,
    )

    public_client = MagicMock()
    public_client.fetch_books_batch.return_value = {"mkt-batch": book}
    public_client.get_market.return_value = market

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_open_orders.return_value = []
    auth_client.get_fills.return_value = []
    auth_client.get_positions.return_value = []
    auth_client.get_buying_power.return_value = Decimal("500.00")
    auth_client.create_orders_batch.return_value = []

    ledger = MagicMock()
    ledger.get_open_orders.return_value = []
    ledger.get_realized_loss_today.return_value = Decimal("0")

    risk_mgr = MagicMock()
    risk_mgr.is_kill_switch_active.return_value = False
    risk_mgr.is_maintenance_active.return_value = False
    risk_mgr.check_pre_submit.return_value = MagicMock(allowed=True)

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
        public_client=public_client,
        auth_client=auth_client,
        risk_manager=risk_mgr,
        logger=logger,
    )
    engine.target_slugs = ["mkt-batch"]
    engine.active_windows = {"mkt-batch": w}

    period = engine.tick(now.timestamp())
    assert period == 1.0
    public_client.fetch_books_batch.assert_called_once_with(["mkt-batch"])
    public_client.fetch_book.assert_not_called()


def test_allocate_markets_keeps_300_book_limit_under_500_and_expands_above_500():
    logger = logging.getLogger("test_engine_book_limit")
    conditions = [
        make_condition(cond_id=f"c{i}", pool=str(1000 - i), target="50")
        for i in range(400)
    ]

    client = MagicMock()
    client.fetch_reward_conditions.return_value = list(conditions)
    client.fetch_books_batch.return_value = {}

    settings_under = Settings(
        venue="global",
        total_budget=Decimal("400.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=4,
        max_contracts_per_market=200,
    )
    allocate_markets(settings_under, client, logger)
    slugs_under = client.fetch_books_batch.call_args[0][0]
    assert len(slugs_under) == 300

    client.fetch_reward_conditions.return_value = list(conditions)
    client.fetch_books_batch.reset_mock()
    settings_above = Settings(
        venue="global",
        total_budget=Decimal("6000.00"),
        max_per_market=Decimal("300.00"),
        max_collateral_per_market=Decimal("300.00"),
        max_markets=20,
        max_contracts_per_market=500,
    )
    allocate_markets(settings_above, client, logger)
    slugs_above = client.fetch_books_batch.call_args[0][0]
    assert len(slugs_above) == 600


