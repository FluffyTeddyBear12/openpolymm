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
MULTI_LEVEL_SUPPORT_TARGETS = Decimal("4")
MIN_BOOK_DEPTH_TARGETS = Decimal("2")
MIN_SUPPORT_SHARES = Decimal("250")
MIN_NEAR_NOTIONAL_USD = Decimal("75")
MIN_FRONT_SHIELD_USD = Decimal("25")
ASK_OVERHANG_MIN_ASK_USD = Decimal("500")
MIN_BID_TO_ASK_USD_RATIO = Decimal("0.08")
MAX_TICKS_ABOVE_SUPPORT = Decimal("2")
LAST_TRADE_TOLERANCE_SPREADS = Decimal("1.25")


def _post_discount_depth(book: Book, top_discount: Decimal = Decimal("0")) -> Decimal:
    cum = Decimal("0")
    discount_top = len(book.bids) >= 2 and top_discount > Decimal("0")
    for idx, lvl in enumerate(book.bids):
        qty = lvl.qty - min(lvl.qty, top_discount) if (idx == 0 and discount_top) else lvl.qty
        cum += qty
    return cum


def support_price(book: Book, depth: Decimal, top_discount: Decimal = Decimal("0")) -> Decimal:
    cum = Decimal("0")
    discount_top = len(book.bids) >= 2 and top_discount > Decimal("0")
    for idx, lvl in enumerate(book.bids):
        qty = lvl.qty - min(lvl.qty, top_discount) if (idx == 0 and discount_top) else lvl.qty
        cum += qty
        if cum >= depth:
            return lvl.price
    if discount_top:
        return Decimal("0")
    return book.bids[-1].price


def last_trade_reference(book: Book, support: Decimal | None = None) -> Decimal | None:
    last = book.last_trade_price
    if last is None or not book.bids or not book.asks:
        return last
    ref_bid = support if (support is not None and support > Decimal("0")) else book.bids[0].price
    mid = (ref_bid + book.asks[0].price) / Decimal("2")
    flipped = Decimal("1") - last
    return last if abs(last - mid) <= abs(flipped - mid) else flipped


def quote_guard(
    book: Book,
    window: IncentiveWindow,
    price: Decimal,
    tick: Decimal,
    other_book: Book | None = None,
    hysteresis: Decimal = Decimal("1.0"),
) -> str | None:
    if other_book is not None and other_book.bids:
        opp_bid = other_book.bids[0].price
        if price + opp_bid > Decimal("0.99"):
            return f"cross-book arbitrage: price {price} + opp bid {opp_bid} > 0.99"
    total_depth = sum((lvl.qty for lvl in book.bids), Decimal("0"))
    min_depth = MIN_BOOK_DEPTH_TARGETS * window.target_size
    if total_depth < min_depth:
        return f"thin book: total bid depth {total_depth} below {min_depth}"
    multi_level = len(book.bids) >= 2
    top_discount = window.target_size if multi_level else Decimal("0")
    support_depth = (
        max(MULTI_LEVEL_SUPPORT_TARGETS * window.target_size, MIN_SUPPORT_SHARES)
        if multi_level
        else SUPPORT_DEPTH_TARGETS * window.target_size
    )
    if multi_level:
        post_disc_depth = _post_discount_depth(book, top_discount=top_discount)
        if post_disc_depth < support_depth:
            return f"thin book: post-discount support depth {post_disc_depth} below {support_depth}"
    max_support_gap = max(
        MAX_TICKS_ABOVE_SUPPORT * tick,
        Decimal("0.02") if multi_level else Decimal("0"),
    )
    support = support_price(
        book,
        support_depth,
        top_discount=top_discount,
    )
    if price > support + max_support_gap:
        return f"bid {price} more than {MAX_TICKS_ABOVE_SUPPORT} ticks above support {support}"
    near_bid_usd = sum(
        (lvl.price * lvl.qty for lvl in book.bids if lvl.price >= price - max_support_gap),
        Decimal("0"),
    )
    if book.asks and book.bids:
        top_ask_usd = book.asks[0].price * book.asks[0].qty
        top_bid_usd = book.bids[0].price * book.bids[0].qty
        ask_band = max(Decimal("2") * tick, Decimal("0.02"))
        near_ask_usd = sum(
            (lvl.price * lvl.qty for lvl in book.asks if lvl.price <= book.asks[0].price + ask_band),
            Decimal("0"),
        )
        min_ratio = MIN_BID_TO_ASK_USD_RATIO * hysteresis
        if top_ask_usd >= ASK_OVERHANG_MIN_ASK_USD and max(top_bid_usd, near_bid_usd) < top_ask_usd * min_ratio:
            return f"ask overhang: top bid notional {top_bid_usd} vs top ask {top_ask_usd}"
        if near_ask_usd >= ASK_OVERHANG_MIN_ASK_USD and near_bid_usd < near_ask_usd * min_ratio:
            return f"ask overhang: near bid notional {near_bid_usd} vs near ask {near_ask_usd}"
    if multi_level:
        if (
            price >= book.bids[0].price - tick
            and (book.bids[0].price - book.bids[1].price) > max(Decimal("3") * tick, Decimal("0.03"))
        ):
            return f"cliff book: top bid gap {book.bids[0].price - book.bids[1].price} above 3 ticks"
        for i in range(len(book.bids) - 1):
            if book.bids[i].price <= support or book.bids[i].price < price - tick:
                break
            gap = book.bids[i].price - book.bids[i + 1].price
            if gap > max(Decimal("3") * tick, Decimal("0.03")):
                return f"cliff book: bid gap {gap} between {book.bids[i].price} and {book.bids[i + 1].price}"
        min_near_usd = MIN_NEAR_NOTIONAL_USD * hysteresis
        if near_bid_usd < min_near_usd:
            return f"cliff book: near bid notional {near_bid_usd} below {min_near_usd}"
        ahead_qty = sum((lvl.qty for lvl in book.bids if lvl.price > price), Decimal("0"))
        at_or_ahead_qty = sum((lvl.qty for lvl in book.bids if lvl.price >= price), Decimal("0"))
        req_ahead = window.target_size * hysteresis
        req_at_or_ahead = Decimal("2") * window.target_size * hysteresis
        if ahead_qty < req_ahead and at_or_ahead_qty < req_at_or_ahead:
            return f"thin front shield: ahead {ahead_qty} and at_or_ahead {at_or_ahead_qty} below target {req_ahead}"
        at_or_ahead_usd = sum((lvl.price * lvl.qty for lvl in book.bids if lvl.price >= price), Decimal("0"))
        min_shield_usd = MIN_FRONT_SHIELD_USD * hysteresis
        if at_or_ahead_usd < min_shield_usd:
            return f"thin front shield: notional {at_or_ahead_usd} below {min_shield_usd}"
    ref = last_trade_reference(book, support=support)
    if ref is not None and window.max_spread is not None and price > ref + LAST_TRADE_TOLERANCE_SPREADS * window.max_spread:
        return f"bid {price} above last trade reference {ref} by more than {window.max_spread}"
    return None

