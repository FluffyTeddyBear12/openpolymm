from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from pmus.models import Book, ConditionMarket, IncentiveWindow, Level, Market, Position
from pmus.strategy import global_rewards
from pmus.strategy.condition_selector import (
    PAIR_LEG_MAX_PRICE,
    PAIR_LEG_MIN_PRICE,
    SINGLE_SIDED_MIN_PRICE,
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
    yes_b = make_book([("0.48", "250")], [("0.52", "250")], slug=cond.yes.slug)
    no_b = make_book([("0.47", "250")], [("0.53", "250")], slug=cond.no.slug)

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
    yes_b = make_book([("0.24", "250")], [("0.26", "250")], slug=cond.yes.slug)
    no_b = make_book([("0.74", "250")], [("0.76", "250")], slug=cond.no.slug)

    target = evaluate_condition(
        cond=cond,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("150"),
        max_contracts=Decimal("150"),
    )
    assert target is not None
    assert target.mode == "two_sided"
    assert target.yes_price == Decimal("0.24")
    assert target.no_price == Decimal("0.74")

    skewed_yes_b = make_book([("0.12", "250")], [("0.14", "250")], slug=cond.yes.slug)
    skewed_no_b = make_book([("0.86", "250")], [("0.88", "250")], slug=cond.no.slug)
    skewed_target = evaluate_condition(
        cond=cond,
        yes_book=skewed_yes_b,
        no_book=skewed_no_b,
        max_cost=Decimal("150"),
        max_contracts=Decimal("500"),
        pair_preference=Decimal("0.0"),
    )
    assert skewed_target is None

    extreme_yes_b = make_book([("0.04", "100")], [("0.06", "100")], slug=cond.yes.slug)
    extreme_no_b = make_book([("0.94", "100")], [("0.96", "100")], slug=cond.no.slug)
    extreme_target = evaluate_condition(
        cond=cond,
        yes_book=extreme_yes_b,
        no_book=extreme_no_b,
        max_cost=Decimal("150"),
        max_contracts=Decimal("500"),
    )
    assert extreme_target is None


def test_extreme_tail_below_10c_blocks_two_sided_pair():
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
    assert target is None


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
    yes_b = make_book([("0.48", "250")], [("0.52", "250")], slug=cond_pair.yes.slug)
    no_b = make_book([("0.47", "250")], [("0.53", "250")], slug=cond_pair.no.slug)

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
    assert alloc.condition_pairs == ((cond_pair.yes.slug, cond_pair.no.slug),)
    assert alloc.side_budgets[(cond_pair.yes.slug, "bid")] == target_pair.size * Decimal("0.48")
    assert alloc.side_budgets[(cond_pair.no.slug, "bid")] == target_pair.size * Decimal("0.47")
    assert alloc.side_qtys[(cond_pair.yes.slug, "bid")] == target_pair.size
    assert alloc.side_qtys[(cond_pair.no.slug, "bid")] == target_pair.size
    assert len(alloc.selected_candidates) == 2
    assert alloc.unfunded_reason is None

    empty_alloc = targets_to_allocation([])
    assert empty_alloc.selected_slugs == []
    assert empty_alloc.paired_slugs == frozenset()
    assert empty_alloc.condition_pairs == ()
    assert empty_alloc.unfunded_reason == "no condition clears payout floor within budget"


def test_pair_preference_selects_two_sided_when_within_ratio():
    cond = make_condition(pool="200", target="50", max_spread="0.05")
    yes_b = make_book([("0.48", "250")], [("0.52", "250")], slug=cond.yes.slug)
    no_b = make_book([("0.47", "250")], [("0.53", "250")], slug=cond.no.slug)

    target_default = evaluate_condition(
        cond=cond,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("50"),
        max_contracts=Decimal("200"),
        pair_preference=Decimal("1.0"),
    )
    assert target_default is not None
    assert target_default.mode == "yes_only"

    target_preferred = evaluate_condition(
        cond=cond,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("50"),
        max_contracts=Decimal("200"),
        pair_preference=Decimal("0.7"),
    )
    assert target_preferred is not None
    assert target_preferred.mode == "two_sided"

    target_strict = evaluate_condition(
        cond=cond,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("50"),
        max_contracts=Decimal("200"),
        pair_preference=Decimal("0.9"),
    )
    assert target_strict is not None
    assert target_strict.mode == "yes_only"


def test_allocate_conditions_resizes_candidate_to_fit_remaining_budget():
    cond1 = make_condition(cond_id="c1", pool="200", target="50")
    cond2 = make_condition(cond_id="c2", pool="180", target="50")

    books = {
        cond1.yes.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond1.yes.slug),
        cond1.no.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond1.no.slug),
        cond2.yes.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond2.yes.slug),
        cond2.no.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond2.no.slug),
    }

    targets = allocate_conditions(
        conditions=[cond1, cond2],
        books=books,
        total_budget=Decimal("162"),
        max_cost_per_condition=Decimal("82"),
        max_markets=4,
        max_contracts=Decimal("500"),
        pair_preference=Decimal("0.7"),
    )
    assert len(targets) == 2
    assert targets[0].condition.condition_id == "c1"
    assert targets[0].mode == "two_sided"
    assert targets[0].size == Decimal("85")
    assert targets[0].cost == Decimal("81.60")
    assert targets[1].condition.condition_id == "c2"
    assert targets[1].mode == "two_sided"
    assert targets[1].size == Decimal("80")
    assert targets[1].cost == Decimal("76.80")


