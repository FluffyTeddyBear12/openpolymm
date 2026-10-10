import random
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import requests

from pmus.models import Book, IncentiveWindow, Level, LiveOrder, Market, Position, Quote

try:
    from polymarket_us import PolymarketUS
    from polymarket_us.types.orders import (
        CancelAllOrdersParams,
        CancelOrderListItem,
        CancelOrderListParams,
        ClosePositionParams,
        CreateOrderListParams,
        CreateOrderParams,
    )
except ImportError:
    PolymarketUS = None
    CancelAllOrdersParams = None
    CancelOrderListItem = None
    CancelOrderListParams = None
    ClosePositionParams = None
    CreateOrderListParams = None
    CreateOrderParams = None

def chunk_list(items: list[Any], chunk_size: int = 20):
    for i in range(0, len(items), chunk_size):
        yield items[i : i + chunk_size]

class PublicGatewayClient:
    def __init__(
        self,
        base_url: str = "https://gateway.polymarket.us",
        timeout: float = 5.0,
        max_retries: int = 3,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "pmus/1.0"})

    def _request(self, method: str, endpoint: str, **kwargs) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        kwargs.setdefault("timeout", self.timeout)

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(method, url, **kwargs)
                if response.status_code == 429:
                    if attempt == self.max_retries:
                        response.raise_for_status()
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        sleep_s = float(retry_after)
                    else:
                        sleep_s = 0.5 * (2 ** attempt) + random.uniform(0.05, 0.2)
                    time.sleep(sleep_s)
                    continue

                response.raise_for_status()
                return response.json()
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == self.max_retries:
                    raise exc
                time.sleep(0.5 * (2 ** attempt) + random.uniform(0.05, 0.2))

        raise RuntimeError(f"Failed request to {url} after {self.max_retries} retries")

    def fetch_markets(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Market]:
        params = {"limit": limit, "offset": offset, "active": active, "closed": closed}
        data = self._request("GET", "/v1/markets", params=params)
        return [self.parse_market(m) for m in data.get("markets", [])]

    def fetch_all_active_markets(self, limit_per_page: int = 200, max_pages: int = 10) -> list[Market]:
        results: list[Market] = []
        offset = 0
        for _ in range(max_pages):
            batch = self.fetch_markets(active=True, closed=False, limit=limit_per_page, offset=offset)
            if not batch:
                break
            results.extend(batch)
            offset += len(batch)
            if len(batch) < limit_per_page:
                break
        return results

    def fetch_events(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params = {"limit": limit, "active": active, "closed": closed}
        data = self._request("GET", "/v1/events", params=params)
        return data.get("events", [])

    def fetch_book(self, slug: str) -> Book:
        data = self._request("GET", f"/v1/markets/{slug}/book")
        market_data = data.get("marketData", {})
        return self.parse_book(slug, market_data)

    def fetch_bbo(self, slug: str) -> tuple[Level | None, Level | None]:
        data = self._request("GET", f"/v1/markets/{slug}/bbo")
        market_data = data.get("marketData", {})
        best_bid_data = market_data.get("bestBid")
        best_ask_data = market_data.get("bestAsk")

        best_bid = None
        if best_bid_data and "value" in best_bid_data:
            qty = Decimal(str(market_data.get("bestBidQty", "0")))
            best_bid = Level(price=Decimal(str(best_bid_data["value"])), qty=qty)

        best_ask = None
        if best_ask_data and "value" in best_ask_data:
            qty = Decimal(str(market_data.get("bestAskQty", "0")))
            best_ask = Level(price=Decimal(str(best_ask_data["value"])), qty=qty)

        return best_bid, best_ask

    def fetch_slug_to_event_map(self) -> dict[str, str]:
        slug_to_event: dict[str, str] = {}
        try:
            events = self.fetch_events(active=True, closed=False, limit=200)
            for ev in events:
                ev_id = ev.get("slug") or str(ev.get("id", ""))
                for m in ev.get("markets", []):
                    m_slug = m.get("slug")
                    if m_slug and ev_id:
                        slug_to_event[m_slug] = ev_id
        except Exception:
            pass
        return slug_to_event

    def fetch_incentives(self, status: str = "active", page_size: int = 100) -> list[IncentiveWindow]:
        params = {"pageSize": page_size, "statuses": status}
        data = self._request("GET", "/v1/incentives", params=params)
        return self.parse_incentives(data)

    @staticmethod
    def parse_market(raw: dict[str, Any], event_id: str | None = None) -> Market:
        tick_str = str(raw.get("orderPriceMinTickSize", "0.001"))
        qty_str = str(raw.get("minimumTradeQty", "1"))
        active = bool(raw.get("active", False)) and not bool(raw.get("closed", False))
        mutually_exclusive = bool(raw.get("mutuallyExclusive", False))
        return Market(
            slug=raw["slug"],
            event_id=event_id or raw.get("eventId"),
            category=raw.get("category", ""),
            tick=Decimal(tick_str),
            min_qty=Decimal(qty_str),
            active=active,
            mutually_exclusive=mutually_exclusive,
        )

    @staticmethod
    def parse_book(slug: str, market_data: dict[str, Any]) -> Book:
        raw_bids = market_data.get("bids", [])
        raw_offers = market_data.get("offers", [])

        bids = [
            Level(price=Decimal(str(b["px"]["value"])), qty=Decimal(str(b["qty"])))
            for b in raw_bids
            if "px" in b and "value" in b["px"] and "qty" in b
        ]
        asks = [
            Level(price=Decimal(str(a["px"]["value"])), qty=Decimal(str(a["qty"])))
            for a in raw_offers
            if "px" in a and "value" in a["px"] and "qty" in a
        ]

        bids.sort(key=lambda lvl: lvl.price, reverse=True)
        asks.sort(key=lambda lvl: lvl.price)

        transact_time = market_data.get("transactTime")
        if transact_time:
            dt = datetime.fromisoformat(transact_time.replace("Z", "+00:00"))
            ts_ms = int(dt.timestamp() * 1000)
        else:
            ts_ms = int(time.time() * 1000)

        return Book(
            slug=slug,
            bids=tuple(bids),
            asks=tuple(asks),
            ts_ms=ts_ms,
        )

    @staticmethod
    def parse_incentives(data: dict[str, Any]) -> list[IncentiveWindow]:
        results: list[IncentiveWindow] = []
        for program in data.get("programs", []):
            slug = program.get("marketSlug", "")
            event_start_str = program.get("eventStartTime")
            event_start_time = None
            if event_start_str:
                try:
                    event_start_time = datetime.fromisoformat(event_start_str.replace("Z", "+00:00"))
                except Exception:
                    pass

            for tp in program.get("timePeriods", []):
                program_id = tp.get("programId", "")
                pool_usd = Decimal(str(tp.get("rewardPool", "0")))
                discount = Decimal(str(tp.get("discountFactor", "0")))
                target_size = Decimal(str(tp.get("targetSize", "0")))
                max_spread_raw = tp.get("maxSpread")
                max_spread = Decimal(str(max_spread_raw)) if max_spread_raw is not None else None

                start_str = tp.get("start")
                if start_str:
                    try:
                        start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                    except Exception:
                        start = datetime.now(timezone.utc)
                else:
                    start = datetime.now(timezone.utc)

                end_str = tp.get("end")
                if end_str:
                    try:
                        end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    except Exception:
                        end = None
                else:
                    end = None

                period = tp.get("period", "early").lower()

                if end is None:
                    if event_start_time is not None and period in ("early", "day_of"):
                        end = event_start_time
                    elif event_start_time is not None and period == "live":
                        end = event_start_time + timedelta(hours=3)
                    elif period == "daily":
                        end = start + timedelta(days=1)
                    else:
                        end = start + timedelta(days=1)

                status = tp.get("status", "active")

                results.append(
                    IncentiveWindow(
                        slug=slug,
                        program_id=program_id,
                        pool_usd=pool_usd,
                        discount=discount,
                        target_size=target_size,
                        max_spread=max_spread,
                        start=start,
                        end=end,
                        status=status,
                        period=period,
                        event_start_time=event_start_time,
                    )
                )
        return results

class AuthenticatedClient:
    def __init__(self, key_id: str = "", secret_key: str = "", sdk: Any = None):
        self.key_id = key_id
        self.secret_key = secret_key
        if sdk is not None:
            self._sdk = sdk
        elif key_id and secret_key and PolymarketUS is not None:
            self._sdk = PolymarketUS(key_id=key_id, secret_key=secret_key)
        else:
            self._sdk = None

    @property
    def is_available(self) -> bool:
        return self._sdk is not None

    def create_orders_batch(
        self,
        quotes: list[Quote],
        tick_map: dict[str, Decimal] | None = None,
        min_qty_map: dict[str, Decimal] | None = None,
    ) -> list[LiveOrder]:
        if not self.is_available:
            raise RuntimeError("Polymarket US authenticated SDK not configured")

        tick_map = tick_map or {}
        min_qty_map = min_qty_map or {}
        created_live_orders: list[LiveOrder] = []

        for chunk in chunk_list(quotes, 20):
            payload_orders = []
            order_refs: list[tuple[str, str, Decimal, Decimal]] = []
            for q in chunk:
                tick = tick_map.get(q.slug, Decimal("0.001"))
                min_qty = min_qty_map.get(q.slug, Decimal("1"))
                rounded_px = (q.price // tick) * tick
                rounded_qty = (q.qty // min_qty) * min_qty

                qty_val: int | float = (
                    int(rounded_qty) if rounded_qty == rounded_qty.to_integral_value() else float(rounded_qty)
                )
                intent_val = f"ORDER_INTENT_{q.side}"
                order_refs.append((q.slug, q.side, rounded_px, rounded_qty))

                param = {
                    "marketSlug": q.slug,
                    "intent": intent_val,
                    "type": "ORDER_TYPE_LIMIT",
                    "price": {"value": str(rounded_px), "currency": "USD"},
                    "quantity": qty_val,
                    "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
                    "participateDontInitiate": True,
                    "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
                }
                payload_orders.append(param)

            resp = self._sdk.orders.create_many({"orders": payload_orders})
            assigned_ids = resp.get("createdOrderIds", []) if isinstance(resp, dict) else []

            if len(assigned_ids) < len(order_refs):
                slugs_in_batch = list({slug for slug, _, _, _ in order_refs})
                try:
                    open_resp = self._sdk.orders.list({"slugs": slugs_in_batch})
                    open_orders = open_resp.get("orders", []) if isinstance(open_resp, dict) else []
                    matched_ids: list[str] = []
                    unmatched_refs = list(order_refs)
                    for oo in open_orders:
                        for idx, ref in enumerate(unmatched_refs):
                            r_slug, r_side, r_px, _ = ref
                            o_intent = f"ORDER_INTENT_{r_side}"
                            oo_px = (oo.get("price") or {}).get("value")
                            try:
                                oo_px_match = (Decimal(str(oo_px)) == r_px) if oo_px is not None else False
                            except Exception:
                                oo_px_match = (oo_px == str(r_px))
                            if (
                                oo.get("marketSlug") == r_slug
                                and oo.get("intent") == o_intent
                                and oo_px_match
                            ):
                                matched_ids.append(oo["id"])
                                unmatched_refs.pop(idx)
                                break
                    if len(matched_ids) == len(order_refs):
                        assigned_ids = matched_ids
                except Exception:
                    pass

            if len(assigned_ids) < len(order_refs):
                raise RuntimeError(
                    f"create_many returned incomplete order IDs ({len(assigned_ids)} IDs for {len(order_refs)} orders)"
                )

            for idx, (slug, side, px, qty) in enumerate(order_refs):
                created_live_orders.append(
                    LiveOrder(
                        order_id=str(assigned_ids[idx]),
                        slug=slug,
                        side=side,
                        price=px,
                        qty_open=qty,
                    )
                )

        return created_live_orders

    def cancel_orders_batch(self, orders: list[LiveOrder]) -> list[str]:
        if not self.is_available:
            raise RuntimeError("Polymarket US authenticated SDK not configured")

        canceled_ids: list[str] = []
        for chunk in chunk_list(orders, 20):
            payload = [{"marketSlug": o.slug, "orderId": o.order_id} for o in chunk]
            resp = self._sdk.orders.cancel_many({"orders": payload})
            c_ids = resp.get("canceledOrderIds") if isinstance(resp, dict) else None
            if c_ids is not None:
                canceled_ids.extend([str(cid) for cid in c_ids])
            else:
                canceled_ids.extend([o.order_id for o in chunk])
        return canceled_ids

    def cancel_all_scoped(self, slugs: list[str]) -> list[str]:
        if not self.is_available:
            raise RuntimeError("Polymarket US authenticated SDK not configured")
        resp = self._sdk.orders.cancel_all({"slugs": slugs})
        return resp.get("canceledOrderIds", []) if isinstance(resp, dict) else []

    def close_position(self, slug: str) -> None:
        if not self.is_available:
            raise RuntimeError("Polymarket US authenticated SDK not configured")
        self._sdk.orders.close_position(
            {
                "marketSlug": slug,
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            }
        )

    def get_buying_power(self) -> Decimal:
        if not self.is_available:
            raise RuntimeError("Polymarket US authenticated SDK not configured")
        resp = self._sdk.account.balances()
        balances = resp.get("balances", []) if isinstance(resp, dict) else []
        for b in balances:
            if "buyingPower" in b:
                return Decimal(str(b["buyingPower"]))
        return Decimal("0")

    def get_positions(self) -> list[Position]:
        if not self.is_available:
            raise RuntimeError("Polymarket US authenticated SDK not configured")
        resp = self._sdk.portfolio.positions()
        pos_map = resp.get("positions", {}) if isinstance(resp, dict) else {}
        results: list[Position] = []
        for key, p in pos_map.items():
            market_meta = p.get("marketMetadata") or {}
            slug = market_meta.get("slug") or key
            net_str = p.get("netPositionDecimal") or p.get("netPosition") or "0"
            net_qty = Decimal(str(net_str))
            avg_px_dict = p.get("avgPx")
            avg_price = (
                Decimal(str(avg_px_dict["value"]))
                if (avg_px_dict and "value" in avg_px_dict)
                else Decimal("0")
            )
            results.append(Position(slug=slug, net_qty=net_qty, avg_price=avg_price))
        return results
