from datetime import datetime
from decimal import Decimal

from pmus.models import Book, IncentiveWindow
from pmus.shadow import _walk_side_score

def rank_markets(
    windows: list[IncentiveWindow],
    books: dict[str, Book],
    now: datetime,
    max_n: int = 4,
    test_size: Decimal = Decimal("50"),
    tick: Decimal = Decimal("0.001"),
) -> list[str]:
    scored_markets: list[tuple[str, Decimal]] = []
    seen_slugs: set[str] = set()

    for w in windows:
        if w.slug in seen_slugs or w.status.lower() != "active":
            continue
        if w.slug not in books:
            continue

        book = books[w.slug]
        if not book.bids or not book.asks:
            continue

        best_bid = book.bids[0].price
        best_ask = book.asks[0].price
        if best_bid >= best_ask:
            continue

        if w.end is not None:
            if not (w.start <= now < w.end):
                continue
            duration = Decimal(str(max(1, int((w.end - w.start).total_seconds()))))
        else:
            if now < w.start:
                continue
            duration = Decimal("86400")

        bid_sap, total_bid_score = _walk_side_score(book.bids, best_bid, w.target_size, w.discount, tick, is_bid=True)
        ask_sap, total_ask_score = _walk_side_score(book.asks, best_ask, w.target_size, w.discount, tick, is_bid=False)

        if w.max_spread is not None:
            if bid_sap is None or ask_sap is None:
                continue
            midpoint = (bid_sap + ask_sap) / Decimal("2")
            if (midpoint - bid_sap) > w.max_spread or (ask_sap - midpoint) > w.max_spread:
                continue
        else:
            if total_bid_score <= 0 and total_ask_score <= 0:
                continue

        pool_rate = w.pool_usd / duration

        our_bid_score = test_size
        our_ask_score = test_size
        our_bid_collateral = best_bid * test_size
        our_ask_collateral = (Decimal("1") - best_ask) * test_size
        total_collateral = our_bid_collateral + our_ask_collateral
        if total_collateral <= Decimal("0"):
            continue

        combined_bid = total_bid_score + our_bid_score
        combined_ask = total_ask_score + our_ask_score
        bid_share = (our_bid_score / combined_bid) if combined_bid > 0 else Decimal("0")
        ask_share = (our_ask_score / combined_ask) if combined_ask > 0 else Decimal("0")

        reward_sec = pool_rate * (bid_share * Decimal("0.5") + ask_share * Decimal("0.5"))
        efficiency = reward_sec / total_collateral

        scored_markets.append((w.slug, efficiency))
        seen_slugs.add(w.slug)

    scored_markets.sort(key=lambda item: item[1], reverse=True)
    return [slug for slug, _ in scored_markets[:max_n]]
