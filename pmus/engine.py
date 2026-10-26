import dataclasses
import inspect
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from pmus.config import Settings
from pmus.ledger import Ledger
from pmus.models import Book, IncentiveWindow, LiveOrder, Market, Position, Quote, ShadowFill
from pmus.notify import Notifier
from pmus.risk import RiskManager
from pmus.shadow import check_paper_fills, estimate_global_lip_score, estimate_global_pair_score, estimate_lip_score
from pmus.strategy import global_rewards
from pmus.strategy.condition_selector import allocate_conditions, targets_to_allocation
from pmus.strategy.lip_quoter import ACTIVE_STOP_UNWIND_MID_RATIO, desired_quotes
from pmus.strategy.market_selector import AllocationResult, greedy_allocate
from pmus.venue import AuthenticatedClient, PublicGatewayClient

MarketState = Literal["IDLE", "QUOTING", "PAUSED", "FLATTENING"]

CANDIDATE_BOOK_LIMIT = 600
FALLBACK_BOOK_CONDITION_LIMIT = 150
FILL_LOOKBACK_SECONDS = 60
FILL_WINDOW_SECONDS = 3600
LOCAL_FILL_POSITION_TTL_SECONDS = 60.0
REALLOCATE_SECONDS = 3600
YIELD_COLLAPSE_COOLDOWN_SECONDS = 180.0
YIELD_COLLAPSE_RATIO = Decimal("0.55")
YIELD_COLLAPSE_TICKS = 15
CAPITAL_UTILIZATION_RATIO = Decimal("0.75")
CAPITAL_UNDERDEPLOYED_TICKS = 30
DEGRADED_SLUG_COOLDOWN_SECONDS = 300.0
LOSS_SLUG_COOLDOWN_SECONDS = 86400.0
MANUAL_SELL_EDIT_GRACE_SECONDS = 90.0
SELL_HYSTERESIS_BAND = Decimal("0.015")
RETAINED_QTY_TOLERANCE_RATIO = Decimal("0.20")
RETAINED_QTY_TOLERANCE_MAX_SHARES = Decimal("15")


def _qty_within_tolerance(
    existing_qty: Decimal,
    desired_qty: Decimal,
    min_keep_qty: Decimal = Decimal("0"),
) -> bool:
    if existing_qty < max(Decimal("1"), min_keep_qty) or desired_qty < existing_qty:
        return False
    qty_diff = desired_qty - existing_qty
    return (
        qty_diff <= RETAINED_QTY_TOLERANCE_MAX_SHARES
        and qty_diff <= (desired_qty * RETAINED_QTY_TOLERANCE_RATIO)
    )


def reconcile_quotes(
    current_orders: list[LiveOrder],
    desired_quotes_list: list[Quote],
    min_keep_qty: Decimal = Decimal("0"),
) -> tuple[list[Quote], list[LiveOrder], list[LiveOrder]]:
    to_keep: list[LiveOrder] = []
    to_cancel: list[LiveOrder] = []
    to_create: list[Quote] = []

    remaining_current = list(current_orders)

    for quote in desired_quotes_list:
        matched_idx: int | None = None
        for i, order in enumerate(remaining_current):
            if order.side == quote.side and order.price == quote.price and order.qty_open == quote.qty:
                matched_idx = i
                break
        if matched_idx is None and quote.side in ("BUY", "BUY_LONG", "BUY_SHORT"):
            for i, order in enumerate(remaining_current):
                if (
                    order.side == quote.side
                    and order.price == quote.price
                    and _qty_within_tolerance(order.qty_open, quote.qty, min_keep_qty=min_keep_qty)
                ):
                    matched_idx = i
                    break
        if matched_idx is not None:
            to_keep.append(remaining_current.pop(matched_idx))
        else:
            to_create.append(quote)

    to_cancel.extend(remaining_current)
    return to_create, to_cancel, to_keep

def update_position_from_fill(current_pos: Position, fill_side: str, fill_price: Decimal, fill_qty: Decimal) -> Position:
    old_qty = current_pos.net_qty
    old_avg = current_pos.avg_price

    if fill_side in ("BUY_LONG", "BUY_SHORT"):
        new_qty = old_qty + fill_qty
        if new_qty > 0 and old_qty >= 0:
            new_avg = (old_qty * old_avg + fill_qty * fill_price) / new_qty
        elif new_qty > 0 and old_qty < 0:
            new_avg = fill_price
        elif new_qty == 0:
            new_avg = Decimal("0")
        else:
            new_avg = old_avg
    else:
        new_qty = old_qty - fill_qty
        if new_qty < 0 and old_qty <= 0:
            new_avg = (abs(old_qty) * old_avg + fill_qty * fill_price) / abs(new_qty)
        elif new_qty < 0 and old_qty > 0:
            new_avg = fill_price
        elif new_qty == 0:
            new_avg = Decimal("0")
        else:
            new_avg = old_avg

    return dataclasses.replace(current_pos, net_qty=new_qty, avg_price=new_avg)

def resolve_market(client: Any, slug: str) -> Market:
    market = None
    if hasattr(client, "get_market"):
        res = client.get_market(slug)
        if isinstance(res, Market):
            market = res
    if market is None and hasattr(client, "fetch_market"):
        res = client.fetch_market(slug)
        if isinstance(res, Market):
            market = res
    if market is not None:
        return market
    return Market(
        slug=slug,
        event_id=None,
        category="sports",
        tick=Decimal("0.001"),
        min_qty=Decimal("1"),
        active=True,
        mutually_exclusive=False,
    )


