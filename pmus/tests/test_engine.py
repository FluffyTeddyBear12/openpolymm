import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest

from pmus.config import Settings
from pmus.engine import LiveEngine, allocate_markets, reconcile_quotes, update_position_from_fill
from pmus.ledger import Ledger
from pmus.models import Book, ConditionMarket, IncentiveWindow, Level, LiveOrder, Market, Position, Quote, ShadowFill
from pmus.strategy.market_selector import AllocationResult
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
        bids=(Level(Decimal("0.48"), Decimal("250")),),
        asks=(Level(Decimal("0.52"), Decimal("250")),),
        ts_ms=1000,
    )
    no_b = Book(
        slug="cond-123-no",
        bids=(Level(Decimal("0.47"), Decimal("250")),),
        asks=(Level(Decimal("0.53"), Decimal("250")),),
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
    client.fetch_book.return_value = make_book([("0.48", "250")], [("0.52", "250")])

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
    client.fetch_book.return_value = make_book([("0.48", "250")], [("0.52", "250")])

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
        bids=(Level(Decimal("0.48"), Decimal("250")),),
        asks=(Level(Decimal("0.52"), Decimal("250")),),
        ts_ms=1000,
    )
    no_b = Book(
        slug="cond-123-no",
        bids=(Level(Decimal("0.47"), Decimal("250")),),
        asks=(Level(Decimal("0.53"), Decimal("250")),),
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
    client.fetch_book.return_value = make_book([("0.48", "250")], [("0.52", "250")])

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

    book_yes_small = make_book([("0.48", "250")], [("0.52", "250")], slug="c_small_tgt-yes")
    book_no_small = make_book([("0.48", "250")], [("0.52", "250")], slug="c_small_tgt-no")
    book_yes_large = make_book([("0.48", "500")], [("0.52", "500")], slug="c_large_tgt-yes")
    book_no_large = make_book([("0.48", "500")], [("0.52", "500")], slug="c_large_tgt-no")

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
    client.fetch_book.return_value = make_book([("0.48", "250")], [("0.52", "250")])

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

    book_yes = make_book([("0.48", "250")], [("0.52", "250")], slug="c_feasible-yes")
    book_no = make_book([("0.48", "250")], [("0.52", "250")], slug="c_feasible-no")

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


def test_allocate_markets_caps_candidate_books_at_3000():
    logger = logging.getLogger("test_engine_book_limit")
    conditions = [
        make_condition(cond_id=f"c{i}", pool=str(2000 - i), target="50")
        for i in range(1600)
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
    assert len(slugs_under) == 3000

    client.fetch_reward_conditions.return_value = list(conditions)
    client.fetch_books_batch.reset_mock()
    settings_above = Settings(
        venue="global",
        total_budget=Decimal("10000.00"),
        max_per_market=Decimal("300.00"),
        max_collateral_per_market=Decimal("300.00"),
        max_markets=20,
        max_contracts_per_market=500,
    )
    allocate_markets(settings_above, client, logger)
    slugs_above = client.fetch_books_batch.call_args[0][0]
    assert len(slugs_above) == 3000


def test_allocate_markets_prioritizes_affordable_pair_target_size_and_strips_open_orders():
    logger = logging.getLogger("test_engine_prioritizes_affordable_and_strips")
    cond_a = make_condition(cond_id="ca", pool="500", target="50", max_spread="0.06")
    cond_b = make_condition(cond_id="cb", pool="200", target="40", max_spread="0.06")

    books = {
        cond_a.yes.slug: make_book([("0.47", "250")], [("0.51", "250")], slug=cond_a.yes.slug),
        cond_a.no.slug: make_book([("0.47", "250")], [("0.51", "250")], slug=cond_a.no.slug),
        cond_b.yes.slug: make_book([("0.49", "80"), ("0.47", "250")], [("0.51", "250")], slug=cond_b.yes.slug),
        cond_b.no.slug: make_book([("0.47", "250")], [("0.51", "250")], slug=cond_b.no.slug),
    }

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond_a, cond_b]
    client.fetch_books_batch.return_value = dict(books)

    settings = Settings(
        venue="global",
        total_budget=Decimal("40.00"),
        max_per_market=Decimal("40.00"),
        max_collateral_per_market=Decimal("40.00"),
        max_markets=2,
        max_contracts_per_market=100,
        pair_preference=Decimal("0"),
    )

    open_orders = [LiveOrder("o-1", cond_b.yes.slug, "BUY_LONG", Decimal("0.49"), Decimal("80"))]

    _, alloc_without_open = allocate_markets(settings, client, logger, available_budget=None, positions=None, open_orders=None)
    assert alloc_without_open.selected_slugs == [cond_b.yes.slug, cond_b.no.slug]
    assert alloc_without_open.side_qtys[(cond_b.yes.slug, "bid")] == Decimal("40")
    assert alloc_without_open.side_budgets[(cond_b.yes.slug, "bid")] == Decimal("40") * Decimal("0.48")

    client.fetch_reward_conditions.return_value = [cond_a, cond_b]
    client.fetch_books_batch.return_value = dict(books)

    _, alloc_with_open = allocate_markets(settings, client, logger, available_budget=None, positions=None, open_orders=open_orders)
    assert alloc_with_open.selected_slugs == [cond_b.yes.slug, cond_b.no.slug]
    assert alloc_with_open.side_qtys[(cond_b.yes.slug, "bid")] == Decimal("40")
    assert alloc_with_open.side_budgets[(cond_b.yes.slug, "bid")] == Decimal("40") * Decimal("0.47")
    assert alloc_with_open.side_budgets[(cond_b.no.slug, "bid")] == Decimal("40") * Decimal("0.47")



def test_live_engine_prepare_passes_held_positions_and_sets_unwind_targets():
    logger = logging.getLogger("test_engine_held_prepare")
    cond_over = make_condition(cond_id="c_over", pool="200", target="50")
    cond_clean = make_condition(cond_id="c_clean", pool="150", target="50")

    books = {
        cond_over.yes.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond_over.yes.slug),
        cond_over.no.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond_over.no.slug),
        cond_clean.yes.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond_clean.yes.slug),
        cond_clean.no.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond_clean.no.slug),
    }

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond_over, cond_clean]
    client.fetch_books_batch.return_value = books

    held_pos = Position(slug="c_over-no", net_qty=Decimal("540"), avg_price=Decimal("0.184"))
    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.cancel_all_scoped.return_value = []
    auth_client.get_buying_power.return_value = Decimal("200.00")
    auth_client.get_positions.return_value = [held_pos]

    settings = Settings(
        venue="global",
        total_budget=Decimal("165.00"),
        max_per_market=Decimal("55.00"),
        max_collateral_per_market=Decimal("55.00"),
        max_markets=2,
        max_contracts_per_market=1000,
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

    assert "c_clean-yes" in engine.target_slugs
    assert "c_clean-no" in engine.target_slugs
    assert "c_over-no" in engine.target_slugs
    assert engine.side_budgets[("c_over-no", "bid")] == Decimal("0")


def test_live_engine_daily_loss_baseline_offsets_acknowledged_unwind_loss():
    logger = logging.getLogger("test_daily_loss_baseline")
    now = datetime(2026, 10, 15, 21, 5, tzinfo=timezone.utc)

    book = Book(
        slug="mkt-test",
        bids=(Level(Decimal("0.48"), Decimal("100")),),
        asks=(Level(Decimal("0.52"), Decimal("100")),),
        ts_ms=1000,
    )
    market = Market(
        slug="mkt-test",
        event_id="ev-test",
        category="politics",
        tick=Decimal("0.01"),
        min_qty=Decimal("5"),
        active=True,
        question="Will test pass?",
    )

    public_client = MagicMock()
    public_client.fetch_books_batch.return_value = {"mkt-test": book}
    public_client.get_market.return_value = market

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_open_orders.return_value = []
    auth_client.get_fills.return_value = []
    auth_client.get_positions.return_value = []
    auth_client.get_buying_power.return_value = Decimal("152.22")
    auth_client.create_orders_batch.return_value = []
    auth_client.cancel_all_scoped.return_value = []

    ledger = MagicMock()
    ledger.get_open_orders.return_value = []
    ledger.get_realized_loss_today.return_value = Decimal("56.72")

    risk_mgr = MagicMock()
    risk_mgr.is_kill_switch_active.return_value = False
    risk_mgr.is_maintenance_active.return_value = False
    risk_mgr.check_pre_submit.return_value = MagicMock(allowed=True)

    settings = Settings(
        venue="global",
        daily_loss_stop=Decimal("15.00"),
        daily_loss_baseline=Decimal("56.72"),
        total_budget=Decimal("100.00"),
        max_per_market=Decimal("50.00"),
        max_collateral_per_market=Decimal("50.00"),
        max_markets=1,
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
    engine.notifier = MagicMock()
    engine.target_slugs = ["mkt-test"]

    period = engine.tick(now.timestamp())
    assert period == 1.0
    assert not engine.daily_loss_stop_tripped
    engine.notifier.notify_daily_loss_stop.assert_not_called()
    assert risk_mgr.check_pre_submit.call_count == 1
    assert risk_mgr.check_pre_submit.call_args.kwargs["realized_loss_today"] == Decimal("0.00")

    ledger.get_realized_loss_today.return_value = Decimal("72.00")
    period_tripped = engine.tick(now.timestamp())
    assert period_tripped == 10.0
    assert engine.daily_loss_stop_tripped
    engine.notifier.notify_daily_loss_stop.assert_called_once_with(Decimal("15.28"), Decimal("15.00"))


def test_live_engine_tick_overlays_new_fill_onto_stale_api_positions_preventing_double_buy(tmp_path):
    logger = logging.getLogger("test_overlay_fill")
    t0 = 1791596070.0
    now = datetime.fromtimestamp(t0, timezone.utc)
    w = IncentiveWindow(
        slug="tok-1",
        program_id="p-1",
        pool_usd=Decimal("200.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("220"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=23),
        status="active",
    )
    market = Market(
        slug="tok-1",
        event_id="ev-1",
        category="sports",
        tick=Decimal("0.01"),
        min_qty=Decimal("5"),
        active=True,
        question="Will X occur?",
    )
    book = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.24"), Decimal("1000")),),
        asks=(Level(Decimal("0.26"), Decimal("1000")),),
        ts_ms=int(t0 * 1000),
    )

    public_client = MagicMock()
    public_client.fetch_book.return_value = book
    public_client.get_market.return_value = market
    public_client.fetch_books_batch.return_value = {"tok-1": book}

    fill = ShadowFill(
        fill_id="f-1",
        order_id="o-buy-1",
        slug="tok-1",
        side="BUY_LONG",
        price=Decimal("0.25"),
        qty=Decimal("220"),
        fee=Decimal("0"),
        is_maker=True,
        ts_ms=int(t0 * 1000),
    )

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_open_orders.return_value = []
    auth_client.get_fills.return_value = [fill]
    auth_client.get_positions.return_value = []
    auth_client.get_buying_power.return_value = Decimal("500.00")
    auth_client.create_orders_batch.return_value = []
    auth_client.cancel_all_scoped.return_value = []
    auth_client.invalidate_positions_cache = MagicMock()

    ledger = Ledger(tmp_path / "test_overlay.db")

    risk_mgr = MagicMock()
    risk_mgr.is_kill_switch_active.return_value = False
    risk_mgr.is_maintenance_active.return_value = False
    risk_mgr.check_pre_submit.return_value = MagicMock(allowed=True)

    settings = Settings(
        venue="global",
        total_budget=Decimal("100.00"),
        max_per_market=Decimal("55.00"),
        max_collateral_per_market=Decimal("55.00"),
        max_markets=1,
        max_contracts_per_market=220,
    )

    engine = LiveEngine(
        settings=settings,
        ledger=ledger,
        public_client=public_client,
        auth_client=auth_client,
        risk_manager=risk_mgr,
        logger=logger,
    )
    engine.target_slugs = ["tok-1"]
    engine.active_windows = {"tok-1": w}
    engine.side_budgets = {("tok-1", "bid"): Decimal("55.00"), ("tok-1", "ask"): Decimal("55.00")}
    engine.side_qtys = {("tok-1", "bid"): Decimal("220"), ("tok-1", "ask"): Decimal("220")}

    period = engine.tick(t0)
    assert period == 1.0

    auth_client.invalidate_positions_cache.assert_called_once()
    assert "tok-1" in engine.local_fill_positions
    pos, fill_ts = engine.local_fill_positions["tok-1"]
    assert pos.net_qty == Decimal("220")
    assert pos.avg_price == Decimal("0.25")
    assert fill_ts == t0

    assert risk_mgr.check_pre_submit.call_count == 1
    submitted_positions = risk_mgr.check_pre_submit.call_args.kwargs["positions"]
    assert submitted_positions["tok-1"].net_qty == Decimal("220")
    assert submitted_positions["tok-1"].avg_price == Decimal("0.25")

    saved_pos = ledger.get_position("tok-1")
    assert saved_pos.net_qty == Decimal("220")
    assert saved_pos.avg_price == Decimal("0.25")

    for call in auth_client.create_orders_batch.call_args_list:
        quotes = call[0][0]
        assert not any(q.side == "BUY_LONG" for q in quotes)

    auth_client.create_orders_batch.reset_mock()
    risk_mgr.check_pre_submit.reset_mock()

    t1 = t0 + 10.0
    period2 = engine.tick(t1)
    assert period2 == 1.0
    assert risk_mgr.check_pre_submit.call_count == 1
    submitted_positions2 = risk_mgr.check_pre_submit.call_args.kwargs["positions"]
    assert submitted_positions2["tok-1"].net_qty == Decimal("220")
    for call in auth_client.create_orders_batch.call_args_list:
        quotes = call[0][0]
        assert not any(q.side == "BUY_LONG" for q in quotes)

    auth_client.create_orders_batch.reset_mock()
    risk_mgr.check_pre_submit.reset_mock()

    t2 = t0 + 50.0
    period3 = engine.tick(t2)
    assert period3 == 1.0
    assert risk_mgr.check_pre_submit.call_count == 1
    submitted_positions3 = risk_mgr.check_pre_submit.call_args.kwargs["positions"]
    assert submitted_positions3["tok-1"].net_qty == Decimal("220")
    for call in auth_client.create_orders_batch.call_args_list:
        quotes = call[0][0]
        assert not any(q.side == "BUY_LONG" for q in quotes)


def test_live_engine_anti_chase_keeps_resting_scoring_buy_order_when_bid_rises(tmp_path):
    logger = logging.getLogger("test_anti_chase")
    t0 = 1791596070.0
    now = datetime.fromtimestamp(t0, timezone.utc)
    w = IncentiveWindow(
        slug="tok-1",
        program_id="p-1",
        pool_usd=Decimal("200.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.04"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=23),
        status="active",
    )
    market = Market(
        slug="tok-1",
        event_id="ev-1",
        category="sports",
        tick=Decimal("0.01"),
        min_qty=Decimal("5"),
        active=True,
        question="Will X occur?",
    )
    book_up = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.46"), Decimal("100")),),
        asks=(Level(Decimal("0.48"), Decimal("100")),),
        ts_ms=int(t0 * 1000),
    )
    book_down = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.44"), Decimal("100")),),
        asks=(Level(Decimal("0.46"), Decimal("100")),),
        ts_ms=int(t0 * 1000),
    )

    ledger = Ledger(tmp_path / "test_anti_chase.db")
    existing_order = LiveOrder("o-1", "tok-1", "BUY_LONG", Decimal("0.45"), Decimal("55"))
    ledger.record_order(existing_order, status="OPEN", ts_ms=int(t0 * 1000))

    public_client = MagicMock()
    public_client.get_market.return_value = market
    public_client.fetch_book.return_value = book_up

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_open_orders.return_value = [existing_order]
    auth_client.get_buying_power.return_value = Decimal("500.00")
    auth_client.cancel_orders_batch.return_value = []
    auth_client.create_orders_batch.return_value = [
        LiveOrder("o-new", "tok-1", "BUY_LONG", Decimal("0.44"), Decimal("55"))
    ]

    risk_mgr = MagicMock()
    risk_mgr.check_pre_submit.return_value = MagicMock(allowed=True)

    settings = Settings(
        venue="global",
        total_budget=Decimal("100.00"),
        max_per_market=Decimal("55.00"),
        max_collateral_per_market=Decimal("55.00"),
        max_markets=1,
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
    engine.target_slugs = ["tok-1"]
    engine.paired_slugs = {"tok-1"}
    engine.active_windows = {"tok-1": w}
    engine.side_budgets = {("tok-1", "bid"): Decimal("55.00"), ("tok-1", "ask"): Decimal("55.00")}
    engine.side_qtys = {("tok-1", "bid"): Decimal("55"), ("tok-1", "ask"): Decimal("55")}

    engine._quote_market(
        slug="tok-1",
        now_dt=now,
        now_ts=t0,
        now_ms=int(t0 * 1000),
        exchange_open=[existing_order],
        live_positions={},
        buying_power=Decimal("500.00"),
        realized_loss_today=Decimal("0"),
        book=book_up,
    )

    auth_client.cancel_orders_batch.assert_not_called()
    auth_client.create_orders_batch.assert_not_called()
    assert [o.order_id for o in ledger.get_open_orders("tok-1")] == ["o-1"]

    public_client.fetch_book.return_value = book_down
    t1 = t0 + 1.0
    now1 = datetime.fromtimestamp(t1, timezone.utc)

    engine._quote_market(
        slug="tok-1",
        now_dt=now1,
        now_ts=t1,
        now_ms=int(t1 * 1000),
        exchange_open=[existing_order],
        live_positions={},
        buying_power=Decimal("500.00"),
        realized_loss_today=Decimal("0"),
        book=book_down,
    )

    auth_client.cancel_orders_batch.assert_called_once()
    canceled = auth_client.cancel_orders_batch.call_args[0][0]
    assert any(o.order_id == "o-1" for o in canceled)

    auth_client.create_orders_batch.assert_called_once()
    created = auth_client.create_orders_batch.call_args[0][0]
    assert any(q.side == "BUY_LONG" and q.price == Decimal("0.44") for q in created)

    auth_client.cancel_orders_batch.reset_mock()
    auth_client.create_orders_batch.reset_mock()

    t2 = t1 + 1.0
    now2 = datetime.fromtimestamp(t2, timezone.utc)
    ledger.close_order("o-new", status="CANCELED", ts_ms=int(t2 * 1000))
    order_drift = LiveOrder("o-drift", "tok-1", "BUY_LONG", Decimal("0.70"), Decimal("55"))
    ledger.record_order(order_drift, status="OPEN", ts_ms=int(t2 * 1000))
    book_drift = Book(
        slug="tok-1",
        bids=(
            Level(Decimal("0.73"), Decimal("300")),
            Level(Decimal("0.72"), Decimal("300")),
        ),
        asks=(Level(Decimal("0.74"), Decimal("300")),),
        ts_ms=int(t2 * 1000),
    )
    public_client.fetch_book.return_value = book_drift

    engine._quote_market(
        slug="tok-1",
        now_dt=now2,
        now_ts=t2,
        now_ms=int(t2 * 1000),
        exchange_open=[order_drift],
        live_positions={},
        buying_power=Decimal("500.00"),
        realized_loss_today=Decimal("0"),
        book=book_drift,
    )

    auth_client.cancel_orders_batch.assert_not_called()
    auth_client.create_orders_batch.assert_not_called()

    auth_client.cancel_orders_batch.reset_mock()
    auth_client.create_orders_batch.reset_mock()

    t3 = t2 + 1.0
    now3 = datetime.fromtimestamp(t3, timezone.utc)
    book_outside_spread = Book(
        slug="tok-1",
        bids=(
            Level(Decimal("0.75"), Decimal("300")),
            Level(Decimal("0.74"), Decimal("300")),
        ),
        asks=(Level(Decimal("0.76"), Decimal("300")),),
        ts_ms=int(t3 * 1000),
    )
    public_client.fetch_book.return_value = book_outside_spread
    auth_client.create_orders_batch.return_value = [
        LiveOrder("o-new-2", "tok-1", "BUY_LONG", Decimal("0.74"), Decimal("55"))
    ]

    engine._quote_market(
        slug="tok-1",
        now_dt=now3,
        now_ts=t3,
        now_ms=int(t3 * 1000),
        exchange_open=[order_drift],
        live_positions={},
        buying_power=Decimal("500.00"),
        realized_loss_today=Decimal("0"),
        book=book_outside_spread,
    )

    auth_client.cancel_orders_batch.assert_called_once()
    canceled_drift = auth_client.cancel_orders_batch.call_args[0][0]
    assert any(o.order_id == "o-drift" for o in canceled_drift)

    auth_client.create_orders_batch.assert_called_once()
    created_drift = auth_client.create_orders_batch.call_args[0][0]
    assert any(q.side == "BUY_LONG" and q.price == Decimal("0.74") for q in created_drift)



def test_live_engine_prepare_fee_buffer_global_vs_us(tmp_path):
    logger = logging.getLogger("test_fee_buffer")
    ledger = Ledger(tmp_path / "test_fee_buffer.db")
    client = MagicMock()
    client.fetch_reward_conditions.return_value = []
    client.fetch_books_batch.return_value = {}

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_buying_power.return_value = Decimal("100.00")
    auth_client.get_positions.return_value = []
    auth_client.get_open_orders.return_value = []

    risk_mgr = MagicMock()
    risk_mgr.is_kill_switch_active.return_value = False
    risk_mgr.is_maintenance_active.return_value = False

    settings_global = Settings(
        venue="global",
        total_budget=Decimal("200.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=4,
        max_contracts_per_market=100,
    )
    engine_global = LiveEngine(
        settings=settings_global,
        ledger=ledger,
        public_client=client,
        auth_client=auth_client,
        risk_manager=risk_mgr,
        logger=logger,
    )
    with patch("pmus.engine.allocate_markets") as mock_alloc:
        mock_alloc.return_value = ({}, MagicMock(selected_slugs=[], active_windows={}, condition_pairs=(), side_budgets={}, side_qtys={}, paired_slugs=frozenset()))
        engine_global.prepare()
        assert mock_alloc.call_args.kwargs["available_budget"] == Decimal("97.50")

    settings_us = Settings(
        venue="us",
        total_budget=Decimal("200.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=4,
        max_contracts_per_market=100,
    )
    engine_us = LiveEngine(
        settings=settings_us,
        ledger=ledger,
        public_client=client,
        auth_client=auth_client,
        risk_manager=risk_mgr,
        logger=logger,
    )
    with patch("pmus.engine.allocate_markets") as mock_alloc:
        mock_alloc.return_value = ({}, MagicMock(selected_slugs=[], active_windows={}, condition_pairs=(), side_budgets={}, side_qtys={}, paired_slugs=frozenset()))
        engine_us.prepare()
        assert mock_alloc.call_args.kwargs["available_budget"] == Decimal("95.00")


def test_allocate_markets_scales_max_cost_per_condition_with_available_budget():
    logger = logging.getLogger("test_alloc_scale")
    conds = [
        make_condition(cond_id=f"c{i}", pool=str(100 - i), target="50")
        for i in range(3)
    ]
    client = MagicMock()
    client.fetch_reward_conditions.return_value = conds
    client.fetch_book.return_value = make_book([("0.48", "250")], [("0.52", "250")])

    settings = Settings(
        venue="global",
        total_budget=Decimal("300.00"),
        max_per_market=Decimal("55.00"),
        max_collateral_per_market=Decimal("55.00"),
        max_markets=6,
        max_contracts_per_market=100,
    )

    _, alloc = allocate_markets(settings, client, logger, available_budget=Decimal("300.00"))
    assert len(alloc.condition_pairs) == 3
    total_deployed_cost = sum(alloc.side_budgets.values())
    assert total_deployed_cost > Decimal("165.00")
    assert total_deployed_cost == Decimal("288.00")
    for (slug, side), qty in alloc.side_qtys.items():
        assert qty == Decimal("100")

    _, alloc_static = allocate_markets(settings, client, logger)
    for (slug, side), qty in alloc_static.side_qtys.items():
        assert qty == Decimal("55")


def test_live_engine_uses_5s_cooldown_on_post_fill_balance_error(tmp_path):
    logger = logging.getLogger("test_cooldown")
    t0 = 1791596070.0
    now = datetime.fromtimestamp(t0, timezone.utc)
    w = IncentiveWindow(
        slug="tok-1",
        program_id="p-1",
        pool_usd=Decimal("200.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.04"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=23),
        status="active",
    )
    market = Market(
        slug="tok-1",
        event_id="ev-1",
        category="sports",
        tick=Decimal("0.01"),
        min_qty=Decimal("5"),
        active=True,
        question="Will X occur?",
    )
    book = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.44"), Decimal("100")),),
        asks=(Level(Decimal("0.46"), Decimal("100")),),
        ts_ms=int(t0 * 1000),
    )

    ledger = Ledger(tmp_path / "test_cooldown.db")
    public_client = MagicMock()
    public_client.get_market.return_value = market
    public_client.fetch_book.return_value = book

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_open_orders.return_value = []
    auth_client.get_buying_power.return_value = Decimal("500.00")
    auth_client.create_orders_batch.side_effect = Exception("400 Client Error: not enough balance / allowance")
    auth_client.invalidate_positions_cache = MagicMock()

    risk_mgr = MagicMock()
    risk_mgr.check_pre_submit.return_value = MagicMock(allowed=True)

    settings = Settings(
        venue="global",
        total_budget=Decimal("100.00"),
        max_per_market=Decimal("55.00"),
        max_collateral_per_market=Decimal("55.00"),
        max_markets=1,
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
    engine.target_slugs = ["tok-1"]
    engine.active_windows = {"tok-1": w}
    engine.side_budgets = {("tok-1", "bid"): Decimal("55.00"), ("tok-1", "ask"): Decimal("55.00")}
    engine.side_qtys = {("tok-1", "bid"): Decimal("55"), ("tok-1", "ask"): Decimal("55")}

    engine._quote_market(
        slug="tok-1",
        now_dt=now,
        now_ts=t0,
        now_ms=int(t0 * 1000),
        exchange_open=[],
        live_positions={},
        buying_power=Decimal("500.00"),
        realized_loss_today=Decimal("0"),
        book=book,
    )

    assert engine.slug_cooldown["tok-1"] == t0 + 5.0
    auth_client.invalidate_positions_cache.assert_called_once()
    assert engine.market_states["tok-1"] == "PAUSED"

    auth_client.create_orders_batch.side_effect = Exception("500 Internal Server Error")
    t1 = t0 + 10.0
    now1 = datetime.fromtimestamp(t1, timezone.utc)

    engine._quote_market(
        slug="tok-1",
        now_dt=now1,
        now_ts=t1,
        now_ms=int(t1 * 1000),
        exchange_open=[],
        live_positions={},
        buying_power=Decimal("500.00"),
        realized_loss_today=Decimal("0"),
        book=book,
    )

    assert engine.slug_cooldown["tok-1"] == t1 + 30.0


def test_live_engine_held_inventory_sets_bid_budget_zero(tmp_path):
    logger = logging.getLogger("test_held_inv")
    t0 = 1791596070.0
    now = datetime.fromtimestamp(t0, timezone.utc)
    w = IncentiveWindow(
        slug="tok-1",
        program_id="p-1",
        pool_usd=Decimal("200.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.04"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=23),
        status="active",
    )
    market = Market(
        slug="tok-1",
        event_id="ev-1",
        category="sports",
        tick=Decimal("0.01"),
        min_qty=Decimal("5"),
        active=True,
        question="Will X occur?",
    )
    book = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.44"), Decimal("100")),),
        asks=(Level(Decimal("0.46"), Decimal("100")),),
        ts_ms=int(t0 * 1000),
    )

    ledger = Ledger(tmp_path / "test_held_inv.db")
    public_client = MagicMock()
    public_client.get_market.return_value = market
    public_client.fetch_book.return_value = book

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_open_orders.return_value = []
    auth_client.get_buying_power.return_value = Decimal("500.00")
    auth_client.create_orders_batch.return_value = []

    risk_mgr = MagicMock()
    risk_mgr.check_pre_submit.return_value = MagicMock(allowed=True)

    settings = Settings(
        venue="global",
        total_budget=Decimal("100.00"),
        max_per_market=Decimal("55.00"),
        max_collateral_per_market=Decimal("55.00"),
        max_markets=1,
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
    engine.target_slugs = ["tok-1"]
    engine.active_windows = {"tok-1": w}
    engine.side_budgets = {("tok-1", "bid"): Decimal("55.00"), ("tok-1", "ask"): Decimal("55.00")}
    engine.side_qtys = {("tok-1", "bid"): Decimal("55"), ("tok-1", "ask"): Decimal("55")}

    held_position = Position(slug="tok-1", net_qty=Decimal("50"), avg_price=Decimal("0.45"))

    with patch("pmus.engine.desired_quotes") as mock_desired_quotes:
        mock_desired_quotes.return_value = []
        engine._quote_market(
            slug="tok-1",
            now_dt=now,
            now_ts=t0,
            now_ms=int(t0 * 1000),
            exchange_open=[],
            live_positions={"tok-1": held_position},
            buying_power=Decimal("500.00"),
            realized_loss_today=Decimal("0"),
            book=book,
        )
        assert mock_desired_quotes.call_count == 1
        call_kwargs = mock_desired_quotes.call_args.kwargs
        assert call_kwargs["bid_budget"] == Decimal("0")


def test_live_engine_prepare_realloc_cancels_downsized_kept_slugs():
    logger = logging.getLogger("test_prepare_realloc_downsized")
    cond = make_condition(cond_id="c1", pool="200", target="50")
    books = {
        cond.yes.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond.yes.slug),
        cond.no.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond.no.slug),
    }

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond]
    client.fetch_books_batch.return_value = books

    open_order_70 = LiveOrder("o-70", cond.yes.slug, "BUY_LONG", Decimal("0.48"), Decimal("70"))
    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.cancel_all_scoped.return_value = []
    auth_client.get_buying_power.return_value = Decimal("200.00")
    auth_client.get_positions.return_value = []
    auth_client.get_open_orders.return_value = [open_order_70]

    settings = Settings(
        venue="global",
        total_budget=Decimal("100.00"),
        max_per_market=Decimal("50.00"),
        max_collateral_per_market=Decimal("50.00"),
        max_markets=2,
        max_contracts_per_market=1000,
    )

    ledger = MagicMock()
    ledger.get_open_orders.return_value = [open_order_70]

    engine = LiveEngine(
        settings=settings,
        ledger=ledger,
        public_client=client,
        auth_client=auth_client,
        risk_manager=MagicMock(),
        logger=logger,
    )
    engine.target_slugs = [cond.yes.slug, cond.no.slug]
    engine.side_qtys = {(cond.yes.slug, "bid"): Decimal("70"), (cond.no.slug, "bid"): Decimal("70")}
    engine.market_states = {cond.yes.slug: "QUOTING", cond.no.slug: "QUOTING"}

    downsized_alloc = AllocationResult(
        candidates=[],
        selected_candidates=[],
        selected_slugs=[cond.yes.slug, cond.no.slug],
        side_budgets={(cond.yes.slug, "bid"): Decimal("28.80"), (cond.no.slug, "bid"): Decimal("28.80")},
        side_qtys={(cond.yes.slug, "bid"): Decimal("60"), (cond.no.slug, "bid"): Decimal("60")},
        unfunded_reason=None,
        paired_slugs=frozenset({cond.yes.slug, cond.no.slug}),
        condition_pairs=((cond.yes.slug, cond.no.slug),),
    )

    with patch("pmus.engine.allocate_markets", return_value=({cond.yes.slug: cond.window, cond.no.slug: cond.window}, downsized_alloc)):
        engine.prepare(is_realloc=True)

    auth_client.cancel_all_scoped.assert_called_once_with([cond.yes.slug])


