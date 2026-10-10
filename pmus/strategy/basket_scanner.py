from decimal import Decimal
from typing import Any

from pmus.fees import taker_fee
from pmus.models import BasketOpp, Book

def scan(
    events: list[dict[str, Any]],
    books: dict[str, Book],
    min_edge: Decimal = Decimal("0.001"),
) -> list[BasketOpp]:
    opportunities: list[BasketOpp] = []

    for event in events:
        event_id = str(event.get("id", ""))
        markets = event.get("markets", [])
        if len(markets) <= 1:
            continue

        all_me = True
        for m in markets:
            is_me = getattr(m, "mutually_exclusive", None)
            if is_me is None and isinstance(m, dict):
                is_me = m.get("mutually_exclusive") or m.get("mutuallyExclusive")
            if not is_me:
                all_me = False
                break
        if not all_me:
            continue

        valid = True
        legs_bids: list[tuple[str, Decimal]] = []
        legs_asks: list[tuple[str, Decimal]] = []

        for m in markets:
            slug = m.slug if hasattr(m, "slug") else m.get("slug")
            if not slug or slug not in books:
                valid = False
                break
            book = books[slug]
            if not book.bids or not book.asks:
                valid = False
                break
            legs_bids.append((slug, book.bids[0].price))
            legs_asks.append((slug, book.asks[0].price))

        if not valid:
            continue

        sum_bids = sum(px for _, px in legs_bids)
        gross_short = sum_bids - Decimal("1.00")
        fees_short = sum(taker_fee(Decimal("1"), px) for _, px in legs_bids)
        net_short = gross_short - fees_short

        if net_short >= min_edge:
            opportunities.append(
                BasketOpp(
                    event_id=event_id,
                    direction="SHORT_ALL",
                    legs=tuple(legs_bids),
                    gross_edge=gross_short,
                    fees=fees_short,
                    net_edge=net_short,
                )
            )

        sum_asks = sum(px for _, px in legs_asks)
        gross_buy = Decimal("1.00") - sum_asks
        fees_buy = sum(taker_fee(Decimal("1"), px) for _, px in legs_asks)
        net_buy = gross_buy - fees_buy

        if net_buy >= min_edge:
            opportunities.append(
                BasketOpp(
                    event_id=event_id,
                    direction="BUY_ALL",
                    legs=tuple(legs_asks),
                    gross_edge=gross_buy,
                    fees=fees_buy,
                    net_edge=net_buy,
                )
            )

    return opportunities
