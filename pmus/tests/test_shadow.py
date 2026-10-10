from datetime import datetime, timezone
from decimal import Decimal
import pytest

from pmus.models import Book, IncentiveWindow, Level, LiveOrder
from pmus.shadow import check_paper_fills, estimate_lip_score

@pytest.fixture
def window():
    return IncentiveWindow(
        slug="mkt-1",
        program_id="prog-1",
        pool_usd=Decimal("20.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=Decimal("0.05"),
        start=datetime.now(timezone.utc),
        end=None,
        status="active",
    )

def test_estimate_lip_score_qualifies(window):
    book = Book(
        slug="mkt-1",
        bids=(Level(Decimal("0.490"), Decimal("200")),),
        asks=(Level(Decimal("0.510"), Decimal("200")),),
        ts_ms=1000,
    )
    resting = [
        LiveOrder("o-1", "mkt-1", "BUY_LONG", Decimal("0.490"), Decimal("10")),
        LiveOrder("o-2", "mkt-1", "SELL_LONG", Decimal("0.510"), Decimal("10")),
    ]
    snap = estimate_lip_score(book, window, resting, Decimal("0.001"))
    assert snap.qualifies is True
    assert snap.trader_bid_score > Decimal("0")
    assert snap.trader_ask_score > Decimal("0")
    assert snap.reward_usd > Decimal("0")

def test_estimate_lip_score_spread_breached(window):
    book = Book(
        slug="mkt-1",
        bids=(Level(Decimal("0.400"), Decimal("200")),),
        asks=(Level(Decimal("0.600"), Decimal("200")),),
        ts_ms=1000,
    )
    resting = [LiveOrder("o-1", "mkt-1", "BUY_LONG", Decimal("0.400"), Decimal("10"))]
    snap = estimate_lip_score(book, window, resting, Decimal("0.001"))
    assert snap.qualifies is False
    assert snap.reward_usd == Decimal("0")

def test_check_paper_fills_buy_and_sell():
    book = Book(
        slug="mkt-1",
        bids=(Level(Decimal("0.520"), Decimal("100")),),
        asks=(Level(Decimal("0.480"), Decimal("100")),),
        ts_ms=1000,
    )
    orders = [
        LiveOrder("o-buy", "mkt-1", "BUY_LONG", Decimal("0.490"), Decimal("10")),
        LiveOrder("o-sell", "mkt-1", "SELL_LONG", Decimal("0.510"), Decimal("10")),
        LiveOrder("o-open", "mkt-1", "BUY_LONG", Decimal("0.450"), Decimal("10")),
    ]
    fills, remaining = check_paper_fills(book, orders)
    assert len(fills) == 2
    assert len(remaining) == 1
    assert remaining[0].order_id == "o-open"
    assert {f.order_id for f in fills} == {"o-buy", "o-sell"}
    for f in fills:
        assert f.fee < 0
        assert f.is_maker is True
