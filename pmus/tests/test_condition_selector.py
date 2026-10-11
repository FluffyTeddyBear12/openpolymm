from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from pmus.models import Book, ConditionMarket, IncentiveWindow, Level, Market
from pmus.strategy import global_rewards
from pmus.strategy.condition_selector import (
    allocate_conditions,
    evaluate_condition,
    targets_to_allocation,
)

NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)


def make_window(slug="cond-1", pool="100", target="50", max_spread="0.05") -> IncentiveWindow:
    return IncentiveWindow(
        slug=slug,
        program_id=f"prog-{slug}",
        pool_usd=Decimal(pool),
        discount=Decimal("0.30"),
        target_size=Decimal(target),
        max_spread=Decimal(max_spread),
        start=NOW - timedelta(hours=1),
        end=NOW + timedelta(hours=23),
        status="active",
    )


def make_book(bids, asks, slug="tok") -> Book:
    return Book(
        slug,
        tuple(Level(Decimal(p), Decimal(q)) for p, q in bids),
        tuple(Level(Decimal(p), Decimal(q)) for p, q in asks),
        0,
    )


def make_condition(
    cond_id="cond-1",
    event_id="ev-1",
    question="Will it occur?",
    yes_tick="0.01",
    no_tick="0.01",
    min_qty="5",
    pool="100",
    target="50",
    max_spread="0.05",
) -> ConditionMarket:
    yes_mkt = Market(
        slug=f"{cond_id}-yes",
        event_id=event_id,
        category="sports",
        tick=Decimal(yes_tick),
        min_qty=Decimal(min_qty),
        active=True,
        question=question,
    )
    no_mkt = Market(
        slug=f"{cond_id}-no",
        event_id=event_id,
        category="sports",
        tick=Decimal(no_tick),
        min_qty=Decimal(min_qty),
        active=True,
        question=question,
    )
    w = make_window(slug=cond_id, pool=pool, target=target, max_spread=max_spread)
    return ConditionMarket(
        condition_id=cond_id,
        event_id=event_id,
        question=question,
        yes=yes_mkt,
        no=no_mkt,
        window=w,
    )


def test_two_sided_chosen_over_single_in_band_when_scoring_more():
    cond = make_condition(pool="200", target="50", max_spread="0.05")
    yes_b = make_book([("0.48", "100")], [("0.52", "100")], slug=cond.yes.slug)
    no_b = make_book([("0.47", "100")], [("0.53", "100")], slug=cond.no.slug)

    target = evaluate_condition(
        cond=cond,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("100"),
    )
    assert target is not None
    assert target.mode == "two_sided"
    assert target.yes_price == Decimal("0.48")
    assert target.no_price == Decimal("0.47")
    assert target.size == Decimal("100")
    assert target.cost == target.size * (Decimal("0.48") + Decimal("0.47"))
    assert target.expected_reward_sec > Decimal("0")


def test_tail_market_yields_two_sided_only():
    cond = make_condition(pool="200", target="50", max_spread="0.03")
    yes_b = make_book([("0.04", "100")], [("0.06", "100")], slug=cond.yes.slug)
    no_b = make_book([("0.94", "100")], [("0.96", "100")], slug=cond.no.slug)

    target = evaluate_condition(
        cond=cond,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("150"),
        max_contracts=Decimal("500"),
    )
    assert target is not None
    assert target.mode == "two_sided"
    assert target.yes_price == Decimal("0.04")
    assert target.no_price == Decimal("0.94")


def test_condition_below_payout_floor_dropped(monkeypatch):
    cond = make_condition(pool="1", target="50", max_spread="0.05")
    yes_b = make_book([("0.48", "100")], [("0.52", "100")], slug=cond.yes.slug)
    no_b = make_book([("0.47", "100")], [("0.53", "100")], slug=cond.no.slug)

    monkeypatch.setattr(global_rewards, "MIN_DAILY_PAYOUT_USD", Decimal("500.00"))
    target = evaluate_condition(
        cond=cond,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("500"),
    )
    assert target is None


