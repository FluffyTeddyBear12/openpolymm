from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pmus.models import Book, IncentiveWindow, LiveOrder
from pmus.shadow import estimate_lip_score

@dataclass(frozen=True)
class CandidateSide:
    slug: str
    event_id: str
    side: str  # "bid" or "ask"
    price: Decimal
    existing_qty: Decimal
    needed_qty: Decimal
    cost: Decimal  # needed_qty * collateral
    expected_reward_sec: Decimal  # pool / duration * 0.5 * our_share
    efficiency: Decimal  # expected_reward_sec / cost
    period: str
    pool_usd: Decimal
    duration_seconds: Decimal

@dataclass
class AllocationResult:
    candidates: list[CandidateSide]
    selected_candidates: list[CandidateSide]
    selected_slugs: list[str]
    side_budgets: dict[tuple[str, str], Decimal]  # (slug, side) -> budget
    side_qtys: dict[tuple[str, str], Decimal]  # (slug, side) -> needed qty
    unfunded_reason: str | None = None

def extract_event_id(slug: str, slug_to_event_map: dict[str, str] | None = None) -> str:
    if slug_to_event_map and slug in slug_to_event_map:
        return slug_to_event_map[slug]
    for sep in ("-neg-", "-pos-", "-spread-", "-total-"):
        if sep in slug:
            return slug.split(sep)[0]
    parts = slug.split("-")
    return "-".join(parts[:-1]) if len(parts) > 2 else slug

