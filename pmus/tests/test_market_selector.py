import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import pytest

from pmus.models import Book, IncentiveWindow, Level
from pmus.strategy.market_selector import rank_markets
from pmus.venue import PublicGatewayClient

FIXTURES_DIR = Path(__file__).parent / "fixtures"

def test_rank_markets_prioritizes_higher_pool_and_lower_competition():
    now = datetime.now(timezone.utc)
    w1 = IncentiveWindow(
        slug="mkt-high-reward",
        program_id="p1",
        pool_usd=Decimal("100.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    w2 = IncentiveWindow(
        slug="mkt-low-reward",
        program_id="p2",
        pool_usd=Decimal("5.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    w_inactive = IncentiveWindow(
        slug="mkt-inactive",
        program_id="p3",
        pool_usd=Decimal("500.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="completed",
    )

    book = Book(
        slug="any",
        bids=(Level(Decimal("0.490"), Decimal("100")),),
        asks=(Level(Decimal("0.510"), Decimal("100")),),
        ts_ms=1000,
    )
    books = {
        "mkt-high-reward": Book("mkt-high-reward", book.bids, book.asks, 1000),
        "mkt-low-reward": Book("mkt-low-reward", book.bids, book.asks, 1000),
        "mkt-inactive": Book("mkt-inactive", book.bids, book.asks, 1000),
    }

    ranked = rank_markets([w1, w2, w_inactive], books, now=now, max_n=2)
    assert ranked == ["mkt-high-reward", "mkt-low-reward"]
    assert "mkt-inactive" not in ranked

def test_rank_markets_with_fixtures():
    with open(FIXTURES_DIR / "incentives.json", "r", encoding="utf-8") as f:
        inc_data = json.load(f)
    windows = PublicGatewayClient.parse_incentives(inc_data)

    target_slug = windows[0].slug
    fake_book = Book(
        slug=target_slug,
        bids=(Level(Decimal("0.490"), Decimal("100")),),
        asks=(Level(Decimal("0.510"), Decimal("100")),),
        ts_ms=1000,
    )
    books = {target_slug: fake_book}

    ranked = rank_markets(windows, books, now=datetime.now(timezone.utc), max_n=3)
    assert target_slug in ranked

def test_market_selector_diversifies_across_events():
    now = datetime.now(timezone.utc)
    w1 = IncentiveWindow(
        slug="euroleague-real-madrid-vs-barcelona-spread",
        program_id="p1",
        pool_usd=Decimal("100.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    w2 = IncentiveWindow(
        slug="euroleague-real-madrid-vs-barcelona-total",
        program_id="p2",
        pool_usd=Decimal("90.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    w3 = IncentiveWindow(
        slug="euroleague-monaco-vs-panathinaikos-spread",
        program_id="p3",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    book = Book(
        slug="any",
        bids=(Level(Decimal("0.490"), Decimal("100")),),
        asks=(Level(Decimal("0.510"), Decimal("100")),),
        ts_ms=1000,
    )
    books = {
        w1.slug: Book(w1.slug, book.bids, book.asks, 1000),
        w2.slug: Book(w2.slug, book.bids, book.asks, 1000),
        w3.slug: Book(w3.slug, book.bids, book.asks, 1000),
    }

    ranked = rank_markets([w1, w2, w3], books, now=now, max_n=2)
    assert len(ranked) == 2
    assert ranked == [w1.slug, w3.slug]

