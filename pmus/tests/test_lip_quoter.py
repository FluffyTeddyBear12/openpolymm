from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from pmus.models import Book, IncentiveWindow, Level, Market, Position, Quote
from pmus.strategy import global_rewards
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

def test_long_inventory_actively_unwinds_even_when_ask_shortfall_zero(base_market, active_window):
    # Book asks saturated (500 >= target_size 500) -> shortfall is 0
    bids = (Level(Decimal("0.490"), Decimal("10")),)
    asks = (Level(Decimal("0.510"), Decimal("500")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)

    pos = Position(slug="mkt-test", net_qty=Decimal("50"), avg_price=Decimal("0.480"))
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=active_window,
        position=pos,
        budget=Decimal("10.00"),
        ask_budget=Decimal("0.00"),  # Even with $0 ask budget, selling long doesn't need cash collateral
    )

    ask_q = next((q for q in quotes if q.side == "SELL_LONG"), None)
    assert ask_q is not None
    assert ask_q.price == Decimal("0.510")
    assert ask_q.qty == Decimal("50")

def test_flat_account_does_not_quote_ask_when_shortfall_zero(base_market, active_window):
    # Book asks saturated (500 >= target_size 500) -> shortfall is 0
    bids = (Level(Decimal("0.490"), Decimal("10")),)
    asks = (Level(Decimal("0.510"), Decimal("500")),)
    book = Book(slug="mkt-test", bids=bids, asks=asks, ts_ms=1000)

    pos = Position(slug="mkt-test", net_qty=Decimal("0"), avg_price=Decimal("0"))
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=active_window,
        position=pos,
        budget=Decimal("10.00"),
    )

    assert not any(q.side in ("SELL_LONG", "SELL_SHORT") for q in quotes)

def test_global_quotes_min_size_despite_saturated_book(base_market):
    win = IncentiveWindow(
        slug="mkt-global",
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("200"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    bids = (Level(Decimal("0.500"), Decimal("500000")),)
    asks = (Level(Decimal("0.520"), Decimal("500000")),)
    book = Book(slug="mkt-global", bids=bids, asks=asks, ts_ms=1000)

    us_quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-global", net_qty=Decimal("0"), avg_price=Decimal("0")),
        budget=Decimal("50.00"),
        is_global=False,
    )
    assert len(us_quotes) == 0

    global_quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-global", net_qty=Decimal("0"), avg_price=Decimal("0")),
        budget=Decimal("400.00"),
        is_global=True,
    )
    assert global_quotes == [Quote(slug=base_market.slug, side="BUY_LONG", price=Decimal("0.500"), qty=Decimal("200"))]

def test_global_long_inventory_unwinds(base_market):
    win = IncentiveWindow(
        slug="mkt-global",
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("200"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    bids = (Level(Decimal("0.050"), Decimal("500000")),)
    asks = (Level(Decimal("0.060"), Decimal("500000")),)
    book = Book(slug="mkt-global", bids=bids, asks=asks, ts_ms=1000)

    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-global", net_qty=Decimal("50"), avg_price=Decimal("0.050")),
        budget=Decimal("50.00"),
        is_global=True,
    )
    ask_q = next((q for q in quotes if q.side == "SELL_LONG"), None)
    assert ask_q is not None
    assert ask_q.qty == Decimal("50")
    assert ask_q.price == Decimal("0.059")


def test_quoter_paired_bids_in_tail_market(base_market):
    win = IncentiveWindow(
        slug="mkt-tail",
        program_id="p-tail",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    bids = (Level(Decimal("0.050"), Decimal("500")),)
    asks = (Level(Decimal("0.060"), Decimal("500")),)
    book = Book(slug="mkt-tail", bids=bids, asks=asks, ts_ms=1000)

    quotes_unpaired = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-tail", net_qty=Decimal("0"), avg_price=Decimal("0")),
        budget=Decimal("50.00"),
        is_global=True,
        paired=False,
    )
    assert quotes_unpaired == []

    quotes_paired = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-tail", net_qty=Decimal("0"), avg_price=Decimal("0")),
        budget=Decimal("50.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("100"),
    )
    assert len(quotes_paired) == 1
    assert quotes_paired[0].side == "BUY_LONG"
    assert quotes_paired[0].price == Decimal("0.050")
    assert quotes_paired[0].qty == Decimal("100")


def test_quoter_paired_stops_bidding_and_emits_sell_long_unwind(base_market):
    win = IncentiveWindow(
        slug="mkt-tail",
        program_id="p-tail",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    bids = (Level(Decimal("0.050"), Decimal("500")),)
    asks = (Level(Decimal("0.060"), Decimal("500")),)
    book = Book(slug="mkt-tail", bids=bids, asks=asks, ts_ms=1000)

    quotes_filled = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-tail", net_qty=Decimal("100"), avg_price=Decimal("0.050")),
        budget=Decimal("50.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("100"),
    )
    assert quotes_filled == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.059"), qty=Decimal("100"))
    ]

    quotes_overfilled = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-tail", net_qty=Decimal("150"), avg_price=Decimal("0.050")),
        budget=Decimal("50.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("100"),
    )
    assert quotes_overfilled == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.059"), qty=Decimal("150"))
    ]