def test_allocate_conditions_waterfills_across_conditions_when_budget_at_least_500():
    cond1 = make_condition(cond_id="c1", pool="100", target="50")
    cond2 = make_condition(cond_id="c2", pool="100", target="50")

    books = {
        cond1.yes.slug: make_book([("0.48", "500")], [("0.52", "500")], slug=cond1.yes.slug),
        cond1.no.slug: make_book([("0.48", "500")], [("0.52", "500")], slug=cond1.no.slug),
        cond2.yes.slug: make_book([("0.48", "500")], [("0.52", "500")], slug=cond2.yes.slug),
        cond2.no.slug: make_book([("0.48", "500")], [("0.52", "500")], slug=cond2.no.slug),
    }

    targets = allocate_conditions(
        conditions=[cond1, cond2],
        books=books,
        total_budget=Decimal("600"),
        max_cost_per_condition=Decimal("500"),
        max_markets=4,
        max_contracts=Decimal("1000"),
    )
    assert len(targets) == 2
    assert all(Decimal("200") <= t.cost <= Decimal("360") for t in targets)

    greedy_c1 = evaluate_condition(
        cond1,
        books[cond1.yes.slug],
        books[cond1.no.slug],
        Decimal("500"),
        Decimal("1000"),
    )
    assert greedy_c1 is not None
    greedy_c2 = evaluate_condition(
        cond2,
        books[cond2.yes.slug],
        books[cond2.no.slug],
        Decimal("600") - greedy_c1.cost,
        Decimal("1000"),
    )
    assert greedy_c2 is not None
    waterfill_reward = sum(t.expected_reward_sec for t in targets)
    greedy_reward = greedy_c1.expected_reward_sec + greedy_c2.expected_reward_sec
    assert waterfill_reward > greedy_reward


def test_allocate_conditions_skips_condition_when_inventory_at_or_above_cap():
    cond1 = make_condition(cond_id="c1", pool="200", target="50")
    cond2 = make_condition(cond_id="c2", pool="150", target="50")
    books = {
        cond1.yes.slug: make_book([("0.48", "100")], [("0.52", "100")], slug=cond1.yes.slug),
        cond1.no.slug: make_book([("0.48", "100")], [("0.52", "100")], slug=cond1.no.slug),
        cond2.yes.slug: make_book([("0.48", "100")], [("0.52", "100")], slug=cond2.yes.slug),
        cond2.no.slug: make_book([("0.48", "100")], [("0.52", "100")], slug=cond2.no.slug),
    }
    positions = {
        cond1.no.slug: Position(slug=cond1.no.slug, net_qty=Decimal("540"), avg_price=Decimal("0.184")),
    }
    targets = allocate_conditions(
        conditions=[cond1, cond2],
        books=books,
        total_budget=Decimal("165.00"),
        max_cost_per_condition=Decimal("55.00"),
        max_markets=2,
        max_contracts=Decimal("1000"),
        positions=positions,
    )
    assert len(targets) == 1
    assert targets[0].condition.condition_id == "c2"


