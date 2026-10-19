import dataclasses
import logging
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
from pmus.strategy.lip_quoter import desired_quotes
from pmus.strategy.market_selector import AllocationResult, greedy_allocate
from pmus.venue import AuthenticatedClient, PublicGatewayClient

MarketState = Literal["IDLE", "QUOTING", "PAUSED", "FLATTENING"]

if not hasattr(LiveOrder, "remaining_qty"):
    setattr(LiveOrder, "remaining_qty", property(lambda self: self.qty_open))

CANDIDATE_BOOK_LIMIT = 3000
FALLBACK_BOOK_CONDITION_LIMIT = 150
FILL_LOOKBACK_SECONDS = 60
FILL_WINDOW_SECONDS = 3600
LOCAL_FILL_POSITION_TTL_SECONDS = 60.0
REALLOCATE_SECONDS = 300
YIELD_COLLAPSE_COOLDOWN_SECONDS = 90.0
YIELD_COLLAPSE_RATIO = Decimal("0.55")
YIELD_COLLAPSE_TICKS = 15

def reconcile_quotes(
    current_orders: list[LiveOrder],
    desired_quotes_list: list[Quote],
) -> tuple[list[Quote], list[LiveOrder], list[LiveOrder]]:
    to_keep: list[LiveOrder] = []
    to_cancel: list[LiveOrder] = []
    to_create: list[Quote] = []

    remaining_current = list(current_orders)

    for quote in desired_quotes_list:
        matched = False
        for i, order in enumerate(remaining_current):
            if order.side == quote.side and order.price == quote.price and order.qty_open == quote.qty:
                to_keep.append(order)
                remaining_current.pop(i)
                matched = True
                break
        if not matched:
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

    return Position(slug=current_pos.slug, net_qty=new_qty, avg_price=new_avg)

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