def test_quoter_non_paired_uses_quote_size(base_market):
    win = IncentiveWindow(
        slug="mkt-global",
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    bids = (Level(Decimal("0.500"), Decimal("500")),)
    asks = (Level(Decimal("0.510"), Decimal("500")),)
    book = Book(slug="mkt-global", bids=bids, asks=asks, ts_ms=1000)

    quotes_custom = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-global", net_qty=Decimal("0"), avg_price=Decimal("0")),
        budget=Decimal("100.00"),
        is_global=True,
        paired=False,
        quote_size=Decimal("150"),
    )
    assert len(quotes_custom) == 1
    assert quotes_custom[0].qty == Decimal("150")

    quotes_default = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-global", net_qty=Decimal("0"), avg_price=Decimal("0")),
        budget=Decimal("100.00"),
        is_global=True,
        paired=False,
    )
    assert len(quotes_default) == 1
    assert quotes_default[0].qty == Decimal("100")


def test_paired_partial_fill_quotes_remaining_when_eligible(base_market):
    win = IncentiveWindow(
        slug="mkt-pair",
        program_id="p-pair",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("40"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    bids = (Level(Decimal("0.480"), Decimal("500")),)
    asks = (Level(Decimal("0.520"), Decimal("500")),)
    book = Book(slug="mkt-pair", bids=bids, asks=asks, ts_ms=1000)

    market_sub_min = Market(
        slug=base_market.slug,
        event_id=base_market.event_id,
        category=base_market.category,
        tick=base_market.tick,
        min_qty=Decimal("50"),
        active=base_market.active,
        mutually_exclusive=base_market.mutually_exclusive,
    )
    quotes_sub_min = desired_quotes(
        market=market_sub_min,
        book=book,
        window=win,
        position=Position(slug="mkt-pair", net_qty=Decimal("30"), avg_price=Decimal("0.480")),
        budget=Decimal("100.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("100"),
    )
    assert quotes_sub_min == [
        Quote(slug=base_market.slug, side="BUY_LONG", price=Decimal("0.480"), qty=Decimal("70"))
    ]

    quotes_partial = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-pair", net_qty=Decimal("30"), avg_price=Decimal("0.480")),
        budget=Decimal("100.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("100"),
    )
    assert quotes_partial == [
        Quote(slug=base_market.slug, side="BUY_LONG", price=Decimal("0.480"), qty=Decimal("70")),
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.519"), qty=Decimal("30")),
    ]

    win_65 = IncentiveWindow(
        slug="mkt-pair",
        program_id="p-pair",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("65"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    quotes_dust_5 = desired_quotes(
        market=base_market,
        book=book,
        window=win_65,
        position=Position(slug="mkt-pair", net_qty=Decimal("5"), avg_price=Decimal("0.400")),
        budget=Decimal("100.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("65"),
    )
    assert quotes_dust_5 == [
        Quote(slug=base_market.slug, side="BUY_LONG", price=Decimal("0.480"), qty=Decimal("65")),
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.519"), qty=Decimal("5")),
    ]

    quotes_insufficient = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-pair", net_qty=Decimal("70"), avg_price=Decimal("0.480")),
        budget=Decimal("100.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("100"),
    )
    assert quotes_insufficient == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.519"), qty=Decimal("70"))
    ]

    quotes_exact = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-pair", net_qty=Decimal("100"), avg_price=Decimal("0.480")),
        budget=Decimal("100.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("100"),
    )
    assert quotes_exact == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.519"), qty=Decimal("100"))
    ]

    quotes_over = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-pair", net_qty=Decimal("120"), avg_price=Decimal("0.480")),
        budget=Decimal("100.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("100"),
    )
    assert quotes_over == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.519"), qty=Decimal("120"))
    ]


def test_quoter_non_paired_steps_down_qty_on_upward_price_drift_within_budget(base_market):
    win = IncentiveWindow(
        slug="mkt-global",
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("80"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    bids = (Level(Decimal("0.201"), Decimal("500")),)
    asks = (Level(Decimal("0.210"), Decimal("500")),)
    book = Book(slug="mkt-global", bids=bids, asks=asks, ts_ms=1000)

    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-global", net_qty=Decimal("0"), avg_price=Decimal("0")),
        budget=Decimal("55.00"),
        is_global=True,
        paired=False,
        quote_size=Decimal("275"),
    )
    assert len(quotes) == 1
    assert quotes[0].qty == Decimal("273")
    assert quotes[0].qty * quotes[0].price <= Decimal("55.00")


def test_global_non_paired_inventory_unwinds_at_breakeven_floor_and_on_wide_spread(base_market):
    win = IncentiveWindow(
        slug="mkt-global",
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("80"),
        max_spread=Decimal("0.035"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book_below_entry = Book(
        slug="mkt-global",
        bids=(Level(Decimal("0.149"), Decimal("100")),),
        asks=(Level(Decimal("0.168"), Decimal("100")),),
        ts_ms=1000,
    )
    quotes_breakeven = desired_quotes(
        market=base_market,
        book=book_below_entry,
        window=win,
        position=Position(slug="mkt-global", net_qty=Decimal("320"), avg_price=Decimal("0.171")),
        budget=Decimal("55.00"),
        is_global=True,
        paired=False,
        quote_size=Decimal("385"),
    )
    assert quotes_breakeven == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.171"), qty=Decimal("320"))
    ]

    book_wide = Book(
        slug="mkt-global",
        bids=(Level(Decimal("0.201"), Decimal("100")),),
        asks=(Level(Decimal("0.420"), Decimal("100")),),
        ts_ms=1000,
    )
    quotes_wide = desired_quotes(
        market=base_market,
        book=book_wide,
        window=win,
        position=Position(slug="mkt-global", net_qty=Decimal("200"), avg_price=Decimal("0.261")),
        budget=Decimal("55.00"),
        is_global=True,
        paired=False,
        quote_size=Decimal("230"),
    )
    assert quotes_wide == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.261"), qty=Decimal("200"))
    ]


def test_global_adverse_selection_freeze_blocks_buy_long_paired(base_market):
    win = IncentiveWindow(
        slug="mkt-global",
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug="mkt-global",
        bids=(Level(Decimal("0.180"), Decimal("500")),),
        asks=(Level(Decimal("0.190"), Decimal("500")),),
        ts_ms=1000,
    )
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-global", net_qty=Decimal("100"), avg_price=Decimal("0.200")),
        budget=Decimal("55.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("200"),
    )
    assert not any(q.side == "BUY_LONG" for q in quotes)


def test_global_adverse_selection_freeze_single_sided_preserves_unwind(base_market):
    win = IncentiveWindow(
        slug="mkt-global",
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug="mkt-global",
        bids=(Level(Decimal("0.180"), Decimal("500")),),
        asks=(Level(Decimal("0.190"), Decimal("500")),),
        ts_ms=1000,
    )
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-global", net_qty=Decimal("100"), avg_price=Decimal("0.200")),
        budget=Decimal("55.00"),
        is_global=True,
        paired=False,
        quote_size=Decimal("200"),
    )
    assert not any(q.side == "BUY_LONG" for q in quotes)
    sell_q = next(q for q in quotes if q.side == "SELL_LONG")
    assert sell_q.qty == Decimal("100")
    assert sell_q.price >= Decimal("0.200")


def test_global_inventory_budget_cap_blocks_buy_long(base_market):
    win = IncentiveWindow(
        slug="mkt-global",
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug="mkt-global",
        bids=(Level(Decimal("0.210"), Decimal("500")),),
        asks=(Level(Decimal("0.220"), Decimal("500")),),
        ts_ms=1000,
    )
    quotes_paired = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-global", net_qty=Decimal("275"), avg_price=Decimal("0.200")),
        budget=Decimal("55.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("350"),
    )
    assert not any(q.side == "BUY_LONG" for q in quotes_paired)

    quotes_single = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-global", net_qty=Decimal("275"), avg_price=Decimal("0.200")),
        budget=Decimal("55.00"),
        is_global=True,
        paired=False,
        quote_size=Decimal("350"),
    )
    assert not any(q.side == "BUY_LONG" for q in quotes_single)


def test_global_single_sided_caps_bid_qty_to_avail_budget(base_market):
    win = IncentiveWindow(
        slug="mkt-global",
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug="mkt-global",
        bids=(Level(Decimal("0.200"), Decimal("500")),),
        asks=(Level(Decimal("0.210"), Decimal("500")),),
        ts_ms=1000,
    )
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug="mkt-global", net_qty=Decimal("150"), avg_price=Decimal("0.200")),
        budget=Decimal("55.00"),
        is_global=True,
        paired=False,
        quote_size=Decimal("300"),
    )
    buy_q = next(q for q in quotes if q.side == "BUY_LONG")
    assert buy_q.price == Decimal("0.200")
    assert buy_q.qty == Decimal("125")
    assert buy_q.qty * buy_q.price <= Decimal("25.00")


