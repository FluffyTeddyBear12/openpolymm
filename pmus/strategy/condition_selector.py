import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pmus.models import Book, ConditionMarket, Position
from pmus.strategy import book_guard, global_rewards
from pmus.strategy.market_selector import AllocationResult, CandidateSide

logger = logging.getLogger(__name__)

SINGLE_SIDED_MIN_PRICE = Decimal("0.20")
PAIR_LEG_MIN_PRICE = Decimal("0.22")
PAIR_LEG_MAX_PRICE = Decimal("0.75")
PAIR_MIN_PRICE_SUM = Decimal("0.92")
PAIR_MAX_PRICE_SUM = Decimal("0.98")
MIN_COMPETITOR_SCORE = Decimal("45")


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


def _admit(cond: ConditionMarket, book: Book, price: Decimal, tick: Decimal, other_book: Book | None = None) -> bool:
    reason = book_guard.quote_guard(book, cond.window, price, tick, other_book=other_book)
    if reason is not None:
        logger.debug("Guard rejected %s bid on %s: %s", book.slug, cond.question, reason)
    return reason is None


def evaluate_condition(
    cond: ConditionMarket,
    yes_book: Book,
    no_book: Book,
    max_cost: Decimal,
    max_contracts: Decimal,
    pair_preference: Decimal = Decimal("1.0"),
    positions: dict[str, Position] | None = None,
) -> ConditionTarget | None:
    window = cond.window
    lot = cond.yes.min_qty
    min_size = window.target_size
    yes_tick = yes_book.tick_size or cond.yes.tick
    no_tick = no_book.tick_size or cond.no.tick
    surviving: list[ConditionTarget] = []

    yes_pos = (positions or {}).get(cond.yes.slug)
    no_pos = (positions or {}).get(cond.no.slug)
    yes_held = yes_pos.net_qty if yes_pos is not None else Decimal("0")
    no_held = no_pos.net_qty if no_pos is not None else Decimal("0")
    either_leg_filled = yes_held >= min_size or no_held >= min_size

    prices = (
        global_rewards.pair_quote_prices(
            yes_book, no_book, window, yes_tick, no_tick
        )
        if not either_leg_filled
        else None
    )
    if prices is not None:
        p_yes, p_no = prices
        comp_q = global_rewards.competitor_q(yes_book, no_book, window)
        opp_yes_bid = yes_book.bids[0].price if yes_book.bids else Decimal("0")
        opp_no_bid = no_book.bids[0].price if no_book.bids else Decimal("0")
        if (
            PAIR_LEG_MIN_PRICE <= p_yes <= PAIR_LEG_MAX_PRICE
            and PAIR_LEG_MIN_PRICE <= p_no <= PAIR_LEG_MAX_PRICE
            and PAIR_MIN_PRICE_SUM <= (p_yes + p_no) <= global_rewards.pair_max_sum(window)
            and (p_yes + opp_no_bid) <= global_rewards.CROSS_MAX_SUM
            and (p_no + opp_yes_bid) <= global_rewards.CROSS_MAX_SUM
            and comp_q >= min(window.target_size, MIN_COMPETITOR_SCORE)
            and _admit(cond, yes_book, p_yes, yes_tick, other_book=no_book)
            and _admit(cond, no_book, p_no, no_tick, other_book=yes_book)
        ):
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

    if pair_preference > Decimal("0"):
        if yes_held < min_size:
            p_yes = global_rewards.bid_price(yes_book, window, yes_tick)
            opp_no_bid = no_book.bids[0].price if no_book.bids else Decimal("0")
            held_no_px = no_pos.avg_price if (no_held > Decimal("0") and no_pos is not None and no_pos.avg_price > Decimal("0")) else Decimal("0")
            if (
                p_yes is not None
                and p_yes >= SINGLE_SIDED_MIN_PRICE
                and (p_yes + opp_no_bid) <= global_rewards.pair_max_sum(window)
                and (p_yes + held_no_px) < Decimal("1")
                and not (no_held >= min_size and held_no_px > Decimal("0") and (p_yes + held_no_px) > PAIR_MAX_PRICE_SUM)
                and _admit(cond, yes_book, p_yes, yes_tick, other_book=no_book)
            ):
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

        if no_held < min_size:
            p_no = global_rewards.bid_price(no_book, window, no_tick)
            opp_yes_bid = yes_book.bids[0].price if yes_book.bids else Decimal("0")
            held_yes_px = yes_pos.avg_price if (yes_held > Decimal("0") and yes_pos is not None and yes_pos.avg_price > Decimal("0")) else Decimal("0")
            if (
                p_no is not None
                and p_no >= SINGLE_SIDED_MIN_PRICE
                and (p_no + opp_yes_bid) <= global_rewards.pair_max_sum(window)
                and (p_no + held_yes_px) < Decimal("1")
                and not (yes_held >= min_size and held_yes_px > Decimal("0") and (p_no + held_yes_px) > PAIR_MAX_PRICE_SUM)
                and _admit(cond, no_book, p_no, no_tick, other_book=yes_book)
            ):
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


