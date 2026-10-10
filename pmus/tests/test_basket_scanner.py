from decimal import Decimal
import pytest

from pmus.models import Book, Level
from pmus.strategy.basket_scanner import scan

def test_basket_scanner_short_all_profitable():
    event = {
        "id": "ev-multi",
        "markets": [
            {"slug": "leg-1", "mutually_exclusive": True},
            {"slug": "leg-2", "mutually_exclusive": True},
        ],
    }
    books = {
        "leg-1": Book(
            slug="leg-1",
            bids=(Level(Decimal("0.600"), Decimal("100")),),
            asks=(Level(Decimal("0.610"), Decimal("100")),),
            ts_ms=1000,
        ),
        "leg-2": Book(
            slug="leg-2",
            bids=(Level(Decimal("0.550"), Decimal("100")),),
            asks=(Level(Decimal("0.560"), Decimal("100")),),
            ts_ms=1000,
        ),
    }

    opps = scan([event], books, min_edge=Decimal("0.05"))
    assert len(opps) == 1
    opp = opps[0]
    assert opp.direction == "SHORT_ALL"
    assert opp.gross_edge == Decimal("0.150")
    assert opp.net_edge > Decimal("0.05")

def test_basket_scanner_buy_all_profitable():
    event = {
        "id": "ev-multi",
        "markets": [
            {"slug": "leg-1", "mutually_exclusive": True},
            {"slug": "leg-2", "mutually_exclusive": True},
        ],
    }
    books = {
        "leg-1": Book(
            slug="leg-1",
            bids=(Level(Decimal("0.350"), Decimal("100")),),
            asks=(Level(Decimal("0.360"), Decimal("100")),),
            ts_ms=1000,
        ),
        "leg-2": Book(
            slug="leg-2",
            bids=(Level(Decimal("0.450"), Decimal("100")),),
            asks=(Level(Decimal("0.460"), Decimal("100")),),
            ts_ms=1000,
        ),
    }

    opps = scan([event], books, min_edge=Decimal("0.05"))
    assert len(opps) == 1
    opp = opps[0]
    assert opp.direction == "BUY_ALL"
    assert opp.gross_edge == Decimal("0.180")
    assert opp.net_edge > Decimal("0.05")

def test_basket_scanner_edge_wiped_by_fees():
    event = {
        "id": "ev-multi",
        "markets": [
            {"slug": "leg-1", "mutually_exclusive": True},
            {"slug": "leg-2", "mutually_exclusive": True},
        ],
    }
    books = {
        "leg-1": Book(
            slug="leg-1",
            bids=(Level(Decimal("0.509"), Decimal("100")),),
            asks=(Level(Decimal("0.515"), Decimal("100")),),
            ts_ms=1000,
        ),
        "leg-2": Book(
            slug="leg-2",
            bids=(Level(Decimal("0.509"), Decimal("100")),),
            asks=(Level(Decimal("0.515"), Decimal("100")),),
            ts_ms=1000,
        ),
    }
    opps = scan([event], books, min_edge=Decimal("0.01"))
    assert len(opps) == 0

def test_basket_scanner_skips_single_market():
    event = {"id": "ev-single", "markets": [{"slug": "leg-1", "mutually_exclusive": True}]}
    books = {
        "leg-1": Book(
            slug="leg-1",
            bids=(Level(Decimal("0.99"), Decimal("100")),),
            asks=(Level(Decimal("0.99"), Decimal("100")),),
            ts_ms=1000,
        )
    }
    assert scan([event], books) == []

def test_basket_scanner_skips_non_mutually_exclusive_event():
    event = {
        "id": "ev-handicap",
        "markets": [
            {"slug": "leg-1", "mutually_exclusive": False},
            {"slug": "leg-2", "mutually_exclusive": False},
        ],
    }
    books = {
        "leg-1": Book("leg-1", bids=(Level(Decimal("0.600"), Decimal("100")),), asks=(Level(Decimal("0.610"), Decimal("100")),), ts_ms=1000),
        "leg-2": Book("leg-2", bids=(Level(Decimal("0.550"), Decimal("100")),), asks=(Level(Decimal("0.560"), Decimal("100")),), ts_ms=1000),
    }
    assert scan([event], books) == []
