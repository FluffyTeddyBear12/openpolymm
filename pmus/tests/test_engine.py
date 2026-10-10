from decimal import Decimal
import pytest

from pmus.engine import reconcile_quotes, update_position_from_fill
from pmus.models import LiveOrder, Position, Quote

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