def test_global_underwater_non_scoring_position_quotes_scoring_ask(base_market):
    win = IncentiveWindow(
        slug=base_market.slug,
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("60"),
        max_spread=Decimal("0.065"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.082"), Decimal("500")),),
        asks=(Level(Decimal("0.122"), Decimal("500")),),
        ts_ms=1000,
    )
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("535"), avg_price=Decimal("0.184")),
        budget=Decimal("0"),
        is_global=True,
        paired=False,
    )
    assert quotes == [Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.184"), qty=Decimal("535"))]


def test_global_underwater_non_scoring_caps_at_max_scoring_ask_when_spread_wide(base_market):
    win = IncentiveWindow(
        slug=base_market.slug,
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.035"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.080"), Decimal("100")),),
        asks=(Level(Decimal("0.160"), Decimal("100")),),
        ts_ms=1000,
    )
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("100"), avg_price=Decimal("0.220")),
        budget=Decimal("0"),
        is_global=True,
        paired=False,
    )
    sell_q = next(q for q in quotes if q.side == "SELL_LONG")
    assert sell_q.price == Decimal("0.220")
    assert sell_q.qty == Decimal("100")


def test_global_underwater_non_scoring_refuses_penny_chase_below_reward_band_floor(base_market):
    win = IncentiveWindow(
        slug=base_market.slug,
        program_id="p-global",
        pool_usd=Decimal("100.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("60"),
        max_spread=Decimal("0.065"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.082"), Decimal("5.01")), Level(Decimal("0.081"), Decimal("60"))),
        asks=(Level(Decimal("0.114"), Decimal("600")), Level(Decimal("0.123"), Decimal("393.25"))),
        ts_ms=1000,
    )
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("535"), avg_price=Decimal("0.184")),
        budget=Decimal("0"),
        is_global=True,
        paired=False,
    )
    assert quotes == [Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.184"), qty=Decimal("535"))]


def test_global_within_budget_position_holds_breakeven_instead_of_underwater_unwind(base_market):
    win = IncentiveWindow(
        slug=base_market.slug,
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.040"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.600"), Decimal("100")),),
        asks=(Level(Decimal("0.680"), Decimal("100")),),
        ts_ms=1000,
    )
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("55"), avg_price=Decimal("0.740")),
        budget=Decimal("55.00"),
        is_global=True,
        paired=False,
    )
    sell_q = next(q for q in quotes if q.side == "SELL_LONG")
    assert sell_q.price == Decimal("0.740")
    assert sell_q.qty == Decimal("55")


