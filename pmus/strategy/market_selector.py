from datetime import datetime
from decimal import Decimal

from pmus.models import Book, IncentiveWindow, LiveOrder, Market
from pmus.shadow import estimate_lip_score
from pmus.strategy.lip_quoter import desired_quotes

def extract_event_id(slug: str) -> str:
    for sep in ("-neg-", "-pos-", "-spread-", "-total-"):
        if sep in slug:
            return slug.split(sep)[0]
    parts = slug.split("-")
    return "-".join(parts[:-1]) if len(parts) > 2 else slug

def rank_markets(
    windows: list[IncentiveWindow],
    books: dict[str, Book],
    now: datetime,
    max_n: int = 4,
    budget_per_market: Decimal = Decimal("15.00"),
    tick: Decimal = Decimal("0.001"),
    pre_start_cancel_seconds: int = 300,
    quote_live_periods: bool = False,
) -> list[str]:
    scored_markets: list[tuple[str, Decimal, Decimal]] = []
    seen_slugs: set[str] = set()

    for w in windows:
        if w.slug in seen_slugs or w.status.lower() != "active":
            continue
        if w.slug not in books:
            continue

        period = getattr(w, "period", "").lower()
        if not quote_live_periods and period == "live":
            continue

        event_start = getattr(w, "event_start_time", None)
        if event_start is not None:
            if now.timestamp() >= (event_start.timestamp() - pre_start_cancel_seconds):
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
        else:
            if now < w.start:
                continue

        dummy_market = Market(
            slug=w.slug,
            event_id=None,
            category="sports",
            tick=tick,
            min_qty=Decimal("1"),
            active=True,
            mutually_exclusive=False,
        )

        test_quotes = desired_quotes(
            market=dummy_market,
            book=book,
            window=w,
            position=None,
            budget=budget_per_market,
            max_contracts_cap=Decimal("6000"),
            now=now,
            pre_start_cancel_seconds=pre_start_cancel_seconds,
            quote_live_periods=quote_live_periods,
        )

        if not test_quotes:
            continue

        test_orders = [
            LiveOrder(order_id="test", slug=q.slug, side=q.side, price=q.price, qty_open=q.qty)
            for q in test_quotes
        ]

        now_ms = int(now.timestamp() * 1000)
        snap = estimate_lip_score(book, w, test_orders, tick, ts_ms=now_ms)
        reward_sec = snap.reward_usd

        total_col = Decimal("0")
        for q in test_quotes:
            px = q.price
            col = px if q.side in ("BUY_LONG", "BUY_SHORT") else (Decimal("1") - px)
            total_col += col * q.qty

        efficiency = (reward_sec / max(Decimal("0.01"), total_col)) if reward_sec > Decimal("0") else Decimal("0")

        scored_markets.append((w.slug, efficiency, reward_sec))
        seen_slugs.add(w.slug)

    scored_markets.sort(key=lambda item: (item[1], item[2]), reverse=True)

    selected_slugs: list[str] = []
    seen_events: set[str] = set()

    for slug, _, _ in scored_markets:
        event_id = extract_event_id(slug)
        if event_id in seen_events:
            continue
        seen_events.add(event_id)
        selected_slugs.append(slug)
        if len(selected_slugs) >= max_n:
            break

    if len(selected_slugs) < max_n:
        for slug, _, _ in scored_markets:
            if slug not in selected_slugs:
                selected_slugs.append(slug)
                if len(selected_slugs) >= max_n:
                    break

    return selected_slugs
