"""Admission guard for maker bids on reward books.

Support is the highest price at which cumulative bid depth from the top reaches
SUPPORT_DEPTH_TARGETS times the reward target size, or the lowest bid when the
whole book is shallower than that. A book with less than MIN_BOOK_DEPTH_TARGETS
times the target in total bids is too thin to rest on. A bid more than
MAX_TICKS_ABOVE_SUPPORT ticks above support is resting on lone orders over a
gap. A bid more than LAST_TRADE_TOLERANCE_SPREADS reward spreads above the last
trade is resting on a stale stack.

The CLOB reports one last_trade_price per market, identical on the YES and NO
books, quoting whichever outcome traded last. The reading on the same side of
the book's mid is taken as this outcome's last trade.
"""
from decimal import Decimal

from pmus.models import Book, IncentiveWindow

SUPPORT_DEPTH_TARGETS = Decimal("3")
MIN_BOOK_DEPTH_TARGETS = Decimal("2")
MIN_SUPPORT_SHARES = Decimal("150")
MIN_NEAR_NOTIONAL_USD = Decimal("40")
MAX_TICKS_ABOVE_SUPPORT = Decimal("2")
LAST_TRADE_TOLERANCE_SPREADS = Decimal("1.25")


def support_price(book: Book, depth: Decimal, top_discount: Decimal = Decimal("0")) -> Decimal:
    cum = Decimal("0")
    discount_top = len(book.bids) >= 2 and top_discount > Decimal("0")
    for idx, lvl in enumerate(book.bids):
        qty = lvl.qty - min(lvl.qty, top_discount) if (idx == 0 and discount_top) else lvl.qty
        cum += qty
        if cum >= depth:
            return lvl.price
    return book.bids[-1].price


def last_trade_reference(book: Book, support: Decimal | None = None) -> Decimal | None:
    last = book.last_trade_price
    if last is None or not book.bids or not book.asks:
        return last
    ref_bid = support if support is not None else book.bids[0].price
    mid = (ref_bid + book.asks[0].price) / Decimal("2")
    flipped = Decimal("1") - last
    return last if abs(last - mid) <= abs(flipped - mid) else flipped


def quote_guard(book: Book, window: IncentiveWindow, price: Decimal, tick: Decimal) -> str | None:
    total_depth = sum((lvl.qty for lvl in book.bids), Decimal("0"))
    min_depth = MIN_BOOK_DEPTH_TARGETS * window.target_size
    if total_depth < min_depth:
        return f"thin book: total bid depth {total_depth} below {min_depth}"
    multi_level = len(book.bids) >= 2
    support_depth = (
        max(SUPPORT_DEPTH_TARGETS * window.target_size, MIN_SUPPORT_SHARES)
        if multi_level
        else SUPPORT_DEPTH_TARGETS * window.target_size
    )
    support = support_price(
        book,
        support_depth,
        top_discount=window.target_size if multi_level else Decimal("0"),
    )
    if price > support + MAX_TICKS_ABOVE_SUPPORT * tick:
        return f"bid {price} more than {MAX_TICKS_ABOVE_SUPPORT} ticks above support {support}"
    if multi_level:
        near_notional = sum(
            (lvl.price * lvl.qty for lvl in book.bids if lvl.price >= price - MAX_TICKS_ABOVE_SUPPORT * tick),
            Decimal("0"),
        )
        if near_notional < MIN_NEAR_NOTIONAL_USD:
            return f"cliff book: near bid notional {near_notional} below {MIN_NEAR_NOTIONAL_USD}"
    ref = last_trade_reference(book, support=support)
    if ref is not None and window.max_spread is not None and price > ref + LAST_TRADE_TOLERANCE_SPREADS * window.max_spread:
        return f"bid {price} above last trade reference {ref} by more than {window.max_spread}"
    return None

