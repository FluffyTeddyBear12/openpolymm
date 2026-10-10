import logging
import time
import uuid
from decimal import Decimal
from typing import Literal

from pmus.config import Settings
from pmus.ledger import Ledger
from pmus.models import Book, IncentiveWindow, LiveOrder, Market, Position, Quote
from pmus.shadow import check_paper_fills, estimate_lip_score
from pmus.strategy.lip_quoter import desired_quotes
from pmus.venue import PublicGatewayClient

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

    if fill_side == "BUY_LONG":
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

        windows = self.client.fetch_incentives(status="active", page_size=50)
        active_windows: dict[str, IncentiveWindow] = {}
        for w in windows:
            if w.status.lower() == "active" and w.slug not in active_windows:
                active_windows[w.slug] = w

        target_slugs = list(active_windows.keys())[:3]
        active_mkts = self.client.fetch_markets(active=True, closed=False, limit=10)
        for m in active_mkts:
            if m.slug not in target_slugs and len(target_slugs) < 5:
                target_slugs.append(m.slug)

        market_metadata: dict[str, Market] = {}
        for slug in target_slugs:
            self.market_states[slug] = "IDLE"

        self.logger.info("Targeting markets: %s", target_slugs)

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
                    if abs(pos.net_qty) >= self.settings.max_per_market:
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
                        budget=self.settings.max_per_market,
                        max_position_cap=self.settings.max_per_market,
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
