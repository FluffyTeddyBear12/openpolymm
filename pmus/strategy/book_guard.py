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
MAX_TICKS_ABOVE_SUPPORT = Decimal("2")
LAST_TRADE_TOLERANCE_SPREADS = Decimal("1.25")


def support_price(book: Book, depth: Decimal) -> Decimal:
    cum = Decimal("0")
    for lvl in book.bids:
        cum += lvl.qty
        if cum >= depth:
            return lvl.price
    return book.bids[-1].price


def last_trade_reference(book: Book) -> Decimal | None:
    last = book.last_trade_price
    if last is None or not book.bids or not book.asks:
        return last
    mid = (book.bids[0].price + book.asks[0].price) / Decimal("2")
    flipped = Decimal("1") - last
    return last if abs(last - mid) <= abs(flipped - mid) else flipped


def quote_guard(book: Book, window: IncentiveWindow, price: Decimal, tick: Decimal) -> str | None:
    total_depth = sum((lvl.qty for lvl in book.bids), Decimal("0"))
    min_depth = MIN_BOOK_DEPTH_TARGETS * window.target_size
    if total_depth < min_depth:
        return f"thin book: total bid depth {total_depth} below {min_depth}"
    support = support_price(book, SUPPORT_DEPTH_TARGETS * window.target_size)
    if price > support + MAX_TICKS_ABOVE_SUPPORT * tick:
        return f"bid {price} more than {MAX_TICKS_ABOVE_SUPPORT} ticks above support {support}"
    ref = last_trade_reference(book)
    if ref is not None and window.max_spread is not None and price > ref + LAST_TRADE_TOLERANCE_SPREADS * window.max_spread:
        return f"bid {price} above last trade reference {ref} by more than {window.max_spread}"
    return None