def test_prepare_skips_dead_penny_and_empty_positions():
    logger = logging.getLogger("test_dead_penny")
    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_buying_power.return_value = Decimal("500.00")
    pos_empty = Position("slug-empty", Decimal("50"), Decimal("0.10"))
    pos_dead = Position("slug-dead", Decimal("50"), Decimal("0.10"))
    pos_active = Position("slug-active", Decimal("50"), Decimal("0.40"))
    auth_client.get_positions.return_value = [pos_empty, pos_dead, pos_active]
    auth_client.cancel_all_scoped.return_value = []

    client = MagicMock()
    client.fetch_reward_conditions.return_value = []
    book_empty = Book("slug-empty", (), (), 1000)
    book_dead = Book("slug-dead", (Level(Decimal("0.01"), Decimal("100")),), (Level(Decimal("0.01"), Decimal("100")),), 1000)
    book_active = Book("slug-active", (Level(Decimal("0.40"), Decimal("100")),), (Level(Decimal("0.42"), Decimal("100")),), 1000)

    def fetch_book_dispatch(slug):
        if slug == "slug-empty":
            return book_empty
        if slug == "slug-dead":
            return book_dead
        return book_active

    client.fetch_book.side_effect = fetch_book_dispatch

    settings = Settings(venue="global", total_budget=Decimal("100"), max_per_market=Decimal("50"))
    engine = LiveEngine(
        settings=settings,
        ledger=MagicMock(),
        public_client=client,
        auth_client=auth_client,
        risk_manager=MagicMock(),
        logger=logger,
    )
    with patch("pmus.engine.allocate_markets", return_value=({}, AllocationResult([], [], [], {}, {}, None, frozenset(), ()))):
        engine.prepare()

    assert "slug-empty" not in engine.target_slugs
    assert "slug-dead" not in engine.target_slugs
    assert "slug-active" in engine.target_slugs


