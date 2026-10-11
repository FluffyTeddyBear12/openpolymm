import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from pmus.fees import maker_rebate
from pmus.models import Book, IncentiveWindow, Level, LipSnapshot, LiveOrder, ShadowFill
from pmus.strategy import global_rewards

BID_SIDES = ("BUY_LONG", "BUY_SHORT")
ASK_SIDES = ("SELL_LONG", "SELL_SHORT")

def estimate_global_lip_score(
    book: Book,
    window: IncentiveWindow | None,
    resting_orders: list[LiveOrder],
    ts_ms: int | None = None,
) -> LipSnapshot:
    now_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
    zero = Decimal("0")
    mid = global_rewards.adjusted_mid(book, window.target_size) if window is not None else None
    if window is None or window.status.lower() != "active" or window.max_spread is None or mid is None:
        return LipSnapshot(
            ts_ms=now_ms,
            slug=book.slug,
            program_id=window.program_id if window else "none",
            trader_bid_score=zero,
            total_bid_score=zero,
            trader_ask_score=zero,
            total_ask_score=zero,
            qualifies=False,
            reward_usd=zero,
        )

    v = window.max_spread
    ours = [o for o in resting_orders if o.slug == book.slug and o.qty_open >= window.target_size]
    our_bid = sum((global_rewards.order_score(v, abs(o.price - mid), o.qty_open) for o in ours if o.side in BID_SIDES), zero)
    our_ask = sum((global_rewards.order_score(v, abs(o.price - mid), o.qty_open) for o in ours if o.side in ASK_SIDES), zero)
    comp_bid = global_rewards.side_score(book.bids, mid, v)
    comp_ask = global_rewards.side_score(book.asks, mid, v)

    our_q = global_rewards.combined_score(our_bid, our_ask, mid)
    competitors = (comp_bid if our_bid > zero else zero) + (comp_ask if our_ask > zero else zero)
    qualifies = our_q > zero

    return LipSnapshot(
        ts_ms=now_ms,
        slug=book.slug,
        program_id=window.program_id,
        trader_bid_score=our_bid,
        total_bid_score=comp_bid + our_bid,
        trader_ask_score=our_ask,
        total_ask_score=comp_ask + our_ask,
        qualifies=qualifies,
        reward_usd=global_rewards.reward_rate(window.pool_usd, our_q, competitors),
        bid_qualifies=qualifies and our_bid > zero,
        ask_qualifies=qualifies and our_ask > zero,
    )

def _evaluate_side_with_orders(
    book_levels: tuple[Level, ...],
    resting_orders: list[LiveOrder],
    is_bid: bool,
    target_size: Decimal,
    discount: Decimal,
    tick: Decimal,
    slug: str,
) -> tuple[Decimal | None, Decimal, Decimal, bool]:
    valid_sides = ("BUY_LONG", "BUY_SHORT") if is_bid else ("SELL_LONG", "SELL_SHORT")
    our_orders = [o for o in resting_orders if o.slug == slug and o.side in valid_sides and o.qty_open > Decimal("0")]

    price_existing: dict[Decimal, Decimal] = {}
    for lvl in book_levels:
        price_existing[lvl.price] = price_existing.get(lvl.price, Decimal("0")) + lvl.qty

    price_ours: dict[Decimal, Decimal] = {}
    for o in our_orders:
        price_ours[o.price] = price_ours.get(o.price, Decimal("0")) + o.qty_open

    all_prices = sorted(set(price_existing.keys()) | set(price_ours.keys()), reverse=is_bid)
    if not all_prices:
        return None, Decimal("0"), Decimal("0"), False

    best_price = all_prices[0]

    cum_raw = Decimal("0")
    total_score = Decimal("0")
    our_score = Decimal("0")
    sap: Decimal | None = None

    for px in all_prices:
        ex_qty = price_existing.get(px, Decimal("0"))
        our_qty = price_ours.get(px, Decimal("0"))

        ticks = max(0, int((best_price - px) / tick)) if is_bid else max(0, int((px - best_price) / tick))
        weight = discount ** ticks

        for owner, qty in (("existing", ex_qty), ("us", our_qty)):
            if qty <= Decimal("0"):
                continue

            if cum_raw >= target_size:
                cum_raw += qty
                continue

            needed = target_size - cum_raw
            take_qty = min(qty, needed)
            score = weight * take_qty
            total_score += score
            if owner == "us":
                our_score += score

            cum_raw += qty
            if cum_raw >= target_size and sap is None:
                sap = px

    qualifies = (cum_raw >= target_size) and (sap is not None)
    if not qualifies:
        return None, Decimal("0"), Decimal("0"), False

    return sap, total_score, our_score, True