def test_global_underwater_non_scoring_enforces_loss_floor_when_mid_below_reward_band(base_market):
    win = IncentiveWindow(
        slug=base_market.slug,
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=Decimal("0.065"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.055"), Decimal("380")),),
        asks=(Level(Decimal("0.091"), Decimal("130")),),
        ts_ms=1000,
    )
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("535"), avg_price=Decimal("0.184")),
        budget=Decimal("0"),
        is_global=True,
        paired=False,
    )
    sell_q = next(q for q in quotes if q.side == "SELL_LONG")
    assert sell_q.price == Decimal("0.184")
    assert sell_q.qty == Decimal("535")

    book_crashed = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.020"), Decimal("380")),),
        asks=(Level(Decimal("0.034"), Decimal("130")),),
        ts_ms=1000,
    )
    quotes_crashed = desired_quotes(
        market=base_market,
        book=book_crashed,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("535"), avg_price=Decimal("0.184")),
        budget=Decimal("0"),
        is_global=True,
        paired=False,
    )
    sell_crashed = next(q for q in quotes_crashed if q.side == "SELL_LONG")
    assert sell_crashed.price == Decimal("0.184")
    assert sell_crashed.qty == Decimal("535")


def test_global_paired_leg_with_inventory_emits_sell_long_unwind(base_market):
    win = IncentiveWindow(
        slug=base_market.slug,
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    bids = (Level(Decimal("0.34"), Decimal("100")),)
    asks = (Level(Decimal("0.36"), Decimal("100")),)
    book = Book(slug=base_market.slug, bids=bids, asks=asks, ts_ms=1000)

    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("55"), avg_price=Decimal("0.35")),
        budget=Decimal("55.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("55"),
    )
    assert quotes == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.359"), qty=Decimal("55"))
    ]


