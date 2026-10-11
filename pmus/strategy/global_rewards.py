"""Polymarket Global liquidity-reward scoring.

Order score S = ((v - s) / v)^2 * size, where v is the max incentive spread and
s the distance from the size-adjusted midpoint. Orders below the minimum size
or at/after v score zero. With the midpoint in [0.10, 0.90] a single-sided
book scores S/3; outside that band only two-sided liquidity scores.
"""
import os
from decimal import Decimal

from pmus.models import Book, IncentiveWindow, Level

REWARD_MID_LOW = Decimal("0.10")
REWARD_MID_HIGH = Decimal("0.90")
SINGLE_SIDED_DIVISOR = Decimal("3")
SECONDS_PER_DAY = Decimal("86400")
MIN_DAILY_PAYOUT_USD = Decimal(os.getenv("PMUS_GLOBAL_MIN_DAILY_REWARD", "1.00"))
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
    """Price the bot posts: join the best bid on tick, only if it scores and does not cross."""
    mid = adjusted_mid(book, window.target_size)
    if mid is None or window.max_spread is None:
        return None
    price = (book.bids[0].price // tick) * tick
    if price <= ZERO or price >= book.asks[0].price or abs(price - mid) >= window.max_spread:
        return None
    return price


def expected_bid_reward_sec(book: Book, window: IncentiveWindow, price: Decimal, size: Decimal) -> Decimal:
    mid = adjusted_mid(book, window.target_size)
    if mid is None or window.max_spread is None or size < window.target_size:
        return ZERO
    ours = combined_score(order_score(window.max_spread, abs(price - mid), size), ZERO, mid)
    return reward_rate(window.pool_usd, ours, side_score(book.bids, mid, window.max_spread))
