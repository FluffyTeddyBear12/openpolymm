import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pmus.config import Settings
from pmus.ledger import Ledger
from pmus.models import Book, IncentiveWindow, LiveOrder, Market, Position, Quote
from pmus.risk import RiskManager
from pmus.shadow import check_paper_fills, estimate_lip_score
from pmus.strategy.lip_quoter import desired_quotes
from pmus.strategy.market_selector import rank_markets
from pmus.venue import AuthenticatedClient, PublicGatewayClient

MarketState = Literal["IDLE", "QUOTING", "PAUSED", "FLATTENING"]

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

class ShadowEngine:
    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        client: PublicGatewayClient,
        logger: logging.Logger,
    ):
        self.settings = settings
        self.ledger = ledger
        self.client = client
        self.logger = logger
        self.market_states: dict[str, MarketState] = {}

    def run(self, duration_seconds: int = 600) -> None:
        self.logger.info("Starting ShadowEngine for %s seconds", duration_seconds)
        end_time = time.time() + duration_seconds

        windows = self.client.fetch_incentives(status="active", page_size=200)
        active_windows: dict[str, IncentiveWindow] = {}
        candidate_books: dict[str, Book] = {}

        now_dt = datetime.now(timezone.utc)
        for w in windows:
            if w.status.lower() == "active" and w.slug not in active_windows:
                active_windows[w.slug] = w

        for slug in list(active_windows.keys())[:15]:
            try:
                candidate_books[slug] = self.client.fetch_book(slug)
            except Exception as e:
                self.logger.debug("Failed to fetch candidate book %s: %s", slug, e)

        target_slugs = rank_markets(
            windows=list(active_windows.values()),
            books=candidate_books,
            now=now_dt,
            max_n=self.settings.max_markets,
        )

        if not target_slugs:
            target_slugs = list(active_windows.keys())[: self.settings.max_markets]

        market_budget = min(
            self.settings.max_per_market,
            self.settings.total_budget / Decimal(str(max(1, len(target_slugs)))),
        )

        market_metadata: dict[str, Market] = {}
        for slug in target_slugs:
            self.market_states[slug] = "IDLE"

        self.logger.info("Selected %s markets with budget $%s each: %s", len(target_slugs), market_budget, target_slugs)

        tick_count = 0
        while time.time() < end_time:
            tick_start = time.time()
            now_ms = int(time.time() * 1000)

            for slug in target_slugs:
                window = active_windows.get(slug)
                try:
                    book = self.client.fetch_book(slug)
                    if slug not in market_metadata:
                        market_metadata[slug] = Market(
                            slug=slug,
                            event_id=None,
                            category="sports",
                            tick=Decimal("0.001"),
                            min_qty=Decimal("1"),
                            active=True,
                            mutually_exclusive=False,
                        )
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

                    quotes = desired_quotes(
                        market=market,
                        book=book,
                        window=window,
                        position=pos,
                        budget=market_budget,
                        max_contracts_cap=Decimal(str(self.settings.max_contracts_per_market)),
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

                    lip_snap = estimate_lip_score(book, window, current_open_orders, market.tick, ts_ms=now_ms)
                    self.ledger.record_lip_snapshot(lip_snap)

                except Exception as exc:
                    self.market_states[slug] = "PAUSED"
                    self.logger.warning("Error processing %s: %s", slug, exc)

            tick_count += 1
            if tick_count % 30 == 0:
                self.logger.info("Completed %s ticks, remaining %s s", tick_count, int(end_time - time.time()))

            elapsed = time.time() - tick_start
            sleep_time = max(0.1, 1.0 - elapsed)
            time.sleep(sleep_time)

        self.logger.info("ShadowEngine finished successfully after %s ticks", tick_count)

class LiveEngine:
    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        public_client: PublicGatewayClient,
        auth_client: AuthenticatedClient,
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

    def run(self, duration_seconds: int = 600) -> None:
        if not self.auth_client.is_available:
            raise RuntimeError("Polymarket US authenticated SDK not configured")

        self.logger.info("Starting LiveEngine for %s seconds", duration_seconds)
        end_time = time.time() + duration_seconds

        windows = self.public_client.fetch_incentives(status="active", page_size=200)
        active_windows: dict[str, IncentiveWindow] = {w.slug: w for w in windows if w.status.lower() == "active"}

        candidate_books: dict[str, Book] = {}
        for slug in list(active_windows.keys())[:15]:
            try:
                candidate_books[slug] = self.public_client.fetch_book(slug)
            except Exception:
                pass

        target_slugs = rank_markets(
            windows=list(active_windows.values()),
            books=candidate_books,
            now=datetime.now(timezone.utc),
            max_n=self.settings.max_markets,
        )
        if not target_slugs:
            target_slugs = list(active_windows.keys())[: self.settings.max_markets]

        self.logger.info("Scoped startup cancel for target slugs: %s", target_slugs)
        self.auth_client.cancel_all_scoped(target_slugs)

        market_budget = min(
            self.settings.max_per_market,
            self.settings.total_budget / Decimal(str(max(1, len(target_slugs)))),
        )

        market_metadata: dict[str, Market] = {}
        for slug in target_slugs:
            self.market_states[slug] = "IDLE"

        tick_count = 0
        while time.time() < end_time:
            tick_start = time.time()
            now_dt = datetime.now(timezone.utc)
            now_ms = int(time.time() * 1000)

            if self.risk_manager.is_kill_switch_active():
                self.logger.warning("Kill switch active! Canceling orders and pausing.")
                self.auth_client.cancel_all_scoped(target_slugs)
                time.sleep(5)
                continue

            if self.risk_manager.is_maintenance_active(now_dt):
                self.logger.warning("Exchange maintenance active! Canceling orders and pausing.")
                self.auth_client.cancel_all_scoped(target_slugs)
                time.sleep(10)
                continue

            buying_power = self.auth_client.get_buying_power()
            live_positions = {p.slug: p for p in self.auth_client.get_positions()}

            for slug in target_slugs:
                window = active_windows.get(slug)
                try:
                    book = self.public_client.fetch_book(slug)
                    if slug not in market_metadata:
                        market_metadata[slug] = Market(
                            slug=slug,
                            event_id=None,
                            category="sports",
                            tick=Decimal("0.001"),
                            min_qty=Decimal("1"),
                            active=True,
                            mutually_exclusive=False,
                        )
                    market = market_metadata[slug]
                    pos = live_positions.get(slug, self.ledger.get_position(slug))

                    open_orders = self.ledger.get_open_orders(slug)

                    quotes = desired_quotes(
                        market=market,
                        book=book,
                        window=window,
                        position=pos,
                        budget=market_budget,
                        max_contracts_cap=Decimal(str(self.settings.max_contracts_per_market)),
                    )

                    to_create, to_cancel, to_keep = reconcile_quotes(open_orders, quotes)

                    risk_res = self.risk_manager.check_pre_submit(
                        new_quotes=to_create,
                        open_orders=to_keep,
                        positions=live_positions,
                        buying_power=buying_power,
                        realized_loss_today=Decimal("0"),
                        now=now_dt,
                    )
                    if not risk_res.allowed:
                        self.logger.warning("Risk gate blocked submission on %s: %s", slug, risk_res.reason)
                        self.market_states[slug] = "PAUSED"
                        continue

                    if to_cancel:
                        self.auth_client.cancel_orders_batch(to_cancel)
                        for o in to_cancel:
                            self.ledger.close_order(o.order_id, status="CANCELED", ts_ms=now_ms)

                    if to_create:
                        tick_map = {slug: market.tick}
                        min_qty_map = {slug: market.min_qty}
                        created = self.auth_client.create_orders_batch(to_create, tick_map, min_qty_map)
                        for o in created:
                            self.ledger.record_order(o, status="OPEN", ts_ms=now_ms)

                    self.market_states[slug] = "QUOTING"
                except Exception as exc:
                    self.market_states[slug] = "PAUSED"
                    self.logger.warning("Error processing %s: %s", slug, exc)

            tick_count += 1
            elapsed = time.time() - tick_start
            sleep_time = max(0.1, 1.0 - elapsed)
            time.sleep(sleep_time)