def allocate_markets(
    settings: Settings,
    client: Any,
    logger: logging.Logger,
    available_budget: Decimal | None = None,
    positions: dict[str, Position] | None = None,
    open_orders: list[LiveOrder] | None = None,
) -> tuple[dict[str, IncentiveWindow], AllocationResult]:
    budget = available_budget if available_budget is not None else settings.total_budget
    is_global = settings.venue == "global"
    if is_global and hasattr(client, "fetch_reward_conditions"):
        conditions = client.fetch_reward_conditions()
        base_cond_cap = min(settings.max_per_market, settings.max_collateral_per_market)
        num_pairs = max(1, settings.max_markets // 2)
        max_cost_per_condition = (
            max(base_cond_cap, budget / Decimal(str(num_pairs)))
            if available_budget is not None
            else base_cond_cap
        )
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

        allocatable = [c for c in conditions if "temperature in " not in c.question.lower()]
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

        effective_max_conditions = (
            min(settings.max_markets, max(1, max(settings.max_markets // 2, int(budget // Decimal("48")))))
            if (settings.pair_preference <= Decimal("0") and available_budget is not None)
            else settings.max_markets
        )
        targets = allocate_conditions(
            conditions=allocatable,
            books=candidate_books,
            total_budget=budget,
            max_cost_per_condition=max_cost_per_condition,
            max_markets=effective_max_conditions,
            max_contracts=max_contracts,
            pair_preference=settings.pair_preference,
            positions=positions,
        )
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
        self.yield_collapse_streak: int = 0
        self.dead_unwind_slugs: set[str] = set()

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
                if not paused and tick_start - self.last_prepare_ts >= REALLOCATE_SECONDS:
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
                fee_buffer = (
                    Decimal("2.50")
                    if self.settings.venue == "global"
                    else max(Decimal("3.00"), bp * Decimal("0.05"))
                )
                effective_budget = min(self.settings.total_budget, max(Decimal("0"), bp - fee_buffer))
        except Exception as exc:
            self.logger.warning("Could not fetch buying power for allocation: %s", exc)
        held_positions = None
        pos_map = None
        try:
            held_positions = self.auth_client.get_positions()
            if isinstance(held_positions, list):
                pos_map = {p.slug: p for p in held_positions}
        except Exception as exc:
            self.logger.warning("Could not fetch held positions for allocation: %s", exc)

        now_prep = time.time()
        for slug, (opt_pos, fill_ts) in list(self.local_fill_positions.items()):
            if now_prep - fill_ts > LOCAL_FILL_POSITION_TTL_SECONDS:
                self.local_fill_positions.pop(slug, None)
            else:
                if pos_map is None:
                    pos_map = {}
                pos_map[slug] = opt_pos

        try:
            self.active_windows, alloc_res = allocate_markets(
                self.settings,
                self.public_client,
                self.logger,
                available_budget=effective_budget,
                positions=pos_map,
                open_orders=open_orders,
            )
        except Exception as exc:
            if is_realloc:
                self.logger.warning("Reallocation failed, retaining current targets: %s", exc)
                self.last_prepare_ts = time.time()
                self.yield_collapse_streak = 0
                return
            raise
        self.expected_portfolio_rate_sec = sum(
            (c.expected_reward_sec for c in alloc_res.selected_candidates),
            Decimal("0"),
        )
        self.last_prepare_ts = time.time()
        self.yield_collapse_streak = 0
        self.target_slugs = list(alloc_res.selected_slugs)
        self.side_budgets = dict(alloc_res.side_budgets)
        self.side_qtys = dict(alloc_res.side_qtys)
        self.side_prices = dict(getattr(alloc_res, "side_prices", {}) or {})
        self.paired_slugs = alloc_res.paired_slugs
        self.condition_pairs = alloc_res.condition_pairs
        if pos_map is not None:
            now_utc = datetime.now(timezone.utc)
            for p in pos_map.values():
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
            self._cancel_targets(self.started_at)
            target_set = set(self.target_slugs)
            all_open = open_orders
            if not all_open:
                try:
                    all_open = self.auth_client.get_open_orders()
                except Exception as exc:
                    self.logger.warning("Could not fetch open orders for startup cancel: %s", exc)
                    all_open = []
            non_target_orders = [o for o in all_open if o.slug not in target_set]
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

    def tick(self, now_ts: float) -> float:
        now_dt = datetime.fromtimestamp(now_ts, timezone.utc)
        now_ms = int(now_ts * 1000)

        is_kill = self.risk_manager.is_kill_switch_active()
        if is_kill != self.kill_switch_active:
            self.kill_switch_active = is_kill
            self.notifier.notify_kill_switch(is_kill)
        if is_kill:
            self.yield_collapse_streak = 0
            self.logger.warning("Kill switch active! Canceling orders and pausing.")
            self._cancel_targets(now_ts)
            return 5.0

        if self.risk_manager.is_maintenance_active(now_dt):
            self.yield_collapse_streak = 0
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
        self._sync_orders(exchange_open, {f.order_id for f in fills}, now_ts, now_ms)
        raw_realized_loss_today = self.ledger.get_realized_loss_today(now_dt)
        baseline = getattr(self.settings, "daily_loss_baseline", Decimal("0"))
        realized_loss_today = max(Decimal("0"), raw_realized_loss_today - baseline)
        if realized_loss_today >= self.settings.daily_loss_stop:
            self.yield_collapse_streak = 0
            if not self.daily_loss_stop_tripped:
                self.daily_loss_stop_tripped = True
                self.notifier.notify_daily_loss_stop(realized_loss_today, self.settings.daily_loss_stop)
            self.logger.warning(
                "Daily loss stop tripped ($%s >= $%s)! Canceling all orders and pausing.",
                realized_loss_today,
                self.settings.daily_loss_stop,
            )
            self._cancel_targets(now_ts)
            return 10.0
        self.daily_loss_stop_tripped = False

        active_slugs = [
            s for s in self.target_slugs
            if now_ts >= self.slug_cooldown.get(s, 0.0)
        ]
        batch_books: dict[str, Book] = {}
        if hasattr(self.public_client, "fetch_books_batch") and active_slugs:
            try:
                res = self.public_client.fetch_books_batch(active_slugs)
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
            )
            live_portfolio_rate_sec += mkt_rate

        if self.condition_pairs:
            for yes_slug, no_slug in self.condition_pairs:
                if yes_slug in pair_inputs and no_slug in pair_inputs:
                    yes_book, window, yes_orders = pair_inputs[yes_slug]
                    no_book, _, no_orders = pair_inputs[no_slug]
                    for snap in estimate_global_pair_score(yes_book, no_book, window, yes_orders + no_orders, ts_ms=now_ms):
                        self.ledger.record_lip_snapshot(snap)
                        if snap.qualifies:
                            live_portfolio_rate_sec += snap.reward_usd

        if self.expected_portfolio_rate_sec > Decimal("0"):
            if self.last_prepare_ts <= 0.0:
                self.last_prepare_ts = now_ts
            if len(active_slugs) == len(self.target_slugs):
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
                updated_pos = update_position_from_fill(base_pos, f.side, f.price, f.qty)
                self.local_fill_positions[f.slug] = (updated_pos, ts)
                self.ledger.update_position(updated_pos, ts_ms=f.ts_ms)
                self.notifier.notify_fill(f)
                self.logger.info("Fill recorded: %s %s %s @ %s (order %s)", f.slug, f.side, f.qty, f.price, f.order_id)

    def _sync_orders(self, exchange_open: list[LiveOrder], filled_order_ids: set[str], now_ts: float, now_ms: int) -> None:
        exchange_ids = {o.order_id for o in exchange_open}
        local_open = self.ledger.get_open_orders()
        for o in local_open:
            if o.order_id in exchange_ids or self._in_grace(o.order_id, now_ts):
                continue
            status = "FILLED" if o.order_id in filled_order_ids else "CANCELED"
            self.ledger.close_order(o.order_id, status=status, ts_ms=now_ms)
            self.order_created_at.pop(o.order_id, None)
            self.logger.info("Order %s marked %s from exchange sync", o.order_id, status)

        local_ids = {o.order_id for o in local_open}
        orphans = [
            o for o in exchange_open
            if o.slug in self.target_slugs and o.order_id not in local_ids and o.order_id not in self.recent_cancels
        ]
        if orphans:
            self.logger.warning("Canceling %s untracked exchange orders: %s", len(orphans), [o.order_id for o in orphans])
            try:
                self.auth_client.cancel_orders_batch(orphans)
                for o in orphans:
                    self.recent_cancels[o.order_id] = now_ts
            except Exception as exc:
                self.logger.warning("Orphan cancel failed: %s", exc)

        grace = self.settings.order_sync_grace_seconds
        self.recent_cancels = {oid: ts for oid, ts in self.recent_cancels.items() if now_ts - ts < grace}

    def _risk_open_orders(self, exchange_open: list[LiveOrder], now_ts: float) -> list[LiveOrder]:
        orders = {o.order_id: o for o in exchange_open if o.order_id not in self.recent_cancels}
        for o in self.ledger.get_open_orders():
            if self._in_grace(o.order_id, now_ts):
                orders.setdefault(o.order_id, o)
        return list(orders.values())

    def _cancel_targets(self, now_ts: float, slugs: list[str] | None = None) -> None:
        target_slugs = slugs if slugs is not None else self.target_slugs
        if not target_slugs:
            return
        canceled = set(self.auth_client.cancel_all_scoped(target_slugs))
        local = [o for slug in target_slugs for o in self.ledger.get_open_orders(slug)]
        leftover = [o for o in local if o.order_id not in canceled]
        if leftover:
            self.auth_client.cancel_orders_batch(leftover)
        now_ms = int(now_ts * 1000)
        for o in local:
            self.ledger.close_order(o.order_id, status="CANCELED", ts_ms=now_ms)
            self.order_created_at.pop(o.order_id, None)
            self.recent_cancels[o.order_id] = now_ts

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

            inv_freeze_qty = (
                window.target_size
                if (self.settings.venue == "global" and window is not None)
                else market.min_qty
            )
            effective_bid_budget = (
                Decimal("0")
                if pos.net_qty >= inv_freeze_qty
                else self.side_budgets.get((slug, "bid"), Decimal("0"))
            )

            resting_on_slug = [
                o for o in exchange_open if o.slug == slug and o.order_id not in self.recent_cancels
            ] or self.ledger.get_open_orders(slug)
            orders_to_strip = (
                resting_on_slug
                if book.bids
                else [o for o in resting_on_slug if o.side in ("BUY", "BUY_LONG")]
            )
            quote_book = (
                global_rewards.strip_orders_from_book(book, orders_to_strip)
                if (self.settings.venue == "global" and orders_to_strip)
                else book
            )

            quotes = desired_quotes(
                market=market,
                book=quote_book,
                window=window,
                position=pos,
                budget=self.settings.max_per_market,
                max_contracts_cap=Decimal(str(self.settings.max_contracts_per_market)),
                now=now_dt,
                pre_start_cancel_seconds=self.settings.pre_start_cancel_seconds,
                quote_live_periods=self.settings.quote_live_periods,
                bid_budget=effective_bid_budget,
                ask_budget=self.side_budgets.get((slug, "ask"), Decimal("0")),
                is_global=(self.settings.venue == "global"),
                paired=(slug in self.paired_slugs),
                quote_size=self.side_qtys.get((slug, "bid")),
            )

            if self.settings.venue == "global" and slug in self.paired_slugs:
                other_slug = None
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
                existing_buy_prices = [
                    o.price for o in resting_on_slug
                    if o.side in ("BUY", "BUY_LONG")
                ]
                alloc_px = getattr(self, "side_prices", {}).get((slug, "bid"))
                max_allowed: Decimal | None = alloc_px
                if existing_buy_prices and window is not None and window.max_spread is not None:
                    min_existing = min(existing_buy_prices)
                    mid_curr = global_rewards.adjusted_mid(quote_book, window.target_size)
                    if mid_curr is not None and abs(min_existing - mid_curr) < window.max_spread:
                        max_allowed = min(max_allowed, min_existing) if max_allowed is not None else min_existing
                if other_slug:
                    other_pos = live_positions.get(
                        other_slug,
                        Position(slug=other_slug, net_qty=Decimal("0"), avg_price=Decimal("0")),
                    )
                    other_win = self.active_windows.get(other_slug) or window
                    other_target_size = (
                        other_win.target_size
                        if other_win is not None
                        else market.min_qty
                    )
                    other_bids = [
                        o.price for o in exchange_open
                        if o.slug == other_slug
                        and o.side in ("BUY", "BUY_LONG")
                        and o.order_id not in self.recent_cancels
                    ] or [
                        o.price for o in self.ledger.get_open_orders(other_slug)
                        if o.side in ("BUY", "BUY_LONG")
                    ]
                    other_filled = other_pos.net_qty >= other_target_size
                    if other_filled and other_pos.avg_price > Decimal("0"):
                        other_bids = list(other_bids) + [other_pos.avg_price]
                    if other_bids:
                        other_cap = global_rewards.pair_max_sum(window) - max(other_bids)
                        max_allowed = min(max_allowed, other_cap) if max_allowed is not None else other_cap
                    if other_filled and existing_buy_prices:
                        freeze_cap = max(existing_buy_prices)
                        max_allowed = min(max_allowed, freeze_cap) if max_allowed is not None else freeze_cap
                    if (
                        not other_filled
                        and batch_books is not None
                        and other_slug in batch_books
                        and window is not None
                    ):
                        other_raw_book = batch_books[other_slug]
                        other_resting = [
                            o for o in exchange_open
                            if o.slug == other_slug and o.order_id not in self.recent_cancels
                        ] or self.ledger.get_open_orders(other_slug)
                        other_orders_to_strip = (
                            other_resting
                            if other_raw_book.bids
                            else [o for o in other_resting if o.side in ("BUY", "BUY_LONG")]
                        )
                        other_quote_book = (
                            global_rewards.strip_orders_from_book(other_raw_book, other_orders_to_strip)
                            if other_orders_to_strip
                            else other_raw_book
                        )
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
                if max_allowed is not None:
                    clamped_quotes: list[Quote] = []
                    for q in quotes:
                        if q.side == "BUY_LONG" and q.price > max_allowed:
                            clamped_px = (max_allowed // tick) * tick
                            if clamped_px > Decimal("0"):
                                clamped_quotes.append(dataclasses.replace(q, price=clamped_px))
                        else:
                            clamped_quotes.append(q)
                    quotes = clamped_quotes

            to_create, to_cancel, _ = reconcile_quotes(self.ledger.get_open_orders(slug), quotes)

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
                self.ledger.record_lip_snapshot(snap)
                return snap.reward_usd if snap.qualifies else Decimal("0")
            else:
                snap = estimate_lip_score(book, window, orders, market.tick, ts_ms=now_ms)
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
