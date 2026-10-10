from datetime import datetime, timezone
import time
from decimal import Decimal
import pytest

from pmus.ledger import Ledger
from pmus.models import LipSnapshot, LiveOrder, Position, Quote, ShadowFill

@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "test_ledger.db")

def test_ledger_orders_lifecycle(ledger):
    order = LiveOrder(
        order_id="ord-1",
        slug="test-slug",
        side="BUY_LONG",
        price=Decimal("0.450"),
        qty_open=Decimal("10"),
    )
    ledger.record_order(order, status="OPEN")
    open_orders = ledger.get_open_orders("test-slug")
    assert len(open_orders) == 1
    assert open_orders[0].order_id == "ord-1"

    ledger.close_order("ord-1", status="CANCELED")
    assert len(ledger.get_open_orders("test-slug")) == 0

def test_ledger_fills_and_positions(ledger):
    fill = ShadowFill(
        fill_id="f-1",
        order_id="ord-1",
        slug="test-slug",
        side="BUY_LONG",
        price=Decimal("0.450"),
        qty=Decimal("10"),
        fee=Decimal("-0.03"),
        is_maker=True,
        ts_ms=1000,
    )
    ledger.record_fill(fill)
    fills = ledger.get_fills_since(0)
    assert len(fills) == 1
    assert fills[0].fill_id == "f-1"

    pos = Position(slug="test-slug", net_qty=Decimal("10"), avg_price=Decimal("0.450"))
    ledger.update_position(pos)
    retrieved = ledger.get_position("test-slug")
    assert retrieved.net_qty == Decimal("10")
    assert retrieved.avg_price == Decimal("0.450")

def test_ledger_lip_snapshots(ledger):
    snap = LipSnapshot(
        ts_ms=1000,
        slug="test-slug",
        program_id="prog-1",
        trader_bid_score=Decimal("10"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("5"),
        total_ask_score=Decimal("50"),
        qualifies=True,
        reward_usd=Decimal("0.005"),
    )
    ledger.record_lip_snapshot(snap)
    snaps = ledger.get_lip_snapshots_since(0)
    assert len(snaps) == 1
    assert snaps[0].qualifies is True
    assert snaps[0].reward_usd == Decimal("0.005")

def test_ledger_realized_loss_today(ledger):
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    # Buy 10 @ 0.50, Sell 10 @ 0.40 -> loss of $1.00
    fill_buy = ShadowFill("f-b", "o-b", "mkt-1", "BUY_LONG", Decimal("0.50"), Decimal("10"), Decimal("0"), True, now_ms)
    fill_sell = ShadowFill("f-s", "o-s", "mkt-1", "SELL_LONG", Decimal("0.40"), Decimal("10"), Decimal("0"), True, now_ms + 100)
    ledger.record_fill(fill_buy)
    ledger.record_fill(fill_sell)

    loss = ledger.get_realized_loss_today(now)
    assert loss == Decimal("1.00")