def test_allocate_conditions_reduces_max_cost_by_held_inventory():
    cond1 = make_condition(cond_id="c1", pool="200", target="50")
    books = {
        cond1.yes.slug: make_book([("0.48", "500")], [("0.52", "500")], slug=cond1.yes.slug),
        cond1.no.slug: make_book([("0.48", "500")], [("0.52", "500")], slug=cond1.no.slug),
    }
    positions = {
        cond1.yes.slug: Position(slug=cond1.yes.slug, net_qty=Decimal("50"), avg_price=Decimal("0.50")),
    }
    targets = allocate_conditions(
        conditions=[cond1],
        books=books,
        total_budget=Decimal("100.00"),
        max_cost_per_condition=Decimal("55.00"),
        max_markets=1,
        max_contracts=Decimal("1000"),
        positions=positions,
    )
    assert len(targets) == 1
    assert targets[0].cost <= Decimal("30.00")


def test_waterfill_conditions_skips_condition_with_held_inventory_at_cap():
    cond1 = make_condition(cond_id="c1", pool="200", target="50")
    cond2 = make_condition(cond_id="c2", pool="150", target="50")
    books = {
        cond1.yes.slug: make_book([("0.48", "500")], [("0.52", "500")], slug=cond1.yes.slug),
        cond1.no.slug: make_book([("0.48", "500")], [("0.52", "500")], slug=cond1.no.slug),
        cond2.yes.slug: make_book([("0.48", "500")], [("0.52", "500")], slug=cond2.yes.slug),
        cond2.no.slug: make_book([("0.48", "500")], [("0.52", "500")], slug=cond2.no.slug),
    }
    positions = {
        cond1.no.slug: Position(slug=cond1.no.slug, net_qty=Decimal("540"), avg_price=Decimal("0.184")),
    }
    targets = allocate_conditions(
        conditions=[cond1, cond2],
        books=books,
        total_budget=Decimal("600.00"),
        max_cost_per_condition=Decimal("55.00"),
        max_markets=4,
        max_contracts=Decimal("1000"),
        positions=positions,
    )
    assert all(t.condition.condition_id != "c1" for t in targets)
    assert any(t.condition.condition_id == "c2" for t in targets)


