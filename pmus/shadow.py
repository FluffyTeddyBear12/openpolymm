import time
import uuid
from decimal import Decimal

from pmus.fees import maker_rebate
from pmus.models import Book, IncentiveWindow, Level, LipSnapshot, LiveOrder, ShadowFill

def _walk_side_score(
    levels: tuple[Level, ...],
    best_price: Decimal,
    target_size: Decimal,
    discount: Decimal,
    tick: Decimal,
    is_bid: bool,
) -> tuple[Decimal | None, Decimal]:
    cum_qty = Decimal("0")
    total_score = Decimal("0")
    sap: Decimal | None = None

    for lvl in levels:
        needed = target_size - cum_qty
        if needed <= Decimal("0"):
            break

        take_qty = min(lvl.qty, needed)
        ticks = (best_price - lvl.price) / tick if is_bid else (lvl.price - best_price) / tick
        ticks_int = max(0, int(ticks))
        weight = discount ** ticks_int
        total_score += weight * take_qty
        cum_qty += take_qty

        if cum_qty >= target_size:
            sap = lvl.price
            break

    if sap is None and levels:
        sap = levels[-1].price

    return sap, total_score

def estimate_lip_score(
    book: Book,
    window: IncentiveWindow | None,
    resting_orders: list[LiveOrder],
    tick: Decimal,
    ts_ms: int | None = None,
) -> LipSnapshot:
    now_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)

    if window is None or window.status.lower() != "active" or not book.bids or not book.asks:
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
        )

    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    if best_bid >= best_ask:
        return LipSnapshot(
            ts_ms=now_ms,
            slug=book.slug,
            program_id=window.program_id,
            trader_bid_score=Decimal("0"),
            total_bid_score=Decimal("0"),
            trader_ask_score=Decimal("0"),
            total_ask_score=Decimal("0"),
            qualifies=False,
            reward_usd=Decimal("0"),
        )

    bid_sap, total_bid_score = _walk_side_score(book.bids, best_bid, window.target_size, window.discount, tick, is_bid=True)
    ask_sap, total_ask_score = _walk_side_score(book.asks, best_ask, window.target_size, window.discount, tick, is_bid=False)

    qualifies = True
    if window.max_spread is not None:
        if bid_sap is None or ask_sap is None:
            qualifies = False
        else:
            midpoint = (bid_sap + ask_sap) / Decimal("2")
            if (midpoint - bid_sap) > window.max_spread or (ask_sap - midpoint) > window.max_spread:
                qualifies = False
    else:
        if total_bid_score <= 0 and total_ask_score <= 0:
            qualifies = False

    trader_bid_score = Decimal("0")
    trader_ask_score = Decimal("0")

    for order in resting_orders:
        if order.slug != book.slug or order.qty_open <= Decimal("0"):
            continue
        if order.side == "BUY_LONG":
            if bid_sap is not None and order.price >= bid_sap:
                ticks = max(0, int((best_bid - order.price) / tick))
                trader_bid_score += (window.discount ** ticks) * order.qty_open
        elif order.side == "SELL_LONG":
            if ask_sap is not None and order.price <= ask_sap:
                ticks = max(0, int((order.price - best_ask) / tick))
                trader_ask_score += (window.discount ** ticks) * order.qty_open

    combined_bid_score = total_bid_score + trader_bid_score
    combined_ask_score = total_ask_score + trader_ask_score

    pool_per_second = window.pool_usd / Decimal("86400")
    reward = Decimal("0")

    if qualifies:
        bid_share = (trader_bid_score / combined_bid_score) if combined_bid_score > 0 else Decimal("0")
        ask_share = (trader_ask_score / combined_ask_score) if combined_ask_score > 0 else Decimal("0")
        reward = pool_per_second * (bid_share * Decimal("0.5") + ask_share * Decimal("0.5"))

    return LipSnapshot(
        ts_ms=now_ms,
        slug=book.slug,
        program_id=window.program_id,
        trader_bid_score=trader_bid_score,
        total_bid_score=combined_bid_score,
        trader_ask_score=trader_ask_score,
        total_ask_score=combined_ask_score,
        qualifies=qualifies,
        reward_usd=reward,
    )

def check_paper_fills(book: Book, open_orders: list[LiveOrder]) -> tuple[list[ShadowFill], list[LiveOrder]]:
    fills: list[ShadowFill] = []
    remaining: list[LiveOrder] = []

    best_bid = book.bids[0].price if book.bids else None
    best_ask = book.asks[0].price if book.asks else None

    for order in open_orders:
        filled = False
        if order.side == "BUY_LONG" and best_ask is not None:
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
        elif order.side == "SELL_LONG" and best_bid is not None:
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
