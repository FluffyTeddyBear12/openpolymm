import logging
import signal
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from pmus import engine as engine_mod
from pmus.__main__ import install_sigterm_handler, raise_system_exit
from pmus.config import Settings
from pmus.engine import LiveEngine
from pmus.ledger import Ledger
from pmus.models import Book, IncentiveWindow, Level, LiveOrder, Market, Position, ShadowFill
from pmus.risk import RiskManager
from pmus.strategy.market_selector import AllocationResult

T0 = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc).timestamp()
GRACE = 20


class FakeAuth:
    is_available = True

    def __init__(self):
        self.open_orders: list[LiveOrder] = []
        self.fills: list[ShadowFill] = []
        self.positions: list[Position] = []
        self.fail = False
        self.created: list[LiveOrder] = []
        self.canceled: list[str] = []
        self.scoped_cancels: list[list[str]] = []

    def get_open_orders(self, slugs=None):
        if self.fail:
            raise RuntimeError("502 from open orders")
        return list(self.open_orders)

    def get_fills(self, since=None):
        return list(self.fills)

    def get_positions(self):
        return list(self.positions)

    def get_buying_power(self):
        return Decimal("100")

    def create_orders_batch(self, quotes, tick_map=None, min_qty_map=None):
        orders = [
            LiveOrder(f"0x{len(self.created) + i + 1}", q.slug, q.side, q.price, q.qty)
            for i, q in enumerate(quotes)
        ]
        self.created.extend(orders)
        return orders

    def cancel_orders_batch(self, orders):
        self.canceled.extend(o.order_id for o in orders)
        return [o.order_id for o in orders]

    def cancel_all_scoped(self, slugs):
        self.scoped_cancels.append(list(slugs))
        return []


class FakePublic:
    def __init__(self, bid: str = "0.49", ask: str = "0.51"):
        self.bid = Decimal(bid)
        self.ask = Decimal(ask)

    def fetch_book(self, slug):
        return Book(slug, (Level(self.bid, Decimal("1000")),), (Level(self.ask, Decimal("1000")),), 0)

    def get_market(self, slug):
        return Market(slug=slug, event_id=f"ev-{slug}", category="global", tick=Decimal("0.01"), min_qty=Decimal("5"), active=True)


def window(slug: str) -> IncentiveWindow:
    start = datetime.fromtimestamp(T0, timezone.utc) - timedelta(hours=1)
    return IncentiveWindow(
        slug=slug,
        program_id=f"p-{slug}",
        pool_usd=Decimal("100"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.03"),
        start=start,
        end=start + timedelta(hours=24),
        status="active",
    )


def make_engine(tmp_path, auth=None, public=None, slugs=("tok",), ledger=None, **overrides) -> LiveEngine:
    settings = Settings(
        venue="global",
        data_dir=tmp_path / "data",
        order_sync_grace_seconds=GRACE,
        **overrides,
    )
    eng = LiveEngine(
        settings=settings,
        ledger=ledger or Ledger(tmp_path / "ledger.db"),
        public_client=public or FakePublic(),
        auth_client=auth or FakeAuth(),
        risk_manager=RiskManager(settings, stop_file_path=tmp_path / "STOP"),
        logger=logging.getLogger("test-live"),
    )
    eng.notifier = MagicMock()
    eng.started_at = T0
    eng.target_slugs = list(slugs)
    eng.active_windows = {s: window(s) for s in slugs}
    eng.side_budgets = {(s, "bid"): Decimal("100") for s in slugs}
    return eng


def order_status(ledger: Ledger, order_id: str) -> str:
    with sqlite3.connect(ledger.db_path) as conn:
        return conn.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,)).fetchone()[0]


def test_new_order_missing_from_exchange_list_is_not_recreated_within_grace(tmp_path):
    auth = FakeAuth()
    eng = make_engine(tmp_path, auth=auth)

    eng.tick(T0)
    eng.tick(T0 + 1)
    eng.tick(T0 + GRACE - 1)

    assert [o.order_id for o in auth.created] == ["0x1"]
    assert order_status(eng.ledger, "0x1") == "OPEN"