WATERFILL_MIN_BUDGET = Decimal("100")
WATERFILL_COST_TIERS: tuple[Decimal, ...] = (
    Decimal("36"),
    Decimal("50"),
    Decimal("65"),
    Decimal("85"),
    Decimal("110"),
    Decimal("160"),
    Decimal("240"),
    Decimal("360"),
    Decimal("550"),
    Decimal("850"),
    Decimal("1300"),
    Decimal("2000"),
)


def _rank_score(t: ConditionTarget, pair_preference: Decimal = Decimal("1.0")) -> Decimal:
    weight = pair_preference if (t.mode == "two_sided" and pair_preference > Decimal("0")) else Decimal("1.0")
    return t.expected_reward_sec / weight


def _condition_held_cost(cond: ConditionMarket, positions: dict[str, Position] | None) -> Decimal:
    if not positions:
        return Decimal("0")
    yes_pos = positions.get(cond.yes.slug)
    no_pos = positions.get(cond.no.slug)
    return (
        (max(Decimal("0"), yes_pos.net_qty) * yes_pos.avg_price if yes_pos and yes_pos.avg_price > Decimal("0") else Decimal("0"))
        + (max(Decimal("0"), no_pos.net_qty) * no_pos.avg_price if no_pos and no_pos.avg_price > Decimal("0") else Decimal("0"))
    )


