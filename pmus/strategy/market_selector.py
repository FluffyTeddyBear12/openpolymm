from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pmus.models import Book, IncentiveWindow, LiveOrder
from pmus.shadow import estimate_lip_score
from pmus.strategy import global_rewards

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
    paired_slugs: frozenset[str] = frozenset()

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
    is_global: bool = False,
    ticks: dict[str, Decimal] | None = None,
    max_cost_per_market: Decimal | None = None,
) -> AllocationResult:
    candidates: list[CandidateSide] = []
    seen_window_keys: set[str] = set()
    global_skips = {"outside 0.10-0.90 mid or one-sided": 0, "no scoring bid price": 0, "over budget": 0, "below $1/day": 0}

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
        if is_global and not global_rewards.reward_eligible(book, w.target_size):
            global_skips["outside 0.10-0.90 mid or one-sided"] += 1
            continue
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

        existing_bids = sum(lvl.qty for lvl in book.bids)
        existing_asks = sum(lvl.qty for lvl in book.asks)
        col_bid = bid_px if bid_px > Decimal("0") else tick
        col_ask = (Decimal("1") - ask_px) if (Decimal("1") - ask_px) > Decimal("0") else tick

        if is_global:
            price = global_rewards.bid_price(book, w, (ticks or {}).get(w.slug, tick))
            if price is None:
                global_skips["no scoring bid price"] += 1
                continue
            size = w.target_size
            cost = size * price
            per_market_cap = max_cost_per_market if max_cost_per_market is not None else total_budget
            if cost <= Decimal("0") or cost > min(total_budget, per_market_cap) or size > max_contracts_cap:
                global_skips["over budget"] += 1
                continue
            rew = global_rewards.expected_bid_reward_sec(book, w, price, size)
            if rew * global_rewards.SECONDS_PER_DAY < global_rewards.MIN_DAILY_PAYOUT_USD:
                global_skips["below $1/day"] += 1
                continue
            candidates.append(
                CandidateSide(
                    slug=w.slug,
                    event_id=event_id,
                    side="bid",
                    price=price,
                    existing_qty=existing_bids,
                    needed_qty=size,
                    cost=cost,
                    expected_reward_sec=rew,
                    efficiency=rew / cost,
                    period=period or "early",
                    pool_usd=w.pool_usd,
                    duration_seconds=dur_secs,
                )
            )
        else:
            existing_bids_in_spread = sum(
                lvl.qty for lvl in book.bids
                if w.max_spread is None or (best_bid - lvl.price) <= w.max_spread
            )
            existing_asks_in_spread = sum(
                lvl.qty for lvl in book.asks
                if w.max_spread is None or (lvl.price - best_ask) <= w.max_spread
            )
            bids_qualify_existing = existing_bids_in_spread >= w.target_size
            asks_qualify_existing = existing_asks_in_spread >= w.target_size

            needed_bid = max(Decimal("0"), w.target_size - existing_bids)
            cost_bid = needed_bid * col_bid

            needed_ask = max(Decimal("0"), w.target_size - existing_asks)
            cost_ask = needed_ask * col_ask

            if Decimal("0") < needed_bid <= max_contracts_cap:
                if w.max_spread is None or asks_qualify_existing:
                    test_order_bid = [
                        LiveOrder(order_id="test-bid", slug=w.slug, side="BUY_LONG", price=bid_px, qty_open=needed_bid)
                    ]
                    snap_bid = estimate_lip_score(book, w, test_order_bid, tick, ts_ms=now_ms)
                    rew_bid = snap_bid.reward_usd

                    if rew_bid > Decimal("0") and cost_bid > Decimal("0") and (rew_bid * dur_secs) >= Decimal("1.00"):
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

            if Decimal("0") < needed_ask <= max_contracts_cap:
                if w.max_spread is None or bids_qualify_existing:
                    test_order_ask = [
                        LiveOrder(order_id="test-ask", slug=w.slug, side="SELL_SHORT", price=ask_px, qty_open=needed_ask)
                    ]
                    snap_ask = estimate_lip_score(book, w, test_order_ask, tick, ts_ms=now_ms)
                    rew_ask = snap_ask.reward_usd

                    if rew_ask > Decimal("0") and cost_ask > Decimal("0") and (rew_ask * dur_secs) >= Decimal("1.00"):
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

            if w.max_spread is not None and not asks_qualify_existing and not bids_qualify_existing:
                if Decimal("0") < needed_bid <= max_contracts_cap and Decimal("0") < needed_ask <= max_contracts_cap:
                    joint_cost = cost_bid + cost_ask
                    if joint_cost <= total_budget:
                        test_orders_both = [
                            LiveOrder(order_id="test-bid", slug=w.slug, side="BUY_LONG", price=bid_px, qty_open=needed_bid),
                            LiveOrder(order_id="test-ask", slug=w.slug, side="SELL_SHORT", price=ask_px, qty_open=needed_ask),
                        ]
                        snap_both = estimate_lip_score(book, w, test_orders_both, tick, ts_ms=now_ms)
                        rew_both = snap_both.reward_usd
                        if rew_both > Decimal("0") and joint_cost > Decimal("0") and (rew_both * dur_secs) >= Decimal("1.00"):
                            eff_both = rew_both / joint_cost
                            candidates.append(
                                CandidateSide(
                                    slug=w.slug,
                                    event_id=event_id,
                                    side="both",
                                    price=bid_px,
                                    existing_qty=existing_bids + existing_asks,
                                    needed_qty=needed_bid + needed_ask,
                                    cost=joint_cost,
                                    expected_reward_sec=rew_both,
                                    efficiency=eff_both,
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

    payout_floor = global_rewards.MIN_DAILY_PAYOUT_USD if is_global else Decimal("1.00")
    for c in candidates:
        if (c.expected_reward_sec * c.duration_seconds) < payout_floor:
            continue
        if c.expected_reward_sec <= Decimal("0"):
            continue
        if c.cost <= Decimal("0"):
            continue

        if c.slug in market_to_sides and c.side in market_to_sides[c.slug]:
            continue

        if c.slug not in market_to_sides:
            if c.event_id in selected_events:
                continue
            if len(selected_slugs) >= max_markets:
                continue

        if c.cost > remaining_budget:
            continue

        remaining_budget -= c.cost
        selected_candidates.append(c)
        if c.slug not in market_to_sides:
            selected_slugs.append(c.slug)
            selected_events.add(c.event_id)
            market_to_sides[c.slug] = set()

        if c.side == "both":
            market_to_sides[c.slug].add("bid")
            market_to_sides[c.slug].add("ask")
            book_s = books[c.slug]
            b_px = (book_s.bids[0].price // tick) * tick
            a_px = (book_s.asks[0].price // tick) * tick
            w_match = next((win for win in windows if win.slug == c.slug), None)
            target = w_match.target_size if w_match else Decimal("100")
            n_bid = max(Decimal("0"), target - sum(lvl.qty for lvl in book_s.bids))
            n_ask = max(Decimal("0"), target - sum(lvl.qty for lvl in book_s.asks))
            c_bid = n_bid * (b_px if b_px > Decimal("0") else tick)
            c_ask = n_ask * ((Decimal("1") - a_px) if (Decimal("1") - a_px) > Decimal("0") else tick)
            side_budgets[(c.slug, "bid")] = c_bid
            side_budgets[(c.slug, "ask")] = c_ask
            side_qtys[(c.slug, "bid")] = n_bid
            side_qtys[(c.slug, "ask")] = n_ask
        else:
            market_to_sides[c.slug].add(c.side)
            side_budgets[(c.slug, c.side)] = c.cost
            side_qtys[(c.slug, c.side)] = c.needed_qty

    unfunded_reason = None
    if not selected_candidates:
        if not candidates and is_global:
            skipped = ", ".join(f"{n} {why}" for why, n in global_skips.items() if n)
            unfunded_reason = f"No fundable global markets among {len(books)} books ({skipped or 'none eligible'})"
        elif not candidates:
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
    is_global: bool = False,
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
        is_global=is_global,
    )
    return result.selected_slugs
