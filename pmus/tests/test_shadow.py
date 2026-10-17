from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from pmus.models import Book, IncentiveWindow, Level, LiveOrder
from pmus.shadow import check_paper_fills, estimate_global_pair_score, estimate_lip_score

@pytest.fixture
def window():
    now = datetime.now(timezone.utc)
    return IncentiveWindow(
        slug="mkt-1",
        program_id="prog-1",
        pool_usd=Decimal("20.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )

def test_estimate_lip_score_two_hour_window(window):
    book = Book(
        slug="mkt-1",
        bids=(Level(Decimal("0.490"), Decimal("50")),),
        asks=(Level(Decimal("0.510"), Decimal("50")),),
        ts_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
    )
    resting = [
        LiveOrder("o-1", "mkt-1", "BUY_LONG", Decimal("0.490"), Decimal("60")),
        LiveOrder("o-2", "mkt-1", "SELL_SHORT", Decimal("0.510"), Decimal("60")),
    ]
    snap = estimate_lip_score(book, window, resting, Decimal("0.001"))
    assert snap.qualifies is True
    assert snap.trader_bid_score > Decimal("0")
    assert snap.trader_ask_score > Decimal("0")
    assert snap.reward_usd > Decimal("0")

    two_hours_s = Decimal("7200")
    expected_pool_per_s = Decimal("20.00") / two_hours_s
    assert snap.reward_usd <= expected_pool_per_s

def test_estimate_lip_score_outside_window():
    now = datetime.now(timezone.utc)
    past_window = IncentiveWindow(
        slug="mkt-1",
        program_id="prog-1",
        pool_usd=Decimal("20.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=5),
        end=now - timedelta(hours=3),
        status="active",
    )
    book = Book(
        slug="mkt-1",
        bids=(Level(Decimal("0.490"), Decimal("200")),),
        asks=(Level(Decimal("0.510"), Decimal("200")),),
        ts_ms=int(now.timestamp() * 1000),
    )
    resting = [LiveOrder("o-1", "mkt-1", "BUY_LONG", Decimal("0.490"), Decimal("10"))]
    snap = estimate_lip_score(book, past_window, resting, Decimal("0.001"))
    assert snap.qualifies is False
    assert snap.reward_usd == Decimal("0")

def test_check_paper_fills_buy_short_and_sell_short():
    book = Book(
        slug="mkt-1",
        bids=(Level(Decimal("0.520"), Decimal("100")),),
        asks=(Level(Decimal("0.480"), Decimal("100")),),
        ts_ms=1000,
    )
    orders = [
        LiveOrder("o-cover", "mkt-1", "BUY_SHORT", Decimal("0.490"), Decimal("10")),
        LiveOrder("o-short", "mkt-1", "SELL_SHORT", Decimal("0.510"), Decimal("10")),
    ]
    fills, remaining = check_paper_fills(book, orders)
    assert len(fills) == 2
    assert len(remaining) == 0
    assert fills[0].side == "BUY_SHORT"
    assert fills[1].side == "SELL_SHORT"

def test_lip_score_side_below_target_pays_zero():
    now = datetime.now(timezone.utc)
    win = IncentiveWindow(
        slug="mkt-below",
        program_id="prog-1",
        pool_usd=Decimal("100.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("5000"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    book = Book(
        slug="mkt-below",
        bids=(Level(Decimal("0.010"), Decimal("200")),),
        asks=(Level(Decimal("0.020"), Decimal("5000")),),
        ts_ms=int(now.timestamp() * 1000),
    )
    resting = [
        LiveOrder("o-1", "mkt-below", "BUY_LONG", Decimal("0.010"), Decimal("100")),
    ]
    snap = estimate_lip_score(book, win, resting, Decimal("0.001"))
    assert snap.bid_qualifies is False
    assert snap.trader_bid_score == Decimal("0")
    assert snap.ask_qualifies is True

def test_lip_score_completing_target_makes_side_pay():
    now = datetime.now(timezone.utc)
    win = IncentiveWindow(
        slug="mkt-complete",
        program_id="prog-1",
        pool_usd=Decimal("7200.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("1000"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    book = Book(
        slug="mkt-complete",
        bids=(Level(Decimal("0.490"), Decimal("200")),),
        asks=(),
        ts_ms=int(now.timestamp() * 1000),
    )
    resting = [
        LiveOrder("o-1", "mkt-complete", "BUY_LONG", Decimal("0.490"), Decimal("800")),
    ]
    snap = estimate_lip_score(book, win, resting, Decimal("0.001"))
    assert snap.bid_qualifies is True
    assert snap.trader_bid_score == Decimal("800")
    assert snap.total_bid_score == Decimal("1000")
    assert snap.reward_usd == Decimal("0.40")

def test_lip_score_orders_beyond_target_score_zero():
    now = datetime.now(timezone.utc)
    win = IncentiveWindow(
        slug="mkt-beyond",
        program_id="prog-1",
        pool_usd=Decimal("100.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("500"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    book = Book(
        slug="mkt-beyond",
        bids=(Level(Decimal("0.490"), Decimal("500")),),
        asks=(),
        ts_ms=int(now.timestamp() * 1000),
    )
    resting = [
        LiveOrder("o-1", "mkt-beyond", "BUY_LONG", Decimal("0.490"), Decimal("200")),
    ]
    snap = estimate_lip_score(book, win, resting, Decimal("0.001"))
    assert snap.bid_qualifies is True
    assert snap.trader_bid_score == Decimal("0")
    assert snap.total_bid_score == Decimal("500")

def test_lip_score_max_spread_forfeits_both_sides():
    now = datetime.now(timezone.utc)
    win = IncentiveWindow(
        slug="mkt-spread",
        program_id="prog-1",
        pool_usd=Decimal("100.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=Decimal("0.02"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    book = Book(
        slug="mkt-spread",
        bids=(Level(Decimal("0.400"), Decimal("100")),),
        asks=(Level(Decimal("0.500"), Decimal("100")),),
        ts_ms=int(now.timestamp() * 1000),
    )
    resting = [
        LiveOrder("o-1", "mkt-spread", "BUY_LONG", Decimal("0.400"), Decimal("50")),
        LiveOrder("o-2", "mkt-spread", "SELL_SHORT", Decimal("0.500"), Decimal("50")),
    ]
    snap = estimate_lip_score(book, win, resting, Decimal("0.001"))
    assert snap.qualifies is False
    assert snap.bid_qualifies is False
    assert snap.ask_qualifies is False
    assert snap.reward_usd == Decimal("0")

def test_lip_score_docs_30_discount_example():
    now = datetime.now(timezone.utc)
    win = IncentiveWindow(
        slug="mkt-docs",
        program_id="prog-1",
        pool_usd=Decimal("7200.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("4000"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    book_for_a = Book(
        slug="mkt-docs",
        bids=(
            Level(Decimal("0.489"), Decimal("1000")),
            Level(Decimal("0.488"), Decimal("1000")),
            Level(Decimal("0.487"), Decimal("1000")),
        ),
        asks=(),
        ts_ms=int(now.timestamp() * 1000),
    )
    order_a = [LiveOrder("o-a", "mkt-docs", "BUY_LONG", Decimal("0.490"), Decimal("1000"))]
    snap_a = estimate_lip_score(book_for_a, win, order_a, Decimal("0.001"))
    assert snap_a.total_bid_score == Decimal("1417")
    assert snap_a.trader_bid_score == Decimal("1000")
    share_a = snap_a.trader_bid_score / snap_a.total_bid_score
    assert round(share_a, 3) == Decimal("0.706")

    book_for_d = Book(
        slug="mkt-docs",
        bids=(
            Level(Decimal("0.490"), Decimal("1000")),
            Level(Decimal("0.489"), Decimal("1000")),
            Level(Decimal("0.488"), Decimal("1000")),
        ),
        asks=(),
        ts_ms=int(now.timestamp() * 1000),
    )
    order_d = [LiveOrder("o-d", "mkt-docs", "BUY_LONG", Decimal("0.487"), Decimal("1000"))]
    snap_d = estimate_lip_score(book_for_d, win, order_d, Decimal("0.001"))
    assert snap_d.total_bid_score == Decimal("1417")
    assert snap_d.trader_bid_score == Decimal("27")
    share_d = snap_d.trader_bid_score / snap_d.total_bid_score
    assert round(share_d, 3) == Decimal("0.019")


def test_estimate_global_pair_score_strips_own_resting_orders_from_competitors():
    now = datetime.now(timezone.utc)
    win = IncentiveWindow(
        slug="mkt-pair",
        program_id="prog-1",
        pool_usd=Decimal("86400.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    yes_book = Book(
        slug="mkt-pair-yes",
        bids=(Level(Decimal("0.48"), Decimal("100")),),
        asks=(Level(Decimal("0.52"), Decimal("100")),),
        ts_ms=1000,
    )
    no_book = Book(
        slug="mkt-pair-no",
        bids=(Level(Decimal("0.48"), Decimal("100")),),
        asks=(Level(Decimal("0.52"), Decimal("100")),),
        ts_ms=1000,
    )
    resting = [
        LiveOrder("o-y", "mkt-pair-yes", "BUY_LONG", Decimal("0.48"), Decimal("50")),
        LiveOrder("o-n", "mkt-pair-no", "BUY_LONG", Decimal("0.48"), Decimal("50")),
    ]
    snap_y, snap_n = estimate_global_pair_score(yes_book, no_book, win, resting, ts_ms=1000)
    total_reward = snap_y.reward_usd + snap_n.reward_usd
    assert total_reward == Decimal("0.50")
    assert snap_y.reward_usd == Decimal("0.25")
    assert snap_n.reward_usd == Decimal("0.25")
    assert snap_y.total_bid_score == snap_y.trader_bid_score * Decimal("2")
    assert snap_n.total_bid_score == snap_n.trader_bid_score * Decimal("2")