def test_order_still_missing_after_grace_is_closed_and_requoted(tmp_path):
    auth = FakeAuth()
    eng = make_engine(tmp_path, auth=auth)

    eng.tick(T0)
    eng.tick(T0 + GRACE + 1)

    assert order_status(eng.ledger, "0x1") == "CANCELED"
    assert [o.order_id for o in auth.created] == ["0x1", "0x2"]


def test_order_listed_on_exchange_is_kept_after_grace(tmp_path):
    auth = FakeAuth()
    eng = make_engine(tmp_path, auth=auth)

    eng.tick(T0)
    auth.open_orders = list(auth.created)
    eng.tick(T0 + 100)

    assert len(auth.created) == 1
    assert order_status(eng.ledger, "0x1") == "OPEN"


def test_missing_order_with_fill_is_closed_as_filled(tmp_path):
    auth = FakeAuth()
    eng = make_engine(tmp_path, auth=auth)
    eng.tick(T0)
    auth.fills = [ShadowFill("trade-1:0x1", "0x1", "tok", "BUY_LONG", Decimal("0.49"), Decimal("50"), Decimal("0"), True, int(T0 * 1000))]
    auth.positions = [Position("tok", Decimal("50"), Decimal("0.49"))]

    eng.tick(T0 + GRACE + 1)

    assert order_status(eng.ledger, "0x1") == "FILLED"


def test_exchange_read_failure_skips_tick_without_mutation(tmp_path):
    auth = FakeAuth()
    eng = make_engine(tmp_path, auth=auth)
    stale = LiveOrder("0xold", "tok", "BUY_LONG", Decimal("0.40"), Decimal("50"))
    eng.ledger.record_order(stale, status="OPEN")
    auth.fail = True

    eng.tick(T0)

    assert auth.created == []
    assert auth.canceled == []
    assert order_status(eng.ledger, "0xold") == "OPEN"


def test_order_from_before_restart_counts_as_old(tmp_path):
    auth = FakeAuth()
    eng = make_engine(tmp_path, auth=auth)
    eng.ledger.record_order(LiveOrder("0xold", "tok", "BUY_LONG", Decimal("0.49"), Decimal("50")), status="OPEN")

    eng.tick(T0)

    assert order_status(eng.ledger, "0xold") == "CANCELED"
    assert [o.order_id for o in auth.created] == ["0x1"]


