from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from pmus.models import Book, IncentiveWindow, Level, LiveOrder
from pmus.shadow import check_paper_fills, estimate_lip_score

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
        bids=(Level(Decimal("0.490"), Decimal("200")),),
        asks=(Level(Decimal("0.510"), Decimal("200")),),
        ts_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
    )
    resting = [
        LiveOrder("o-1", "mkt-1", "BUY_LONG", Decimal("0.490"), Decimal("10")),
        LiveOrder("o-2", "mkt-1", "SELL_SHORT", Decimal("0.510"), Decimal("10")),
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
