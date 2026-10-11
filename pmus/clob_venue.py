from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time
from typing import Any
import requests

try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import (
        ApiCreds,
        AssetType,
        BalanceAllowanceParams,
        OpenOrderParams,
        OrderArgs,
        OrderScoringParams,
    )
    from py_clob_client_v2.order_builder.constants import BUY, SELL
    IS_V2 = True
except ImportError:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds,
        AssetType,
        BalanceAllowanceParams,
        OpenOrderParams,
        OrderArgs,
        OrderScoringParams,
    )
    from py_clob_client.order_builder.constants import BUY, SELL
    IS_V2 = False

from pmus.config import Settings
from pmus.models import (
    Book,
    IncentiveWindow,
    Level,
    LiveOrder,
    Market,
    Position,
    Quote,
    ShadowFill,
    Side,
)

class ClobVenueClient:
    def __init__(self, settings: Settings, client: Any = None):
        self.settings = settings
        self.host = settings.clob_host.rstrip("/")
        self._market_cache: dict[str, Market] = {}

        if client is not None:
            self.client = client
            self.is_authenticated = getattr(client, "is_authenticated", True)
            return

        key = settings.polygon_private_key.strip()
        if key:
            sig_type = settings.polymarket_signature_type
            funder = settings.polymarket_proxy_address.strip() or None
            if IS_V2:
                if funder and sig_type in (2, 3):
                    sig_type = 3
                self.client = ClobClient(
                    self.host,
                    settings.chain_id,
                    key=key,
                    signature_type=sig_type,
                    funder=funder,
                )
                try:
                    if hasattr(self.client, "create_or_derive_api_key"):
                        creds = self.client.create_or_derive_api_key()
                    else:
                        creds = self.client.create_or_derive_api_creds()
                except Exception:
                    if settings.clob_api_key and settings.clob_secret and settings.clob_passphrase:
                        creds = ApiCreds(
                            api_key=settings.clob_api_key,
                            api_secret=settings.clob_secret,
                            api_passphrase=settings.clob_passphrase,
                        )
                    else:
                        raise
                self.client.set_api_creds(creds)
            else:
                if sig_type == 3:
                    sig_type = 2
                self.client = ClobClient(
                    host=self.host,
                    key=key,
                    chain_id=settings.chain_id,
                    signature_type=sig_type,
                    funder=funder,
                )
                try:
                    creds = self.client.create_or_derive_api_creds()
                except Exception:
                    if settings.clob_api_key and settings.clob_secret and settings.clob_passphrase:
                        creds = ApiCreds(
                            api_key=settings.clob_api_key,
                            api_secret=settings.clob_secret,
                            api_passphrase=settings.clob_passphrase,
                        )
                    else:
                        raise
                self.client.set_api_creds(creds)
            self.is_authenticated = True
        else:
            self.client = ClobClient(self.host, settings.chain_id) if IS_V2 else ClobClient(host=self.host)
            self.is_authenticated = False

    @property
    def is_available(self) -> bool:
        return self.is_authenticated

    def fetch_reward_markets(self, limit: int = 20) -> list[tuple[Market, IncentiveWindow]]:
        if hasattr(self.client, "get_sampling_markets"):
            try:
                raw_resp = self.client.get_sampling_markets()
            except Exception:
                resp = requests.get(f"{self.host}/sampling-markets", timeout=10.0)
                resp.raise_for_status()
                raw_resp = resp.json()
        else:
            resp = requests.get(f"{self.host}/sampling-markets", timeout=10.0)
            resp.raise_for_status()
            raw_resp = resp.json()

        if isinstance(raw_resp, dict):
            raw_markets = raw_resp.get("data", [])
        elif isinstance(raw_resp, list):
            raw_markets = raw_resp
        else:
            raw_markets = []

        now_utc = datetime.now(timezone.utc)
        results: list[tuple[Market, IncentiveWindow]] = []

        for m in raw_markets:
            if not isinstance(m, dict):
                continue
            rewards = m.get("rewards")
            if not isinstance(rewards, dict):
                continue
            rates = rewards.get("rates")
            if not isinstance(rates, list) or not rates:
                continue

            rate = rates[0]
            if not isinstance(rate, dict):
                continue

            try:
                pool_usd = Decimal(str(rate.get("rewards_daily_rate", "0")))
            except Exception:
                continue

            if pool_usd <= Decimal("0"):
                continue

            target_size = Decimal(str(rewards.get("min_size", 200)))
            raw_max_spread = rewards.get("max_spread", 3.5)
            max_spread = Decimal(str(raw_max_spread)) / Decimal("100")
            cond_id = str(m.get("condition_id", ""))
            market_slug = str(m.get("market_slug", "") or cond_id)
            tick = Decimal(str(m.get("minimum_tick_size", "0.001")))
            min_qty = Decimal(str(m.get("minimum_order_size", "5")))
            question = str(m.get("question", ""))

            tokens = m.get("tokens", [])
            if not isinstance(tokens, list):
                continue

            for tok in tokens:
                if not isinstance(tok, dict):
                    continue
                token_id = str(tok.get("token_id", ""))
                if not token_id:
                    continue

                market = Market(
                    slug=token_id,
                    event_id=market_slug,
                    category="global",
                    tick=tick,
                    min_qty=min_qty,
                    active=True,
                    mutually_exclusive=False,
                    question=question,
                )
                self._market_cache[token_id] = market

                window = IncentiveWindow(
                    slug=token_id,
                    program_id=cond_id,
                    pool_usd=pool_usd,
                    discount=Decimal("0.30"),
                    target_size=target_size,
                    max_spread=max_spread,
                    start=now_utc - timedelta(hours=1),
                    end=now_utc + timedelta(hours=23),
                    status="active",
                )
                results.append((market, window))
                if limit > 0 and len(results) >= limit:
                    return results

        return results

    def fetch_book(self, token_id: str) -> Book:
        resp = requests.get(f"{self.host}/book?token_id={token_id}", timeout=10.0)
        resp.raise_for_status()
        data = resp.json()

        def parse_level(item: Any) -> Level:
            if isinstance(item, dict):
                return Level(
                    price=Decimal(str(item.get("price", "0"))),
                    qty=Decimal(str(item.get("size", item.get("qty", "0")))),
                )
            if hasattr(item, "price") and hasattr(item, "size"):
                return Level(price=Decimal(str(item.price)), qty=Decimal(str(item.size)))
            return Level(price=Decimal(str(item[0])), qty=Decimal(str(item[1])))

        raw_bids = data.get("bids", []) if isinstance(data, dict) else []
        raw_asks = data.get("asks", []) if isinstance(data, dict) else []

        bids = [parse_level(b) for b in raw_bids]
        asks = [parse_level(a) for a in raw_asks]

        bids.sort(key=lambda lvl: lvl.price, reverse=True)
        asks.sort(key=lambda lvl: lvl.price)

        ts_raw = data.get("timestamp") if isinstance(data, dict) else None
        ts_ms = int(ts_raw) if ts_raw is not None else int(time.time() * 1000)

        return Book(
            slug=token_id,
            bids=tuple(bids),
            asks=tuple(asks),
            ts_ms=ts_ms,
        )

    def fetch_bbo(self, token_id: str) -> tuple[Level | None, Level | None]:
        book = self.fetch_book(token_id)
        best_bid = book.bids[0] if book.bids else None
        best_ask = book.asks[0] if book.asks else None
        return best_bid, best_ask

    def fetch_incentives(self, status: str = "active", page_size: int = 200) -> list[IncentiveWindow]:
        pairs = self.fetch_reward_markets(limit=page_size)
        return [w for _, w in pairs]

    def fetch_markets(self, limit: int = 200, **kwargs: Any) -> list[Market]:
        pairs = self.fetch_reward_markets(limit=limit)
        return [m for m, _ in pairs]

    def fetch_slug_to_event_map(self) -> dict[str, str]:
        if not self._market_cache:
            self.fetch_reward_markets(limit=100)
        return {m.slug: (m.event_id or "") for m in self._market_cache.values()}

    def get_market(self, token_id: str) -> Market | None:
        return self._market_cache.get(token_id)

    def create_orders_batch(
        self,
        quotes: list[Quote],
        tick_map: dict[str, Decimal] | None = None,
        min_qty_map: dict[str, Decimal] | None = None,
    ) -> list[LiveOrder]:
        if not self.is_authenticated:
            raise RuntimeError("Polymarket Global client not authenticated")

        created: list[LiveOrder] = []
        for quote in quotes:
            side = BUY if "BUY" in quote.side else SELL
            args = OrderArgs(
                token_id=quote.slug,
                price=float(quote.price),
                size=float(quote.qty),
                side=side,
            )
            resp = self.client.create_and_post_order(args)
            if isinstance(resp, dict):
                order_id = str(resp.get("orderID") or resp.get("order_id") or resp.get("id") or "")
            elif hasattr(resp, "orderID"):
                order_id = str(resp.orderID)
            elif hasattr(resp, "id"):
                order_id = str(resp.id)
            else:
                order_id = str(resp)

            created.append(
                LiveOrder(
                    order_id=order_id,
                    slug=quote.slug,
                    side=quote.side,
                    price=quote.price,
                    qty_open=quote.qty,
                )
            )
        return created

    def cancel_orders(self, order_ids: list[str]) -> bool:
        if not order_ids:
            return True
        if not self.is_authenticated:
            raise RuntimeError("Polymarket Global client not authenticated")
        self.client.cancel_orders(order_ids)
        return True

    def cancel_orders_batch(self, orders: list[LiveOrder]) -> list[str]:
        order_ids = [o.order_id for o in orders if o.order_id]
        self.cancel_orders(order_ids)
        return order_ids

    def cancel_all_scoped(self, slugs: list[str]) -> list[str]:
        open_orders = self.get_open_orders(slugs=slugs)
        order_ids = [o.order_id for o in open_orders if o.order_id]
        if order_ids:
            self.cancel_orders(order_ids)
        return order_ids

    def get_open_orders(self, slugs: list[str] | None = None) -> list[LiveOrder]:
        if not self.is_authenticated:
            return []
        try:
            if hasattr(self.client, "get_open_orders"):
                raw_orders = self.client.get_open_orders(OpenOrderParams())
            else:
                raw_orders = self.client.get_orders(OpenOrderParams())
        except Exception:
            return []

        results: list[LiveOrder] = []
        slug_filter = set(slugs) if slugs else None

        for o in raw_orders:
            if isinstance(o, dict):
                order_id = str(o.get("id") or o.get("order_id") or o.get("orderID") or "")
                slug = str(o.get("asset_id") or o.get("token_id") or o.get("market") or "")
                raw_side = str(o.get("side") or "").upper()
                px_val = o.get("price", "0")
                price = Decimal(str(px_val))
                size_val = o.get("open_size") or o.get("size") or o.get("original_size") or "0"
                size_matched = o.get("size_matched", "0")
                try:
                    qty = (
                        Decimal(str(size_val))
                        if "open_size" in o
                        else Decimal(str(size_val)) - Decimal(str(size_matched))
                    )
                except Exception:
                    qty = Decimal(str(size_val))
            else:
                order_id = str(getattr(o, "id", "") or getattr(o, "order_id", ""))
                slug = str(getattr(o, "asset_id", "") or getattr(o, "token_id", ""))
                raw_side = str(getattr(o, "side", "")).upper()
                price = Decimal(str(getattr(o, "price", "0")))
                qty = Decimal(str(getattr(o, "size", "0")))

            if not order_id or not slug:
                continue
            if slug_filter and slug not in slug_filter:
                continue

            side: Side = "BUY_LONG" if raw_side in ("BUY", "BUY_LONG") else "SELL_LONG"
            results.append(
                LiveOrder(
                    order_id=order_id,
                    slug=slug,
                    side=side,
                    price=price,
                    qty_open=qty,
                )
            )
        return results

    def get_buying_power(self) -> Decimal:
        if not self.is_authenticated:
            return Decimal("0")
        sig_candidates = [3, getattr(getattr(self.client, "builder", None), "sig_type", 2), 2, 1, 0]
        for sig in sig_candidates:
            try:
                resp = self.client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig)
                )
                if isinstance(resp, dict):
                    bal = resp.get("balance", "0")
                elif hasattr(resp, "balance"):
                    bal = getattr(resp, "balance")
                else:
                    bal = str(resp)
                val = Decimal(str(bal)) / Decimal("1000000")
                if val > Decimal("0"):
                    return val
            except Exception:
                continue
        return Decimal("0")

    def is_order_scoring(self, order_id: str) -> bool:
        if not self.is_authenticated:
            return False
        try:
            try:
                resp = self.client.is_order_scoring(OrderScoringParams(orderId=order_id))
            except (TypeError, AttributeError):
                resp = self.client.is_order_scoring(order_id)

            if isinstance(resp, dict):
                return bool(resp.get("scoring", False))
            return bool(resp)
        except Exception:
            return False

    def close_position_safe(
        self,
        token_id: str,
        position: Position,
        bbo: tuple[Any, Any] | None = None,
        max_loss_cents: Decimal = Decimal("0.02"),
    ) -> LiveOrder | None:
        if not self.is_authenticated:
            raise RuntimeError("Polymarket Global client not authenticated")

        bbo_bid = None
        bbo_ask = None
        if bbo is not None:
            bid_raw, ask_raw = bbo[0], bbo[1]
            if bid_raw is not None:
                bbo_bid = bid_raw.price if hasattr(bid_raw, "price") else Decimal(str(bid_raw))
            if ask_raw is not None:
                bbo_ask = ask_raw.price if hasattr(ask_raw, "price") else Decimal(str(ask_raw))

        if position.net_qty > Decimal("0"):
            if bbo_bid is not None and bbo_bid >= (position.avg_price - max_loss_cents):
                created = self.create_orders_batch([
                    Quote(slug=token_id, side="SELL_LONG", price=bbo_bid, qty=position.net_qty)
                ])
                return created[0] if created else None
            created = self.create_orders_batch([
                Quote(slug=token_id, side="SELL_LONG", price=position.avg_price, qty=position.net_qty)
            ])
            return created[0] if created else None

        if position.net_qty < Decimal("0"):
            qty = abs(position.net_qty)
            if bbo_ask is not None and bbo_ask <= (position.avg_price + max_loss_cents):
                created = self.create_orders_batch([
                    Quote(slug=token_id, side="BUY_SHORT", price=bbo_ask, qty=qty)
                ])
                return created[0] if created else None
            created = self.create_orders_batch([
                Quote(slug=token_id, side="BUY_SHORT", price=position.avg_price, qty=qty)
            ])
            return created[0] if created else None

        return None

    def close_position(self, token_id: str) -> None:
        self.cancel_all_scoped([token_id])

    def get_positions(self) -> list[Position]:
        return []

    def get_fills(self, since: datetime | None = None) -> list[ShadowFill]:
        return []