def greedy_allocate(
    windows: list[IncentiveWindow],
    books: dict[str, Book],
    total_budget: Decimal,
    now: datetime,
    max_markets: int = 4,
    max_contracts_cap: Decimal = Decimal("6000"),
    tick: Decimal = Decimal("0.001"),
    pre_start_cancel_seconds: int = 300,
    quote_live_periods: bool = False,
    slug_to_event_map: dict[str, str] | None = None,
) -> AllocationResult:
    candidates: list[CandidateSide] = []
    seen_window_keys: set[str] = set()

    for w in windows:
        if w.status.lower() != "active":
            continue
        if w.slug in seen_window_keys:
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

        if w.end is not None:
            if not (w.start <= now < w.end):
                continue
        else:
            if now < w.start:
                continue

        book = books[w.slug]
        if not book.bids or not book.asks:
            continue

        best_bid = book.bids[0].price
        best_ask = book.asks[0].price
        if best_bid >= best_ask:
            continue

        bid_px = (best_bid // tick) * tick
        ask_px = (best_ask // tick) * tick

        if w.max_spread is not None:
            if (ask_px - bid_px) > (Decimal("2") * w.max_spread):
                continue

        dur_secs = Decimal(str((w.end - w.start).total_seconds())) if w.end else Decimal("86400")
        if dur_secs <= Decimal("0"):
            dur_secs = Decimal("86400")

        event_id = extract_event_id(w.slug, slug_to_event_map)
        now_ms = int(now.timestamp() * 1000)

        # Bid side candidate
        existing_bids = sum(lvl.qty for lvl in book.bids)
        needed_bid = max(Decimal("0"), w.target_size - existing_bids)
        if Decimal("0") < needed_bid <= max_contracts_cap:
            col_bid = bid_px
            if col_bid <= Decimal("0"):
                col_bid = tick
            cost_bid = needed_bid * col_bid

            test_order_bid = [
                LiveOrder(order_id="test-bid", slug=w.slug, side="BUY_LONG", price=bid_px, qty_open=needed_bid)
            ]
            snap_bid = estimate_lip_score(book, w, test_order_bid, tick, ts_ms=now_ms)
            rew_bid = snap_bid.reward_usd

            if rew_bid > Decimal("0") and cost_bid > Decimal("0"):
                eff_bid = rew_bid / cost_bid
                candidates.append(
                    CandidateSide(
                        slug=w.slug,
                        event_id=event_id,
                        side="bid",
                        price=bid_px,
                        existing_qty=existing_bids,
                        needed_qty=needed_bid,
                        cost=cost_bid,
                        expected_reward_sec=rew_bid,
                        efficiency=eff_bid,
                        period=period or "early",
                        pool_usd=w.pool_usd,
                        duration_seconds=dur_secs,
                    )
                )

        # Ask side candidate
        existing_asks = sum(lvl.qty for lvl in book.asks)
        needed_ask = max(Decimal("0"), w.target_size - existing_asks)
        if Decimal("0") < needed_ask <= max_contracts_cap:
            col_ask = Decimal("1") - ask_px
            if col_ask <= Decimal("0"):
                col_ask = tick
            cost_ask = needed_ask * col_ask

            test_order_ask = [
                LiveOrder(order_id="test-ask", slug=w.slug, side="SELL_SHORT", price=ask_px, qty_open=needed_ask)
            ]
            snap_ask = estimate_lip_score(book, w, test_order_ask, tick, ts_ms=now_ms)
            rew_ask = snap_ask.reward_usd

            if rew_ask > Decimal("0") and cost_ask > Decimal("0"):
                eff_ask = rew_ask / cost_ask
                candidates.append(
                    CandidateSide(
                        slug=w.slug,
                        event_id=event_id,
                        side="ask",
                        price=ask_px,
                        existing_qty=existing_asks,
                        needed_qty=needed_ask,
                        cost=cost_ask,
                        expected_reward_sec=rew_ask,
                        efficiency=eff_ask,
                        period=period or "early",
                        pool_usd=w.pool_usd,
                        duration_seconds=dur_secs,
                    )
                )

        seen_window_keys.add(w.slug)

    candidates.sort(key=lambda c: (c.efficiency, c.expected_reward_sec), reverse=True)

    remaining_budget = total_budget
    selected_candidates: list[CandidateSide] = []
    selected_slugs: list[str] = []
    selected_events: set[str] = set()
    market_to_sides: dict[str, set[str]] = {}
    side_budgets: dict[tuple[str, str], Decimal] = {}
    side_qtys: dict[tuple[str, str], Decimal] = {}

    for c in candidates:
        if c.expected_reward_sec <= Decimal("0"):
            continue
        if c.cost <= Decimal("0"):
            continue

        if c.slug not in market_to_sides:
            if c.event_id in selected_events:
                # Strictly enforce at most 1 market per event
                continue
            if len(selected_slugs) >= max_markets:
                continue

        if c.cost > remaining_budget:
            # Skip candidates that cannot be fully funded
            continue

        remaining_budget -= c.cost
        selected_candidates.append(c)
        if c.slug not in market_to_sides:
            selected_slugs.append(c.slug)
            selected_events.add(c.event_id)
            market_to_sides[c.slug] = set()
        market_to_sides[c.slug].add(c.side)
        side_budgets[(c.slug, c.side)] = c.cost
        side_qtys[(c.slug, c.side)] = c.needed_qty

    unfunded_reason = None
    if not selected_candidates:
        if not candidates:
            unfunded_reason = "No active candidate markets with positive expected reward"
        else:
            min_cost = min(c.cost for c in candidates)
            unfunded_reason = (
                f"Total budget ${total_budget:.2f} insufficient: minimum completion cost is ${min_cost:.2f}"
            )

    return AllocationResult(
        candidates=candidates,
        selected_candidates=selected_candidates,
        selected_slugs=selected_slugs,
        side_budgets=side_budgets,
        side_qtys=side_qtys,
        unfunded_reason=unfunded_reason,
    )

def rank_markets(
    windows: list[IncentiveWindow],
    books: dict[str, Book],
    now: datetime,
    max_n: int = 4,
    budget_per_market: Decimal = Decimal("15.00"),
    tick: Decimal = Decimal("0.001"),
    pre_start_cancel_seconds: int = 300,
    quote_live_periods: bool = False,
    slug_to_event_map: dict[str, str] | None = None,
    total_budget: Decimal | None = None,
) -> list[str]:
    budget = total_budget if total_budget is not None else (budget_per_market * Decimal(str(max_n)))
    result = greedy_allocate(
        windows=windows,
        books=books,
        total_budget=budget,
        now=now,
        max_markets=max_n,
        tick=tick,
        pre_start_cancel_seconds=pre_start_cancel_seconds,
        quote_live_periods=quote_live_periods,
        slug_to_event_map=slug_to_event_map,
    )
    return result.selected_slugs
