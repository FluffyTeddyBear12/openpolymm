from decimal import Decimal

from pmus.models import Book, IncentiveWindow, Level, Market, Position, Quote

def _size_adjusted_price(levels: tuple[Level, ...], target_size: Decimal) -> Decimal | None:
    cum_qty = Decimal("0")
    for lvl in levels:
        cum_qty += lvl.qty
        if cum_qty >= target_size:
            return lvl.price
    return None

def desired_quotes(
    market: Market,
    book: Book,
    window: IncentiveWindow | None,
    position: Position | None,
    budget: Decimal,
    max_position_cap: Decimal | None = None,
) -> list[Quote]:
    if window is None or window.status.lower() != "active":
        return []

    if not book.bids or not book.asks:
        return []

    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    if best_bid >= best_ask:
        return []

    if window.max_spread is not None:
        bid_sap = _size_adjusted_price(book.bids, window.target_size)
        ask_sap = _size_adjusted_price(book.asks, window.target_size)
        if bid_sap is None or ask_sap is None:
            return []
        midpoint = (bid_sap + ask_sap) / Decimal("2")
        if (midpoint - bid_sap) > window.max_spread or (ask_sap - midpoint) > window.max_spread:
            return []

    net_qty = position.net_qty if position is not None else Decimal("0")
    cap = max_position_cap if max_position_cap is not None else Decimal("50")

    quote_bid = True
    quote_ask = True
    if net_qty >= cap:
        quote_bid = False
    elif net_qty <= -cap:
        quote_ask = False

    if not quote_bid and not quote_ask:
        return []

    side_budget = (budget / Decimal("2")) if (quote_bid and quote_ask) else budget
    quotes: list[Quote] = []

    if quote_bid:
        bid_price = (best_bid // market.tick) * market.tick
        collateral = bid_price
        if collateral > Decimal("0"):
            qty = (side_budget / collateral) // market.min_qty * market.min_qty
            if qty >= market.min_qty and bid_price < best_ask:
                quotes.append(Quote(slug=market.slug, side="BUY_LONG", price=bid_price, qty=qty))

    if quote_ask:
        ask_price = (best_ask // market.tick) * market.tick
        collateral = Decimal("1") - ask_price
        if collateral > Decimal("0"):
            qty = (side_budget / collateral) // market.min_qty * market.min_qty
            if qty >= market.min_qty and ask_price > best_bid:
                quotes.append(Quote(slug=market.slug, side="SELL_LONG", price=ask_price, qty=qty))

    return quotes