def estimate_lip_score(
    book: Book,
    window: IncentiveWindow | None,
    resting_orders: list[LiveOrder],
    tick: Decimal,
    ts_ms: int | None = None,
) -> LipSnapshot:
    now_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
    now_dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)

    if window is None or window.status.lower() != "active":
        return LipSnapshot(
            ts_ms=now_ms,
            slug=book.slug,
            program_id=window.program_id if window else "none",
            trader_bid_score=Decimal("0"),
            total_bid_score=Decimal("0"),
            trader_ask_score=Decimal("0"),
            total_ask_score=Decimal("0"),
            qualifies=False,
            reward_usd=Decimal("0"),
            bid_qualifies=False,
            ask_qualifies=False,
        )

    if window.end is not None:
        duration_seconds = Decimal(str(max(1, int((window.end - window.start).total_seconds()))))
        in_time_window = window.start <= now_dt < window.end
    else:
        duration_seconds = Decimal("86400")
        in_time_window = now_dt >= window.start

    bid_sap, total_bid_score, our_bid_score, bid_qualifies = _evaluate_side_with_orders(
        book.bids, resting_orders, is_bid=True, target_size=window.target_size, discount=window.discount, tick=tick, slug=book.slug
    )
    ask_sap, total_ask_score, our_ask_score, ask_qualifies = _evaluate_side_with_orders(
        book.asks, resting_orders, is_bid=False, target_size=window.target_size, discount=window.discount, tick=tick, slug=book.slug
    )

    qualifies = False
    if in_time_window:
        if window.max_spread is not None:
            if bid_qualifies and ask_qualifies and bid_sap is not None and ask_sap is not None:
                midpoint = (bid_sap + ask_sap) / Decimal("2")
                if (midpoint - bid_sap) <= window.max_spread and (ask_sap - midpoint) <= window.max_spread:
                    qualifies = True
            if not qualifies:
                bid_qualifies = False
                ask_qualifies = False
        else:
            qualifies = bid_qualifies or ask_qualifies

    reward = Decimal("0")
    if in_time_window and qualifies:
        pool_per_second = window.pool_usd / duration_seconds
        side_pool_per_second = pool_per_second * Decimal("0.5")

        if bid_qualifies and total_bid_score > Decimal("0"):
            bid_share = our_bid_score / total_bid_score
            reward += side_pool_per_second * bid_share

        if ask_qualifies and total_ask_score > Decimal("0"):
            ask_share = our_ask_score / total_ask_score
            reward += side_pool_per_second * ask_share

    return LipSnapshot(
        ts_ms=now_ms,
        slug=book.slug,
        program_id=window.program_id,
        trader_bid_score=our_bid_score,
        total_bid_score=total_bid_score,
        trader_ask_score=our_ask_score,
        total_ask_score=total_ask_score,
        qualifies=qualifies,
        reward_usd=reward,
        bid_qualifies=bid_qualifies,
        ask_qualifies=ask_qualifies,
    )

def check_paper_fills(book: Book, open_orders: list[LiveOrder]) -> tuple[list[ShadowFill], list[LiveOrder]]:
    fills: list[ShadowFill] = []
    remaining: list[LiveOrder] = []

    best_bid = book.bids[0].price if book.bids else None
    best_ask = book.asks[0].price if book.asks else None

    for order in open_orders:
        filled = False
        if order.side in ("BUY_LONG", "BUY_SHORT") and best_ask is not None:
            if best_ask <= order.price:
                rebate = maker_rebate(order.qty_open, order.price)
                fill = ShadowFill(
                    fill_id=str(uuid.uuid4()),
                    order_id=order.order_id,
                    slug=order.slug,
                    side=order.side,
                    price=order.price,
                    qty=order.qty_open,
                    fee=-rebate,
                    is_maker=True,
                    ts_ms=book.ts_ms,
                )
                fills.append(fill)
                filled = True
        elif order.side in ("SELL_LONG", "SELL_SHORT") and best_bid is not None:
            if best_bid >= order.price:
                rebate = maker_rebate(order.qty_open, order.price)
                fill = ShadowFill(
                    fill_id=str(uuid.uuid4()),
                    order_id=order.order_id,
                    slug=order.slug,
                    side=order.side,
                    price=order.price,
                    qty=order.qty_open,
                    fee=-rebate,
                    is_maker=True,
                    ts_ms=book.ts_ms,
                )
                fills.append(fill)
                filled = True

        if not filled:
            remaining.append(order)

    return fills, remaining
