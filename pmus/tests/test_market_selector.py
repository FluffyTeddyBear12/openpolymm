import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import pytest

from pmus.models import Book, IncentiveWindow, Level
from pmus.strategy.market_selector import greedy_allocate, rank_markets
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
        bids=(Level(Decimal("0.010"), Decimal("50")),),
        asks=(Level(Decimal("0.020"), Decimal("100")),),
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
        bids=(Level(Decimal("0.010"), Decimal("20")),),
        asks=(Level(Decimal("0.020"), Decimal("5000")),),
        ts_ms=1000,
    )
    books = {target_slug: fake_book}

    ranked = rank_markets(windows, books, now=datetime.now(timezone.utc), max_n=3, budget_per_market=Decimal("50.00"))
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
        bids=(Level(Decimal("0.010"), Decimal("50")),),
        asks=(Level(Decimal("0.020"), Decimal("100")),),
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

def test_greedy_allocator_60_budget_50_completion_beats_two_30_partials():
    now = datetime.now(timezone.utc)
    # Market A: cost to complete = $50.00 (5000 @ 0.010), high reward pool $200
    win_a = IncentiveWindow(
        slug="mkt-a",
        program_id="p-a",
        pool_usd=Decimal("200.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("5000"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    # Market B: cost to complete = $30.00 (3000 needed @ 0.010, existing 2000), lower reward pool $10
    win_b = IncentiveWindow(
        slug="mkt-b",
        program_id="p-b",
        pool_usd=Decimal("10.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("5000"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )

    book_a = Book(
        slug="mkt-a",
        bids=(Level(Decimal("0.010"), Decimal("0")),),
        asks=(Level(Decimal("0.020"), Decimal("5000")),),
        ts_ms=1000,
    )
    book_b = Book(
        slug="mkt-b",
        bids=(Level(Decimal("0.010"), Decimal("2000")),),
        asks=(Level(Decimal("0.020"), Decimal("5000")),),
        ts_ms=1000,
    )
    books = {"mkt-a": book_a, "mkt-b": book_b}

    res = greedy_allocate(
        windows=[win_a, win_b],
        books=books,
        total_budget=Decimal("60.00"),
        now=now,
    )

    # Market A requires $50. Market B requires $30.
    # $60 funds Market A ($50). Remaining $10 cannot fund Market B ($30).
    # Market A is selected, Market B is not funded as a partial.
    assert res.selected_slugs == ["mkt-a"]
    assert res.side_budgets[("mkt-a", "bid")] == Decimal("50.00")

def test_zero_ev_candidates_never_selected():
    now = datetime.now(timezone.utc)
    # Market with existing book already reaching target size -> queue position past target -> EV = 0
    win_zero = IncentiveWindow(
        slug="mkt-zero",
        program_id="p-zero",
        pool_usd=Decimal("100.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("5000"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    book_zero = Book(
        slug="mkt-zero",
        bids=(Level(Decimal("0.010"), Decimal("5000")),),
        asks=(Level(Decimal("0.020"), Decimal("5000")),),
        ts_ms=1000,
    )
    books = {"mkt-zero": book_zero}

    res = greedy_allocate(
        windows=[win_zero],
        books=books,
        total_budget=Decimal("100.00"),
        now=now,
    )
    assert res.selected_slugs == []
    assert res.unfunded_reason is not None

def test_real_fixture_slugs_one_event():
    with open(FIXTURES_DIR / "incentives.json", "r", encoding="utf-8") as f:
        inc_data = json.load(f)
    windows = PublicGatewayClient.parse_incentives(inc_data)

    # 4 fixture slugs from same event: asc-eurolg-asv-czv-2026-10-13
    target_slugs = [
        "asc-eurolg-asv-czv-2026-10-13-neg-10pt5",
        "asc-eurolg-asv-czv-2026-10-13-neg-1pt5",
        "asc-eurolg-asv-czv-2026-10-13-neg-4pt5",
        "asc-eurolg-asv-czv-2026-10-13-neg-7pt5",
    ]
    test_windows = [w for w in windows if w.slug in target_slugs]

    books = {}
    for s in target_slugs:
        books[s] = Book(
            slug=s,
            bids=(Level(Decimal("0.010"), Decimal("20")),),
            asks=(Level(Decimal("0.020"), Decimal("5000")),),
            ts_ms=1000,
        )

    # Build slug->event map mapping all 4 to the same event identifier
    slug_to_event = {s: "asv-czv-2026-10-13" for s in target_slugs}

    res = greedy_allocate(
        windows=test_windows,
        books=books,
        total_budget=Decimal("500.00"),
        now=datetime.now(timezone.utc),
        max_markets=4,
        slug_to_event_map=slug_to_event,
    )

    # Exactly 1 market from that event must be selected, despite ample budget ($500)
    assert len(res.selected_slugs) == 1
    assert res.selected_slugs[0] in target_slugs

def test_market_selector_max_spread_joint_funding():
    now = datetime.now(timezone.utc)
    win = IncentiveWindow(
        slug="mkt-spread-joint",
        program_id="p-spread",
        pool_usd=Decimal("100.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=Decimal("0.02"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    # Empty book depth on both sides
    book = Book(
        slug="mkt-spread-joint",
        bids=(Level(Decimal("0.490"), Decimal("10")),),
        asks=(Level(Decimal("0.510"), Decimal("10")),),
        ts_ms=1000,
    )
    res = greedy_allocate(
        windows=[win],
        books={win.slug: book},
        total_budget=Decimal("100.00"),
        now=now,
    )
    assert res.selected_slugs == [win.slug]
    assert (win.slug, "bid") in res.side_budgets
    assert (win.slug, "ask") in res.side_budgets

def test_greedy_allocate_filters_sub_dollar_lip_payout_floor():
    now = datetime.now(timezone.utc)
    w_sub = IncentiveWindow(
        slug="mkt-sub-dollar",
        program_id="p-sub",
        pool_usd=Decimal("1.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    w_valid = IncentiveWindow(
        slug="mkt-valid-payout",
        program_id="p-valid",
        pool_usd=Decimal("100.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("100"),
        max_spread=None,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        status="active",
    )
    book = Book(
        slug="any",
        bids=(Level(Decimal("0.100"), Decimal("20")),),
        asks=(Level(Decimal("0.200"), Decimal("100")),),
        ts_ms=1000,
    )
    books = {
        w_sub.slug: Book(w_sub.slug, book.bids, book.asks, 1000),
        w_valid.slug: Book(w_valid.slug, book.bids, book.asks, 1000),
    }

    res = greedy_allocate(
        windows=[w_sub, w_valid],
        books=books,
        total_budget=Decimal("100.00"),
        now=now,
    )

    assert "mkt-sub-dollar" not in res.selected_slugs
    assert "mkt-valid-payout" in res.selected_slugs
    assert not any(c.slug == "mkt-sub-dollar" for c in res.candidates)

def test_greedy_allocate_global_uses_min_size_with_deep_book():
    now = datetime.now(timezone.utc)
    w_global = IncentiveWindow(
        slug="mkt-global-deep",
        program_id="p-global",
        pool_usd=Decimal("50.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("200"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=23),
        status="active",
    )
    # Book with 500,000 contracts depth
    book = Book(
        slug="mkt-global-deep",
        bids=(Level(Decimal("0.050"), Decimal("500000")),),
        asks=(Level(Decimal("0.060"), Decimal("500000")),),
        ts_ms=1000,
    )
    books = {w_global.slug: book}

    # In US mode (is_global=False), needed_bid is 0, so 0 candidates
    res_us = greedy_allocate([w_global], books, total_budget=Decimal("50.00"), now=now, is_global=False)
    assert len(res_us.selected_slugs) == 0

    # In Global mode (is_global=True), needed_bid is target_size (200)
    res_global = greedy_allocate([w_global], books, total_budget=Decimal("50.00"), now=now, is_global=True)
    assert res_global.selected_slugs == [w_global.slug]
    assert (w_global.slug, "bid") in res_global.side_budgets
    assert res_global.side_qtys[(w_global.slug, "bid")] == Decimal("200")
    assert res_global.side_budgets[(w_global.slug, "bid")] == Decimal("200") * Decimal("0.050")

def test_rank_markets_global():
    now = datetime.now(timezone.utc)
    w1 = IncentiveWindow(
        slug="mkt-g1",
        program_id="p1",
        pool_usd=Decimal("100.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=23),
        status="active",
    )
    w2 = IncentiveWindow(
        slug="mkt-g2",
        program_id="p2",
        pool_usd=Decimal("10.00"),
        discount=Decimal("0.30"),
        target_size=Decimal("50"),
        max_spread=Decimal("0.05"),
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=23),
        status="active",
    )
    b1 = Book("mkt-g1", (Level(Decimal("0.100"), Decimal("10000")),), (Level(Decimal("0.110"), Decimal("10000")),), 1000)
    b2 = Book("mkt-g2", (Level(Decimal("0.100"), Decimal("10000")),), (Level(Decimal("0.110"), Decimal("10000")),), 1000)
    books = {"mkt-g1": b1, "mkt-g2": b2}

    ranked = rank_markets([w1, w2], books, now=now, max_n=2, total_budget=Decimal("150.00"), is_global=True)
    assert ranked == ["mkt-g1", "mkt-g2"]