def test_single_sided_tail_token_below_20c_blocked_while_two_sided_allowed():
    cond = make_condition(pool="200", target="50", max_spread="0.05")
    yes_wide_b = make_book([("0.70", "100")], [("0.98", "100")], slug=cond.yes.slug)
    yes_tight_b = make_book([("0.84", "100")], [("0.86", "100")], slug=cond.yes.slug)
    no_tail_b = make_book([("0.14", "100")], [("0.16", "100")], slug=cond.no.slug)

    target_blocked = evaluate_condition(
        cond=cond,
        yes_book=yes_wide_b,
        no_book=no_tail_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
    )
    assert target_blocked is None

    target_skewed_pair = evaluate_condition(
        cond=cond,
        yes_book=yes_tight_b,
        no_book=no_tail_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
        pair_preference=Decimal("0.0"),
    )
    assert target_skewed_pair is None

    yes_bal_b = make_book([("0.74", "100")], [("0.76", "100")], slug=cond.yes.slug)
    no_bal_b = make_book([("0.24", "100")], [("0.26", "100")], slug=cond.no.slug)
    target_paired = evaluate_condition(
        cond=cond,
        yes_book=yes_bal_b,
        no_book=no_bal_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
    )
    assert target_paired is not None
    assert target_paired.mode == "two_sided"
    assert target_paired.yes_price == Decimal("0.73")
    assert target_paired.no_price == Decimal("0.24")
    assert PAIR_LEG_MIN_PRICE <= target_paired.no_price <= PAIR_LEG_MAX_PRICE
    assert PAIR_LEG_MIN_PRICE <= target_paired.yes_price <= PAIR_LEG_MAX_PRICE

    no_extreme_b = make_book([("0.08", "100")], [("0.10", "100")], slug=cond.no.slug)
    target_extreme_blocked = evaluate_condition(
        cond=cond,
        yes_book=yes_tight_b,
        no_book=no_extreme_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
    )
    assert target_extreme_blocked is not None
    assert target_extreme_blocked.mode == "yes_only"

    target_both_blocked = evaluate_condition(
        cond=cond,
        yes_book=yes_wide_b,
        no_book=no_extreme_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
    )
    assert target_both_blocked is None

    no_at_floor_b = make_book([("0.20", "100")], [("0.22", "100")], slug=cond.no.slug)
    target_allowed = evaluate_condition(
        cond=cond,
        yes_book=yes_wide_b,
        no_book=no_at_floor_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
    )
    assert target_allowed is not None
    assert target_allowed.mode == "no_only"
    assert target_allowed.no_price == Decimal("0.20")
    assert target_allowed.no_price >= SINGLE_SIDED_MIN_PRICE

    yes_tail_b = make_book([("0.14", "100")], [("0.16", "100")], slug=cond.yes.slug)
    no_wide_b = make_book([("0.70", "100")], [("0.98", "100")], slug=cond.no.slug)
    target_yes_blocked = evaluate_condition(
        cond=cond,
        yes_book=yes_tail_b,
        no_book=no_wide_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
    )
    assert target_yes_blocked is None


def test_single_sided_crossed_against_opposite_book_blocked():
    cond = make_condition(pool="200", target="50", max_spread="0.05")
    yes_b = make_book([("0.25", "100")], [("0.27", "100")], slug=cond.yes.slug)
    no_b = make_book([("0.84", "100")], [("0.86", "100")], slug=cond.no.slug)

    target = evaluate_condition(
        cond=cond,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
    )
    assert target is None


def test_zero_pair_preference_strictly_blocks_single_sided_fallback():
    cond = make_condition(pool="500", target="50", max_spread="0.05")
    yes_b = make_book([("0.30", "100")], [("0.32", "100")], slug=cond.yes.slug)
    no_b = make_book([], [("0.70", "100")], slug=cond.no.slug)

    target_fallback = evaluate_condition(
        cond=cond,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
        pair_preference=Decimal("1.0"),
    )
    assert target_fallback is not None
    assert target_fallback.mode == "yes_only"
    assert target_fallback.yes_price == Decimal("0.30")

    target_blocked = evaluate_condition(
        cond=cond,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
        pair_preference=Decimal("0.0"),
    )
    assert target_blocked is None


def test_wide_spread_pair_below_92c_sum_rejected():
    cond = make_condition(pool="500", target="50", max_spread="0.05")
    yes_b = make_book([("0.22", "250")], [("0.24", "250")], slug=cond.yes.slug)
    no_b = make_book([("0.68", "250")], [("0.70", "250")], slug=cond.no.slug)

    target = evaluate_condition(
        cond=cond,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
        pair_preference=Decimal("0.0"),
    )
    assert target is None

    yes_tight = make_book([("0.24", "250")], [("0.26", "250")], slug=cond.yes.slug)
    target_tight = evaluate_condition(
        cond=cond,
        yes_book=yes_tight,
        no_book=no_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
        pair_preference=Decimal("0.0"),
    )
    assert target_tight is not None
    assert target_tight.mode == "two_sided"


def test_pair_sum_above_98c_rejected():
    cond = make_condition(pool="500", target="50", max_spread="0.01")
    yes_b = make_book([("0.50", "200")], [("0.51", "200")], slug=cond.yes.slug)
    no_b = make_book([("0.49", "200")], [("0.50", "200")], slug=cond.no.slug)

    target = evaluate_condition(
        cond=cond,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
        pair_preference=Decimal("0.0"),
    )
    assert target is None