def test_tick_fetches_books_batch_only_for_uncooled_slugs():
    logger = logging.getLogger("test_tick_uncooled")
    client = MagicMock()
    client.fetch_books_batch.return_value = {}
    auth_client = MagicMock()
    auth_client.get_open_orders.return_value = []
    auth_client.get_fills.return_value = []
    auth_client.get_positions.return_value = []
    auth_client.get_buying_power.return_value = Decimal("500.00")

    settings = Settings(venue="global", total_budget=Decimal("100"), max_per_market=Decimal("50"))
    engine = LiveEngine(
        settings=settings,
        ledger=MagicMock(),
        public_client=client,
        auth_client=auth_client,
        risk_manager=MagicMock(),
        logger=logger,
    )
    engine.risk_manager.is_kill_switch_active.return_value = False
    engine.risk_manager.is_maintenance_active.return_value = False
    engine.ledger.get_realized_loss_today.return_value = Decimal("0")
    engine.target_slugs = ["s-active", "s-cooled"]
    engine.slug_cooldown["s-cooled"] = 2000.0

    engine.tick(now_ts=1000.0)
    client.fetch_books_batch.assert_called_once_with(["s-active"])


def test_quote_market_clamps_paired_bid_to_98c():
    from pmus.risk import RiskGateResult
    logger = logging.getLogger("test_quote_clamp")
    mkt = Market(slug="y1", event_id="e1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)
    w = IncentiveWindow("y1", "p1", Decimal("100"), Decimal("0.30"), Decimal("50"), Decimal("0.03"), datetime.now(timezone.utc), None, "active")

    client = MagicMock()
    auth_client = MagicMock()
    auth_client.create_orders_batch.return_value = []
    risk_manager = MagicMock()
    risk_manager.check_pre_submit.return_value = RiskGateResult(True)

    settings = Settings(venue="global", total_budget=Decimal("100"), max_per_market=Decimal("50"))
    engine = LiveEngine(
        settings=settings,
        ledger=MagicMock(),
        public_client=client,
        auth_client=auth_client,
        risk_manager=risk_manager,
        logger=logger,
    )
    engine.active_windows["y1"] = w
    engine.paired_slugs = frozenset({"y1", "n1"})
    engine.condition_pairs = (("y1", "n1"),)
    engine.side_budgets[("y1", "bid")] = Decimal("50")
    engine.side_qtys[("y1", "bid")] = Decimal("50")

    resting_no_order = LiveOrder("o-no", "n1", "BUY_LONG", Decimal("0.50"), Decimal("50"))
    book_y = Book("y1", (Level(Decimal("0.49"), Decimal("200")),), (Level(Decimal("0.51"), Decimal("200")),), 1000)

    engine._quote_market(
        slug="y1",
        now_dt=datetime.now(timezone.utc),
        now_ts=1000.0,
        now_ms=1000000,
        exchange_open=[resting_no_order],
        live_positions={"y1": Position("y1", Decimal("0"), Decimal("0"))},
        buying_power=Decimal("500"),
        realized_loss_today=Decimal("0"),
        pair_inputs={},
        book=book_y,
    )

    quotes_submitted = auth_client.create_orders_batch.call_args[0][0]
    assert len(quotes_submitted) == 1
    assert quotes_submitted[0].price == Decimal("0.48")
    assert quotes_submitted[0].price + resting_no_order.price <= Decimal("0.98")

    auth_client.create_orders_batch.reset_mock()
    engine.active_windows["y1"] = IncentiveWindow("y1", "p1", Decimal("100"), Decimal("0.30"), Decimal("50"), Decimal("0.045"), datetime.now(timezone.utc), None, "active")
    engine._quote_market(
        slug="y1",
        now_dt=datetime.now(timezone.utc),
        now_ts=1000.0,
        now_ms=1000000,
        exchange_open=[resting_no_order],
        live_positions={"y1": Position("y1", Decimal("0"), Decimal("0"))},
        buying_power=Decimal("500"),
        realized_loss_today=Decimal("0"),
        pair_inputs={},
        book=book_y,
    )
    quotes_wide = auth_client.create_orders_batch.call_args[0][0]
    assert len(quotes_wide) == 1
    assert quotes_wide[0].price == Decimal("0.47")
    assert quotes_wide[0].price + resting_no_order.price <= Decimal("0.97")


def test_quote_market_clamps_and_freezes_unfilled_leg_when_partner_holds_inventory():
    from pmus.risk import RiskGateResult
    logger = logging.getLogger("test_quote_freeze")
    mkt_y = Market(slug="y1", event_id="e1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)
    mkt_n = Market(slug="n1", event_id="e1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)
    w = IncentiveWindow("y1", "p1", Decimal("100"), Decimal("0.30"), Decimal("50"), Decimal("0.05"), datetime.now(timezone.utc), None, "active")

    client = MagicMock()
    auth_client = MagicMock()
    auth_client.create_orders_batch.return_value = []
    risk_manager = MagicMock()
    risk_manager.check_pre_submit.return_value = RiskGateResult(True)

    settings = Settings(venue="global", total_budget=Decimal("100"), max_per_market=Decimal("50"))
    engine = LiveEngine(
        settings=settings,
        ledger=MagicMock(),
        public_client=client,
        auth_client=auth_client,
        risk_manager=risk_manager,
        logger=logger,
    )
    engine.markets["y1"] = mkt_y
    engine.markets["n1"] = mkt_n
    engine.active_windows["y1"] = w
    engine.paired_slugs = frozenset({"y1", "n1"})
    engine.condition_pairs = (("y1", "n1"),)
    engine.side_budgets[("y1", "bid")] = Decimal("50")
    engine.side_qtys[("y1", "bid")] = Decimal("50")

    pos_n = Position("n1", Decimal("50"), Decimal("0.52"))
    book_y = Book("y1", (Level(Decimal("0.49"), Decimal("200")),), (Level(Decimal("0.51"), Decimal("200")),), 1000)

    engine._quote_market(
        slug="y1",
        now_dt=datetime.now(timezone.utc),
        now_ts=1000.0,
        now_ms=1000000,
        exchange_open=[],
        live_positions={"y1": Position("y1", Decimal("0"), Decimal("0")), "n1": pos_n},
        buying_power=Decimal("500"),
        realized_loss_today=Decimal("0"),
        pair_inputs={},
        book=book_y,
    )

    quotes_submitted = auth_client.create_orders_batch.call_args[0][0]
    assert len(quotes_submitted) == 1
    assert quotes_submitted[0].price == Decimal("0.45")
    assert quotes_submitted[0].price + pos_n.avg_price <= Decimal("0.97")


    auth_client.create_orders_batch.reset_mock()
    resting_y = LiveOrder("o-y", "y1", "BUY_LONG", Decimal("0.43"), Decimal("50"))
    engine.ledger.get_open_orders.return_value = [resting_y]
    engine._quote_market(
        slug="y1",
        now_dt=datetime.now(timezone.utc),
        now_ts=1000.0,
        now_ms=1000000,
        exchange_open=[resting_y],
        live_positions={"y1": Position("y1", Decimal("0"), Decimal("0")), "n1": pos_n},
        buying_power=Decimal("500"),
        realized_loss_today=Decimal("0"),
        pair_inputs={},
        book=book_y,
    )
    auth_client.create_orders_batch.assert_not_called()


def test_quote_market_runs_check_pre_submit_before_canceling_and_blocks_cancel_when_disallowed():
    from pmus.risk import RiskGateResult
    logger = logging.getLogger("test_pre_cancel_risk")
    mkt = Market(slug="y1", event_id="e1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)
    w = IncentiveWindow("y1", "p1", Decimal("100"), Decimal("0.30"), Decimal("50"), Decimal("0.05"), datetime.now(timezone.utc), None, "active")

    client = MagicMock()
    auth_client = MagicMock()
    risk_manager = MagicMock()
    risk_manager.check_pre_submit.return_value = RiskGateResult(False, "Collateral exceeded")

    settings = Settings(venue="global", total_budget=Decimal("100"), max_per_market=Decimal("50"))
    engine = LiveEngine(
        settings=settings,
        ledger=MagicMock(),
        public_client=client,
        auth_client=auth_client,
        risk_manager=risk_manager,
        logger=logger,
    )
    engine.active_windows["y1"] = w
    engine.side_budgets[("y1", "bid")] = Decimal("50")
    engine.side_qtys[("y1", "bid")] = Decimal("50")

    old_order = LiveOrder("old-1", "y1", "BUY_LONG", Decimal("0.40"), Decimal("50"))
    engine.ledger.get_open_orders.return_value = [old_order]
    book_y = Book("y1", (Level(Decimal("0.48"), Decimal("200")),), (Level(Decimal("0.52"), Decimal("200")),), 1000)

    engine._quote_market(
        slug="y1",
        now_dt=datetime.now(timezone.utc),
        now_ts=1000.0,
        now_ms=1000000,
        exchange_open=[old_order],
        live_positions={"y1": Position("y1", Decimal("0"), Decimal("0"))},
        buying_power=Decimal("500"),
        realized_loss_today=Decimal("0"),
        pair_inputs={},
        book=book_y,
    )

    auth_client.cancel_orders_batch.assert_not_called()
    auth_client.create_orders_batch.assert_not_called()
    assert engine.market_states["y1"] == "PAUSED"
    engine.ledger.close_order.assert_not_called()


def test_quote_market_sub_target_dust_neither_zeroes_bid_budget_nor_freezes_partner_leg():
    from pmus.risk import RiskGateResult
    logger = logging.getLogger("test_sub_target_dust_engine")
    mkt_y = Market(slug="y1", event_id="e1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)
    mkt_n = Market(slug="n1", event_id="e1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)
    w_y = IncentiveWindow("y1", "p1", Decimal("100"), Decimal("0.30"), Decimal("65"), Decimal("0.05"), datetime.now(timezone.utc), None, "active")
    w_n = IncentiveWindow("n1", "p1", Decimal("100"), Decimal("0.30"), Decimal("65"), Decimal("0.05"), datetime.now(timezone.utc), None, "active")

    client = MagicMock()
    auth_client = MagicMock()
    auth_client.create_orders_batch.return_value = []
    risk_manager = MagicMock()
    risk_manager.check_pre_submit.return_value = RiskGateResult(True)

    settings = Settings(venue="global", total_budget=Decimal("100"), max_per_market=Decimal("50"))
    engine = LiveEngine(
        settings=settings,
        ledger=MagicMock(),
        public_client=client,
        auth_client=auth_client,
        risk_manager=risk_manager,
        logger=logger,
    )
    engine.markets["y1"] = mkt_y
    engine.markets["n1"] = mkt_n
    engine.active_windows["y1"] = w_y
    engine.active_windows["n1"] = w_n
    engine.paired_slugs = frozenset({"y1", "n1"})
    engine.condition_pairs = (("y1", "n1"),)
    engine.side_budgets[("y1", "bid")] = Decimal("50")
    engine.side_qtys[("y1", "bid")] = Decimal("65")

    resting_y = LiveOrder("o-y", "y1", "BUY_LONG", Decimal("0.44"), Decimal("65"))
    engine.ledger.get_open_orders.side_effect = lambda slug=None: [resting_y] if slug == "y1" else []
    book_y = Book("y1", (Level(Decimal("0.49"), Decimal("200")),), (Level(Decimal("0.51"), Decimal("200")),), 1000)

    dust_y = Position("y1", Decimal("5"), Decimal("0.46"))
    dust_n = Position("n1", Decimal("5"), Decimal("0.52"))

    engine._quote_market(
        slug="y1",
        now_dt=datetime.now(timezone.utc),
        now_ts=1000.0,
        now_ms=1000000,
        exchange_open=[resting_y],
        live_positions={"y1": dust_y, "n1": dust_n},
        buying_power=Decimal("500"),
        realized_loss_today=Decimal("0"),
        pair_inputs={},
        book=book_y,
    )

    quotes_submitted = auth_client.create_orders_batch.call_args[0][0]
    buy_q = next((q for q in quotes_submitted if q.side == "BUY_LONG"), None)
    assert buy_q is not None
    assert buy_q.price == Decimal("0.49")
    assert buy_q.qty == Decimal("65")


def test_live_engine_yield_collapse_early_reallocation():
    from pmus.engine import REALLOCATE_SECONDS
    from pmus.models import LipSnapshot
    from pmus.strategy.market_selector import CandidateSide

    assert REALLOCATE_SECONDS == 300

    logger = logging.getLogger("test_yield_collapse")
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
    market_y = Market(slug="c1-yes", event_id="c1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)
    market_n = Market(slug="c1-no", event_id="c1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)

    public_client = MagicMock()
    public_client.fetch_books_batch.return_value = {"c1-yes": yes_b, "c1-no": no_b}
    public_client.get_market.side_effect = lambda slug: market_y if slug == "c1-yes" else market_n

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_open_orders.return_value = []
    auth_client.get_fills.return_value = []
    auth_client.get_positions.return_value = []
    auth_client.get_buying_power.return_value = Decimal("500.00")
    auth_client.create_orders_batch.return_value = []
    auth_client.cancel_all_scoped.return_value = []

    ledger = MagicMock()
    ledger.get_open_orders.return_value = []
    ledger.get_realized_loss_today.return_value = Decimal("0")

    risk_mgr = MagicMock()
    risk_mgr.is_kill_switch_active.return_value = False
    risk_mgr.is_maintenance_active.return_value = False
    risk_mgr.check_pre_submit.return_value = MagicMock(allowed=True)

    settings = Settings(venue="global", total_budget=Decimal("200.00"), max_per_market=Decimal("100.00"))
    engine = LiveEngine(
        settings=settings,
        ledger=ledger,
        public_client=public_client,
        auth_client=auth_client,
        risk_manager=risk_mgr,
        logger=logger,
    )

    cand_y = CandidateSide("c1-yes", "c1", "bid", Decimal("0.48"), Decimal("0"), Decimal("50"), Decimal("24"), Decimal("0.0005"), Decimal("0.00002"), "global", Decimal("200"), Decimal("86400"))
    cand_n = CandidateSide("c1-no", "c1", "bid", Decimal("0.48"), Decimal("0"), Decimal("50"), Decimal("24"), Decimal("0.0005"), Decimal("0.00002"), "global", Decimal("200"), Decimal("86400"))
    fake_alloc = AllocationResult(
        candidates=[cand_y, cand_n],
        selected_candidates=[cand_y, cand_n],
        selected_slugs=["c1-yes", "c1-no"],
        side_budgets={("c1-yes", "bid"): Decimal("24"), ("c1-no", "bid"): Decimal("24")},
        side_qtys={("c1-yes", "bid"): Decimal("50"), ("c1-no", "bid"): Decimal("50")},
        unfunded_reason=None,
        paired_slugs=frozenset({"c1-yes", "c1-no"}),
        condition_pairs=(("c1-yes", "c1-no"),),
    )

    with patch("pmus.engine.allocate_markets", return_value=({"c1-yes": w, "c1-no": w}, fake_alloc)):
        engine.prepare(is_realloc=False)

    assert engine.expected_portfolio_rate_sec == Decimal("0.0010")
    t0 = 1000.0
    engine.last_prepare_ts = t0

    collapsed_snaps = [
        LipSnapshot(
            ts_ms=1000000,
            slug="c1-yes",
            program_id="p-c1",
            trader_bid_score=Decimal("10"),
            total_bid_score=Decimal("100"),
            trader_ask_score=Decimal("0"),
            total_ask_score=Decimal("100"),
            qualifies=True,
            reward_usd=Decimal("0.0002"),
        ),
        LipSnapshot(
            ts_ms=1000000,
            slug="c1-no",
            program_id="p-c1",
            trader_bid_score=Decimal("10"),
            total_bid_score=Decimal("100"),
            trader_ask_score=Decimal("0"),
            total_ask_score=Decimal("100"),
            qualifies=True,
            reward_usd=Decimal("0.0002"),
        ),
    ]
    healthy_snaps = [
        LipSnapshot(
            ts_ms=1000000,
            slug="c1-yes",
            program_id="p-c1",
            trader_bid_score=Decimal("50"),
            total_bid_score=Decimal("100"),
            trader_ask_score=Decimal("0"),
            total_ask_score=Decimal("100"),
            qualifies=True,
            reward_usd=Decimal("0.0003"),
        ),
        LipSnapshot(
            ts_ms=1000000,
            slug="c1-no",
            program_id="p-c1",
            trader_bid_score=Decimal("50"),
            total_bid_score=Decimal("100"),
            trader_ask_score=Decimal("0"),
            total_ask_score=Decimal("100"),
            qualifies=True,
            reward_usd=Decimal("0.0003"),
        ),
    ]

    with patch("pmus.engine.estimate_global_pair_score", return_value=collapsed_snaps), patch.object(engine, "prepare") as mock_prep:
        for i in range(1, 20):
            engine.tick(t0 + float(i))
        mock_prep.assert_not_called()
        assert engine.yield_collapse_streak == 19

    with patch("pmus.engine.estimate_global_pair_score", return_value=healthy_snaps), patch.object(engine, "prepare") as mock_prep:
        engine.tick(t0 + 85.0)
        mock_prep.assert_not_called()
        assert engine.yield_collapse_streak == 0

    with patch("pmus.engine.estimate_global_pair_score", return_value=collapsed_snaps), patch.object(engine, "prepare") as mock_prep:
        for i in range(14):
            engine.tick(t0 + 90.0 + float(i))
        mock_prep.assert_not_called()
        assert engine.yield_collapse_streak == 14

        engine.tick(t0 + 105.0)
        mock_prep.assert_called_once_with(is_realloc=True)
        assert engine.yield_collapse_streak == 0


def test_prepare_caches_404_unwind_slugs_and_allows_ask_only_books():
    logger = logging.getLogger("test_dead_unwind_404")
    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_buying_power.return_value = Decimal("500.00")
    pos_404 = Position("slug-404", Decimal("65"), Decimal("0.20"))
    pos_timeout_with_404 = Position("123404567", Decimal("65"), Decimal("0.20"))
    pos_ask_only = Position("slug-ask-only", Decimal("65"), Decimal("0.22"))
    auth_client.get_positions.return_value = [pos_404, pos_timeout_with_404, pos_ask_only]
    auth_client.cancel_all_scoped.return_value = []

    client = MagicMock()
    client.fetch_reward_conditions.return_value = []
    book_ask_only = Book("slug-ask-only", (), (Level(Decimal("0.12"), Decimal("100")),), 1000)

    timeout_mode = {"is_404": False}

    def fetch_book_dispatch(slug):
        if slug == "slug-404":
            raise RuntimeError("404 Client Error: Not Found for url")
        if slug == "123404567":
            if timeout_mode["is_404"]:
                raise RuntimeError("404 Not Found for token_id=123404567")
            raise RuntimeError("ReadTimeout for token_id=123404567")
        return book_ask_only

    client.fetch_book.side_effect = fetch_book_dispatch

    settings = Settings(venue="global", total_budget=Decimal("100"), max_per_market=Decimal("50"))
    engine = LiveEngine(
        settings=settings,
        ledger=MagicMock(),
        public_client=client,
        auth_client=auth_client,
        risk_manager=MagicMock(),
        logger=logger,
    )
    with patch("pmus.engine.allocate_markets", return_value=({}, AllocationResult([], [], [], {}, {}, None, frozenset(), ()))):
        engine.prepare()
        assert "slug-404" in engine.dead_unwind_slugs
        assert "123404567" not in engine.dead_unwind_slugs
        assert "slug-404" not in engine.target_slugs
        assert "slug-ask-only" in engine.target_slugs
        assert "slug-ask-only" in engine.active_windows
        assert client.fetch_book.call_count == 3

        client.fetch_book.reset_mock()
        engine.active_windows.clear()
        timeout_mode["is_404"] = True
        engine.prepare(is_realloc=True)
        assert "123404567" in engine.dead_unwind_slugs
        assert client.fetch_book.call_count == 2


def test_allocate_markets_filters_unaffordable_conditions_and_reaches_rank_1400():
    logger = logging.getLogger("test_filters_unaffordable")
    cond_unaffordable = make_condition(cond_id="c_unaffordable", pool="1000000", target="500")
    affordable_conditions = [
        make_condition(cond_id=f"c_aff_{i}", pool=str(1500 - i), target="50")
        for i in range(1401)
    ]
    all_conditions = [cond_unaffordable] + affordable_conditions

    client = MagicMock()
    client.fetch_reward_conditions.return_value = list(all_conditions)
    client.fetch_books_batch.return_value = {}

    settings = Settings(
        venue="global",
        total_budget=Decimal("200.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=4,
        max_contracts_per_market=100,
    )

    allocate_markets(settings, client, logger)
    fetched_slugs = client.fetch_books_batch.call_args[0][0]
    assert "c_aff_0-yes" in fetched_slugs
    assert "c_aff_1399-yes" in fetched_slugs
    assert "c_aff_1400-yes" in fetched_slugs
    assert cond_unaffordable.yes.slug in fetched_slugs
    assert fetched_slugs.index("c_aff_1400-no") < fetched_slugs.index(cond_unaffordable.yes.slug)


def test_allocate_markets_sequential_fallback_caps_at_fallback_limit_and_warns(caplog):
    logger = logging.getLogger("test_seq_fallback")
    conditions = [
        make_condition(cond_id=f"c_{i}", pool=str(500 - i), target="50")
        for i in range(200)
    ]

    client = MagicMock(spec=["fetch_reward_conditions", "fetch_book", "fetch_books_batch"])
    client.fetch_reward_conditions.return_value = list(conditions)
    client.fetch_books_batch.return_value = {}
    client.fetch_book.return_value = make_book([("0.48", "250")], [("0.52", "250")])

    settings = Settings(
        venue="global",
        total_budget=Decimal("200.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=4,
        max_contracts_per_market=100,
    )

    with caplog.at_level(logging.WARNING):
        allocate_markets(settings, client, logger)

    assert client.fetch_book.call_count == 300
    warning_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Falling back to sequential book fetch" in r.message
    ]
    assert len(warning_records) == 1
    assert "FALLBACK_BOOK_CONDITION_LIMIT=150" in warning_records[0].message


def test_allocate_markets_partial_batch_books_skips_missing_legs_and_logs(caplog):
    logger = logging.getLogger("test_partial_batch")
    cond1 = make_condition(cond_id="c1", pool="300", target="50")
    cond2 = make_condition(cond_id="c2", pool="200", target="50")

    book1_yes = make_book([("0.48", "250")], [("0.52", "250")], slug="c1-yes")
    book1_no = make_book([("0.48", "250")], [("0.52", "250")], slug="c1-no")
    book2_yes = make_book([("0.48", "250")], [("0.52", "250")], slug="c2-yes")

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond1, cond2]
    client.fetch_books_batch.return_value = {
        "c1-yes": book1_yes,
        "c1-no": book1_no,
        "c2-yes": book2_yes,
    }

    settings = Settings(
        venue="global",
        total_budget=Decimal("200.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=4,
        max_contracts_per_market=100,
    )

    with caplog.at_level(logging.WARNING):
        _, alloc = allocate_markets(settings, client, logger)

    assert alloc.selected_slugs == ["c1-yes", "c1-no"]
    warns = [r for r in caplog.records if "fetch_books_batch returned partial books" in r.message]
    assert len(warns) == 1
    assert "1 conditions had books vs 2 requested" in warns[0].message


def test_allocate_markets_batch_books_below_50_pct_falls_back_to_sequential(caplog):
    logger = logging.getLogger("test_batch_below_50")
    cond1 = make_condition(cond_id="c1", pool="300", target="50")
    cond2 = make_condition(cond_id="c2", pool="200", target="50")
    cond3 = make_condition(cond_id="c3", pool="100", target="50")

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond1, cond2, cond3]
    client.fetch_books_batch.return_value = {
        "c1-yes": make_book([("0.48", "250")], [("0.52", "250")], slug="c1-yes"),
        "c1-no": make_book([("0.48", "250")], [("0.52", "250")], slug="c1-no"),
    }
    client.fetch_book.return_value = make_book([("0.48", "250")], [("0.52", "250")])

    settings = Settings(
        venue="global",
        total_budget=Decimal("200.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=4,
        max_contracts_per_market=100,
    )

    with caplog.at_level(logging.WARNING):
        allocate_markets(settings, client, logger)

    assert client.fetch_book.call_count == 6
    warns = [r for r in caplog.records if "Falling back to sequential book fetch" in r.message]
    assert len(warns) == 1


def test_prepare_startup_cancels_non_target_orphaned_orders():
    logger = logging.getLogger("test_startup_orphan_cancel")
    cond = make_condition(cond_id="c1", pool="200")

    public_client = MagicMock()
    public_client.fetch_reward_conditions.return_value = [cond]
    public_client.fetch_book.return_value = make_book([("0.48", "250")], [("0.52", "250")])

    orphan_order = LiveOrder(
        order_id="ord-orphan",
        slug="slug-heisman-removed",
        side="BUY_LONG",
        price=Decimal("0.50"),
        qty_open=Decimal("10"),
    )

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_buying_power.return_value = Decimal("500.00")
    auth_client.get_positions.return_value = []
    auth_client.get_open_orders.return_value = [orphan_order]
    auth_client.cancel_all_scoped.return_value = []
    auth_client.cancel_orders_batch.return_value = []

    settings = Settings(
        venue="global",
        total_budget=Decimal("200.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=2,
        max_contracts_per_market=100,
    )

    engine = LiveEngine(
        settings=settings,
        ledger=MagicMock(),
        public_client=public_client,
        auth_client=auth_client,
        risk_manager=MagicMock(),
        logger=logger,
    )

    engine.prepare(is_realloc=False)
    auth_client.cancel_orders_batch.assert_called_once()
    cancelled_orders = auth_client.cancel_orders_batch.call_args[0][0]
    assert any(o.order_id == "ord-orphan" for o in cancelled_orders)

    auth_client.cancel_orders_batch.reset_mock()
    auth_client.cancel_all_scoped.reset_mock()

    engine.prepare(is_realloc=True)
    auth_client.cancel_orders_batch.assert_not_called()


def test_prepare_unwind_non_404_logs_warning_with_slug(caplog):
    logger = logging.getLogger("test_unwind_warning")
    cond = make_condition(cond_id="c1", pool="200")

    public_client = MagicMock()
    public_client.fetch_reward_conditions.return_value = [cond]
    public_client.fetch_book.side_effect = ConnectionError("Connection refused")

    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client.get_buying_power.return_value = Decimal("500.00")
    held_pos = Position(slug="slug-held-unwind", net_qty=Decimal("10"), avg_price=Decimal("0.50"))
    auth_client.get_positions.return_value = [held_pos]
    auth_client.get_open_orders.return_value = []
    auth_client.cancel_all_scoped.return_value = []
    auth_client.cancel_orders_batch.return_value = []

    settings = Settings(
        venue="global",
        total_budget=Decimal("200.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=2,
        max_contracts_per_market=100,
    )

    engine = LiveEngine(
        settings=settings,
        ledger=MagicMock(),
        public_client=public_client,
        auth_client=auth_client,
        risk_manager=MagicMock(),
        logger=logger,
    )

    with caplog.at_level(logging.WARNING):
        engine.prepare(is_realloc=False)

    warn_logs = [r for r in caplog.records if r.levelno == logging.WARNING and "Unwind book fetch failed for slug-held-unwind" in r.message]
    assert len(warn_logs) == 1
    assert "Connection refused" in warn_logs[0].message


def test_allocate_markets_logs_cycle_telemetry_info(caplog):
    logger = logging.getLogger("test_cycle_info")
    cond_temp = make_condition(cond_id="c_temp", pool="500", target="50", question="Temperature in NYC")
    cond_aff = make_condition(cond_id="c_aff", pool="300", target="50", question="Will team A win?")
    cond_unaff = make_condition(cond_id="c_unaff", pool="200", target="500", question="Will team B win?")

    book_yes = make_book([("0.48", "250")], [("0.52", "250")], slug="c_aff-yes")
    book_no = make_book([("0.48", "250")], [("0.52", "250")], slug="c_aff-no")

    client = MagicMock()
    client.fetch_reward_conditions.return_value = [cond_temp, cond_aff, cond_unaff]
    client.fetch_books_batch.return_value = {
        "c_aff-yes": book_yes,
        "c_aff-no": book_no,
    }

    settings = Settings(
        venue="global",
        total_budget=Decimal("100.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=2,
        max_contracts_per_market=100,
    )

    with caplog.at_level(logging.INFO):
        allocate_markets(settings, client, logger)

    info_records = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "Book fetch cycle:" in r.message
    ]
    assert len(info_records) == 1
    msg = info_records[0].message
    assert "2 conditions after filters" in msg
    assert "1 conditions affordable" in msg
    assert "1 conditions with books" in msg
    assert "elapsed=" in msg


def test_quote_market_uses_symmetric_pair_quote_prices_when_batch_books_present():
    from pmus.risk import RiskGateResult

    logger = logging.getLogger("test_symmetric_pair")
    mkt_y = Market(slug="y1", event_id="e1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)
    mkt_n = Market(slug="n1", event_id="e1", category="sports", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)
    w = IncentiveWindow("y1", "p1", Decimal("100"), Decimal("0.30"), Decimal("50"), Decimal("0.045"), datetime.now(timezone.utc), None, "active")
    w_n = IncentiveWindow("n1", "p1", Decimal("100"), Decimal("0.30"), Decimal("50"), Decimal("0.045"), datetime.now(timezone.utc), None, "active")

    client = MagicMock()
    auth_client = MagicMock()
    auth_client.create_orders_batch.return_value = []
    risk_manager = MagicMock()
    risk_manager.check_pre_submit.return_value = RiskGateResult(True)

    settings = Settings(venue="global", total_budget=Decimal("100"), max_per_market=Decimal("50"))
    engine = LiveEngine(
        settings=settings,
        ledger=MagicMock(),
        public_client=client,
        auth_client=auth_client,
        risk_manager=risk_manager,
        logger=logger,
    )
    engine.markets["y1"] = mkt_y
    engine.markets["n1"] = mkt_n
    engine.active_windows["y1"] = w
    engine.active_windows["n1"] = w_n
    engine.paired_slugs = frozenset({"y1", "n1"})
    engine.condition_pairs = (("y1", "n1"),)
    engine.side_budgets[("y1", "bid")] = Decimal("50")
    engine.side_qtys[("y1", "bid")] = Decimal("50")
    engine.side_budgets[("n1", "bid")] = Decimal("50")
    engine.side_qtys[("n1", "bid")] = Decimal("50")

    book_y = Book("y1", (Level(Decimal("0.49"), Decimal("200")),), (Level(Decimal("0.51"), Decimal("200")),), 1000)
    book_n = Book("n1", (Level(Decimal("0.49"), Decimal("200")),), (Level(Decimal("0.51"), Decimal("200")),), 1000)
    batch_books = {"y1": book_y, "n1": book_n}

    engine._quote_market(
        slug="y1",
        now_dt=datetime.now(timezone.utc),
        now_ts=1000.0,
        now_ms=1000000,
        exchange_open=[],
        live_positions={"y1": Position("y1", Decimal("0"), Decimal("0")), "n1": Position("n1", Decimal("0"), Decimal("0"))},
        buying_power=Decimal("500"),
        realized_loss_today=Decimal("0"),
        pair_inputs={},
        book=book_y,
        batch_books=batch_books,
    )
    quotes_y = auth_client.create_orders_batch.call_args[0][0]
    assert len(quotes_y) == 1
    assert quotes_y[0].price == Decimal("0.48")

    auth_client.create_orders_batch.reset_mock()
    resting_y = LiveOrder("o-y", "y1", "BUY_LONG", Decimal("0.48"), Decimal("50"))
    engine._quote_market(
        slug="n1",
        now_dt=datetime.now(timezone.utc),
        now_ts=1000.0,
        now_ms=1000000,
        exchange_open=[resting_y],
        live_positions={"y1": Position("y1", Decimal("0"), Decimal("0")), "n1": Position("n1", Decimal("0"), Decimal("0"))},
        buying_power=Decimal("500"),
        realized_loss_today=Decimal("0"),
        pair_inputs={},
        book=book_n,
        batch_books=batch_books,
    )
    quotes_n = auth_client.create_orders_batch.call_args[0][0]
    assert len(quotes_n) == 1
    assert quotes_n[0].price == Decimal("0.49")


def test_allocate_markets_caps_effective_max_conditions_to_prevent_dust_waterfill():
    logger = logging.getLogger("test_eff_max_cond")
    conditions = [
        make_condition(cond_id=f"c{i}", pool=str(300 - i * 10), target="20", question=f"Will team {i} win?")
        for i in range(10)
    ]
    books = {}
    for c in conditions:
        books[c.yes.slug] = make_book([("0.48", "250")], [("0.52", "250")], slug=c.yes.slug)
        books[c.no.slug] = make_book([("0.48", "250")], [("0.52", "250")], slug=c.no.slug)

    client = MagicMock()
    client.fetch_reward_conditions.return_value = conditions
    client.fetch_books_batch.return_value = books

    settings = Settings(
        venue="global",
        total_budget=Decimal("1000.00"),
        max_per_market=Decimal("100.00"),
        max_collateral_per_market=Decimal("100.00"),
        max_markets=10,
        max_contracts_per_market=100,
        pair_preference=Decimal("0.0"),
    )

    _, alloc_res = allocate_markets(settings, client, logger, available_budget=Decimal("320.00"))
    assert len(alloc_res.condition_pairs) == 6
    assert all(qty >= Decimal("35") for qty in alloc_res.side_qtys.values())



