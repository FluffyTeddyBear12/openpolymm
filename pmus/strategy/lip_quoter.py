import logging
from datetime import datetime
from decimal import Decimal
from enum import Enum

from pmus.models import Book, IncentiveWindow, Level, Market, Position, Quote
from pmus.strategy import book_guard, global_rewards
from pmus.strategy.condition_selector import PAIR_LEG_MIN_PRICE, SINGLE_SIDED_MIN_PRICE

logger = logging.getLogger(__name__)

LIQUID_UNWIND_MIN_BID_PRICE = Decimal("0.12")
LIQUID_UNWIND_MAX_SPREAD = Decimal("0.03")
LIQUID_UNWIND_MIN_SHARES = Decimal("150")
LIQUID_UNWIND_STOP_RATIO = Decimal("0.55")
ACTIVE_STOP_UNWIND_MID_RATIO = Decimal("0.85")
PAIR_LIVE_MAX_BID_PRICE = Decimal("1") - PAIR_LEG_MIN_PRICE


class UnwindRegime(str, Enum):
    HEDGED_PAIR_HOLD = "hedged_pair_hold"
    IN_SPREAD_SCRATCH = "in_spread_scratch"
    LIQUID_STOP_UNWIND = "liquid_stop_unwind"
    COLLAPSED_STUB_HOLD = "collapsed_stub_hold"


