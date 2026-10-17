"""Polymarket Global liquidity-reward scoring.

Order score S = ((v - s) / v)^2 * size, where v is the max incentive spread and
s the distance from the size-adjusted midpoint. Orders below the minimum size
or at/after v score zero. With the midpoint in [0.10, 0.90] a single-sided
book scores S/3; outside that band only two-sided liquidity scores.
"""
import os
from decimal import Decimal

from pmus.models import Book, IncentiveWindow, Level, LiveOrder

REWARD_MID_LOW = Decimal("0.10")
REWARD_MID_HIGH = Decimal("0.90")
SINGLE_SIDED_DIVISOR = Decimal("3")
SECONDS_PER_DAY = Decimal("86400")
MIN_DAILY_PAYOUT_USD = Decimal(os.getenv("PMUS_GLOBAL_MIN_DAILY_REWARD", "1.00"))
MAX_CUSHION_SPREAD_RATIO = Decimal("0.60")
PAIR_MAX_SUM = Decimal("0.98")
WIDE_SPREAD_PAIR_MAX_SUM = Decimal("0.96")
ZERO = Decimal("0")


def adjusted_mid(book: Book, min_size: Decimal) -> Decimal | None:
    if not book.bids or not book.asks:
        return None
    bid = next((lvl.price for lvl in book.bids if lvl.qty >= min_size), book.bids[0].price)
    ask = next((lvl.price for lvl in book.asks if lvl.qty >= min_size), book.asks[0].price)
    return (bid + ask) / Decimal("2")


def mid_in_reward_band(mid: Decimal) -> bool:
    return REWARD_MID_LOW <= mid <= REWARD_MID_HIGH


def reward_eligible(book: Book, min_size: Decimal) -> bool:
    mid = adjusted_mid(book, min_size)
    return mid is not None and mid_in_reward_band(mid)


def order_score(max_spread: Decimal, distance: Decimal, size: Decimal) -> Decimal:
    if distance >= max_spread:
        return ZERO
    return ((max_spread - distance) / max_spread) ** 2 * size


def side_score(levels: tuple[Level, ...], mid: Decimal, max_spread: Decimal) -> Decimal:
    return sum((order_score(max_spread, abs(lvl.price - mid), lvl.qty) for lvl in levels), ZERO)


def combined_score(bid_score: Decimal, ask_score: Decimal, mid: Decimal) -> Decimal:
    if mid_in_reward_band(mid):
        return max(min(bid_score, ask_score), max(bid_score, ask_score) / SINGLE_SIDED_DIVISOR)
    return min(bid_score, ask_score)


def reward_rate(pool_usd: Decimal, ours: Decimal, competitors: Decimal) -> Decimal:
    if ours <= ZERO:
        return ZERO
    return pool_usd / SECONDS_PER_DAY * ours / (competitors + ours)