def test_fill_recorded_and_notified_once_across_restart(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    fill = ShadowFill("trade-9:0xa", "0xa", "tok", "BUY_LONG", Decimal("0.49"), Decimal("10"), Decimal("0"), True, int(T0 * 1000))
    auth = FakeAuth()
    auth.fills = [fill]

    first = make_engine(tmp_path, auth=auth, ledger=ledger)
    first.tick(T0)
    first.tick(T0 + 1)
    second = make_engine(tmp_path, auth=auth, ledger=ledger)
    second.tick(T0 + 2)

    assert len(ledger.get_fills_since(0)) == 1
    first.notifier.notify_fill.assert_called_once_with(fill)
    second.notifier.notify_fill.assert_not_called()


def test_target_absent_from_exchange_positions_is_flat_even_if_ledger_disagrees(tmp_path):
    auth = FakeAuth()
    eng = make_engine(tmp_path, auth=auth)
    eng.ledger.update_position(Position("tok", Decimal("100"), Decimal("0.49")))

    eng.tick(T0)

    assert [(o.side, o.qty_open) for o in auth.created] == [("BUY_LONG", Decimal("50"))]


def test_risk_gate_counts_exchange_orders_outside_the_ledger(tmp_path):
    auth = FakeAuth()
    auth.open_orders = [LiveOrder("0xforeign", "tok-other", "BUY_LONG", Decimal("0.40"), Decimal("100"))]
    eng = make_engine(tmp_path, auth=auth)

    eng.tick(T0)

    assert auth.created == []


def test_risk_gate_counts_orders_created_this_tick_before_exchange_lists_them(tmp_path):
    auth = FakeAuth()
    eng = make_engine(tmp_path, auth=auth, slugs=("tok-a", "tok-b"), total_budget=Decimal("40"))

    eng.tick(T0)
    eng.tick(T0 + 1)

    assert [o.slug for o in auth.created] == ["tok-a"]


def test_order_canceled_this_tick_does_not_block_its_replacement(tmp_path):
    auth = FakeAuth()
    public = FakePublic()
    eng = make_engine(tmp_path, auth=auth, public=public, total_budget=Decimal("40"))
    eng.tick(T0)
    auth.open_orders = list(auth.created)
    public.bid, public.ask = Decimal("0.50"), Decimal("0.52")

    eng.tick(T0 + 30)

    assert auth.canceled == ["0x1"]
    assert [(o.order_id, o.price) for o in auth.created] == [("0x1", Decimal("0.49")), ("0x2", Decimal("0.50"))]


def test_untracked_exchange_order_on_target_is_canceled(tmp_path):
    auth = FakeAuth()
    auth.open_orders = [LiveOrder("0xdup", "tok", "BUY_LONG", Decimal("0.49"), Decimal("50"))]
    eng = make_engine(tmp_path, auth=auth)

    eng.tick(T0)

    assert "0xdup" in auth.canceled


def test_run_cancels_target_orders_in_finally(tmp_path, monkeypatch):
    auth = FakeAuth()
    eng = make_engine(tmp_path, auth=auth)
    alloc = AllocationResult(
        candidates=[],
        selected_candidates=[],
        selected_slugs=["tok"],
        side_budgets={("tok", "bid"): Decimal("100")},
        side_qtys={},
    )
    monkeypatch.setattr(engine_mod, "allocate_markets", lambda *a, **kw: ({"tok": window("tok")}, alloc))

    def sigterm(now_ts):
        eng.ledger.record_order(LiveOrder("0xlive", "tok", "BUY_LONG", Decimal("0.49"), Decimal("50")), status="OPEN")
        raise SystemExit(0)

    monkeypatch.setattr(eng, "tick", sigterm)

    with pytest.raises(SystemExit):
        eng.run(duration_seconds=60)

    assert auth.scoped_cancels == [["tok"], ["tok"]]
    assert auth.canceled == ["0xlive"]
    assert order_status(eng.ledger, "0xlive") == "CANCELED"


def test_sigterm_handler_raises_system_exit():
    previous = signal.getsignal(signal.SIGTERM)
    try:
        install_sigterm_handler()
        assert signal.getsignal(signal.SIGTERM) is raise_system_exit
        with pytest.raises(SystemExit):
            raise_system_exit(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_run_reallocates_markets_periodically(tmp_path, monkeypatch):
    eng = make_engine(tmp_path)
    monkeypatch.setattr(engine_mod, "REALLOCATE_SECONDS", 0)
    calls = {"prepare": 0, "realloc": 0}

    def fake_prepare(is_realloc: bool = False):
        calls["prepare"] += 1
        if is_realloc:
            calls["realloc"] += 1
        eng.target_slugs = ["tok"]

    monkeypatch.setattr(eng, "prepare", fake_prepare)
    monkeypatch.setattr(eng, "tick", lambda now_ts: 0.1)
    monkeypatch.setattr(eng, "shutdown", lambda: None)

    eng.run(duration_seconds=1)

    assert calls["prepare"] >= 3
    assert calls["realloc"] == calls["prepare"] - 1


def test_run_idles_without_ticking_when_no_markets_selected(tmp_path, monkeypatch):
    eng = make_engine(tmp_path)
    ticks = []
    sleeps = []

    def fake_prepare():
        eng.target_slugs = []

    monkeypatch.setattr(eng, "prepare", fake_prepare)
    monkeypatch.setattr(eng, "tick", lambda now_ts: ticks.append(now_ts) or 1.0)
    monkeypatch.setattr(eng, "shutdown", lambda: None)

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(engine_mod.time, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        eng.run(duration_seconds=60)

    assert ticks == []
    assert sleeps and sleeps[0] > 20