def test_global_over_budget_position_with_positive_budget_never_panic_sells_below_entry(base_market):
    win = IncentiveWindow(
        slug=base_market.slug,
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.040"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.140"), Decimal("100")),),
        asks=(Level(Decimal("0.160"), Decimal("100")),),
        ts_ms=1000,
    )
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("352"), avg_price=Decimal("0.200")),
        budget=Decimal("55.00"),
        is_global=True,
        paired=False,
    )
    sell_q = next(q for q in quotes if q.side == "SELL_LONG")
    assert sell_q.price >= Decimal("0.200")
    assert sell_q.qty == Decimal("352")


def test_global_flatten_mode_with_zero_bid_budget_joins_qual_ask_even_within_max_spread(base_market):
    win = IncentiveWindow(
        slug=base_market.slug,
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.030"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.273"), Decimal("200")),),
        asks=(Level(Decimal("0.300"), Decimal("200")),),
        ts_ms=1000,
    )
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("150"), avg_price=Decimal("0.301")),
        budget=Decimal("55.00"),
        bid_budget=Decimal("0"),
        is_global=True,
        paired=False,
    )
    assert quotes == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.301"), qty=Decimal("150"))
    ]


def test_global_paired_leg_freezes_buy_long_when_holding_min_qty(base_market):
    market_min5 = Market(
        slug=base_market.slug,
        event_id=base_market.event_id,
        category=base_market.category,
        tick=base_market.tick,
        min_qty=Decimal("5"),
        active=base_market.active,
        mutually_exclusive=base_market.mutually_exclusive,
    )
    win = IncentiveWindow(
        slug=base_market.slug,
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.34"), Decimal("100")),),
        asks=(Level(Decimal("0.36"), Decimal("100")),),
        ts_ms=1000,
    )
    quotes_dust = desired_quotes(
        market=market_min5,
        book=book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("10"), avg_price=Decimal("0.35")),
        budget=Decimal("55.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("100"),
    )
    assert quotes_dust == [
        Quote(slug=base_market.slug, side="BUY_LONG", price=Decimal("0.340"), qty=Decimal("90")),
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.359"), qty=Decimal("10")),
    ]

    quotes_filled = desired_quotes(
        market=market_min5,
        book=book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("50"), avg_price=Decimal("0.35")),
        budget=Decimal("55.00"),
        is_global=True,
        paired=True,
        quote_size=Decimal("100"),
    )
    assert not any(q.side == "BUY_LONG" for q in quotes_filled)
    assert quotes_filled == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.359"), qty=Decimal("50"))
    ]