def test_competitor_score_below_45_rejects_two_sided():
    cond = make_condition(pool="500", target="50", max_spread="0.05")
    yes_b = make_book([("0.48", "60")], [("0.52", "60")], slug=cond.yes.slug)
    no_b = make_book([("0.47", "60")], [("0.53", "60")], slug=cond.no.slug)

    target = evaluate_condition(
        cond=cond,
        yes_book=yes_b,
        no_book=no_b,
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
        pair_preference=Decimal("0.0"),
    )
    assert target is None


def test_evaluate_and_allocate_conditions_skip_two_sided_when_leg_filled_at_target_size():
    cond1 = make_condition(cond_id="c1", pool="300", target="50")
    cond2 = make_condition(cond_id="c2", pool="200", target="50")
    books = {
        cond1.yes.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond1.yes.slug),
        cond1.no.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond1.no.slug),
        cond2.yes.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond2.yes.slug),
        cond2.no.slug: make_book([("0.48", "250")], [("0.52", "250")], slug=cond2.no.slug),
    }

    dust_positions = {
        cond1.yes.slug: Position(slug=cond1.yes.slug, net_qty=Decimal("5"), avg_price=Decimal("0.40")),
    }
    target_dust = evaluate_condition(
        cond=cond1,
        yes_book=books[cond1.yes.slug],
        no_book=books[cond1.no.slug],
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
        pair_preference=Decimal("0.0"),
        positions=dust_positions,
    )
    assert target_dust is not None
    assert target_dust.mode == "two_sided"

    filled_yes_positions = {
        cond1.yes.slug: Position(slug=cond1.yes.slug, net_qty=Decimal("50"), avg_price=Decimal("0.40")),
    }
    target_filled_pair_only = evaluate_condition(
        cond=cond1,
        yes_book=books[cond1.yes.slug],
        no_book=books[cond1.no.slug],
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
        pair_preference=Decimal("0.0"),
        positions=filled_yes_positions,
    )
    assert target_filled_pair_only is None

    target_filled_single_fallback = evaluate_condition(
        cond=cond1,
        yes_book=books[cond1.yes.slug],
        no_book=books[cond1.no.slug],
        max_cost=Decimal("100"),
        max_contracts=Decimal("1000"),
        pair_preference=Decimal("1.0"),
        positions=filled_yes_positions,
    )
    assert target_filled_single_fallback is not None
    assert target_filled_single_fallback.mode == "no_only"

    high_cost_yes_positions = {
        cond1.yes.slug: Position(slug=cond1.yes.slug, net_qty=Decimal("50"), avg_price=Decimal("0.52")),
    }
    assert (
        evaluate_condition(
            cond=cond1,
            yes_book=books[cond1.yes.slug],
            no_book=books[cond1.no.slug],
            max_cost=Decimal("100"),
            max_contracts=Decimal("1000"),
            pair_preference=Decimal("1.0"),
            positions=high_cost_yes_positions,
        )
        is None
    )

    allocated = allocate_conditions(
        conditions=[cond1, cond2],
        books=books,
        total_budget=Decimal("200"),
        max_cost_per_condition=Decimal("100"),
        max_markets=2,
        max_contracts=Decimal("1000"),
        pair_preference=Decimal("0.0"),
        positions=filled_yes_positions,
    )
    assert [t.condition.condition_id for t in allocated] == ["c2"]
    assert allocated[0].mode == "two_sided"

    allocated_waterfill = allocate_conditions(
        conditions=[cond1, cond2],
        books=books,
        total_budget=Decimal("600"),
        max_cost_per_condition=Decimal("100"),
        max_markets=2,
        max_contracts=Decimal("1000"),
        pair_preference=Decimal("0.0"),
        positions=filled_yes_positions,
    )
    assert [t.condition.condition_id for t in allocated_waterfill] == ["c2"]
    assert allocated_waterfill[0].mode == "two_sided"


def test_waterfill_min_tier_prevents_20_share_dust_allocations():
    from pmus.strategy.condition_selector import WATERFILL_COST_TIERS

    assert WATERFILL_COST_TIERS[0] >= Decimal("36")

