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

def test_flat_account_quotes_buy_long_and_sell_short(base_market, active_window):
    bids = (Level(Decimal("0.490"), Decimal("200")),)
    asks = (Level(Decimal("0.510"), Decimal("200")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)

    pos = Position(slug="mkt-test", net_qty=Decimal("0"), avg_price=Decimal("0"))
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=active_window,
        position=pos,
        budget=Decimal("10.00"),
        max_contracts_cap=Decimal("200"),
    )

    assert len(quotes) == 2
    bid_q = next(q for q in quotes if q.side == "BUY_LONG")
    ask_q = next(q for q in quotes if q.side == "SELL_SHORT")
    assert bid_q.price == Decimal("0.490")
    assert ask_q.price == Decimal("0.510")

def test_long_account_quotes_sell_long(base_market, active_window):
    bids = (Level(Decimal("0.490"), Decimal("200")),)
    asks = (Level(Decimal("0.510"), Decimal("200")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)

    pos = Position(slug="mkt-test", net_qty=Decimal("50"), avg_price=Decimal("0.480"))
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=active_window,
        position=pos,
        budget=Decimal("10.00"),
        max_contracts_cap=Decimal("200"),
    )

    assert len(quotes) == 2
    ask_q = next(q for q in quotes if q.price == Decimal("0.510"))
    assert ask_q.side == "SELL_LONG"
    assert ask_q.qty <= Decimal("50")

def test_short_account_quotes_buy_short_to_cover(base_market, active_window):
    bids = (Level(Decimal("0.490"), Decimal("200")),)
    asks = (Level(Decimal("0.510"), Decimal("200")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)

    pos = Position(slug="mkt-test", net_qty=Decimal("-50"), avg_price=Decimal("0.520"))
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=active_window,
        position=pos,
        budget=Decimal("10.00"),
        max_contracts_cap=Decimal("200"),
    )

    assert len(quotes) == 2
    bid_q = next(q for q in quotes if q.price == Decimal("0.490"))
    ask_q = next(q for q in quotes if q.price == Decimal("0.510"))
    assert bid_q.side == "BUY_SHORT"
    assert ask_q.side == "SELL_SHORT"

def test_respects_contracts_cap_for_cheap_prices(base_market, active_window):
    bids = (Level(Decimal("0.010"), Decimal("200")),)
    asks = (Level(Decimal("0.020"), Decimal("200")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)

    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=active_window,
        position=Position(slug="mkt-test", net_qty=Decimal("0"), avg_price=Decimal("0")),
        budget=Decimal("25.00"),
        max_contracts_cap=Decimal("200"),
    )

    bid_q = next(q for q in quotes if q.price == Decimal("0.010"))
    assert bid_q.qty <= Decimal("200")

def test_inventory_skew_at_caps(base_market, active_window):
    bids = (Level(Decimal("0.490"), Decimal("200")),)
    asks = (Level(Decimal("0.510"), Decimal("200")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)

    pos_long = Position(slug="mkt-test", net_qty=Decimal("200"), avg_price=Decimal("0.490"))
    quotes_long = desired_quotes(base_market, book, active_window, pos_long, Decimal("10.00"), max_contracts_cap=Decimal("200"))
    assert len(quotes_long) == 1
    assert quotes_long[0].side == "SELL_LONG"

    pos_short = Position(slug="mkt-test", net_qty=Decimal("-200"), avg_price=Decimal("0.510"))
    quotes_short = desired_quotes(base_market, book, active_window, pos_short, Decimal("10.00"), max_contracts_cap=Decimal("200"))
    assert len(quotes_short) == 1
    assert quotes_short[0].side == "BUY_SHORT"