def _effective_max_pairs(max_markets: int) -> int:
    if max_markets <= 4:
        return max(1, max_markets)
    return max(1, max_markets // 2)


def calculate_num_pairs(budget: Decimal, max_limit: int = 8) -> int:
    if max_limit <= 0 or budget <= Decimal("0"):
        return 0
    if budget < Decimal("72"):
        target = max(1, int(budget // Decimal("36")))
    elif budget < Decimal("200"):
        target = max(2, int(budget // Decimal("42")))
    else:
        target = max(3, int(budget // Decimal("48")))
    return min(max_limit, target)


def get_pair_tier_threshold(pairs: int) -> Decimal:
    if pairs <= 1:
        return Decimal("0.00")
    if pairs == 2:
        return Decimal("72.00")
    if pairs in (3, 4):
        return Decimal(str(pairs * 42))
    return Decimal(str(pairs * 48))


def allocate_markets(
    settings: Settings,
    client: Any,
    logger: logging.Logger,
    available_budget: Decimal | None = None,
    positions: dict[str, Position] | None = None,
    open_orders: list[LiveOrder] | None = None,
    excluded_slugs: set[str] | frozenset[str] | None = None,
    max_leg_collateral: Decimal | None = None,
) -> tuple[dict[str, IncentiveWindow], AllocationResult]:
    budget = available_budget if available_budget is not None else settings.total_budget
    is_global = settings.venue == "global"
    if is_global and hasattr(client, "fetch_reward_conditions"):
        conditions = client.fetch_reward_conditions()
        base_cond_cap = min(settings.max_per_market, settings.max_collateral_per_market)
        if is_global and settings.pair_preference <= Decimal("0"):
            max_pairs = _effective_max_pairs(settings.max_markets)
            num_pairs = (
                calculate_num_pairs(budget, max_pairs)
                if (available_budget is not None or settings.max_markets <= 4)
                else max_pairs
            )
            effective_max_conditions = num_pairs
            per_pair = budget / Decimal(str(num_pairs)) if num_pairs > 0 else Decimal("0")
            cond_ceiling = max(base_cond_cap, Decimal("85.00"))
            max_cost_per_condition = (
                min(cond_ceiling, max(base_cond_cap, per_pair))
                if (available_budget is not None or settings.max_markets <= 4)
                else base_cond_cap
            )
        else:
            num_pairs = _effective_max_pairs(settings.max_markets)
            max_cost_per_condition = (
                max(base_cond_cap, budget / Decimal(str(num_pairs)))
                if available_budget is not None
                else base_cond_cap
            )
            effective_max_conditions = settings.max_markets
        max_contracts = Decimal(str(settings.max_contracts_per_market))
        if settings.pair_preference <= Decimal("0") and max_cost_per_condition > Decimal("0"):
            max_affordable_contracts = min(
                max_contracts,
                (max_cost_per_condition / Decimal("0.90")) // Decimal("1"),
            )
        else:
            max_affordable_contracts = max_contracts
        active_windows: dict[str, IncentiveWindow] = {}
        for cond in conditions:
            active_windows[cond.yes.slug] = dataclasses.replace(cond.window, slug=cond.yes.slug)
            active_windows[cond.no.slug] = dataclasses.replace(cond.window, slug=cond.no.slug)

        excl = set(excluded_slugs) if excluded_slugs else set()
        allocatable = [
            c for c in conditions
            if "temperature in " not in c.question.lower()
            and c.yes.slug not in excl
            and c.no.slug not in excl
        ]
        allocatable.sort(
            key=lambda c: (
                c.window.target_size <= max_affordable_contracts,
                c.window.target_size <= max_contracts,
                c.window.pool_usd,
                -c.window.target_size,
            ),
            reverse=True,
        )

        affordable = [c for c in allocatable if c.window.target_size <= max_affordable_contracts]
        unaffordable = [c for c in allocatable if c.window.target_size > max_affordable_contracts]
        book_limit = CANDIDATE_BOOK_LIMIT
        max_conds = book_limit // 2
        top_conditions = affordable[:max_conds]
        if len(top_conditions) < max_conds:
            top_conditions.extend(unaffordable[: max_conds - len(top_conditions)])

        candidate_books: dict[str, Book] = {}
        t0 = time.perf_counter()
        batch_books = None
        if hasattr(client, "fetch_books_batch"):
            try:
                res = client.fetch_books_batch([slug for cond in top_conditions for slug in (cond.yes.slug, cond.no.slug)])
                if isinstance(res, dict):
                    batch_books = res
            except Exception as e:
                logger.debug("Failed to fetch candidate books batch: %s", e)

        complete_conds: list[ConditionMarket] = []
        if isinstance(batch_books, dict) and batch_books:
            for cond in top_conditions:
                if cond.yes.slug in batch_books and cond.no.slug in batch_books:
                    complete_conds.append(cond)

        req_count = len(top_conditions)
        is_batch_usable = (
            req_count > 0
            and (len(complete_conds) / req_count) >= 0.5
        )

        if is_batch_usable and isinstance(batch_books, dict):
            for cond in complete_conds:
                candidate_books[cond.yes.slug] = batch_books[cond.yes.slug]
                candidate_books[cond.no.slug] = batch_books[cond.no.slug]
            conds_with_books = len(candidate_books) // 2
            if conds_with_books < req_count:
                logger.warning(
                    "fetch_books_batch returned partial books: %d conditions had books vs %d requested",
                    conds_with_books,
                    req_count,
                )
        else:
            logger.warning(
                "Falling back to sequential book fetch: %d conditions capped at FALLBACK_BOOK_CONDITION_LIMIT=%d",
                req_count,
                FALLBACK_BOOK_CONDITION_LIMIT,
            )
            for cond in top_conditions[:FALLBACK_BOOK_CONDITION_LIMIT]:
                try:
                    yes_book = client.fetch_book(cond.yes.slug)
                    no_book = client.fetch_book(cond.no.slug)
                except Exception as e:
                    logger.debug("Failed to fetch candidate books for condition %s: %s", cond.condition_id, e)
                    continue
                candidate_books[cond.yes.slug] = yes_book
                candidate_books[cond.no.slug] = no_book

        book_fetch_elapsed = time.perf_counter() - t0
        conditions_with_books = len(candidate_books) // 2
        logger.info(
            "Book fetch cycle: %d conditions after filters, %d conditions affordable, %d conditions with books, elapsed=%.2fs",
            len(allocatable),
            len(affordable),
            conditions_with_books,
            book_fetch_elapsed,
        )

        if open_orders:
            orders_by_slug: dict[str, list[LiveOrder]] = {}
            for o in open_orders:
                orders_by_slug.setdefault(o.slug, []).append(o)
            for slug, s_orders in orders_by_slug.items():
                if slug in candidate_books:
                    candidate_books[slug] = global_rewards.strip_orders_from_book(candidate_books[slug], s_orders)

        cond_kwargs: dict[str, Any] = {
            "conditions": allocatable,
            "books": candidate_books,
            "total_budget": budget,
            "max_cost_per_condition": max_cost_per_condition,
            "max_markets": effective_max_conditions,
            "max_contracts": max_contracts,
            "pair_preference": settings.pair_preference,
            "positions": positions,
        }
        if max_leg_collateral is not None:
            try:
                sig = inspect.signature(allocate_conditions)
                if (
                    "max_leg_collateral" in sig.parameters
                    or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                ):
                    cond_kwargs["max_leg_collateral"] = max_leg_collateral
            except (ValueError, TypeError):
                pass
        targets = allocate_conditions(**cond_kwargs)
        for target in targets:
            daily_usd = target.expected_reward_sec * global_rewards.SECONDS_PER_DAY
            logger.info(
                "Condition target question='%s', mode=%s, size=%s, yes_price=%s, no_price=%s, cost=$%.2f, $/day=$%.2f",
                target.condition.question,
                target.mode,
                target.size,
                target.yes_price,
                target.no_price,
                target.cost,
                daily_usd,
            )
        alloc_res = targets_to_allocation(targets)
    else:
        active_windows = {}
        for w in client.fetch_incentives(status="active", page_size=200):
            if w.status.lower() == "active" and w.slug not in active_windows:
                active_windows[w.slug] = w

        candidate_books = {}
        for slug in list(active_windows.keys())[:CANDIDATE_BOOK_LIMIT]:
            try:
                candidate_books[slug] = client.fetch_book(slug)
            except Exception as e:
                logger.debug("Failed to fetch candidate book %s: %s", slug, e)

        alloc_res = greedy_allocate(
            windows=list(active_windows.values()),
            books=candidate_books,
            total_budget=budget,
            now=datetime.now(timezone.utc),
            max_markets=settings.max_markets,
            max_contracts_cap=Decimal(str(settings.max_contracts_per_market)),
            tick=Decimal("0.001"),
            pre_start_cancel_seconds=settings.pre_start_cancel_seconds,
            quote_live_periods=settings.quote_live_periods,
            slug_to_event_map=client.fetch_slug_to_event_map(),
            is_global=is_global,
            ticks={slug: resolve_market(client, slug).tick for slug in candidate_books} if is_global else None,
            max_cost_per_market=min(settings.max_per_market, settings.max_collateral_per_market) if is_global else None,
        )

    if not alloc_res.selected_slugs:
        logger.warning("No fundable markets selected: %s", alloc_res.unfunded_reason)
    for cand in alloc_res.selected_candidates:
        logger.info(
            "Selected candidate market=%s, event=%s, side=%s, cost=$%.2f, expected=$%.4f/hr ($%.6f/s)",
            cand.slug,
            cand.event_id,
            cand.side,
            cand.cost,
            cand.expected_reward_sec * Decimal("3600"),
            cand.expected_reward_sec,
        )
    logger.info("Allocated %s markets with greedy selection: %s", len(alloc_res.selected_slugs), alloc_res.selected_slugs)
    return active_windows, alloc_res

class ShadowEngine:
    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        client: PublicGatewayClient | Any,
        logger: logging.Logger,
    ):
        self.settings = settings
        self.ledger = ledger
        self.client = client
        self.logger = logger
        self.market_states: dict[str, MarketState] = {}
        self.paired_slugs: frozenset[str] = frozenset()
        self.side_qtys: dict[tuple[str, str], Decimal] = {}
        self.condition_pairs: tuple[tuple[str, str], ...] = ()

    def run(self, duration_seconds: int | None = None) -> None:
        if duration_seconds is not None and duration_seconds > 0:
            self.logger.info("Starting ShadowEngine for %s seconds", duration_seconds)
            end_time = time.time() + duration_seconds
        else:
            self.logger.info("Starting ShadowEngine indefinitely")
            end_time = float("inf")

        active_windows, alloc_res = allocate_markets(self.settings, self.client, self.logger)
        target_slugs = alloc_res.selected_slugs
        is_global = (self.settings.venue == "global")
        self.paired_slugs = alloc_res.paired_slugs
        self.side_qtys = alloc_res.side_qtys
        self.condition_pairs = alloc_res.condition_pairs

        market_metadata: dict[str, Market] = {}
        for slug in target_slugs:
            self.market_states[slug] = "IDLE"

        tick_count = 0
        while time.time() < end_time:
            tick_start = time.time()
            now_dt = datetime.now(timezone.utc)
            now_ms = int(time.time() * 1000)
            pair_inputs: dict[str, tuple[Book, IncentiveWindow | None, list[LiveOrder]]] = {}

            for slug in target_slugs:
                window = active_windows.get(slug)
                try:
                    book = self.client.fetch_book(slug)
                    if slug not in market_metadata:
                        market_metadata[slug] = resolve_market(self.client, slug)
                    market = market_metadata[slug]

                    pos = self.ledger.get_position(slug)
                    if abs(pos.net_qty) >= self.settings.max_contracts_per_market:
                        self.market_states[slug] = "FLATTENING"
                    else:
                        self.market_states[slug] = "QUOTING"

                    open_orders = self.ledger.get_open_orders(slug)
                    fills, remaining_orders = check_paper_fills(book, open_orders)
                    for fill in fills:
                        self.ledger.record_fill(fill)
                        self.ledger.close_order(fill.order_id, status="FILLED", ts_ms=now_ms)
                        pos = update_position_from_fill(pos, fill.side, fill.price, fill.qty)
                        self.ledger.update_position(pos, ts_ms=now_ms)
                        self.logger.info("Fill recorded: %s %s %s @ %s", fill.slug, fill.side, fill.qty, fill.price)

                    b_budget = alloc_res.side_budgets.get((slug, "bid"), Decimal("0"))
                    a_budget = alloc_res.side_budgets.get((slug, "ask"), Decimal("0"))

                    quotes = desired_quotes(
                        market=market,
                        book=book,
                        window=window,
                        position=pos,
                        budget=self.settings.max_per_market,
                        max_contracts_cap=Decimal(str(self.settings.max_contracts_per_market)),
                        now=now_dt,
                        pre_start_cancel_seconds=self.settings.pre_start_cancel_seconds,
                        quote_live_periods=self.settings.quote_live_periods,
                        bid_budget=b_budget,
                        ask_budget=a_budget,
                        is_global=is_global,
                        paired=(slug in self.paired_slugs),
                        quote_size=self.side_qtys.get((slug, "bid")),
                    )
                    for q in quotes:
                        self.ledger.record_quote(q, ts_ms=now_ms)

                    to_create, to_cancel, to_keep = reconcile_quotes(remaining_orders, quotes)
                    for old_order in to_cancel:
                        self.ledger.close_order(old_order.order_id, status="CANCELED", ts_ms=now_ms)

                    current_open_orders = list(to_keep)
                    for q in to_create:
                        new_order = LiveOrder(
                            order_id=str(uuid.uuid4()),
                            slug=q.slug,
                            side=q.side,
                            price=q.price,
                            qty_open=q.qty,
                        )
                        self.ledger.record_order(new_order, status="OPEN", ts_ms=now_ms)
                        current_open_orders.append(new_order)

                    if slug in self.paired_slugs:
                        pair_inputs[slug] = (book, window, current_open_orders)
                    elif is_global:
                        self.ledger.record_lip_snapshot(estimate_global_lip_score(book, window, current_open_orders, ts_ms=now_ms))
                    else:
                        self.ledger.record_lip_snapshot(estimate_lip_score(book, window, current_open_orders, market.tick, ts_ms=now_ms))

                except Exception as exc:
                    self.market_states[slug] = "PAUSED"
                    self.logger.warning("Error processing %s: %s", slug, exc)

            for yes_slug, no_slug in self.condition_pairs:
                if yes_slug in pair_inputs and no_slug in pair_inputs:
                    yes_book, window, yes_orders = pair_inputs[yes_slug]
                    no_book, _, no_orders = pair_inputs[no_slug]
                    for snap in estimate_global_pair_score(yes_book, no_book, window, yes_orders + no_orders, ts_ms=now_ms):
                        self.ledger.record_lip_snapshot(snap)

            tick_count += 1
            if tick_count % 30 == 0:
                rem_str = f"{int(end_time - time.time())} s" if end_time != float("inf") else "indefinite"
                self.logger.info("Completed %s ticks, remaining %s", tick_count, rem_str)

            elapsed = time.time() - tick_start
            sleep_time = max(0.1, 1.0 - elapsed)
            time.sleep(sleep_time)

        self.logger.info("ShadowEngine finished successfully after %s ticks", tick_count)

class LiveEngine:
    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        public_client: PublicGatewayClient | Any,
        auth_client: AuthenticatedClient | Any,
        risk_manager: RiskManager,
        logger: logging.Logger,
    ):
        self.settings = settings
        self.ledger = ledger
        self.public_client = public_client
        self.auth_client = auth_client
        self.risk_manager = risk_manager
        self.logger = logger
        self.market_states: dict[str, MarketState] = {}
        self.notifier = Notifier(settings)
        self.kill_switch_active: bool = False
        self.daily_loss_stop_tripped: bool = False
        self.target_slugs: list[str] = []
        self.active_windows: dict[str, IncentiveWindow] = {}
        self.side_budgets: dict[tuple[str, str], Decimal] = {}
        self.side_qtys: dict[tuple[str, str], Decimal] = {}
        self.side_prices: dict[tuple[str, str], Decimal] = {}
        self.paired_slugs: frozenset[str] = frozenset()
        self.condition_pairs: tuple[tuple[str, str], ...] = ()
        self.markets: dict[str, Market] = {}
        self.started_at: float = time.time()
        self.order_created_at: dict[str, float] = {}
        self.recent_cancels: dict[str, float] = {}
        self.slug_cooldown: dict[str, float] = {}
        self.local_fill_positions: dict[str, tuple[Position, float]] = {}
        self.expected_portfolio_rate_sec: Decimal = Decimal("0")
        self.last_prepare_ts: float = 0.0
        self.last_allocated_bp: Decimal = Decimal("0")
        self.last_cash_realloc_ts: float = 0.0
        self.last_hourly_notify_ts: float = 0.0
        self.yield_collapse_streak: int = 0
        self.underdeployed_streak: int = 0
        self.degraded_slugs_until: dict[str, float] = {}
        self.loss_cooldown_until: dict[str, float] = {}
        if hasattr(ledger, "get_active_loss_cooldowns"):
            try:
                loaded = ledger.get_active_loss_cooldowns(time.time())
                if isinstance(loaded, dict):
                    self.loss_cooldown_until.update(loaded)
            except Exception:
                pass
        self.dead_unwind_slugs: set[str] = set()
        self.manual_sell_order_ids: set[str] = set()
        self.manual_sell_cooldown_until: dict[str, float] = {}
        self.recent_cancel_orders: dict[str, tuple[LiveOrder, float]] = {}
        self.baseline_utc_date: str = getattr(settings, "daily_loss_baseline_date", "").strip()
        self.known_sibling_by_slug: dict[str, str] = {}
        self.known_pair_by_slug: dict[str, tuple[str, str]] = {}
        self._last_tick_ts: float = 0.0
        self.last_prune_ts: float = 0.0

    def _compute_redeemable_loss(self, live_positions: dict[str, Position], now_dt: datetime | None = None) -> Decimal:
        redeemable_loss = Decimal("0")
        today_midnight_ms = 0
        if now_dt is not None:
            today_midnight = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            today_midnight_ms = int(today_midnight.timestamp() * 1000)
        for p in live_positions.values():
            if (
                getattr(p, "redeemable", False) is True
                and not getattr(p, "archived", False)
                and isinstance(getattr(p, "net_qty", None), Decimal)
                and isinstance(getattr(p, "avg_price", None), Decimal)
                and p.net_qty > Decimal("0")
                and p.avg_price > Decimal("0")
            ):
                if today_midnight_ms > 0 and hasattr(self.ledger, "has_fill_since"):
                    is_active_today = (
                        p.slug in self.target_slugs
                        or p.slug in self.local_fill_positions
                        or self.ledger.has_fill_since(p.slug, today_midnight_ms)
                    )
                    if not is_active_today:
                        continue
                cur_px = getattr(p, "cur_price", None)
                if isinstance(cur_px, Decimal) and cur_px <= Decimal("0.01"):
                    leg_loss = p.net_qty * max(Decimal("0"), p.avg_price - cur_px)
                    sib_slug = self.known_sibling_by_slug.get(p.slug)
                    sib_pos = live_positions.get(sib_slug) if sib_slug else None
                    sib_gain = Decimal("0")
                    if (
                        sib_pos is not None
                        and not getattr(sib_pos, "archived", False)
                        and isinstance(getattr(sib_pos, "net_qty", None), Decimal)
                        and isinstance(getattr(sib_pos, "avg_price", None), Decimal)
                        and isinstance(getattr(sib_pos, "cur_price", None), Decimal)
                        and sib_pos.net_qty > Decimal("0")
                        and sib_pos.cur_price > sib_pos.avg_price
                    ):
                        sib_gain = sib_pos.net_qty * (sib_pos.cur_price - sib_pos.avg_price)
                    redeemable_loss += max(Decimal("0"), leg_loss - sib_gain)
        return redeemable_loss

    def _record_known_pair(self, yes_slug: str, no_slug: str) -> None:
        if not yes_slug or not no_slug or yes_slug == no_slug:
            return
        pair = (yes_slug, no_slug)
        self.known_sibling_by_slug[yes_slug] = no_slug
        self.known_sibling_by_slug[no_slug] = yes_slug
        self.known_pair_by_slug[yes_slug] = pair
        self.known_pair_by_slug[no_slug] = pair

    def _sync_known_siblings(self, alloc_pairs: tuple[tuple[str, str], ...] = ()) -> None:
        for y_s, n_s in self.condition_pairs:
            self._record_known_pair(y_s, n_s)
        for y_s, n_s in alloc_pairs:
            self._record_known_pair(y_s, n_s)
        client_pairs = getattr(self.public_client, "_pair_by_slug", None)
        if isinstance(client_pairs, dict):
            for pair in client_pairs.values():
                if (
                    isinstance(pair, tuple)
                    and len(pair) == 2
                    and isinstance(pair[0], str)
                    and isinstance(pair[1], str)
                ):
                    self._record_known_pair(pair[0], pair[1])
        client_siblings = getattr(self.public_client, "_sibling_map", None)
        client_yes_slugs = getattr(self.public_client, "_yes_slugs", None)
        if isinstance(client_siblings, dict):
            for s_a, s_b in client_siblings.items():
                if isinstance(s_a, str) and isinstance(s_b, str) and s_a and s_b and s_a != s_b:
                    self.known_sibling_by_slug.setdefault(s_a, s_b)
                    self.known_sibling_by_slug.setdefault(s_b, s_a)
                    if s_a not in self.known_pair_by_slug and s_b not in self.known_pair_by_slug:
                        if isinstance(client_yes_slugs, set) and s_b in client_yes_slugs and s_a not in client_yes_slugs:
                            self._record_known_pair(s_b, s_a)
                        else:
                            self._record_known_pair(s_a, s_b)
        prog_groups: dict[str, list[str]] = {}
        for w_slug, win in self.active_windows.items():
            prog_id = getattr(win, "program_id", "")
            if isinstance(prog_id, str) and prog_id and prog_id not in ("none", "unwind"):
                prog_groups.setdefault(prog_id, []).append(w_slug)
        for slugs in prog_groups.values():
            if len(slugs) == 2 and slugs[0] not in self.known_pair_by_slug and slugs[1] not in self.known_pair_by_slug:
                self._record_known_pair(slugs[0], slugs[1])

    def _min_exit_price(self, slug: str, pos: Position | None = None) -> Decimal:
        if pos is None:
            try:
                pos = self.ledger.get_position(slug)
            except Exception:
                pos = None
        if (
            pos is None
            or not isinstance(getattr(pos, "net_qty", None), Decimal)
            or not isinstance(getattr(pos, "avg_price", None), Decimal)
            or pos.net_qty <= Decimal("0")
            or pos.avg_price <= Decimal("0")
        ):
            return Decimal("0")
        tick = self.markets[slug].tick if slug in self.markets else Decimal("0.001")
        if tick > Decimal("0"):
            return ((pos.avg_price + tick - Decimal("1e-12")) // tick) * tick
        return pos.avg_price

    def run(self, duration_seconds: int | None = None) -> None:
        if not self.auth_client.is_available:
            raise RuntimeError("Polymarket US authenticated SDK not configured")

        if duration_seconds is not None and duration_seconds > 0:
            self.logger.info("Starting LiveEngine for %s seconds", duration_seconds)
            end_time = time.time() + duration_seconds
        else:
            self.logger.info("Starting LiveEngine indefinitely")
            end_time = float("inf")

        self.prepare()
        if self.last_prepare_ts <= 0.0:
            self.last_prepare_ts = time.time()
        try:
            while time.time() < end_time:
                tick_start = time.time()
                paused = (
                    self.kill_switch_active
                    or self.daily_loss_stop_tripped
                    or self.risk_manager.is_maintenance_active(
                        datetime.fromtimestamp(tick_start, timezone.utc)
                    )
                )
                realloc_interval = (
                    REALLOCATE_SECONDS
                    if REALLOCATE_SECONDS != 3600
                    else getattr(self.settings, "reallocate_seconds", 3600)
                )
                if not paused and tick_start - self.last_prepare_ts >= realloc_interval:
                    self.prepare(is_realloc=True)
                    if self.last_prepare_ts < tick_start:
                        self.last_prepare_ts = tick_start
                now_ts = time.time()
                period = self.tick(now_ts) if self.target_slugs else 30.0
                time.sleep(max(0.1, period - (time.time() - tick_start)))
        finally:
            self.shutdown()

    def prepare(self, is_realloc: bool = False) -> None:
        old_targets = list(self.target_slugs)
        effective_budget = self.settings.total_budget
        dynamic_leg_col: Decimal = self.settings.max_collateral_per_market
        open_orders: list[LiveOrder] = []
        try:
            bp = self.auth_client.get_buying_power()
            try:
                open_orders = self.auth_client.get_open_orders()
                if self.settings.venue != "global":
                    open_collateral = sum(o.price * o.qty_open for o in open_orders if o.side.startswith("BUY"))
                    bp += open_collateral
            except Exception:
                pass
            if bp > Decimal("0"):
                self.last_allocated_bp = bp
                self.last_cash_realloc_ts = time.time()
                if self.settings.venue == "global":
                    cash_buffer = max(Decimal("3.00"), min(Decimal("10.00"), bp * Decimal("0.03")))
                    avail_cash = max(Decimal("0"), bp - cash_buffer)
                    if self.settings.total_budget <= Decimal("0"):
                        effective_budget = avail_cash
                    else:
                        effective_budget = min(self.settings.total_budget, avail_cash)

                    dynamic_stop = max(self.settings.daily_loss_stop, bp * Decimal("0.08"))
                    dynamic_max_col = max(effective_budget, self.settings.max_collateral)
                    est_pairs = calculate_num_pairs(
                        effective_budget,
                        _effective_max_pairs(self.settings.max_markets),
                    )
                    est_cond_cap = (
                        min(
                            Decimal("85.00"),
                            max(Decimal("36.00"), (effective_budget / Decimal(str(est_pairs))) * Decimal("1.15")),
                        )
                        if est_pairs > 0
                        else self.settings.max_per_market
                    )
                    dynamic_leg_col = max(
                        self.settings.max_collateral_per_market,
                        (est_cond_cap * Decimal("0.80")).quantize(Decimal("1.00")),
                    )
                    if hasattr(self.risk_manager, "update_dynamic_limits"):
                        self.risk_manager.update_dynamic_limits(
                            total_budget=effective_budget,
                            max_collateral=dynamic_max_col,
                            max_collateral_per_market=dynamic_leg_col,
                            daily_loss_stop=dynamic_stop,
                        )
                    self.logger.info(
                        "Dynamic capital scaling applied: bp=$%s buffer=$%s budget=$%s stop=$%s leg_col=$%s",
                        bp, cash_buffer, effective_budget, dynamic_stop, dynamic_leg_col,
                    )
                else:
                    fee_buffer = max(Decimal("3.00"), bp * Decimal("0.05"))
                    effective_budget = min(self.settings.total_budget, max(Decimal("0"), bp - fee_buffer))
            else:
                self.last_allocated_bp = Decimal("0")
                effective_budget = Decimal("0")
        except Exception as exc:
            self.logger.warning("Could not fetch buying power for allocation: %s", exc)
            self.last_allocated_bp = Decimal("0")
            effective_budget = Decimal("0")
        held_positions = None
        pos_map = None
        try:
            held_positions = self.auth_client.get_positions()
            if isinstance(held_positions, list):
                pos_map = {p.slug: p for p in held_positions}
        except Exception as exc:
            self.logger.warning("Could not fetch held positions for allocation: %s", exc)

        ledger_pos_map: dict[str, Position] = {}
        if hasattr(self.ledger, "get_all_positions"):
            try:
                ledger_positions = self.ledger.get_all_positions()
                if isinstance(ledger_positions, (list, tuple)):
                    for lp in ledger_positions:
                        if isinstance(lp, Position) and lp.net_qty > Decimal("0"):
                            ledger_pos_map[lp.slug] = lp
            except Exception:
                pass

        effective_pos_map: dict[str, Position] | None = (
            dict(pos_map)
            if pos_map is not None
            else (dict(ledger_pos_map) if ledger_pos_map else None)
        )
        now_prep = time.time()
        if (now_prep - self.last_prune_ts) >= 86400.0 and hasattr(self.ledger, "prune_old_snapshots"):
            try:
                self.ledger.prune_old_snapshots(retention_days=7)
            except Exception as exc:
                self.logger.warning("Failed to prune old snapshots: %s", exc)
            self.last_prune_ts = now_prep
        for slug, (opt_pos, fill_ts) in list(self.local_fill_positions.items()):
            if now_prep - fill_ts > LOCAL_FILL_POSITION_TTL_SECONDS:
                self.local_fill_positions.pop(slug, None)
            else:
                if effective_pos_map is None:
                    effective_pos_map = {}
                effective_pos_map[slug] = opt_pos

        self._sync_known_siblings()
        self.degraded_slugs_until = {
            s: ts for s, ts in self.degraded_slugs_until.items() if ts > now_prep
        }
        self.loss_cooldown_until = {
            s: ts for s, ts in self.loss_cooldown_until.items() if ts > now_prep
        }
        loss_excl = set(self.loss_cooldown_until.keys())
        excl_slugs = set(self.degraded_slugs_until.keys()) | loss_excl
        try:
            alloc_kwargs: dict[str, Any] = {
                "available_budget": effective_budget,
                "positions": effective_pos_map,
                "open_orders": open_orders,
            }
            if self.settings.venue == "global":
                alloc_kwargs["max_leg_collateral"] = dynamic_leg_col
            if excl_slugs:
                alloc_kwargs["excluded_slugs"] = excl_slugs
            self.active_windows, alloc_res = allocate_markets(
                self.settings,
                self.public_client,
                self.logger,
                **alloc_kwargs,
            )
            if (
                excl_slugs > loss_excl
                and sum(alloc_res.side_budgets.values(), Decimal("0")) < effective_budget * Decimal("0.80")
            ):
                fallback_kwargs = {k: v for k, v in alloc_kwargs.items() if k != "excluded_slugs"}
                if loss_excl:
                    fallback_kwargs["excluded_slugs"] = loss_excl
                self.active_windows, alloc_res = allocate_markets(
                    self.settings,
                    self.public_client,
                    self.logger,
                    **fallback_kwargs,
                )
        except Exception as exc:
            if is_realloc:
                self.logger.warning("Reallocation failed, retaining current targets: %s", exc)
                self.last_prepare_ts = time.time()
                self.yield_collapse_streak = 0
                self.underdeployed_streak = 0
                return
            raise
        self._sync_known_siblings(alloc_res.condition_pairs)
        self.expected_portfolio_rate_sec = sum(
            (c.expected_reward_sec for c in alloc_res.selected_candidates),
            Decimal("0"),
        )
        self.last_prepare_ts = time.time()
        self.yield_collapse_streak = 0
        self.underdeployed_streak = 0
        self.target_slugs = list(alloc_res.selected_slugs)
        self.side_budgets = dict(alloc_res.side_budgets)
        self.side_qtys = dict(alloc_res.side_qtys)
        self.side_prices = dict(getattr(alloc_res, "side_prices", {}) or {})
        preserved_pairs: list[tuple[str, str]] = list(alloc_res.condition_pairs)
        seen_pairs: set[tuple[str, str]] = set(preserved_pairs)
        preserved_paired: set[str] = set(alloc_res.paired_slugs)
        if effective_pos_map:
            for p_slug, p_obj in effective_pos_map.items():
                if p_obj.net_qty > Decimal("0") and p_slug in self.known_pair_by_slug:
                    pair = self.known_pair_by_slug[p_slug]
                    if pair not in seen_pairs:
                        preserved_pairs.append(pair)
                        seen_pairs.add(pair)
                    if self.settings.venue == "global":
                        y_pos = effective_pos_map.get(pair[0])
                        n_pos = effective_pos_map.get(pair[1])
                        if (
                            y_pos is not None
                            and n_pos is not None
                            and y_pos.net_qty >= Decimal("5")
                            and n_pos.net_qty >= Decimal("5")
                        ):
                            preserved_paired.add(pair[0])
                            preserved_paired.add(pair[1])
        self.paired_slugs = frozenset(preserved_paired)
        self.condition_pairs = tuple(preserved_pairs)
        if effective_pos_map is not None:
            now_utc = datetime.now(timezone.utc)
            for p in effective_pos_map.values():
                if p.net_qty >= Decimal("5") and p.slug not in self.target_slugs:
                    if p.slug in self.dead_unwind_slugs:
                        continue
                    if p.slug not in self.active_windows:
                        try:
                            b = self.public_client.fetch_book(p.slug)
                            if not b.bids and not b.asks:
                                continue
                            best_bid = b.bids[0].price if b.bids else Decimal("0")
                            best_ask = b.asks[0].price if b.asks else Decimal("0")
                            if best_bid <= Decimal("0.01") and best_ask <= Decimal("0.01"):
                                continue
                        except Exception as exc:
                            status_code = getattr(getattr(exc, "response", None), "status_code", None)
                            msg = str(exc).lower().replace(p.slug.lower(), "")
                            if status_code == 404 or "404" in msg or "not found" in msg:
                                self.dead_unwind_slugs.add(p.slug)
                            else:
                                self.logger.warning("Unwind book fetch failed for %s: %s", p.slug, exc)
                            continue
                        self.active_windows[p.slug] = IncentiveWindow(
                            slug=p.slug,
                            program_id="unwind",
                            pool_usd=Decimal("0"),
                            discount=Decimal("0"),
                            target_size=Decimal("5"),
                            max_spread=Decimal("0.04"),
                            start=now_utc - timedelta(hours=1),
                            end=now_utc + timedelta(hours=23),
                            status="active",
                        )
                    self.target_slugs.append(p.slug)
                    self.side_budgets.setdefault((p.slug, "bid"), Decimal("0"))
                    self.side_budgets.setdefault((p.slug, "ask"), Decimal("0"))
        for slug in self.target_slugs:
            if slug not in self.market_states:
                self.market_states[slug] = "IDLE"

        if not is_realloc:
            self.logger.info("Scoped startup cancel for target slugs: %s", self.target_slugs)
            self.started_at = time.time()
            if isinstance(open_orders, list) and effective_pos_map:
                for o in open_orders:
                    if isinstance(o, LiveOrder) and o.side in ("SELL", "SELL_LONG", "SELL_SHORT") and o.slug in effective_pos_map:
                        p = effective_pos_map[o.slug]
                        if p.net_qty > Decimal("0") and o.price > Decimal("0"):
                            adopted = dataclasses.replace(o, side="SELL_LONG") if o.side == "SELL" else o
                            self.ledger.record_order(adopted, status="OPEN", ts_ms=int(self.started_at * 1000))
                            self.order_created_at[adopted.order_id] = self.started_at
                            self.manual_sell_order_ids.add(adopted.order_id)
            self._cancel_targets(self.started_at)
            target_set = set(self.target_slugs)
            all_open = open_orders
            if not all_open:
                try:
                    all_open = self.auth_client.get_open_orders()
                except Exception as exc:
                    self.logger.warning("Could not fetch open orders for startup cancel: %s", exc)
                    all_open = []
            non_target_orders = [
                o for o in all_open
                if o.slug not in target_set and o.order_id not in self.manual_sell_order_ids
            ]
            if non_target_orders:
                self.logger.warning(
                    "Startup cancel for %d orphaned orders on non-target slugs: %s",
                    len(non_target_orders),
                    [o.order_id for o in non_target_orders],
                )
                try:
                    self.auth_client.cancel_orders_batch(non_target_orders)
                except Exception as exc:
                    self.logger.warning("Startup non-target cancel failed: %s", exc)
                for o in non_target_orders:
                    self.recent_cancels[o.order_id] = self.started_at
                    self.recent_cancel_orders[o.order_id] = (o, self.started_at)
        else:
            dropped_slugs = [s for s in old_targets if s not in self.target_slugs]
            downsized_slugs = [
                s for s in self.target_slugs
                if s in old_targets
                and any(
                    o.slug == s
                    and o.side.startswith("BUY")
                    and o.qty_open > self.side_qtys.get((s, "bid"), Decimal("0"))
                    for o in (open_orders or self.ledger.get_open_orders(s))
                )
            ]
            cancel_slugs = dropped_slugs + [s for s in downsized_slugs if s not in dropped_slugs]
            if cancel_slugs:
                self.logger.info("Scoped reallocation cancel for dropped/downsized slugs: %s", cancel_slugs)
                self._cancel_targets(time.time(), slugs=cancel_slugs)
                for s in dropped_slugs:
                    self.market_states.pop(s, None)

    def shutdown(self) -> None:
        try:
            self._cancel_targets(time.time())
            self.logger.info("Shutdown cancel complete for %s", self.target_slugs)
        except Exception as exc:
            self.logger.error("Shutdown cancel failed for %s: %s", self.target_slugs, exc)

    def _dispatch_hourly_status_ping(
        self,
        now_ts: float,
        buying_power: Decimal,
        open_orders_count: int = 0,
        live_positions: dict[str, Position] | None = None,
        realized_loss_today: Decimal | None = None,
    ) -> threading.Thread:
        def _query_and_send() -> None:
            try:
                now_dt = datetime.fromtimestamp(now_ts, timezone.utc)
                midnight_dt = datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc)
                get_summary = getattr(self.ledger, "get_lip_summary_since", getattr(self.ledger, "get_lip_stats", None))
                if callable(get_summary):
                    summary_1h = get_summary(int((now_ts - 3600.0) * 1000))
                    summary_today = get_summary(int(midnight_dt.timestamp() * 1000))
                else:
                    summary_1h = {}
                    summary_today = {}

                qualifying_sec_1h = int(summary_1h.get("qualifying_seconds", 0))
                accrued_usd_1h = Decimal(str(summary_1h.get("lip_reward_total", "0")))

                qualifying_sec_today = int(summary_today.get("qualifying_seconds", 0))
                accrued_usd_today = Decimal(str(summary_today.get("lip_reward_total", "0")))

                elapsed_sec = (now_dt - midnight_dt).total_seconds()
                elapsed_hours = max(Decimal("1.0"), Decimal(str(elapsed_sec)) / Decimal("3600"))
                projected_today = (accrued_usd_today / elapsed_hours) * Decimal("24")

                engine_state = "HALTED" if (self.kill_switch_active or self.daily_loss_stop_tripped) else "QUOTING"
                if engine_state != "QUOTING":
                    active_open_pairs = 0
                elif self.side_budgets:
                    if self.condition_pairs:
                        active_open_pairs = sum(
                            1
                            for y_s, n_s in self.condition_pairs
                            if self.side_budgets.get((y_s, "bid"), Decimal("0")) > Decimal("0")
                            or self.side_budgets.get((n_s, "bid"), Decimal("0")) > Decimal("0")
                        )
                    else:
                        active_slug_count = sum(
                            1
                            for s in self.target_slugs
                            if self.side_budgets.get((s, "bid"), Decimal("0")) > Decimal("0")
                        )
                        active_open_pairs = active_slug_count // 2
                else:
                    active_open_pairs = (
                        len(self.condition_pairs) if self.condition_pairs else (len(self.target_slugs) // 2)
                    )

                if realized_loss_today is not None:
                    loss_today = realized_loss_today
                else:
                    try:
                        raw_loss = self.ledger.get_realized_loss_today(now_dt)
                        loss_today = raw_loss if isinstance(raw_loss, Decimal) else Decimal("0")
                    except Exception:
                        loss_today = Decimal("0")
                realized_pnl_today = -loss_today

                positions_val = Decimal("0")
                unrealized_pnl = Decimal("0")
                for p in (live_positions or {}).values():
                    if not getattr(p, "archived", False) and isinstance(getattr(p, "net_qty", None), Decimal) and p.net_qty > Decimal("0"):
                        cur_px = getattr(p, "cur_price", None)
                        if not isinstance(cur_px, Decimal):
                            cur_px = Decimal("0") if getattr(p, "redeemable", False) else p.avg_price
                        positions_val += p.net_qty * cur_px
                        unrealized_pnl += p.net_qty * (cur_px - p.avg_price)
                mtm_equity = buying_power + positions_val

                notify_kwargs: dict[str, Any] = {
                    "qualifying_sec_1h": qualifying_sec_1h,
                    "accrued_usd_1h": accrued_usd_1h,
                    "qualifying_sec_today": qualifying_sec_today,
                    "accrued_usd_today": accrued_usd_today,
                    "projected_payout_today": projected_today,
                    "open_pairs": active_open_pairs,
                    "buying_power": buying_power,
                }
                extra_fields = {
                    "engine_state": engine_state,
                    "open_orders_count": open_orders_count,
                    "realized_pnl_today": realized_pnl_today,
                    "unrealized_pnl": unrealized_pnl,
                    "mtm_equity": mtm_equity,
                }
                try:
                    sig = inspect.signature(self.notifier.notify_hourly_status)
                    has_var_kw = any(
                        p.kind == inspect.Parameter.VAR_KEYWORD
                        for p in sig.parameters.values()
                    )
                    for k, v in extra_fields.items():
                        if has_var_kw or k in sig.parameters:
                            notify_kwargs[k] = v
                except (ValueError, TypeError):
                    pass

                self.notifier.notify_hourly_status(**notify_kwargs)
                self.logger.info(
                    "Hourly status ping dispatched: state=%s, 1h=$%s (%ds qual), today=$%s (proj=$%s), pairs=%d, bp=$%s",
                    engine_state,
                    accrued_usd_1h,
                    qualifying_sec_1h,
                    accrued_usd_today,
                    projected_today,
                    active_open_pairs,
                    buying_power,
                )
            except Exception as exc:
                self.logger.warning("Failed to dispatch hourly status ping: %s", exc)
            finally:
                if hasattr(self.ledger, "close"):
                    try:
                        self.ledger.close()
                    except Exception:
                        pass

        thread = threading.Thread(target=_query_and_send, daemon=True)
        thread.start()
        return thread

    def tick(self, now_ts: float) -> float:
        now_dt = datetime.fromtimestamp(now_ts, timezone.utc)
        now_ms = int(now_ts * 1000)

        is_kill = self.risk_manager.is_kill_switch_active()
        if is_kill != self.kill_switch_active:
            self.kill_switch_active = is_kill
            self.notifier.notify_kill_switch(is_kill)
        if is_kill:
            self.yield_collapse_streak = 0
            self.underdeployed_streak = 0
            self._last_tick_ts = 0.0
            self.logger.warning("Kill switch active! Canceling orders and pausing.")
            self._cancel_targets(now_ts)
            return 5.0

        if self.risk_manager.is_maintenance_active(now_dt):
            self.yield_collapse_streak = 0
            self.underdeployed_streak = 0
            self._last_tick_ts = 0.0
            self.logger.warning("Exchange weekly maintenance window active! Canceling orders and pausing.")
            self._cancel_targets(now_ts)
            return 10.0

        try:
            exchange_open = self.auth_client.get_open_orders()
            fills = self.auth_client.get_fills(since=self._fill_window_start(now_ts))
            positions = self.auth_client.get_positions()
            buying_power = self.auth_client.get_buying_power()
        except Exception as exc:
            self.yield_collapse_streak = 0
            self.logger.warning("Exchange sync failed, skipping tick: %s", exc)
            return 1.0

        live_positions = {p.slug: p for p in positions}
        self._ingest_fills(fills, live_positions=live_positions, now_ts=now_ts)
        for slug, (opt_pos, fill_ts) in list(self.local_fill_positions.items()):
            if now_ts - fill_ts > LOCAL_FILL_POSITION_TTL_SECONDS:
                self.local_fill_positions.pop(slug, None)
            else:
                live_positions[slug] = opt_pos
        self._sync_orders(exchange_open, {f.order_id for f in fills}, now_ts, now_ms, live_positions=live_positions)

        raw_realized_loss_today = self.ledger.get_realized_loss_today(now_dt)
        today_utc_str = now_dt.date().isoformat()
        raw_baseline = getattr(self.settings, "daily_loss_baseline", Decimal("0"))
        baseline = (
            raw_baseline
            if (bool(self.baseline_utc_date) and today_utc_str == self.baseline_utc_date)
            else Decimal("0")
        )
        realized_loss_today = max(
            Decimal("0"),
            raw_realized_loss_today + self._compute_redeemable_loss(live_positions, now_dt=now_dt) - baseline,
        )
        dyn_stop = getattr(self.risk_manager, "dynamic_daily_loss_stop", None)
        effective_loss_stop = (
            dyn_stop
            if isinstance(dyn_stop, Decimal) and dyn_stop > Decimal("0")
            else self.settings.daily_loss_stop
        )
        if realized_loss_today >= effective_loss_stop:
            self.yield_collapse_streak = 0
            self.underdeployed_streak = 0
            self._last_tick_ts = 0.0
            if not self.daily_loss_stop_tripped:
                self.daily_loss_stop_tripped = True
                self.notifier.notify_daily_loss_stop(realized_loss_today, effective_loss_stop)
                self.logger.warning(
                    "Daily loss stop tripped ($%s >= $%s)! Canceling all orders and pausing.",
                    realized_loss_today,
                    effective_loss_stop,
                )
                self._cancel_targets(now_ts)
            elif any(o.order_id not in self.manual_sell_order_ids for o in exchange_open):
                self._cancel_targets(now_ts)
            if self.last_hourly_notify_ts <= 0.0:
                self.last_hourly_notify_ts = now_ts
            elif now_ts - self.last_hourly_notify_ts >= 3600.0:
                self._dispatch_hourly_status_ping(
                    now_ts,
                    buying_power,
                    open_orders_count=0,
                    live_positions=live_positions,
                    realized_loss_today=realized_loss_today,
                )
                self.last_hourly_notify_ts = now_ts
            return 10.0
        self.daily_loss_stop_tripped = False

        if self.last_hourly_notify_ts <= 0.0:
            self.last_hourly_notify_ts = now_ts
        elif now_ts - self.last_hourly_notify_ts >= 3600.0:
            self._dispatch_hourly_status_ping(
                now_ts,
                buying_power,
                open_orders_count=len(exchange_open),
                live_positions=live_positions,
                realized_loss_today=realized_loss_today,
            )
            self.last_hourly_notify_ts = now_ts

        if self.last_allocated_bp > Decimal("0") and self.target_slugs:
            eff_max_pairs = _effective_max_pairs(self.settings.max_markets)
            new_pairs = calculate_num_pairs(buying_power, eff_max_pairs)
            curr_pairs = calculate_num_pairs(self.last_allocated_bp, eff_max_pairs)

            is_downward_shock = (
                buying_power < self.last_allocated_bp
                and ((self.last_allocated_bp - buying_power) / self.last_allocated_bp) >= Decimal("0.20")
            )

            tier_thresh = get_pair_tier_threshold(new_pairs)
            has_tier_expansion = new_pairs > curr_pairs and buying_power >= (tier_thresh + Decimal("5.00"))
            has_bp_expansion = buying_power >= (self.last_allocated_bp * Decimal("1.25"))
            is_upward_expansion = (
                (now_ts - self.last_cash_realloc_ts >= 300.0)
                and (has_tier_expansion or has_bp_expansion)
            )

            if is_downward_shock or is_upward_expansion:
                reason = "downward shock" if is_downward_shock else "upward expansion"
                bp_shift = abs(buying_power - self.last_allocated_bp) / self.last_allocated_bp
                self.logger.info(
                    "Dynamic cash shift detected (%s: bp=$%s vs last=$%s, shift=%.1f%%, pairs=%d->%d); triggering reallocation",
                    reason,
                    buying_power,
                    self.last_allocated_bp,
                    float(bp_shift * 100),
                    curr_pairs,
                    new_pairs,
                )
                self.prepare(is_realloc=True)
                self.last_cash_realloc_ts = now_ts
                return 1.0

        if self._last_tick_ts > 0.0 and now_ts > self._last_tick_ts:
            tick_elapsed_sec = min(
                Decimal("90"),
                max(Decimal("1"), Decimal(str(round(now_ts - self._last_tick_ts, 3)))),
            )
        else:
            tick_elapsed_sec = Decimal("1")
        self._last_tick_ts = now_ts

        active_slugs = [
            s for s in self.target_slugs
            if now_ts >= self.slug_cooldown.get(s, 0.0)
        ]
        batch_books: dict[str, Book] = {}
        if hasattr(self.public_client, "fetch_books_batch") and active_slugs:
            try:
                books_to_fetch = list(active_slugs)
                active_set = set(active_slugs)
                for y_s, n_s in self.condition_pairs:
                    if y_s in active_set or n_s in active_set:
                        if y_s not in active_set:
                            books_to_fetch.append(y_s)
                            active_set.add(y_s)
                        if n_s not in active_set:
                            books_to_fetch.append(n_s)
                            active_set.add(n_s)
                for s in list(active_slugs):
                    sib = self.known_sibling_by_slug.get(s)
                    if sib and sib not in active_set:
                        books_to_fetch.append(sib)
                        active_set.add(sib)
                res = self.public_client.fetch_books_batch(books_to_fetch)
                if isinstance(res, dict):
                    batch_books = res
            except Exception as exc:
                self.logger.debug("Batch book fetch failed in tick: %s", exc)

        live_portfolio_rate_sec = Decimal("0")
        pair_inputs: dict[str, tuple[Book, IncentiveWindow | None, list[LiveOrder]]] = {}
        for slug in active_slugs:
            mkt_rate = self._quote_market(
                slug,
                now_dt,
                now_ts,
                now_ms,
                exchange_open,
                live_positions,
                buying_power,
                realized_loss_today,
                pair_inputs=pair_inputs,
                book=batch_books.get(slug),
                batch_books=batch_books,
                tick_elapsed_sec=tick_elapsed_sec,
            )
            live_portfolio_rate_sec += mkt_rate

        scored_bid_slugs: set[str] = set()
        if self.condition_pairs:
            for yes_slug, no_slug in self.condition_pairs:
                if yes_slug in pair_inputs and no_slug in pair_inputs:
                    yes_book, window, yes_orders = pair_inputs[yes_slug]
                    no_book, _, no_orders = pair_inputs[no_slug]
                    for snap in estimate_global_pair_score(yes_book, no_book, window, yes_orders + no_orders, ts_ms=now_ms):
                        if tick_elapsed_sec != Decimal("1"):
                            self.ledger.record_lip_snapshot(snap, elapsed_sec=tick_elapsed_sec)
                        else:
                            self.ledger.record_lip_snapshot(snap)
                        if snap.trader_bid_score > Decimal("0"):
                            scored_bid_slugs.add(snap.slug)
                        if snap.qualifies:
                            live_portfolio_rate_sec += snap.reward_usd

        if self.expected_portfolio_rate_sec > Decimal("0"):
            if self.last_prepare_ts <= 0.0:
                self.last_prepare_ts = now_ts
            if (now_ts - self.last_prepare_ts) < YIELD_COLLAPSE_COOLDOWN_SECONDS:
                self.yield_collapse_streak = 0
            elif active_slugs:
                if live_portfolio_rate_sec < (self.expected_portfolio_rate_sec * YIELD_COLLAPSE_RATIO):
                    self.yield_collapse_streak += 1
                else:
                    self.yield_collapse_streak = 0
            if (
                self.yield_collapse_streak >= YIELD_COLLAPSE_TICKS
                and (now_ts - self.last_prepare_ts) >= YIELD_COLLAPSE_COOLDOWN_SECONDS
            ):
                self.logger.info(
                    "Yield collapse detected: live rate %s/s < 55%% of expected %s/s for %d ticks; triggering early reallocation",
                    live_portfolio_rate_sec,
                    self.expected_portfolio_rate_sec,
                    self.yield_collapse_streak,
                )
                self.yield_collapse_streak = 0
                self.last_prepare_ts = now_ts
                self.prepare(is_realloc=True)
                return 1.0

        allocated_bid_budget = sum(
            (v for (s, side), v in self.side_budgets.items() if side == "bid"),
            Decimal("0"),
        )
        if self.settings.venue == "global" and allocated_bid_budget > Decimal("0") and active_slugs:
            if self.last_prepare_ts <= 0.0:
                self.last_prepare_ts = now_ts
            if (now_ts - self.last_prepare_ts) < YIELD_COLLAPSE_COOLDOWN_SECONDS:
                self.underdeployed_streak = 0
            else:
                post_tick_open = self.ledger.get_open_orders()
                open_buy_slugs = {
                    o.slug for o in post_tick_open if o.side in ("BUY", "BUY_LONG")
                }
                deployed_buy_col = sum(
                    (o.price * o.qty_open for o in post_tick_open if o.side in ("BUY", "BUY_LONG")),
                    Decimal("0"),
                )
                filled_cond_credit = Decimal("0")
                for s in self.target_slugs:
                    p_curr = live_positions.get(s)
                    w_curr = self.active_windows.get(s)
                    if (
                        (p_curr is not None and w_curr is not None and p_curr.net_qty >= w_curr.target_size)
                        or (s in scored_bid_slugs and s not in open_buy_slugs)
                    ):
                        filled_cond_credit += self.side_budgets.get((s, "bid"), Decimal("0"))
                effective_deployed = deployed_buy_col + filled_cond_credit
                if effective_deployed < (allocated_bid_budget * CAPITAL_UTILIZATION_RATIO):
                    self.underdeployed_streak += 1
                else:
                    self.underdeployed_streak = 0
                if (
                    self.underdeployed_streak >= CAPITAL_UNDERDEPLOYED_TICKS
                    and (now_ts - self.last_prepare_ts) >= YIELD_COLLAPSE_COOLDOWN_SECONDS
                ):
                    for (s, side), b_val in self.side_budgets.items():
                        if side == "bid" and b_val > Decimal("0") and s not in open_buy_slugs and s not in scored_bid_slugs:
                            p_curr = live_positions.get(s)
                            w_curr = self.active_windows.get(s)
                            if not (p_curr is not None and w_curr is not None and p_curr.net_qty >= w_curr.target_size):
                                self.degraded_slugs_until[s] = now_ts + DEGRADED_SLUG_COOLDOWN_SECONDS
                                for y_s, n_s in self.condition_pairs:
                                    if s in (y_s, n_s):
                                        self.degraded_slugs_until[y_s] = now_ts + DEGRADED_SLUG_COOLDOWN_SECONDS
                                        self.degraded_slugs_until[n_s] = now_ts + DEGRADED_SLUG_COOLDOWN_SECONDS
                    self.logger.info(
                        f"Capital under-deployment detected: deployed $%s < {int(CAPITAL_UTILIZATION_RATIO * 100)}%% of allocated $%s for %d ticks; rotating conditions",
                        effective_deployed,
                        allocated_bid_budget,
                        self.underdeployed_streak,
                    )
                    self.underdeployed_streak = 0
                    self.last_prepare_ts = now_ts
                    self.prepare(is_realloc=True)

        return 1.0

    def _fill_window_start(self, now_ts: float) -> datetime:
        start = max(self.started_at, now_ts - FILL_WINDOW_SECONDS) - FILL_LOOKBACK_SECONDS
        return datetime.fromtimestamp(start, timezone.utc)

    def _in_grace(self, order_id: str, now_ts: float) -> bool:
        created = self.order_created_at.get(order_id)
        return created is not None and now_ts - created < self.settings.order_sync_grace_seconds

    def _ingest_fills(
        self,
        fills: list[ShadowFill],
        live_positions: dict[str, Position] | None = None,
        now_ts: float | None = None,
    ) -> None:
        for f in fills:
            if self.ledger.record_fill(f):
                if hasattr(self.auth_client, "invalidate_positions_cache"):
                    self.auth_client.invalidate_positions_cache()
                ts = now_ts if now_ts is not None else time.time()
                if f.slug in self.local_fill_positions and (ts - self.local_fill_positions[f.slug][1]) <= LOCAL_FILL_POSITION_TTL_SECONDS:
                    base_pos = self.local_fill_positions[f.slug][0]
                elif live_positions is not None and f.slug in live_positions:
                    base_pos = live_positions[f.slug]
                else:
                    base_pos = self.ledger.get_position(f.slug)
                if (
                    f.side in ("SELL", "SELL_LONG")
                    and isinstance(getattr(base_pos, "net_qty", None), Decimal)
                    and isinstance(getattr(base_pos, "avg_price", None), Decimal)
                    and base_pos.net_qty > Decimal("0")
                    and base_pos.avg_price > Decimal("0")
                    and f.price < base_pos.avg_price
                ):
                    cooldown_exp = ts + LOSS_SLUG_COOLDOWN_SECONDS
                    self.loss_cooldown_until[f.slug] = cooldown_exp
                    self.degraded_slugs_until[f.slug] = cooldown_exp
                    self.side_budgets[(f.slug, "bid")] = Decimal("0")
                    if hasattr(self.ledger, "record_loss_cooldown"):
                        try:
                            self.ledger.record_loss_cooldown(f.slug, cooldown_exp)
                        except Exception:
                            pass
                    sib = self.known_sibling_by_slug.get(f.slug)
                    if not sib:
                        for y_s, n_s in self.condition_pairs:
                            if f.slug == y_s:
                                sib = n_s
                                break
                            if f.slug == n_s:
                                sib = y_s
                                break
                    if sib:
                        self.loss_cooldown_until[sib] = cooldown_exp
                        self.degraded_slugs_until[sib] = cooldown_exp
                        self.side_budgets[(sib, "bid")] = Decimal("0")
                        if hasattr(self.ledger, "record_loss_cooldown"):
                            try:
                                self.ledger.record_loss_cooldown(sib, cooldown_exp)
                            except Exception:
                                pass
                updated_pos = update_position_from_fill(base_pos, f.side, f.price, f.qty)
                self.local_fill_positions[f.slug] = (updated_pos, ts)
                self.ledger.update_position(updated_pos, ts_ms=f.ts_ms)
                self.notifier.notify_fill(f)
                self.logger.info("Fill recorded: %s %s %s @ %s (order %s)", f.slug, f.side, f.qty, f.price, f.order_id)

    def _sync_orders(
        self,
        exchange_open: list[LiveOrder],
        filled_order_ids: set[str],
        now_ts: float,
        now_ms: int,
        live_positions: dict[str, Position] | None = None,
    ) -> None:
        exchange_ids = {o.order_id for o in exchange_open}
        local_open = self.ledger.get_open_orders()
        for o in local_open:
            if o.order_id in exchange_ids or self._in_grace(o.order_id, now_ts):
                continue
            status = "FILLED" if o.order_id in filled_order_ids else "CANCELED"
            self.ledger.close_order(o.order_id, status=status, ts_ms=now_ms)
            self.order_created_at.pop(o.order_id, None)
            self.manual_sell_order_ids.discard(o.order_id)
            self.logger.info("Order %s marked %s from exchange sync", o.order_id, status)
            if (
                status == "CANCELED"
                and o.side in ("SELL", "SELL_LONG", "SELL_SHORT")
                and o.order_id not in self.recent_cancels
            ):
                has_replacement_sell = any(
                    eo.slug == o.slug and eo.side in ("SELL", "SELL_LONG", "SELL_SHORT")
                    for eo in exchange_open
                )
                if not has_replacement_sell:
                    self.manual_sell_cooldown_until[o.slug] = now_ts + MANUAL_SELL_EDIT_GRACE_SECONDS
                    self.logger.info(
                        "External SELL cancel detected on %s (order %s); pausing bot SELL placement for %ds to allow manual price adjustment",
                        o.slug,
                        o.order_id,
                        int(MANUAL_SELL_EDIT_GRACE_SECONDS),
                    )

        local_ids = {o.order_id for o in local_open}
        orphans: list[LiveOrder] = []
        for o in exchange_open:
            if o.order_id in local_ids or o.order_id in self.recent_cancels:
                continue
            if o.side in ("SELL", "SELL_LONG", "SELL_SHORT"):
                pos = live_positions.get(o.slug) if live_positions else None
                if pos is None:
                    try:
                        cand_pos = self.ledger.get_position(o.slug)
                        if isinstance(getattr(cand_pos, "net_qty", None), Decimal):
                            pos = cand_pos
                    except Exception:
                        pos = None
                min_exit = (
                    self._min_exit_price(o.slug, pos)
                    if pos is not None and isinstance(getattr(pos, "net_qty", None), Decimal)
                    else Decimal("0")
                )
                if (
                    pos is not None
                    and isinstance(getattr(pos, "net_qty", None), Decimal)
                    and pos.net_qty > Decimal("0")
                    and o.price > Decimal("0")
                ):
                    adopted = dataclasses.replace(o, side="SELL_LONG") if o.side == "SELL" else o
                    self.ledger.record_order(adopted, status="OPEN", ts_ms=now_ms)
                    self.order_created_at[adopted.order_id] = now_ts
                    self.manual_sell_order_ids.add(adopted.order_id)
                    self.manual_sell_cooldown_until.pop(adopted.slug, None)
                    local_ids.add(adopted.order_id)
                    self.logger.info(
                        "Adopted manual SELL order %s on %s: %s @ %s (min_exit=%s)",
                        adopted.order_id,
                        adopted.slug,
                        adopted.qty_open,
                        adopted.price,
                        min_exit,
                    )
                    continue
            if o.slug in self.target_slugs:
                orphans.append(o)
        if orphans:
            self.logger.warning("Canceling %s untracked exchange orders: %s", len(orphans), [o.order_id for o in orphans])
            try:
                self.auth_client.cancel_orders_batch(orphans)
                for o in orphans:
                    self.recent_cancels[o.order_id] = now_ts
                    self.recent_cancel_orders[o.order_id] = (o, now_ts)
            except Exception as exc:
                self.logger.warning("Orphan cancel failed: %s", exc)

        grace = self.settings.order_sync_grace_seconds
        self.recent_cancels = {oid: ts for oid, ts in self.recent_cancels.items() if now_ts - ts < grace}
        self.recent_cancel_orders = {
            oid: (ord_obj, ts) for oid, (ord_obj, ts) in self.recent_cancel_orders.items() if now_ts - ts < grace
        }

    def _risk_open_orders(self, exchange_open: list[LiveOrder], now_ts: float) -> list[LiveOrder]:
        orders = {o.order_id: o for o in exchange_open if o.order_id not in self.recent_cancels}
        for o in self.ledger.get_open_orders():
            if self._in_grace(o.order_id, now_ts):
                orders.setdefault(o.order_id, o)
        return list(orders.values())

    def _slug_open_orders(self, slug: str, exchange_open: list[LiveOrder]) -> list[LiveOrder]:
        merged: list[LiveOrder] = []
        seen_ids: set[str] = set()
        for o in exchange_open:
            if o.slug == slug and o.order_id not in self.recent_cancels:
                merged.append(o)
                if o.order_id:
                    seen_ids.add(o.order_id)
        try:
            local_orders = self.ledger.get_open_orders(slug)
            if isinstance(local_orders, (list, tuple)):
                for o in local_orders:
                    if isinstance(o, LiveOrder) and o.order_id not in self.recent_cancels and o.order_id not in seen_ids:
                        merged.append(o)
                        if o.order_id:
                            seen_ids.add(o.order_id)
        except Exception:
            pass
        return merged

    def _cancel_targets(self, now_ts: float, slugs: list[str] | None = None) -> None:
        target_slugs = slugs if slugs is not None else self.target_slugs
        if not target_slugs:
            return
        protected_slugs: set[str] = set()
        if self.manual_sell_order_ids:
            for s in target_slugs:
                open_on_s = self.ledger.get_open_orders(s)
                if isinstance(open_on_s, (list, tuple)) and any(
                    getattr(o, "order_id", None) in self.manual_sell_order_ids for o in open_on_s
                ):
                    protected_slugs.add(s)
        normal_slugs = [s for s in target_slugs if s not in protected_slugs]
        canceled = set(self.auth_client.cancel_all_scoped(normal_slugs)) if normal_slugs else set()
        local = [
            o
            for slug in target_slugs
            for o in (self.ledger.get_open_orders(slug) or [])
            if getattr(o, "order_id", None) not in self.manual_sell_order_ids
        ]
        leftover = [o for o in local if o.order_id not in canceled]
        if leftover:
            self.auth_client.cancel_orders_batch(leftover)
        now_ms = int(now_ts * 1000)
        for o in local:
            self.ledger.close_order(o.order_id, status="CANCELED", ts_ms=now_ms)
            self.order_created_at.pop(o.order_id, None)
            self.recent_cancels[o.order_id] = now_ts
            self.recent_cancel_orders[o.order_id] = (o, now_ts)

    def _market(self, slug: str) -> Market:
        if slug not in self.markets:
            self.markets[slug] = resolve_market(self.public_client, slug)
        return self.markets[slug]

    def _quote_market(
        self,
        slug: str,
        now_dt: datetime,
        now_ts: float,
        now_ms: int,
        exchange_open: list[LiveOrder],
        live_positions: dict[str, Position],
        buying_power: Decimal,
        realized_loss_today: Decimal,
        pair_inputs: dict[str, tuple[Book, IncentiveWindow | None, list[LiveOrder]]] | None = None,
        book: Book | None = None,
        batch_books: dict[str, Book] | None = None,
        tick_elapsed_sec: Decimal = Decimal("1"),
    ) -> Decimal:
        window = self.active_windows.get(slug)
        try:
            if book is None:
                book = self.public_client.fetch_book(slug)
            market = self._market(slug)
            tick = book.tick_size or market.tick
            pos = live_positions.get(slug, Position(slug=slug, net_qty=Decimal("0"), avg_price=Decimal("0")))
            if abs(pos.net_qty) >= self.settings.max_contracts_per_market:
                self.market_states[slug] = "FLATTENING"
            else:
                self.market_states[slug] = "QUOTING"

            allocated_slug_bid_budget = self.side_budgets.get((slug, "bid"), Decimal("0"))
            inv_freeze_qty = (
                window.target_size
                if (self.settings.venue == "global" and window is not None)
                else market.min_qty
            )
            effective_bid_budget = (
                Decimal("0")
                if pos.net_qty >= inv_freeze_qty
                else allocated_slug_bid_budget
            )

            resting_on_slug = self._slug_open_orders(slug, exchange_open)
            recent_slug_cancels = [
                ord_obj
                for oid, (ord_obj, ts) in self.recent_cancel_orders.items()
                if ord_obj.slug == slug
                and (now_ts - ts) < min(3.0, float(self.settings.order_sync_grace_seconds))
                and all(r.order_id != oid for r in resting_on_slug)
            ]
            strip_candidates = resting_on_slug + recent_slug_cancels
            orders_to_strip = (
                strip_candidates
                if book.bids
                else [o for o in strip_candidates if o.side in ("BUY", "BUY_LONG")]
            )
            quote_book = (
                global_rewards.strip_orders_from_book(book, orders_to_strip)
                if (self.settings.venue == "global" and orders_to_strip)
                else book
            )

            dyn_col = getattr(self.risk_manager, "dynamic_max_collateral_per_market", None)
            dyn_market_col = dyn_col if isinstance(dyn_col, Decimal) else Decimal("0")
            market_budget = max(
                self.settings.max_per_market,
                allocated_slug_bid_budget,
                dyn_market_col,
            )

            if self.settings.venue == "global" and slug not in self.known_sibling_by_slug:
                self._sync_known_siblings()

            other_slug: str | None = None
            other_quote_book: Book | None = None
            other_pos_qty: Decimal = Decimal("0")
            max_allowed: Decimal | None = None
            is_binary_cond = (
                slug in self.paired_slugs
                or any(slug in pair for pair in self.condition_pairs)
                or slug in self.known_sibling_by_slug
                or (slug, "bid") in getattr(self, "side_prices", {})
            )
            if self.settings.venue == "global" and is_binary_cond:
                is_yes_leg = True
                for y, n in self.condition_pairs:
                    if y == slug:
                        other_slug = n
                        is_yes_leg = True
                        break
                    elif n == slug:
                        other_slug = y
                        is_yes_leg = False
                        break
                if other_slug is None and slug in self.known_pair_by_slug:
                    y_k, n_k = self.known_pair_by_slug[slug]
                    if y_k == slug:
                        other_slug = n_k
                        is_yes_leg = True
                    else:
                        other_slug = y_k
                        is_yes_leg = False
                elif other_slug is None and slug in self.known_sibling_by_slug:
                    other_slug = self.known_sibling_by_slug[slug]
                existing_buy_prices = [
                    o.price for o in resting_on_slug
                    if o.side in ("BUY", "BUY_LONG")
                ]
                alloc_px = getattr(self, "side_prices", {}).get((slug, "bid"))
                mid_curr = (
                    global_rewards.adjusted_mid(quote_book, window.target_size)
                    if (window is not None and window.max_spread is not None)
                    else None
                )
                if (
                    alloc_px is not None
                    and mid_curr is not None
                    and window is not None
                    and window.max_spread is not None
                    and abs(alloc_px - mid_curr) >= window.max_spread
                ):
                    max_allowed = None
                else:
                    max_allowed = alloc_px
                if existing_buy_prices and window is not None and window.max_spread is not None:
                    min_existing = min(existing_buy_prices)
                    if mid_curr is not None and abs(min_existing - mid_curr) < window.max_spread:
                        max_allowed = min(max_allowed, min_existing) if max_allowed is not None else min_existing
                if other_slug:
                    other_pos = live_positions.get(
                        other_slug,
                        Position(slug=other_slug, net_qty=Decimal("0"), avg_price=Decimal("0")),
                    )
                    if isinstance(getattr(other_pos, "net_qty", None), Decimal):
                        other_pos_qty = other_pos.net_qty
                    other_win = self.active_windows.get(other_slug) or window
                    other_target_size = (
                        other_win.target_size
                        if other_win is not None
                        else market.min_qty
                    )
                    other_bids = [
                        o.price for o in self._slug_open_orders(other_slug, exchange_open)
                        if o.side in ("BUY", "BUY_LONG")
                    ]
                    other_filled = other_pos.net_qty >= other_target_size
                    if other_filled and other_pos.avg_price > Decimal("0"):
                        other_bids = list(other_bids) + [other_pos.avg_price]
                    if other_bids:
                        other_cap = global_rewards.pair_max_sum(window) - max(other_bids)
                        max_allowed = min(max_allowed, other_cap) if max_allowed is not None else other_cap
                    if batch_books is not None and other_slug in batch_books:
                        opp_raw_book = batch_books.get(other_slug)
                        if opp_raw_book is not None:
                            other_resting = self._slug_open_orders(other_slug, exchange_open)
                            other_orders_to_strip = (
                                other_resting
                                if opp_raw_book.bids
                                else [o for o in other_resting if o.side in ("BUY", "BUY_LONG")]
                            )
                            other_quote_book = (
                                global_rewards.strip_orders_from_book(opp_raw_book, other_orders_to_strip)
                                if other_orders_to_strip
                                else opp_raw_book
                            )
                            if opp_raw_book.bids:
                                opp_market_bid = opp_raw_book.bids[0].price
                                cross_cap = global_rewards.CROSS_MAX_SUM - opp_market_bid
                                max_allowed = min(max_allowed, cross_cap) if max_allowed is not None else cross_cap
                    if other_filled and existing_buy_prices:
                        freeze_cap = max(existing_buy_prices)
                        max_allowed = min(max_allowed, freeze_cap) if max_allowed is not None else freeze_cap
                    if (
                        slug in self.paired_slugs
                        and not other_filled
                        and other_quote_book is not None
                        and window is not None
                    ):
                        yes_qb, no_qb = (
                            (quote_book, other_quote_book)
                            if is_yes_leg
                            else (other_quote_book, quote_book)
                        )
                        pair_px = global_rewards.pair_quote_prices(
                            yes_qb,
                            no_qb,
                            window,
                            yes_qb.tick_size or market.tick,
                            no_qb.tick_size or market.tick,
                        )
                        if pair_px is not None:
                            sym_cap = pair_px[0] if is_yes_leg else pair_px[1]
                            max_allowed = min(max_allowed, sym_cap) if max_allowed is not None else sym_cap

            extra_dq_kwargs: dict[str, Any] = {}
            if self.settings.venue == "global":
                mid_for_unwind = (
                    global_rewards.adjusted_mid(quote_book, window.target_size)
                    if (window is not None and quote_book.bids and quote_book.asks)
                    else (
                        (quote_book.bids[0].price + quote_book.asks[0].price) / Decimal("2")
                        if (quote_book.bids and quote_book.asks)
                        else None
                    )
                )
                extra_dq_kwargs["allow_stop_unwind"] = (
                    pos.avg_price > Decimal("0")
                    and mid_for_unwind is not None
                    and mid_for_unwind <= (pos.avg_price * ACTIVE_STOP_UNWIND_MID_RATIO)
                )
                if is_binary_cond:
                    extra_dq_kwargs["other_book"] = other_quote_book
                    extra_dq_kwargs["max_bid_price"] = max_allowed
                    extra_dq_kwargs["other_position_qty"] = other_pos_qty

            quotes = desired_quotes(
                market=market,
                book=quote_book,
                window=window,
                position=pos,
                budget=market_budget,
                max_contracts_cap=Decimal(str(self.settings.max_contracts_per_market)),
                now=now_dt,
                pre_start_cancel_seconds=self.settings.pre_start_cancel_seconds,
                quote_live_periods=self.settings.quote_live_periods,
                bid_budget=effective_bid_budget,
                ask_budget=self.side_budgets.get((slug, "ask"), Decimal("0")),
                is_global=(self.settings.venue == "global"),
                paired=(slug in self.paired_slugs),
                quote_size=self.side_qtys.get((slug, "bid")),
                **extra_dq_kwargs,
            )

            if self.settings.venue == "global" and is_binary_cond and max_allowed is not None:
                mid_check = (
                    global_rewards.adjusted_mid(quote_book, window.target_size)
                    if (window is not None and window.max_spread is not None)
                    else None
                )
                clamped_quotes: list[Quote] = []
                for q in quotes:
                    if q.side == "BUY_LONG" and q.price > max_allowed:
                        clamped_px = (max_allowed // tick) * tick
                        in_spread = (
                            window is None
                            or window.max_spread is None
                            or mid_check is None
                            or abs(clamped_px - mid_check) <= window.max_spread
                        )
                        if clamped_px > Decimal("0") and in_spread:
                            clamped_quotes.append(dataclasses.replace(q, price=clamped_px))
                    else:
                        clamped_quotes.append(q)
                quotes = clamped_quotes

            min_exit_px = self._min_exit_price(slug, pos)
            if (
                self.settings.venue == "global"
                and allocated_slug_bid_budget > Decimal("0")
                and pos.net_qty > Decimal("0")
                and min_exit_px > Decimal("0")
                and any(q.side == "SELL_LONG" and q.price < min_exit_px for q in quotes)
            ):
                self.degraded_slugs_until[slug] = now_ts + DEGRADED_SLUG_COOLDOWN_SECONDS
                self.side_budgets[(slug, "bid")] = Decimal("0")
                quotes = [q for q in quotes if q.side != "BUY_LONG"]
                if other_slug:
                    self.degraded_slugs_until[other_slug] = now_ts + DEGRADED_SLUG_COOLDOWN_SECONDS
                    self.side_budgets[(other_slug, "bid")] = Decimal("0")

            min_keep_qty = (
                window.target_size
                if (self.settings.venue == "global" and window is not None)
                else market.min_qty
            )
            open_on_slug = self.ledger.get_open_orders(slug)
            if not isinstance(open_on_slug, (list, tuple)):
                open_on_slug = []
            to_create, to_cancel, _ = reconcile_quotes(
                list(open_on_slug),
                quotes,
                min_keep_qty=min_keep_qty,
            )

            manual_sells_on_slug = [
                o for o in open_on_slug
                if isinstance(o, LiveOrder)
                and o.order_id in self.manual_sell_order_ids
                and o.side in ("SELL", "SELL_LONG", "SELL_SHORT")
                and o.price > Decimal("0")
            ]
            if manual_sells_on_slug:
                manual_ids = {o.order_id for o in manual_sells_on_slug}
                to_cancel = [o for o in to_cancel if o.order_id not in manual_ids]
                to_create = [q for q in to_create if q.side not in ("SELL", "SELL_LONG", "SELL_SHORT")]
            elif now_ts < self.manual_sell_cooldown_until.get(slug, 0.0):
                to_create = [q for q in to_create if q.side not in ("SELL", "SELL_LONG", "SELL_SHORT")]

            if self.settings.venue == "global" and other_slug and batch_books is not None:
                opp_raw_book = batch_books.get(other_slug)
                if opp_raw_book is not None and opp_raw_book.bids:
                    opp_market_bid = opp_raw_book.bids[0].price
                    for o in resting_on_slug:
                        if o.side in ("BUY", "BUY_LONG") and (o.price + opp_market_bid) > global_rewards.CROSS_MAX_SUM:
                            if o not in to_cancel:
                                to_cancel.append(o)
                    to_create = [
                        q for q in to_create
                        if not (q.side in ("BUY", "BUY_LONG") and (q.price + opp_market_bid) > global_rewards.CROSS_MAX_SUM)
                    ]

            if (
                self.settings.venue == "global"
                and slug in self.paired_slugs
                and window is not None
                and window.max_spread is not None
                and to_create
                and to_cancel
            ):
                mid = global_rewards.adjusted_mid(quote_book, window.target_size)
                if mid is not None and quote_book.asks:
                    margin = max(tick, Decimal("0.005"))
                    max_keep_dist = min(window.max_spread - margin, window.max_spread * Decimal("0.55"))
                    max_price_lag = Decimal("2") * tick
                    kept_cancel_ids: set[str] = set()
                    kept_create_sides: set[str] = set()
                    for old_o in to_cancel:
                        if old_o.side == "BUY_LONG":
                            if other_slug and batch_books is not None:
                                opp_raw_book = batch_books.get(other_slug)
                                if opp_raw_book is not None and opp_raw_book.bids:
                                    if (old_o.price + opp_raw_book.bids[0].price) > global_rewards.CROSS_MAX_SUM:
                                        continue
                            for new_q in to_create:
                                if (
                                    new_q.side == "BUY_LONG"
                                    and new_q.price > old_o.price
                                    and (new_q.price - old_o.price) <= max_price_lag
                                    and new_q.qty == old_o.remaining_qty
                                    and old_o.price < quote_book.asks[0].price
                                    and abs(old_o.price - mid) <= max_keep_dist
                                ):
                                    kept_cancel_ids.add(old_o.order_id)
                                    kept_create_sides.add("BUY_LONG")
                    if kept_cancel_ids:
                        to_cancel = [o for o in to_cancel if o.order_id not in kept_cancel_ids]
                        to_create = [q for q in to_create if q.side not in kept_create_sides]

            if to_create and to_cancel and pos.net_qty > Decimal("0"):
                best_bid_px = quote_book.bids[0].price if quote_book.bids else Decimal("0")
                kept_sell_cancel_ids: set[str] = set()
                kept_sell_create_sides: set[str] = set()
                for old_o in to_cancel:
                    if old_o.side in ("SELL", "SELL_LONG") and old_o.price > best_bid_px:
                        for new_q in to_create:
                            if (
                                new_q.side == "SELL_LONG"
                                and new_q.qty == old_o.remaining_qty
                                and ((old_o.price >= min_exit_px and new_q.price >= min_exit_px) or new_q.price >= old_o.price)
                                and abs(new_q.price - old_o.price) <= SELL_HYSTERESIS_BAND
                            ):
                                kept_sell_cancel_ids.add(old_o.order_id)
                                kept_sell_create_sides.add("SELL_LONG")
                if kept_sell_cancel_ids:
                    to_cancel = [o for o in to_cancel if o.order_id not in kept_sell_cancel_ids]
                    to_create = [q for q in to_create if q.side not in kept_sell_create_sides]

            cancel_ids = {o.order_id for o in to_cancel if o.order_id}
            risk_open = self._risk_open_orders(exchange_open, now_ts)
            post_cancel_open = [o for o in risk_open if o.order_id not in cancel_ids]

            risk_res = self.risk_manager.check_pre_submit(
                new_quotes=to_create,
                open_orders=post_cancel_open,
                positions=live_positions,
                buying_power=buying_power,
                realized_loss_today=realized_loss_today,
                now=now_dt,
            )
            if not risk_res.allowed:
                self.logger.warning("Risk gate blocked submission on %s: %s", slug, risk_res.reason)
                self.market_states[slug] = "PAUSED"
                return Decimal("0")

            if to_cancel:
                self.auth_client.cancel_orders_batch(to_cancel)
                for o in to_cancel:
                    self.ledger.close_order(o.order_id, status="CANCELED", ts_ms=now_ms)
                    self.order_created_at.pop(o.order_id, None)
                    self.recent_cancels[o.order_id] = now_ts
                    self.recent_cancel_orders[o.order_id] = (o, now_ts)

            if to_create:
                created = self.auth_client.create_orders_batch(to_create, {slug: tick}, {slug: market.min_qty})
                for o in created:
                    self.ledger.record_order(o, status="OPEN", ts_ms=now_ms)
                    self.order_created_at[o.order_id] = now_ts

            self.market_states[slug] = "QUOTING"

            orders = self.ledger.get_open_orders(slug)
            is_global = (self.settings.venue == "global")
            if slug in self.paired_slugs:
                if pair_inputs is not None:
                    pair_inputs[slug] = (book, window, orders)
                return Decimal("0")
            elif is_global:
                snap = estimate_global_lip_score(book, window, orders, ts_ms=now_ms)
                if tick_elapsed_sec != Decimal("1"):
                    self.ledger.record_lip_snapshot(snap, elapsed_sec=tick_elapsed_sec)
                else:
                    self.ledger.record_lip_snapshot(snap)
                return snap.reward_usd if snap.qualifies else Decimal("0")
            else:
                snap = estimate_lip_score(book, window, orders, market.tick, ts_ms=now_ms)
                if tick_elapsed_sec != Decimal("1"):
                    self.ledger.record_lip_snapshot(snap, elapsed_sec=tick_elapsed_sec)
                else:
                    self.ledger.record_lip_snapshot(snap)
                return snap.reward_usd if snap.qualifies else Decimal("0")
        except Exception as exc:
            err_str = str(exc).lower()
            if "not enough balance" in err_str or "allowance" in err_str:
                self.slug_cooldown[slug] = now_ts + 5.0
                if hasattr(self.auth_client, "invalidate_positions_cache"):
                    self.auth_client.invalidate_positions_cache()
            else:
                self.slug_cooldown[slug] = now_ts + 30.0
            self.market_states[slug] = "PAUSED"
            self.logger.warning("Error processing %s: %s", slug, exc)
            return Decimal("0")

    def handle_order_update(self, event: dict) -> None:
        now_ms = int(time.time() * 1000)
        update_body = event.get("orderSubscriptionUpdate") or event.get("orderUpdate") or event
        execution = update_body.get("execution") or {}
        order = execution.get("order") or {}
        order_id = str(order.get("id") or execution.get("id") or "")
        if not order_id:
            return

        exec_type = execution.get("type", "")
        order_state = order.get("state", "")

        if exec_type in ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL") or order_state in ("ORDER_STATE_FILLED", "ORDER_STATE_PARTIALLY_FILLED"):
            self.ledger.close_order(order_id, status="FILLED", ts_ms=now_ms)
            self.logger.info("Order %s filled via websocket update", order_id)
        elif exec_type == "EXECUTION_TYPE_CANCELED" or order_state == "ORDER_STATE_CANCELED":
            self.ledger.close_order(order_id, status="CANCELED", ts_ms=now_ms)
            self.logger.info("Order %s canceled via websocket update", order_id)

    def handle_position_update(self, event: dict) -> None:
        now_ms = int(time.time() * 1000)
        pos_sub = event.get("positionSubscription") or event.get("positionSubscriptionUpdate") or event.get("positionUpdate") or event
        after_pos = pos_sub.get("afterPosition") or {}
        if not after_pos:
            return

        meta = after_pos.get("marketMetadata") or {}
        slug = str(meta.get("slug") or after_pos.get("positionId") or "")
        if not slug:
            return

        net_str = after_pos.get("netPositionDecimal") or after_pos.get("netPosition") or "0"
        net_qty = Decimal(str(net_str))
        avg_px_dict = after_pos.get("avgPx") or {}
        avg_price = Decimal(str(avg_px_dict.get("value", "0")))

        pos = Position(slug=slug, net_qty=net_qty, avg_price=avg_price)
        self.ledger.update_position(pos, ts_ms=now_ms)
        self.logger.info("Position for %s updated to net=%s avg_px=%s via websocket", slug, net_qty, avg_price)