def test_global_dust_and_profitable_inventory_steps_inside_best_ask(base_market):
    win = IncentiveWindow(
        slug=base_market.slug,
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.480"), Decimal("100")),),
        asks=(Level(Decimal("0.502"), Decimal("100")),),
        ts_ms=1000,
    )
    quotes_dust = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("20"), avg_price=Decimal("0.500")),
        budget=Decimal("50.00"),
        is_global=True,
    )
    assert quotes_dust == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.501"), qty=Decimal("20"))
    ]

    book_profit = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.480"), Decimal("100")),),
        asks=(Level(Decimal("0.494"), Decimal("100")),),
        ts_ms=1000,
    )
    quotes_profit = desired_quotes(
        market=base_market,
        book=book_profit,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("50"), avg_price=Decimal("0.490")),
        budget=Decimal("50.00"),
        is_global=True,
    )
    assert quotes_profit == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.493"), qty=Decimal("50"))
    ]

    quotes_no_step = desired_quotes(
        market=base_market,
        book=book_profit,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("50"), avg_price=Decimal("0.493")),
        budget=Decimal("50.00"),
        is_global=True,
    )
    assert quotes_no_step == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.494"), qty=Decimal("50"))
    ]


def test_global_sub_tick_avg_price_min_exit_rounds_up(base_market):
    win = IncentiveWindow(
        slug=base_market.slug,
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.450"), Decimal("100")),),
        asks=(Level(Decimal("0.455"), Decimal("100")),),
        ts_ms=1000,
    )
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("50"), avg_price=Decimal("0.4523")),
        budget=Decimal("50.00"),
        is_global=True,
    )
    assert quotes == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.455"), qty=Decimal("50"))
    ]