def test_budget_and_max_markets_respected():
    cond1 = make_condition(cond_id="c1", pool="300", target="50")
    cond2 = make_condition(cond_id="c2", pool="200", target="50")
    cond3 = make_condition(cond_id="c3", pool="100", target="50")

    books = {
        cond1.yes.slug: make_book([("0.48", "100")], [("0.52", "100")], slug=cond1.yes.slug),
        cond1.no.slug: make_book([("0.47", "100")], [("0.53", "100")], slug=cond1.no.slug),
        cond2.yes.slug: make_book([("0.48", "100")], [("0.52", "100")], slug=cond2.yes.slug),
        cond2.no.slug: make_book([("0.47", "100")], [("0.53", "100")], slug=cond2.no.slug),
        cond3.yes.slug: make_book([("0.48", "100")], [("0.52", "100")], slug=cond3.yes.slug),
        cond3.no.slug: make_book([("0.47", "100")], [("0.53", "100")], slug=cond3.no.slug),
    }

    targets = allocate_conditions(
        conditions=[cond1, cond2, cond3],
        books=books,
        total_budget=Decimal("500"),
        max_cost_per_condition=Decimal("60"),
        max_markets=2,
        max_contracts=Decimal("200"),
    )
    assert len(targets) == 2
    assert targets[0].condition.condition_id == "c1"
    assert targets[1].condition.condition_id == "c2"

    targets_small_budget = allocate_conditions(
        conditions=[cond1, cond2, cond3],
        books=books,
        total_budget=Decimal("70"),
        max_cost_per_condition=Decimal("60"),
        max_markets=5,
        max_contracts=Decimal("200"),
    )
    assert len(targets_small_budget) == 1
    assert targets_small_budget[0].cost <= Decimal("70")


def test_missing_book_skipped():
    cond1 = make_condition(cond_id="c1", pool="300")
    cond2 = make_condition(cond_id="c2", pool="200")

    books = {
        cond1.yes.slug: make_book([("0.48", "100")], [("0.52", "100")], slug=cond1.yes.slug),
        cond2.yes.slug: make_book([("0.48", "100")], [("0.52", "100")], slug=cond2.yes.slug),
        cond2.no.slug: make_book([("0.47", "100")], [("0.53", "100")], slug=cond2.no.slug),
    }

    targets = allocate_conditions(
        conditions=[cond1, cond2],
        books=books,
        total_budget=Decimal("200"),
        max_cost_per_condition=Decimal("60"),
        max_markets=5,
        max_contracts=Decimal("200"),
    )
    assert len(targets) == 1
    assert targets[0].condition.condition_id == "c2"


def test_targets_to_allocation_fields():
    cond_pair = make_condition(cond_id="c-pair", event_id="ev-p", pool="120")
    yes_b = make_book([("0.48", "100")], [("0.52", "100")], slug=cond_pair.yes.slug)
    no_b = make_book([("0.47", "100")], [("0.53", "100")], slug=cond_pair.no.slug)

    target_pair = evaluate_condition(
        cond=cond_pair,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("100"),
    )
    assert target_pair is not None

    alloc = targets_to_allocation([target_pair])
    assert alloc.selected_slugs == [cond_pair.yes.slug, cond_pair.no.slug]
    assert alloc.paired_slugs == frozenset({cond_pair.yes.slug, cond_pair.no.slug})
    assert alloc.side_budgets[(cond_pair.yes.slug, "bid")] == target_pair.size * Decimal("0.48")
    assert alloc.side_budgets[(cond_pair.no.slug, "bid")] == target_pair.size * Decimal("0.47")
    assert alloc.side_qtys[(cond_pair.yes.slug, "bid")] == target_pair.size
    assert alloc.side_qtys[(cond_pair.no.slug, "bid")] == target_pair.size
    assert len(alloc.selected_candidates) == 2
    assert alloc.unfunded_reason is None

    empty_alloc = targets_to_allocation([])
    assert empty_alloc.selected_slugs == []
    assert empty_alloc.paired_slugs == frozenset()
    assert empty_alloc.unfunded_reason == "no condition clears payout floor within budget"
