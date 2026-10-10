from datetime import datetime
from decimal import Decimal

from pmus.models import Book, IncentiveWindow, Level, Market, Position, Quote

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

    if not book.bids or not book.asks:
        return []

    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    if best_bid >= best_ask:
        return []

    bid_price = (best_bid // market.tick) * market.tick
    ask_price = (best_ask // market.tick) * market.tick

    if window.max_spread is not None:
        if (ask_price - bid_price) > (Decimal("2") * window.max_spread):
            return []

    net = position.net_qty if position is not None else Decimal("0")
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
