from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pmus.models import Book, ConditionMarket
from pmus.strategy import global_rewards
from pmus.strategy.market_selector import AllocationResult, CandidateSide


@dataclass(frozen=True)
class ConditionTarget:
    condition: ConditionMarket
    mode: Literal["two_sided", "yes_only", "no_only"]
    size: Decimal
    yes_price: Decimal | None
    no_price: Decimal | None
    cost: Decimal
    expected_reward_sec: Decimal


def _single_size(
    cost_cap: Decimal,
    price: Decimal,
    min_size: Decimal,
    lot: Decimal,
    max_contracts: Decimal,
) -> Decimal:
    if price <= Decimal("0") or lot <= Decimal("0") or cost_cap <= Decimal("0"):
        return Decimal("0")
    max_by_cost = cost_cap / price
    max_allowed = min(max_by_cost, max_contracts)
    if max_allowed < min_size:
        return Decimal("0")
    size = (max_allowed // lot) * lot
    while size * price > cost_cap or size > max_contracts:
        size -= lot
    if size < min_size:
        return Decimal("0")
    return size


def evaluate_condition(
    cond: ConditionMarket,
    yes_book: Book,
    no_book: Book,
    max_cost: Decimal,
    max_contracts: Decimal,
    pair_preference: Decimal = Decimal("1.0"),
) -> ConditionTarget | None:
    window = cond.window
    lot = cond.yes.min_qty
    min_size = window.target_size
    surviving: list[ConditionTarget] = []

    prices = global_rewards.pair_quote_prices(
        yes_book, no_book, window, cond.yes.tick, cond.no.tick
    )
    if prices is not None:
        p_yes, p_no = prices
        size = global_rewards.pair_size(max_cost, p_yes, p_no, min_size, lot, max_contracts)
        cost = size * (p_yes + p_no)
        reward = global_rewards.expected_pair_reward_sec(yes_book, no_book, window, p_yes, p_no, size)
        if size > Decimal("0") and (reward * global_rewards.SECONDS_PER_DAY) >= global_rewards.MIN_DAILY_PAYOUT_USD:
            surviving.append(
                ConditionTarget(
                    condition=cond,
                    mode="two_sided",
                    size=size,
                    yes_price=p_yes,
                    no_price=p_no,
                    cost=cost,
                    expected_reward_sec=reward,
                )
            )

    p_yes = global_rewards.bid_price(yes_book, window, cond.yes.tick)
    if p_yes is not None and p_yes > Decimal("0"):
        size_yes = _single_size(max_cost, p_yes, min_size, lot, max_contracts)
        cost_yes = size_yes * p_yes
        reward_yes = global_rewards.expected_single_reward_sec(yes_book, no_book, window, p_yes, size_yes)
        if size_yes > Decimal("0") and (reward_yes * global_rewards.SECONDS_PER_DAY) >= global_rewards.MIN_DAILY_PAYOUT_USD:
            surviving.append(
                ConditionTarget(
                    condition=cond,
                    mode="yes_only",
                    size=size_yes,
                    yes_price=p_yes,
                    no_price=None,
                    cost=cost_yes,
                    expected_reward_sec=reward_yes,
                )
            )

    p_no = global_rewards.bid_price(no_book, window, cond.no.tick)
    if p_no is not None and p_no > Decimal("0"):
        size_no = _single_size(max_cost, p_no, min_size, cond.no.min_qty, max_contracts)
        cost_no = size_no * p_no
        reward_no = global_rewards.expected_single_reward_sec(no_book, yes_book, window, p_no, size_no)
        if size_no > Decimal("0") and (reward_no * global_rewards.SECONDS_PER_DAY) >= global_rewards.MIN_DAILY_PAYOUT_USD:
            surviving.append(
                ConditionTarget(
                    condition=cond,
                    mode="no_only",
                    size=size_no,
                    yes_price=None,
                    no_price=p_no,
                    cost=cost_no,
                    expected_reward_sec=reward_no,
                )
            )

    if not surviving:
        return None
    if pair_preference < Decimal("1.0"):
        pair_target = next((t for t in surviving if t.mode == "two_sided"), None)
        best_single = max((t for t in surviving if t.mode in ("yes_only", "no_only")), key=lambda t: t.expected_reward_sec, default=None)
        if pair_target is not None and best_single is not None:
            if pair_target.expected_reward_sec >= best_single.expected_reward_sec * pair_preference:
                return pair_target
    return max(surviving, key=lambda t: t.expected_reward_sec)


def allocate_conditions(
    conditions: list[ConditionMarket],
    books: dict[str, Book],
    total_budget: Decimal,
    max_cost_per_condition: Decimal,
    max_markets: int,
    max_contracts: Decimal,
    pair_preference: Decimal = Decimal("1.0"),
) -> list[ConditionTarget]:
    max_cost = min(max_cost_per_condition, total_budget)
    candidates: list[ConditionTarget] = []
    for cond in conditions:
        if cond.yes.slug not in books or cond.no.slug not in books:
            continue
        target = evaluate_condition(
            cond=cond,
            yes_book=books[cond.yes.slug],
            no_book=books[cond.no.slug],
            max_cost=max_cost,
            max_contracts=max_contracts,
            pair_preference=pair_preference,
        )
        if target is not None:
            candidates.append(target)

    candidates.sort(key=lambda t: t.expected_reward_sec, reverse=True)

    selected: list[ConditionTarget] = []
    remaining_budget = total_budget
    for target in candidates:
        if len(selected) >= max_markets:
            break
        if target.cost <= remaining_budget:
            selected.append(target)
            remaining_budget -= target.cost

    return selected


def targets_to_allocation(targets: list[ConditionTarget]) -> AllocationResult:
    selected_slugs: list[str] = []
    side_budgets: dict[tuple[str, str], Decimal] = {}
    side_qtys: dict[tuple[str, str], Decimal] = {}
    paired_slugs: set[str] = set()
    selected_candidates: list[CandidateSide] = []

    for t in targets:
        if t.mode == "two_sided":
            paired_slugs.add(t.condition.yes.slug)
            paired_slugs.add(t.condition.no.slug)
            tokens = [
                (t.condition.yes.slug, t.yes_price),
                (t.condition.no.slug, t.no_price),
            ]
            tok_reward = t.expected_reward_sec / Decimal("2")
        elif t.mode == "yes_only":
            tokens = [(t.condition.yes.slug, t.yes_price)]
            tok_reward = t.expected_reward_sec
        elif t.mode == "no_only":
            tokens = [(t.condition.no.slug, t.no_price)]
            tok_reward = t.expected_reward_sec
        else:
            continue

        for slug, price in tokens:
            if price is None:
                continue
            selected_slugs.append(slug)
            tok_cost = t.size * price
            side_budgets[(slug, "bid")] = tok_cost
            side_qtys[(slug, "bid")] = t.size
            eff = (tok_reward / tok_cost) if tok_cost > Decimal("0") else Decimal("0")
            candidate = CandidateSide(
                slug=slug,
                event_id=t.condition.event_id,
                side="bid",
                price=price,
                existing_qty=Decimal("0"),
                needed_qty=t.size,
                cost=tok_cost,
                expected_reward_sec=tok_reward,
                efficiency=eff,
                period="global",
                pool_usd=t.condition.window.pool_usd,
                duration_seconds=Decimal("86400"),
            )
            selected_candidates.append(candidate)

    unfunded_reason = "no condition clears payout floor within budget" if not targets else None
    pairs = tuple((t.condition.yes.slug, t.condition.no.slug) for t in targets if t.mode == "two_sided")

    return AllocationResult(
        candidates=selected_candidates,
        selected_candidates=selected_candidates,
        selected_slugs=selected_slugs,
        side_budgets=side_budgets,
        side_qtys=side_qtys,
        unfunded_reason=unfunded_reason,
        paired_slugs=frozenset(paired_slugs),
        condition_pairs=pairs,
    )
