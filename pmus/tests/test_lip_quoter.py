from datetime import datetime, timezone
from decimal import Decimal
import pytest

from pmus.models import Book, IncentiveWindow, Level, Market, Position, Quote
from pmus.strategy.lip_quoter import desired_quotes

@pytest.fixture
def base_market():
    return Market(
        slug="mkt-test",
        event_id="ev-1",
        category="sports",
        tick=Decimal("0.001"),
        min_qty=Decimal("1"),
        active=True,
        mutually_exclusive=False,
    )

@pytest.fixture
def active_window():
    return IncentiveWindow(
        slug="mkt-test",
        program_id="prog-1",
        pool_usd=Decimal("20.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )

def test_joins_best_price(base_market, active_window):
    bids = (Level(Decimal("0.490"), Decimal("200")),)
    asks = (Level(Decimal("0.510"), Decimal("200")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)

    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=active_window,
        position=Position(slug="mkt-test", net_qty=Decimal("0"), avg_price=Decimal("0")),
        budget=Decimal("10.00"),
    )

    assert len(quotes) == 2
    bid_q = next(q for q in quotes if q.side == "BUY_LONG")
    ask_q = next(q for q in quotes if q.side == "SELL_LONG")
    assert bid_q.price == Decimal("0.490")
    assert ask_q.price == Decimal("0.510")
    assert bid_q.qty > 0
    assert ask_q.qty > 0

def test_skips_when_window_inactive(base_market, active_window):
    bids = (Level(Decimal("0.490"), Decimal("200")),)
    asks = (Level(Decimal("0.510"), Decimal("200")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)

    inactive_window = IncentiveWindow(
        slug=active_window.slug,
        program_id=active_window.program_id,
        pool_usd=active_window.pool_usd,
        discount=active_window.discount,
        target_size=active_window.target_size,
        max_spread=active_window.max_spread,
        start=active_window.start,
        end=active_window.end,
        status="completed",
    )
    quotes = desired_quotes(base_market, book, inactive_window, None, Decimal("10.00"))
    assert quotes == []

def test_skips_when_max_spread_breached(base_market, active_window):
    bids = (Level(Decimal("0.400"), Decimal("200")),)
    asks = (Level(Decimal("0.600"), Decimal("200")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)

    quotes = desired_quotes(base_market, book, active_window, None, Decimal("10.00"))
    assert quotes == []

def test_skips_when_target_size_unmet(base_market, active_window):
    bids = (Level(Decimal("0.490"), Decimal("50")),)
    asks = (Level(Decimal("0.510"), Decimal("50")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)

    quotes = desired_quotes(base_market, book, active_window, None, Decimal("10.00"))
    assert quotes == []

def test_inventory_skew_long_cap(base_market, active_window):
    bids = (Level(Decimal("0.490"), Decimal("200")),)
    asks = (Level(Decimal("0.510"), Decimal("200")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)

    pos = Position(slug="mkt-test", net_qty=Decimal("60"), avg_price=Decimal("0.490"))
    quotes = desired_quotes(base_market, book, active_window, pos, Decimal("10.00"), max_position_cap=Decimal("50"))

    assert len(quotes) == 1
    assert quotes[0].side == "SELL_LONG"

def test_inventory_skew_short_cap(base_market, active_window):
    bids = (Level(Decimal("0.490"), Decimal("200")),)
    asks = (Level(Decimal("0.510"), Decimal("200")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)

    pos = Position(slug="mkt-test", net_qty=Decimal("-60"), avg_price=Decimal("0.510"))
    quotes = desired_quotes(base_market, book, active_window, pos, Decimal("10.00"), max_position_cap=Decimal("50"))

    assert len(quotes) == 1
    assert quotes[0].side == "BUY_LONG"

def test_sizing_from_budget(base_market, active_window):
    bids = (Level(Decimal("0.500"), Decimal("200")),)
    asks = (Level(Decimal("0.500"), Decimal("200")),)
    book = Book(slug="mkt-test", bids=(Level(Decimal("0.400"), Decimal("200")),), asks=(Level(Decimal("0.450"), Decimal("200")),), ts_ms=1000)

    quotes = desired_quotes(base_market, book, active_window, None, Decimal("10.00"))
    assert len(quotes) == 2
    bid_q = next(q for q in quotes if q.side == "BUY_LONG")
    ask_q = next(q for q in quotes if q.side == "SELL_LONG")

    assert bid_q.qty == Decimal("12")
    assert ask_q.qty == Decimal("9")