def test_global_live_unwind_non_scoring_steps_sell_long_into_scoring_band(base_market):
    win = IncentiveWindow(
        slug=base_market.slug,
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.045"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.088"), Decimal("100")),),
        asks=(Level(Decimal("0.113"), Decimal("100")),),
        ts_ms=1000,
    )
    quotes = desired_quotes(
        market=base_market,
        book=book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("325"), avg_price=Decimal("0.1654")),
        budget=Decimal("65.00"),
        bid_budget=Decimal("0"),
        is_global=True,
        paired=False,
    )
    assert quotes == [
        Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.166"), qty=Decimal("325"))
    ]

    low_mid_book = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.074"), Decimal("100")),),
        asks=(Level(Decimal("0.114"), Decimal("100")),),
        ts_ms=1000,
    )
    assert desired_quotes(
        market=base_market,
        book=low_mid_book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("105"), avg_price=Decimal("0.1654")),
        budget=Decimal("65.00"),
        bid_budget=Decimal("0"),
        is_global=True,
        paired=False,
    ) == [Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.166"), qty=Decimal("105"))]

    zero_pool_win = IncentiveWindow(
        slug=base_market.slug,
        program_id="unwind",
        pool_usd=Decimal("0"),
        discount=Decimal("0"),
        target_size=Decimal("5"),
        max_spread=Decimal("0.04"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    assert desired_quotes(
        market=base_market,
        book=book,
        window=zero_pool_win,
        position=Position(slug=base_market.slug, net_qty=Decimal("105"), avg_price=Decimal("0.1654")),
        budget=Decimal("65.00"),
        bid_budget=Decimal("0"),
        is_global=True,
        paired=False,
    ) == [Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.166"), qty=Decimal("105"))]

    deep_underwater_book = Book(
        slug=base_market.slug,
        bids=(Level(Decimal("0.271"), Decimal("100")),),
        asks=(Level(Decimal("0.307"), Decimal("100")),),
        ts_ms=1000,
    )
    assert desired_quotes(
        market=base_market,
        book=deep_underwater_book,
        window=win,
        position=Position(slug=base_market.slug, net_qty=Decimal("5"), avg_price=Decimal("0.3986")),
        budget=Decimal("65.00"),
        bid_budget=Decimal("0"),
        is_global=True,
        paired=False,
    ) == [Quote(slug=base_market.slug, side="SELL_LONG", price=Decimal("0.399"), qty=Decimal("5"))]


def test_global_empty_bid_book_emits_sell_long_unwind():
    market = Market(
        slug="mkt-empty-bid",
        event_id="ev-1",
        category="sports",
        tick=Decimal("0.01"),
        min_qty=Decimal("5"),
        active=True,
    )
    win = IncentiveWindow(
        slug="mkt-empty-bid",
        program_id="unwind",
        pool_usd=Decimal("0"),
        discount=Decimal("0"),
        target_size=Decimal("5"),
        max_spread=Decimal("0.04"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    book = Book(
        slug="mkt-empty-bid",
        bids=(),
        asks=(Level(Decimal("0.12"), Decimal("100")),),
        ts_ms=1000,
    )
    quotes = desired_quotes(
        market=market,
        book=book,
        window=win,
        position=Position(slug="mkt-empty-bid", net_qty=Decimal("65"), avg_price=Decimal("0.2104")),
        budget=Decimal("0"),
        is_global=True,
    )
    assert quotes == [
        Quote(slug="mkt-empty-bid", side="SELL_LONG", price=Decimal("0.22"), qty=Decimal("65"))
    ]

    empty_book = Book(slug="mkt-empty-bid", bids=(), asks=(), ts_ms=1000)
    quotes_no_asks = desired_quotes(
        market=market,
        book=empty_book,
        window=win,
        position=Position(slug="mkt-empty-bid", net_qty=Decimal("65"), avg_price=Decimal("0.22")),
        budget=Decimal("0"),
        is_global=True,
    )
    assert quotes_no_asks == [
        Quote(slug="mkt-empty-bid", side="SELL_LONG", price=Decimal("0.22"), qty=Decimal("65"))
    ]


def test_desired_quotes_pre_guard_max_bid_price_and_other_book():
    market = Market(
        slug="yes-1",
        event_id="ev-1",
        category="sports",
        tick=Decimal("0.01"),
        min_qty=Decimal("5"),
        active=True,
    )
    win = IncentiveWindow(
        slug="yes-1",
        program_id="prog-1",
        pool_usd=Decimal("100"),
        discount=Decimal("0.30"),
        target_size=Decimal("20"),
        max_spread=Decimal("0.04"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )
    yes_book = Book(
        slug="yes-1",
        bids=(Level(Decimal("0.51"), Decimal("500")),),
        asks=(Level(Decimal("0.53"), Decimal("500")),),
        ts_ms=1000,
    )
    no_book = Book(
        slug="no-1",
        bids=(Level(Decimal("0.49"), Decimal("500")),),
        asks=(Level(Decimal("0.51"), Decimal("500")),),
        ts_ms=1000,
    )
    quotes_unclamped = desired_quotes(
        market=market,
        book=yes_book,
        window=win,
        position=Position(slug="yes-1", net_qty=Decimal("0"), avg_price=Decimal("0")),
        budget=Decimal("50"),
        bid_budget=Decimal("50"),
        is_global=True,
        paired=True,
        quote_size=Decimal("50"),
        other_book=no_book,
    )
    assert quotes_unclamped == []

    quotes_clamped = desired_quotes(
        market=market,
        book=yes_book,
        window=win,
        position=Position(slug="yes-1", net_qty=Decimal("0"), avg_price=Decimal("0")),
        budget=Decimal("50"),
        bid_budget=Decimal("50"),
        is_global=True,
        paired=True,
        quote_size=Decimal("50"),
        other_book=no_book,
        max_bid_price=Decimal("0.50"),
    )
    assert len(quotes_clamped) == 1
    assert quotes_clamped[0].side == "BUY_LONG"
    assert quotes_clamped[0].price == Decimal("0.50")
    assert quotes_clamped[0].qty == Decimal("50")