def _compute_stop_floor(ref_entry: Decimal, tick: Decimal) -> Decimal:
    raw_stop = max(LIQUID_UNWIND_MIN_BID_PRICE, ref_entry * LIQUID_UNWIND_STOP_RATIO)
    if tick <= Decimal("0"):
        return raw_stop
    stop_rem = raw_stop % tick
    if stop_rem == Decimal("0"):
        return (raw_stop // tick) * tick
    return ((raw_stop // tick) + Decimal("1")) * tick


def classify_unwind_regime(
    *,
    market: Market,
    book: Book,
    window: IncentiveWindow,
    unhedged_net: Decimal,
    ask_qty: Decimal,
    best_bid: Decimal,
    bid_price: Decimal,
    ask_price: Decimal,
    mid: Decimal,
    min_exit: Decimal,
    budget: Decimal,
    bid_budget: Decimal | None,
    avg_price: Decimal | None = None,
    tick: Decimal | None = None,
    allow_stop_unwind: bool | None = None,
) -> UnwindRegime:
    if unhedged_net < market.min_qty:
        return UnwindRegime.HEDGED_PAIR_HOLD
    if (
        min_exit <= Decimal("0")
        or window.max_spread is None
        or ask_price >= min_exit
        or (min_exit - mid) < window.max_spread
    ):
        return UnwindRegime.IN_SPREAD_SCRATCH

    if allow_stop_unwind is None:
        flatten_mode = (
            (bid_budget is not None and bid_budget <= Decimal("0"))
            or budget <= Decimal("0")
            or window.pool_usd <= Decimal("0")
        )
    else:
        flatten_mode = allow_stop_unwind
    eff_tick = tick if (tick is not None and tick > Decimal("0")) else (book.tick_size or market.tick)
    ref_entry = avg_price if (avg_price is not None and avg_price > Decimal("0")) else min_exit
    stop_floor = _compute_stop_floor(ref_entry, eff_tick)
    near_band = min(window.max_spread, LIQUID_UNWIND_MAX_SPREAD)
    near_bid_usd = sum(
        (lvl.price * lvl.qty for lvl in book.bids if (best_bid - lvl.price) <= near_band),
        Decimal("0"),
    )
    near_bid_qty = sum(
        (lvl.qty for lvl in book.bids if (best_bid - lvl.price) <= near_band),
        Decimal("0"),
    )
    if (
        flatten_mode
        and best_bid >= LIQUID_UNWIND_MIN_BID_PRICE
        and ask_price >= stop_floor
        and (ask_price - bid_price) <= near_band
        and near_bid_usd >= book_guard.MIN_NEAR_NOTIONAL_USD
        and near_bid_qty >= max(LIQUID_UNWIND_MIN_SHARES, ask_qty)
    ):
        return UnwindRegime.LIQUID_STOP_UNWIND
    return UnwindRegime.COLLAPSED_STUB_HOLD


def _guarded_bid_price(
    book: Book,
    window: IncentiveWindow,
    tick: Decimal,
    other_book: Book | None = None,
    max_bid_price: Decimal | None = None,
    min_bid_price: Decimal | None = None,
) -> Decimal | None:
    price = global_rewards.bid_price(book, window, tick)
    if price is None:
        return None
    if max_bid_price is not None and tick > Decimal("0"):
        clamped_cap = (max_bid_price // tick) * tick
        if clamped_cap < price:
            if clamped_cap <= Decimal("0"):
                return None
            if window.max_spread is not None:
                mid_curr = global_rewards.adjusted_mid(book, window.target_size)
                if mid_curr is not None and abs(clamped_cap - mid_curr) > window.max_spread:
                    return None
            price = clamped_cap
    if min_bid_price is not None and price < min_bid_price:
        return None
    reason = book_guard.quote_guard(book, window, price, tick, other_book=other_book)
    if reason is not None:
        logger.debug("Guard pulled %s bid: %s", book.slug, reason)
        return None
    return price


def desired_quotes(
    market: Market,
    book: Book,
    window: IncentiveWindow | None,
    position: Position | None,
    budget: Decimal,
    max_contracts_cap: Decimal | None = None,
    now: datetime | None = None,
    pre_start_cancel_seconds: int = 300,
    quote_live_periods: bool = False,
    bid_budget: Decimal | None = None,
    ask_budget: Decimal | None = None,
    is_global: bool = False,
    paired: bool = False,
    quote_size: Decimal | None = None,
    other_book: Book | None = None,
    max_bid_price: Decimal | None = None,
    min_bid_price: Decimal | None = None,
    other_position_qty: Decimal = Decimal("0"),
    allow_stop_unwind: bool | None = None,
) -> list[Quote]:
    if window is None or window.status.lower() != "active":
        return []

    period = getattr(window, "period", "").lower()
    if not quote_live_periods and period == "live":
        return []

    event_start = getattr(window, "event_start_time", None)
    if event_start is not None and now is not None:
        deadline = event_start.timestamp() - pre_start_cancel_seconds
        if now.timestamp() >= deadline:
            return []

    tick = book.tick_size or market.tick

    if (
        not book.bids
        and (book.asks or (position is not None and position.avg_price > Decimal("0")))
        and is_global
        and position is not None
        and position.net_qty >= market.min_qty
    ):
        hedged_qty = min(position.net_qty, max(Decimal("0"), other_position_qty))
        unhedged_net = max(Decimal("0"), position.net_qty - hedged_qty)
        cap = max_contracts_cap if max_contracts_cap is not None else unhedged_net
        ask_qty = (min(unhedged_net, cap) // market.min_qty) * market.min_qty
        if ask_qty >= market.min_qty:
            raw_exit = (
                position.avg_price
                if position.avg_price > Decimal("0")
                else (book.asks[0].price if book.asks else Decimal("0"))
            )
            rem = raw_exit % tick
            min_exit = raw_exit if rem == Decimal("0") else ((raw_exit // tick) + Decimal("1")) * tick
            ask_px = (book.asks[0].price // tick) * tick if book.asks else Decimal("0")
            sell_price = min(max(ask_px, min_exit), Decimal("1") - tick, Decimal("0.999"))
            if sell_price > Decimal("0"):
                return [
                    Quote(
                        slug=market.slug,
                        side="SELL_LONG",
                        price=sell_price,
                        qty=ask_qty,
                    )
                ]
        return []

    if not book.bids or not book.asks:
        return []

    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    if best_bid >= best_ask:
        return []

    bid_price = (best_bid // tick) * tick
    ask_price = (best_ask // tick) * tick

    net = position.net_qty if position is not None else Decimal("0")
    if window.max_spread is not None:
        if (ask_price - bid_price) > (Decimal("2") * window.max_spread):
            if not (is_global and net > Decimal("0")):
                return []

    cap = max_contracts_cap if max_contracts_cap is not None else Decimal("6000")

    if net > Decimal("0"):
        max_bid_contracts = max(Decimal("0"), cap - net)
        max_ask_contracts = net
    elif net < Decimal("0"):
        max_bid_contracts = abs(net)
        max_ask_contracts = max(Decimal("0"), cap - abs(net))
    else:
        max_bid_contracts = cap
        max_ask_contracts = cap

    quote_bid = max_bid_contracts >= market.min_qty
    quote_ask = max_ask_contracts >= market.min_qty

    if bid_budget is not None:
        side_budget_bid = bid_budget
        quote_bid = quote_bid and (side_budget_bid > Decimal("0"))
    else:
        side_budget_bid = (budget / Decimal("2")) if (quote_bid and quote_ask) else budget

    if ask_budget is not None:
        side_budget_ask = ask_budget
        if net <= Decimal("0"):
            quote_ask = quote_ask and (side_budget_ask > Decimal("0"))
    else:
        side_budget_ask = (budget / Decimal("2")) if (quote_bid and quote_ask) else budget

    if not quote_bid and not quote_ask:
        return []

    quotes: list[Quote] = []

    if is_global:
        effective_min_bid = (
            min_bid_price
            if min_bid_price is not None
            else (PAIR_LEG_MIN_PRICE if paired else SINGLE_SIDED_MIN_PRICE)
        )
        adverse_freeze = (
            net > Decimal("0")
            and position is not None
            and position.avg_price > Decimal("0")
        )
        if paired:
            effective_max_bid = (
                min(max_bid_price, PAIR_LIVE_MAX_BID_PRICE)
                if max_bid_price is not None
                else PAIR_LIVE_MAX_BID_PRICE
            )
            target_sz = quote_size if quote_size is not None else window.target_size
            if net < window.target_size and target_sz >= window.target_size:
                remaining = max(window.target_size, target_sz - net)
                price = _guarded_bid_price(
                    book,
                    window,
                    tick,
                    other_book=other_book,
                    max_bid_price=effective_max_bid,
                    min_bid_price=effective_min_bid,
                )
                qty = min(remaining, max_bid_contracts)
                budget_blocked = budget > Decimal("0") and adverse_freeze and (net * position.avg_price >= budget)
                adverse_blocked = (
                    adverse_freeze
                    and price is not None
                    and price <= (position.avg_price - Decimal("0.02"))
                )
                if (
                    price is not None
                    and quote_bid
                    and qty >= window.target_size
                    and not budget_blocked
                    and not adverse_blocked
                ):
                    quotes.append(Quote(slug=market.slug, side="BUY_LONG", price=price, qty=qty))
        else:
            spread_ok = window.max_spread is None or (ask_price - bid_price) <= (Decimal("2") * window.max_spread)
            if spread_ok and quote_bid and global_rewards.reward_eligible(book, window.target_size):
                price = _guarded_bid_price(
                    book,
                    window,
                    tick,
                    other_book=other_book,
                    max_bid_price=max_bid_price,
                    min_bid_price=effective_min_bid,
                )
                if price is not None and price > Decimal("0"):
                    held_cost = max(Decimal("0"), net) * (
                        position.avg_price
                        if (position is not None and position.avg_price > Decimal("0"))
                        else price
                    )
                    avail_budget = (
                        max(Decimal("0"), budget - held_cost)
                        if budget > Decimal("0")
                        else Decimal("0")
                    )
                    adverse_blocked = (
                        adverse_freeze
                        and price <= (position.avg_price - Decimal("0.02"))
                    )
                    budget_allows = budget <= Decimal("0") or held_cost < budget
                    if not adverse_blocked and budget_allows:
                        target_sz = quote_size if quote_size is not None else window.target_size
                        remaining = max(
                            window.target_size if net < window.target_size else Decimal("0"),
                            target_sz - max(Decimal("0"), net),
                        )
                        bid_qty = min(remaining, max_bid_contracts)
                        if budget > Decimal("0") and bid_qty * price > avail_budget:
                            bid_qty = min(bid_qty, ((avail_budget / price) // market.min_qty) * market.min_qty)
                        if bid_qty >= window.target_size:
                            quotes.append(Quote(slug=market.slug, side="BUY_LONG", price=price, qty=bid_qty))

        if net > Decimal("0"):
            hedged_qty = min(net, max(Decimal("0"), other_position_qty))
            unhedged_net = max(Decimal("0"), net - hedged_qty)
            ask_qty = min(unhedged_net, max_ask_contracts) // market.min_qty * market.min_qty
            if position is not None and position.avg_price > Decimal("0"):
                rem = position.avg_price % tick
                min_exit = (
                    (position.avg_price // tick) * tick
                    if rem == Decimal("0")
                    else ((position.avg_price // tick) + Decimal("1")) * tick
                )
            else:
                min_exit = Decimal("0")
            mid = global_rewards.adjusted_mid(book, window.target_size) or ((best_bid + best_ask) / Decimal("2"))
            avg_px = position.avg_price if position is not None else Decimal("0")
            regime = classify_unwind_regime(
                market=market,
                book=book,
                window=window,
                unhedged_net=unhedged_net,
                ask_qty=ask_qty,
                best_bid=best_bid,
                bid_price=bid_price,
                ask_price=ask_price,
                mid=mid,
                min_exit=min_exit,
                budget=budget,
                bid_budget=bid_budget,
                avg_price=avg_px,
                tick=tick,
                allow_stop_unwind=allow_stop_unwind,
            )
            if regime == UnwindRegime.HEDGED_PAIR_HOLD:
                return quotes
            if regime == UnwindRegime.LIQUID_STOP_UNWIND:
                stop_floor = _compute_stop_floor(avg_px, tick)
                sell_price = max(ask_price, stop_floor)
            elif regime == UnwindRegime.COLLAPSED_STUB_HOLD:
                sell_price = max(ask_price, min_exit)
            else:
                if window.max_spread is not None and (ask_price - bid_price) > window.max_spread and min_exit > Decimal("0"):
                    target_ask = max(min_exit, min(ask_price, bid_price + window.max_spread))
                    sell_price = (target_ask // tick) * tick
                else:
                    sell_price = max(ask_price, min_exit)
                    if ask_qty >= market.min_qty and (
                        ask_qty < window.target_size
                        or (min_exit > Decimal("0") and (ask_price - min_exit) >= Decimal("3") * tick)
                    ):
                        stepped_ask = max(min_exit, ask_price - tick)
                        if stepped_ask > best_bid:
                            sell_price = stepped_ask
                if min_exit > Decimal("0"):
                    sell_price = max(sell_price, min_exit)
            sell_price = min(sell_price, Decimal("1") - tick, Decimal("0.999"))
            if ask_qty >= market.min_qty and sell_price > best_bid:
                quotes.append(Quote(slug=market.slug, side="SELL_LONG", price=sell_price, qty=ask_qty))

        return quotes

    if quote_bid:
        collateral = bid_price
        if collateral > Decimal("0"):
            existing_bid_raw = sum(
                lvl.qty for lvl in book.bids
                if window.max_spread is None or (best_bid - lvl.price) <= window.max_spread
            )
            shortfall_bid = max(Decimal("0"), window.target_size - existing_bid_raw)
            if shortfall_bid <= Decimal("0"):
                bid_qty = Decimal("0")
            elif (shortfall_bid * collateral) <= side_budget_bid:
                bid_qty = min(shortfall_bid, max_bid_contracts) // market.min_qty * market.min_qty
            else:
                budget_qty = (side_budget_bid / collateral) // market.min_qty * market.min_qty
                bid_qty = min(budget_qty, max_bid_contracts) // market.min_qty * market.min_qty

            if bid_qty >= market.min_qty and bid_price < best_ask:
                side = "BUY_SHORT" if net < Decimal("0") else "BUY_LONG"
                quotes.append(Quote(slug=market.slug, side=side, price=bid_price, qty=bid_qty))

    if quote_ask:
        collateral = (Decimal("1") - ask_price) if net <= Decimal("0") else Decimal("0")
        existing_ask_raw = sum(
            lvl.qty for lvl in book.asks
            if window.max_spread is None or (lvl.price - best_ask) <= window.max_spread
        )
        shortfall_ask = max(Decimal("0"), window.target_size - existing_ask_raw)

        if net > Decimal("0"):
            ask_qty = min(net, max_ask_contracts) // market.min_qty * market.min_qty
        elif shortfall_ask <= Decimal("0"):
            ask_qty = Decimal("0")
        elif collateral == Decimal("0") or (shortfall_ask * collateral) <= side_budget_ask:
            ask_qty = min(shortfall_ask, max_ask_contracts) // market.min_qty * market.min_qty
        else:
            if collateral > Decimal("0"):
                budget_qty = (side_budget_ask / collateral) // market.min_qty * market.min_qty
            else:
                budget_qty = max_ask_contracts
            ask_qty = min(budget_qty, max_ask_contracts) // market.min_qty * market.min_qty

        if ask_qty >= market.min_qty and ask_price > best_bid:
            side = "SELL_LONG" if net > Decimal("0") else "SELL_SHORT"
            quotes.append(Quote(slug=market.slug, side=side, price=ask_price, qty=ask_qty))

    return quotes
