import dataclasses
import logging
import time
import uuid
from datetime import datetime, timezone
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

CANDIDATE_BOOK_LIMIT = 60
FILL_LOOKBACK_SECONDS = 60
FILL_WINDOW_SECONDS = 3600
REALLOCATE_SECONDS = 1800

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
    market = client.get_market(slug) if hasattr(client, "get_market") else None
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
) -> tuple[dict[str, IncentiveWindow], AllocationResult]:
    budget = available_budget if available_budget is not None else settings.total_budget
    is_global = settings.venue == "global"
    if is_global and hasattr(client, "fetch_reward_conditions"):
        conditions = client.fetch_reward_conditions()
        active_windows: dict[str, IncentiveWindow] = {}
        for cond in conditions:
            active_windows[cond.yes.slug] = dataclasses.replace(cond.window, slug=cond.yes.slug)
            active_windows[cond.no.slug] = dataclasses.replace(cond.window, slug=cond.no.slug)

        candidate_books: dict[str, Book] = {}
        for cond in conditions[: CANDIDATE_BOOK_LIMIT // 2]:
            try:
                yes_book = client.fetch_book(cond.yes.slug)
                no_book = client.fetch_book(cond.no.slug)
            except Exception as e:
                logger.debug("Failed to fetch candidate books for condition %s: %s", cond.condition_id, e)
                continue
            candidate_books[cond.yes.slug] = yes_book
            candidate_books[cond.no.slug] = no_book

        targets = allocate_conditions(
            conditions=conditions,
            books=candidate_books,
            total_budget=budget,
            max_cost_per_condition=min(settings.max_per_market, settings.max_collateral_per_market),
            max_markets=settings.max_markets,
            max_contracts=Decimal(str(settings.max_contracts_per_market)),
            pair_preference=settings.pair_preference,
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
        self.paired_slugs: frozenset[str] = frozenset()
        self.condition_pairs: tuple[tuple[str, str], ...] = ()
        self.markets: dict[str, Market] = {}
        self.started_at: float = time.time()
        self.order_created_at: dict[str, float] = {}
        self.recent_cancels: dict[str, float] = {}
        self.slug_cooldown: dict[str, float] = {}

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
        next_realloc = time.time() + REALLOCATE_SECONDS
        try:
            while time.time() < end_time:
                tick_start = time.time()
                if tick_start >= next_realloc:
                    self._cancel_targets(tick_start)
                    self.prepare()
                    next_realloc = tick_start + REALLOCATE_SECONDS
                period = self.tick(tick_start) if self.target_slugs else 30.0
                time.sleep(max(0.1, period - (time.time() - tick_start)))
        finally:
            self.shutdown()

    def prepare(self) -> None:
        effective_budget = self.settings.total_budget
        try:
            bp = self.auth_client.get_buying_power()
            if bp > Decimal("0"):
                fee_buffer = Decimal("0.75")
                effective_budget = min(self.settings.total_budget, max(Decimal("0"), bp - fee_buffer))
        except Exception as exc:
            self.logger.warning("Could not fetch buying power for allocation: %s", exc)
        self.active_windows, alloc_res = allocate_markets(self.settings, self.public_client, self.logger, available_budget=effective_budget)
        self.target_slugs = alloc_res.selected_slugs
        self.side_budgets = alloc_res.side_budgets
        self.side_qtys = alloc_res.side_qtys
        self.paired_slugs = alloc_res.paired_slugs
        self.condition_pairs = alloc_res.condition_pairs
        for slug in self.target_slugs:
            self.market_states[slug] = "IDLE"
        self.logger.info("Scoped startup cancel for target slugs: %s", self.target_slugs)
        self.started_at = time.time()
        self._cancel_targets(self.started_at)

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
            self.logger.warning("Kill switch active! Canceling orders and pausing.")
            self._cancel_targets(now_ts)
            return 5.0

        if self.risk_manager.is_maintenance_active(now_dt):
            self.logger.warning("Exchange weekly maintenance window active! Canceling orders and pausing.")
            self._cancel_targets(now_ts)
            return 10.0

        try:
            exchange_open = self.auth_client.get_open_orders()
            fills = self.auth_client.get_fills(since=self._fill_window_start(now_ts))
            positions = self.auth_client.get_positions()
            buying_power = self.auth_client.get_buying_power()
        except Exception as exc:
            self.logger.warning("Exchange sync failed, skipping tick: %s", exc)
            return 1.0

        self._ingest_fills(fills)
        self._sync_orders(exchange_open, {f.order_id for f in fills}, now_ts, now_ms)

        live_positions = {p.slug: p for p in positions}
        realized_loss_today = self.ledger.get_realized_loss_today(now_dt)
        if realized_loss_today >= self.settings.daily_loss_stop:
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

        for slug in self.target_slugs:
            if now_ts < self.slug_cooldown.get(slug, 0.0):
                continue
            self._quote_market(slug, now_dt, now_ts, now_ms, exchange_open, live_positions, buying_power, realized_loss_today)
        return 1.0

    def _fill_window_start(self, now_ts: float) -> datetime:
        start = max(self.started_at, now_ts - FILL_WINDOW_SECONDS) - FILL_LOOKBACK_SECONDS
        return datetime.fromtimestamp(start, timezone.utc)

    def _in_grace(self, order_id: str, now_ts: float) -> bool:
        created = self.order_created_at.get(order_id)
        return created is not None and now_ts - created < self.settings.order_sync_grace_seconds

    def _ingest_fills(self, fills: list[ShadowFill]) -> None:
        for f in fills:
            if self.ledger.record_fill(f):
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

    def _cancel_targets(self, now_ts: float) -> None:
        if not self.target_slugs:
            return
        canceled = set(self.auth_client.cancel_all_scoped(self.target_slugs))
        local = [o for slug in self.target_slugs for o in self.ledger.get_open_orders(slug)]
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
    ) -> None:
        window = self.active_windows.get(slug)
        try:
            book = self.public_client.fetch_book(slug)
            market = self._market(slug)
            pos = live_positions.get(slug, Position(slug=slug, net_qty=Decimal("0"), avg_price=Decimal("0")))
            if abs(pos.net_qty) >= self.settings.max_contracts_per_market:
                self.market_states[slug] = "FLATTENING"
            else:
                self.market_states[slug] = "QUOTING"

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
                bid_budget=self.side_budgets.get((slug, "bid"), Decimal("0")),
                ask_budget=self.side_budgets.get((slug, "ask"), Decimal("0")),
                is_global=(self.settings.venue == "global"),
                paired=(slug in self.paired_slugs),
                quote_size=self.side_qtys.get((slug, "bid")),
            )

            to_create, to_cancel, _ = reconcile_quotes(self.ledger.get_open_orders(slug), quotes)

            if to_cancel:
                self.auth_client.cancel_orders_batch(to_cancel)
                for o in to_cancel:
                    self.ledger.close_order(o.order_id, status="CANCELED", ts_ms=now_ms)
                    self.order_created_at.pop(o.order_id, None)
                    self.recent_cancels[o.order_id] = now_ts

            risk_res = self.risk_manager.check_pre_submit(
                new_quotes=to_create,
                open_orders=self._risk_open_orders(exchange_open, now_ts),
                positions=live_positions,
                buying_power=buying_power,
                realized_loss_today=realized_loss_today,
                now=now_dt,
            )
            if not risk_res.allowed:
                self.logger.warning("Risk gate blocked submission on %s: %s", slug, risk_res.reason)
                self.market_states[slug] = "PAUSED"
                return

            if to_create:
                created = self.auth_client.create_orders_batch(to_create, {slug: market.tick}, {slug: market.min_qty})
                for o in created:
                    self.ledger.record_order(o, status="OPEN", ts_ms=now_ms)
                    self.order_created_at[o.order_id] = now_ts

            self.market_states[slug] = "QUOTING"
        except Exception as exc:
            self.slug_cooldown[slug] = now_ts + 30.0
            self.market_states[slug] = "PAUSED"
            self.logger.warning("Error processing %s: %s", slug, exc)

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
