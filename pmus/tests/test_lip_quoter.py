from datetime import datetime, timedelta, timezone
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
        target_size=Decimal("500"),
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

def test_pre_start_cancel_boundary(base_market):
    now = datetime.now(timezone.utc)
    book = Book(
        slug="mkt-test",
        bids=(Level(Decimal("0.490"), Decimal("200")),),
        asks=(Level(Decimal("0.510"), Decimal("200")),),
        ts_ms=1000,
    )
    pos = Position(slug="mkt-test", net_qty=Decimal("0"), avg_price=Decimal("0"))

    # Event starts in 299s -> within 300s cancel window -> should refuse to quote
    win_close = IncentiveWindow(
        slug="mkt-test",
        program_id="prog-1",
        pool_usd=Decimal("20.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=None,
        status="active",
        event_start_time=now + timedelta(seconds=299),
    )
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win_close,
        position=pos,
        budget=Decimal("10.00"),
        now=now,
        pre_start_cancel_seconds=300,
    )
    assert len(quotes) == 0

    # Event starts in 305s -> outside 300s cancel window -> allowed to quote
    win_safe = IncentiveWindow(
        slug="mkt-test",
        program_id="prog-1",
        pool_usd=Decimal("20.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("500"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=None,
        status="active",
        event_start_time=now + timedelta(seconds=305),
    )
    quotes_safe = desired_quotes(
        market=base_market,
        book=book,
        window=win_safe,
        position=pos,
        budget=Decimal("10.00"),
        now=now,
        pre_start_cancel_seconds=300,
    )
    assert len(quotes_safe) == 2

    # Live period refusal unless explicitly allowed
    win_live = IncentiveWindow(
        slug="mkt-test",
        program_id="prog-1",
        pool_usd=Decimal("20.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("500"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=None,
        status="active",
        period="live",
    )
    quotes_live = desired_quotes(
        market=base_market,
        book=book,
        window=win_live,
        position=pos,
        budget=Decimal("10.00"),
        quote_live_periods=False,
    )
    assert len(quotes_live) == 0

    quotes_live_allowed = desired_quotes(
        market=base_market,
        book=book,
        window=win_live,
        position=pos,
        budget=Decimal("10.00"),
        quote_live_periods=True,
    )
    assert len(quotes_live_allowed) == 2

def test_quoter_completes_target_size_when_budget_permits(base_market):
    win = IncentiveWindow(
        slug="mkt-test",
        program_id="prog-1",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("5000"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug="mkt-test",
        bids=(Level(Decimal("0.010"), Decimal("20")),),
        asks=(Level(Decimal("0.020"), Decimal("5000")),),
        ts_ms=1000,
    )
    pos = Position(slug="mkt-test", net_qty=Decimal("0"), avg_price=Decimal("0"))

    # Side budget is $50. Shortfall is 5000 - 20 = 4980 contracts.
    # Collateral needed is 4980 * 0.01 = $49.80 <= $50.00.
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=pos,
        budget=Decimal("100.00"),
        max_contracts_cap=Decimal("6000"),
    )
    bid_q = next(q for q in quotes if q.price == Decimal("0.010"))
    assert bid_q.qty == Decimal("4980")

def test_saturated_book_does_not_quote(base_market, active_window):
    # Book bids equal target size (500) -> shortfall is 0 -> do not quote bid
    bids = (Level(Decimal("0.490"), Decimal("500")),)
    asks = (Level(Decimal("0.510"), Decimal("50")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=active_window,
        position=Position(slug="mkt-test", net_qty=Decimal("0"), avg_price=Decimal("0")),
        budget=Decimal("10.00"),
    )
    assert not any(q.side == "BUY_LONG" for q in quotes)
    assert any(q.side == "SELL_SHORT" for q in quotes)

def test_depth_filtering_only_counts_within_max_spread(base_market):
    now = datetime.now(timezone.utc)
    win = IncentiveWindow(
        slug="mkt-spread-filter",
        program_id="p-1",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=Decimal("0.02"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    # best_bid = 0.490. Level at 0.400 is 0.090 away (> max_spread 0.02)
    bids = (Level(Decimal("0.490"), Decimal("20")), Level(Decimal("0.400"), Decimal("500")))
    asks = (Level(Decimal("0.510"), Decimal("20")),)
    book = Book("mkt-spread-filter", bids, asks, 1000)
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position("mkt-spread-filter", Decimal("0"), Decimal("0")),
        budget=Decimal("100.00"),
    )
    bid_q = next(q for q in quotes if q.side == "BUY_LONG")
    # Existing bids within max_spread is 20, not 520. Shortfall is 100 - 20 = 80.
    assert bid_q.qty == Decimal("80")


