import logging
from datetime import datetime
from decimal import Decimal

from pmus.models import Book, IncentiveWindow, Level, Market, Position, Quote
from pmus.strategy import book_guard, global_rewards

logger = logging.getLogger(__name__)


def _guarded_bid_price(
    book: Book,
    window: IncentiveWindow,
    tick: Decimal,
    other_book: Book | None = None,
    max_bid_price: Decimal | None = None,
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
        net = position.net_qty
        cap = max_contracts_cap if max_contracts_cap is not None else net
        ask_qty = (min(net, cap) // market.min_qty) * market.min_qty
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
        adverse_freeze = (
            net > Decimal("0")
            and position is not None
            and position.avg_price > Decimal("0")
        )
        if paired:
            target_sz = quote_size if quote_size is not None else window.target_size
            if net < window.target_size and target_sz >= window.target_size:
                remaining = max(window.target_size, target_sz - net)
                price = _guarded_bid_price(
                    book,
                    window,
                    tick,
                    other_book=other_book,
                    max_bid_price=max_bid_price,
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
            ask_qty = min(net, max_ask_contracts) // market.min_qty * market.min_qty
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
            over_budget_pos = budget <= Decimal("0")
            underwater_non_scoring = (
                over_budget_pos
                and window.max_spread is not None
                and min_exit > Decimal("0")
                and ask_price < min_exit
            )
            live_unwind_non_scoring = (
                bid_budget is not None
                and bid_budget <= Decimal("0")
                and budget > Decimal("0")
                and window.max_spread is not None
                and window.pool_usd > Decimal("0")
                and global_rewards.mid_in_reward_band(mid)
                and ask_qty >= window.target_size
                and min_exit > Decimal("0")
                and (min_exit - mid) >= window.max_spread
            )
            if underwater_non_scoring:
                qual_bid = next((lvl.price for lvl in book.bids if lvl.qty >= window.target_size), best_bid)
                qual_ask = next(((lvl.price // tick) * tick for lvl in book.asks if lvl.qty >= window.target_size), ask_price)
                band_floor = ((Decimal("2") * global_rewards.REWARD_MID_LOW - qual_bid + (Decimal("4") * tick)) // tick) * tick
                loss_floor = min_exit
                max_scoring_ask = (((mid + window.max_spread) - (tick / Decimal("10"))) // tick) * tick
                candidate_ask = max(qual_ask, band_floor)
                clamped_ask = min(candidate_ask, max_scoring_ask) if max_scoring_ask > best_bid else candidate_ask
                sell_price = max(clamped_ask, loss_floor) if mid < global_rewards.REWARD_MID_LOW else clamped_ask
            elif live_unwind_non_scoring:
                loss_floor = min_exit
                max_scoring_ask = (
                    ((mid + window.max_spread) - (tick / Decimal("10")))
                    // tick
                ) * tick
                target_unwind_ask = min(min_exit, max_scoring_ask)
                candidate_sell = max(ask_price, loss_floor, target_unwind_ask)
                sell_price = (
                    candidate_sell
                    if (candidate_sell - mid) < window.max_spread
                    else max(ask_price, min_exit)
                )
            elif window.max_spread is not None and (ask_price - bid_price) > window.max_spread and min_exit > Decimal("0"):
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