def _waterfill_conditions(
    conditions: list[ConditionMarket],
    books: dict[str, Book],
    total_budget: Decimal,
    max_cost_per_condition: Decimal,
    max_markets: int,
    max_contracts: Decimal,
    pair_preference: Decimal = Decimal("1.0"),
    positions: dict[str, Position] | None = None,
) -> list[ConditionTarget]:
    max_cost = min(max_cost_per_condition, total_budget)

    cond_curves: dict[str, list[ConditionTarget]] = {}
    unique_conditions: list[tuple[str, ConditionMarket]] = []
    seen_keys: set[str] = set()

    for cond in conditions:
        key = cond.condition_id if cond.condition_id else cond.yes.slug
        if key in seen_keys:
            continue
        if cond.yes.slug not in books or cond.no.slug not in books:
            continue
        held_cost = _condition_held_cost(cond, positions)
        cond_max_cost = max(Decimal("0"), max_cost - held_cost)
        if cond_max_cost <= Decimal("0"):
            continue
        seen_keys.add(key)
        unique_conditions.append((key, cond))
        yes_book = books[cond.yes.slug]
        no_book = books[cond.no.slug]
        pts: list[ConditionTarget] = []
        last_cost = Decimal("0")
        last_rew = Decimal("0")
        cond_tiers = [t for t in WATERFILL_COST_TIERS if t < cond_max_cost] + [cond_max_cost]
        for tier_cap in cond_tiers:
            t = evaluate_condition(
                cond=cond,
                yes_book=yes_book,
                no_book=no_book,
                max_cost=tier_cap,
                max_contracts=max_contracts,
                pair_preference=pair_preference,
                positions=positions,
            )
            if t is not None and t.cost > last_cost and t.expected_reward_sec > last_rew:
                pts.append(t)
                last_cost = t.cost
                last_rew = t.expected_reward_sec
        if pts:
            cond_curves[key] = pts

    chosen_idx: dict[str, int] = {key: -1 for key in cond_curves}
    active_keys: set[str] = set()
    remaining_budget = total_budget

    while True:
        best_key: str | None = None
        best_eff = Decimal("-1")
        best_d_cost = Decimal("0")
        for key, pts in cond_curves.items():
            idx = chosen_idx[key]
            next_idx = idx + 1
            if next_idx >= len(pts):
                continue
            if idx < 0 and len(active_keys) >= max_markets:
                continue
            cur_cost = pts[idx].cost if idx >= 0 else Decimal("0")
            cur_rew = pts[idx].expected_reward_sec if idx >= 0 else Decimal("0")
            d_cost = pts[next_idx].cost - cur_cost
            d_rew = pts[next_idx].expected_reward_sec - cur_rew
            if d_cost <= Decimal("0") or d_cost > remaining_budget:
                continue
            weight = (
                pair_preference
                if (pts[next_idx].mode == "two_sided" and pair_preference > Decimal("0"))
                else Decimal("1.0")
            )
            eff = (d_rew / weight) / d_cost
            if eff > best_eff:
                best_eff = eff
                best_key = key
                best_d_cost = d_cost
        if best_key is None:
            break
        chosen_idx[best_key] += 1
        active_keys.add(best_key)
        remaining_budget -= best_d_cost

    selected: list[ConditionTarget] = [
        pts[chosen_idx[key]] for key, pts in cond_curves.items() if chosen_idx[key] >= 0
    ]

    if remaining_budget > Decimal("0") and len(active_keys) < max_markets:
        fallback_candidates: list[ConditionTarget] = []
        for key, cond in unique_conditions:
            if key in active_keys:
                continue
            held_cost = _condition_held_cost(cond, positions)
            cand_max = max(Decimal("0"), min(max_cost, remaining_budget) - held_cost)
            if cand_max <= Decimal("0"):
                continue
            resized = evaluate_condition(
                cond=cond,
                yes_book=books[cond.yes.slug],
                no_book=books[cond.no.slug],
                max_cost=cand_max,
                max_contracts=max_contracts,
                pair_preference=pair_preference,
                positions=positions,
            )
            if resized is not None and resized.cost > Decimal("0"):
                fallback_candidates.append(resized)
        fallback_candidates.sort(
            key=lambda t: _rank_score(t, pair_preference) / t.cost,
            reverse=True,
        )
        for resized in fallback_candidates:
            if len(selected) >= max_markets or remaining_budget <= Decimal("0"):
                break
            if resized.cost <= remaining_budget:
                selected.append(resized)
                remaining_budget -= resized.cost

    selected.sort(key=lambda t: _rank_score(t, pair_preference), reverse=True)
    if remaining_budget > Decimal("0"):
        for idx_sel, cur_target in enumerate(selected):
            if remaining_budget <= Decimal("0"):
                break
            cond = cur_target.condition
            held_cost = _condition_held_cost(cond, positions)
            cond_max_cost = max(Decimal("0"), max_cost - held_cost)
            topup_cap = min(cond_max_cost, cur_target.cost + remaining_budget)
            if topup_cap <= cur_target.cost:
                continue
            topped = evaluate_condition(
                cond=cond,
                yes_book=books[cond.yes.slug],
                no_book=books[cond.no.slug],
                max_cost=topup_cap,
                max_contracts=max_contracts,
                pair_preference=pair_preference,
                positions=positions,
            )
            if (
                topped is not None
                and topped.cost > cur_target.cost
                and (topped.cost - cur_target.cost) <= remaining_budget
                and topped.expected_reward_sec > cur_target.expected_reward_sec
            ):
                remaining_budget -= topped.cost - cur_target.cost
                selected[idx_sel] = topped

    selected.sort(key=lambda t: _rank_score(t, pair_preference), reverse=True)
    return selected