def bid_price(book: Book, window: IncentiveWindow, tick: Decimal) -> Decimal | None:
    mid = adjusted_mid(book, window.target_size)
    if mid is None or window.max_spread is None or not book.asks:
        return None
    qual_levels = [lvl for lvl in book.bids if lvl.qty >= window.target_size]
    if not qual_levels:
        return None
    top_lvl = qual_levels[0]
    raw_top = top_lvl.price
    top_bid = (raw_top // tick) * tick
    price = top_bid
    should_step_back = len(qual_levels) >= 2 or (
        len(qual_levels) == 1 and top_lvl.qty < Decimal("2") * window.target_size
    )
    if should_step_back:
        candidate = top_bid - tick
        max_cushioned_dist = window.max_spread * MAX_CUSHION_SPREAD_RATIO
        if (
            ZERO < candidate < top_bid
            and abs(candidate - mid) <= max_cushioned_dist
            and abs(candidate - mid) < window.max_spread
        ):
            price = candidate
            shield_target = Decimal("2") * window.target_size
            is_cliff_top = (
                len(qual_levels) >= 2
                and (top_bid - (qual_levels[1].price // tick) * tick) >= (Decimal("2") * tick)
            )
            if (
                (tick <= Decimal("0.001") or is_cliff_top)
                and sum((lvl.qty for lvl in book.bids if lvl.price >= price), ZERO) < shield_target
            ):
                for lvl in qual_levels[1:]:
                    lvl_px = (lvl.price // tick) * tick
                    if lvl_px >= top_bid:
                        continue
                    next_cand = lvl_px - tick
                    if (
                        ZERO < next_cand < price
                        and abs(next_cand - mid) <= max_cushioned_dist
                        and abs(next_cand - mid) < window.max_spread
                    ):
                        price = next_cand
                    elif (
                        is_cliff_top
                        and ZERO < lvl_px < price
                        and abs(lvl_px - mid) <= max_cushioned_dist
                        and abs(lvl_px - mid) < window.max_spread
                    ):
                        price = lvl_px
                    else:
                        break
                    if sum((b.qty for b in book.bids if b.price >= price), ZERO) >= shield_target:
                        break
    if price <= ZERO or price >= book.asks[0].price or abs(price - mid) >= window.max_spread:
        return None
    return price


def expected_bid_reward_sec(book: Book, window: IncentiveWindow, price: Decimal, size: Decimal) -> Decimal:
    mid = adjusted_mid(book, window.target_size)
    if mid is None or window.max_spread is None or size < window.target_size:
        return ZERO
    ours = combined_score(order_score(window.max_spread, abs(price - mid), size), ZERO, mid)
    return reward_rate(window.pool_usd, ours, side_score(book.bids, mid, window.max_spread))


def pair_max_sum(window: IncentiveWindow | None) -> Decimal:
    if window is not None and window.max_spread is not None and window.max_spread >= Decimal("0.04"):
        return WIDE_SPREAD_PAIR_MAX_SUM
    return PAIR_MAX_SUM


def pair_quote_prices(
    yes_book: Book,
    no_book: Book,
    window: IncentiveWindow,
    yes_tick: Decimal,
    no_tick: Decimal,
) -> tuple[Decimal, Decimal] | None:
    p_yes = bid_price(yes_book, window, yes_tick)
    p_no = bid_price(no_book, window, no_tick)
    if p_yes is None or p_no is None or p_yes + p_no >= Decimal("1"):
        return None
    target_max_sum = pair_max_sum(window)
    if p_yes + p_no > target_max_sum and window.max_spread is not None:
        mid_yes = adjusted_mid(yes_book, window.target_size)
        mid_no = adjusted_mid(no_book, window.target_size)
        if mid_yes is not None and mid_no is not None:
            for _ in range(25):
                if p_yes + p_no <= target_max_sum:
                    break
                cand_yes = p_yes - yes_tick
                cand_no = p_no - no_tick
                yes_ok = cand_yes > ZERO and abs(cand_yes - mid_yes) < window.max_spread
                no_ok = cand_no > ZERO and abs(cand_no - mid_no) < window.max_spread
                if yes_ok and (not no_ok or abs(cand_yes - mid_yes) <= abs(cand_no - mid_no)):
                    p_yes = cand_yes
                elif no_ok:
                    p_no = cand_no
                else:
                    break
    if p_yes + p_no >= Decimal("1"):
        return None
    return (p_yes, p_no)


def pair_size(
    cost_cap: Decimal,
    p_yes: Decimal,
    p_no: Decimal,
    min_size: Decimal,
    lot: Decimal,
    max_contracts: Decimal,
) -> Decimal:
    unit_cost = p_yes + p_no
    if unit_cost <= ZERO or lot <= ZERO or cost_cap <= ZERO:
        return ZERO
    max_by_cost = cost_cap / unit_cost
    max_allowed = min(max_by_cost, max_contracts)
    if max_allowed < min_size:
        return ZERO
    size = (max_allowed // lot) * lot
    while size * unit_cost > cost_cap or size > max_contracts:
        size -= lot
    if size < min_size:
        return ZERO
    return size


def competitor_q(yes_book: Book, no_book: Book, window: IncentiveWindow) -> Decimal:
    """Competitor score from bids on both books, avoiding double-counting NO asks."""
    if window.max_spread is None:
        return ZERO
    mid_y = adjusted_mid(yes_book, window.target_size)
    mid_n = adjusted_mid(no_book, window.target_size)
    if mid_y is None or mid_n is None:
        return ZERO
    q_one = side_score(yes_book.bids, mid_y, window.max_spread)
    q_two = side_score(no_book.bids, mid_n, window.max_spread)
    if mid_in_reward_band(mid_y):
        return min(q_one, q_two) + abs(q_one - q_two) / SINGLE_SIDED_DIVISOR
    return min(q_one, q_two)


def expected_pair_reward_sec(
    yes_book: Book,
    no_book: Book,
    window: IncentiveWindow,
    p_yes: Decimal,
    p_no: Decimal,
    size: Decimal,
) -> Decimal:
    if size < window.target_size or window.max_spread is None:
        return ZERO
    mid_y = adjusted_mid(yes_book, window.target_size)
    mid_n = adjusted_mid(no_book, window.target_size)
    if mid_y is None or mid_n is None:
        return ZERO
    s_one = order_score(window.max_spread, abs(p_yes - mid_y), size)
    s_two = order_score(window.max_spread, abs(p_no - mid_n), size)
    ours = combined_score(s_one, s_two, mid_y)
    competitors = competitor_q(yes_book, no_book, window)
    return reward_rate(window.pool_usd, ours, competitors)


def expected_single_reward_sec(
    own_book: Book,
    other_book: Book,
    window: IncentiveWindow,
    price: Decimal,
    size: Decimal,
) -> Decimal:
    if size < window.target_size or window.max_spread is None:
        return ZERO
    own_mid = adjusted_mid(own_book, window.target_size)
    if own_mid is None or not mid_in_reward_band(own_mid):
        return ZERO
    s = order_score(window.max_spread, abs(price - own_mid), size)
    ours = s / SINGLE_SIDED_DIVISOR
    competitors = competitor_q(own_book, other_book, window)
    return reward_rate(window.pool_usd, ours, competitors)


def strip_orders_from_book(book: Book, orders: list[LiveOrder]) -> Book:
    """Return a copy of book with our own resting orders subtracted so reallocation neither steps behind our own bid nor double-counts our liquidity as competitors."""
    if not orders:
        return book
    eps = Decimal("0.0001")
    bid_deduct: dict[Decimal, Decimal] = {}
    ask_deduct: dict[Decimal, Decimal] = {}
    for o in orders:
        if o.slug != book.slug or o.qty_open <= ZERO:
            continue
        if o.side in ("BUY_LONG", "BUY_SHORT"):
            bid_deduct[o.price] = bid_deduct.get(o.price, ZERO) + o.qty_open
        elif o.side in ("SELL_LONG", "SELL_SHORT"):
            ask_deduct[o.price] = ask_deduct.get(o.price, ZERO) + o.qty_open
    if not bid_deduct and not ask_deduct:
        return book
    new_bids: list[Level] = []
    for lvl in book.bids:
        rem = lvl.qty
        for opx, oqty in list(bid_deduct.items()):
            if oqty > ZERO and abs(lvl.price - opx) <= eps:
                used = min(rem, oqty)
                rem -= used
                bid_deduct[opx] -= used
        if rem > ZERO:
            new_bids.append(Level(price=lvl.price, qty=rem))
    new_asks: list[Level] = []
    for lvl in book.asks:
        rem = lvl.qty
        for opx, oqty in list(ask_deduct.items()):
            if oqty > ZERO and abs(lvl.price - opx) <= eps:
                used = min(rem, oqty)
                rem -= used
                ask_deduct[opx] -= used
        if rem > ZERO:
            new_asks.append(Level(price=lvl.price, qty=rem))
    return Book(slug=book.slug, bids=tuple(new_bids), asks=tuple(new_asks), ts_ms=book.ts_ms)

