from decimal import Decimal
import pytest
from dataclasses import FrozenInstanceError

from pmus.models import Level, Market, Book, Quote, LiveOrder, Position, IncentiveWindow

def test_level_frozen():
    lvl = Level(price=Decimal("0.50"), qty=Decimal("100"))
    assert lvl.price == Decimal("0.50")
    assert lvl.qty == Decimal("100")
    with pytest.raises(FrozenInstanceError):
        lvl.price = Decimal("0.51")

def test_market_frozen():
    m = Market(
        slug="test-slug",
        event_id="ev-1",
        category="sports",
        tick=Decimal("0.001"),
        min_qty=Decimal("1"),
        active=True,
        mutually_exclusive=False,
    )
    assert m.slug == "test-slug"
    with pytest.raises(FrozenInstanceError):
        m.slug = "other"

def test_book_sorting_invariant():
    bids = (Level(Decimal("0.50"), Decimal("10")), Level(Decimal("0.49"), Decimal("10")))
    asks = (Level(Decimal("0.51"), Decimal("10")), Level(Decimal("0.52"), Decimal("10")))
    book = Book(slug="test-slug", bids=bids, asks=asks, ts_ms=1000)
    assert book.bids[0].price > book.bids[1].price
    assert book.asks[0].price < book.asks[1].price


def test_settings_pair_preference(monkeypatch):
    from pmus.config import Settings

    s_default = Settings()
    assert s_default.pair_preference == Decimal("1.0")

    monkeypatch.setenv("PMUS_PAIR_PREFERENCE", "0.85")
    s_env = Settings.from_env()
    assert s_env.pair_preference == Decimal("0.85")