def allocate_conditions(
    conditions: list[ConditionMarket],
    books: dict[str, Book],
    total_budget: Decimal,
    max_cost_per_condition: Decimal,
    max_markets: int,
    max_contracts: Decimal,
    pair_preference: Decimal = Decimal("1.0"),
    positions: dict[str, Position] | None = None,
) -> list[ConditionTarget]:
    if total_budget >= WATERFILL_MIN_BUDGET and max_markets > 1:
        return _waterfill_conditions(
            conditions=conditions,
            books=books,
            total_budget=total_budget,
            max_cost_per_condition=max_cost_per_condition,
            max_markets=max_markets,
            max_contracts=max_contracts,
            pair_preference=pair_preference,
            positions=positions,
        )
    max_cost = min(max_cost_per_condition, total_budget)
    candidates: list[ConditionTarget] = []
    for cond in conditions:
        if cond.yes.slug not in books or cond.no.slug not in books:
            continue
        held_cost = _condition_held_cost(cond, positions)
        cond_max_cost = max(Decimal("0"), max_cost - held_cost)
        if cond_max_cost <= Decimal("0"):
            continue
        target = evaluate_condition(
            cond=cond,
            yes_book=books[cond.yes.slug],
            no_book=books[cond.no.slug],
            max_cost=cond_max_cost,
            max_contracts=max_contracts,
            pair_preference=pair_preference,
            positions=positions,
        )
        if target is not None:
            candidates.append(target)

    candidates.sort(key=lambda t: _rank_score(t, pair_preference), reverse=True)

    selected: list[ConditionTarget] = []
    remaining_budget = total_budget
    for target in candidates:
        if len(selected) >= max_markets or remaining_budget <= Decimal("0"):
            break
        if target.cost <= remaining_budget:
            selected.append(target)
            remaining_budget -= target.cost
        else:
            cond = target.condition
            held_cost = _condition_held_cost(cond, positions)
            cond_rem_cap = max(Decimal("0"), min(max_cost_per_condition, remaining_budget) - held_cost)
            if cond_rem_cap <= Decimal("0"):
                continue
            resized = evaluate_condition(
                cond=cond,
                yes_book=books[cond.yes.slug],
                no_book=books[cond.no.slug],
                max_cost=cond_rem_cap,
                max_contracts=max_contracts,
                pair_preference=pair_preference,
                positions=positions,
            )
            if resized is not None and resized.cost <= remaining_budget:
                selected.append(resized)
                remaining_budget -= resized.cost

    return selected


def targets_to_allocation(targets: list[ConditionTarget]) -> AllocationResult:
    selected_slugs: list[str] = []
    side_budgets: dict[tuple[str, str], Decimal] = {}
    side_qtys: dict[tuple[str, str], Decimal] = {}
    side_prices: dict[tuple[str, str], Decimal] = {}
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
            side_prices[(slug, "bid")] = price
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
    pairs = tuple(dict.fromkeys((t.condition.yes.slug, t.condition.no.slug) for t in targets))

    return AllocationResult(
        candidates=selected_candidates,
        selected_candidates=selected_candidates,
        selected_slugs=selected_slugs,
        side_budgets=side_budgets,
        side_qtys=side_qtys,
        unfunded_reason=unfunded_reason,
        paired_slugs=frozenset(paired_slugs),
        condition_pairs=pairs,
        side_prices=side_prices,
    )
