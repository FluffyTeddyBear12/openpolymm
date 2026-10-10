import random
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import requests

from pmus.models import Book, IncentiveWindow, Level, Market

try:
    from polymarket_us import PolymarketUS
except ImportError:
    PolymarketUS = None

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
            for tp in program.get("timePeriods", []):
                program_id = tp.get("programId", "")
                pool_usd = Decimal(str(tp.get("rewardPool", "0")))
                discount = Decimal(str(tp.get("discountFactor", "0")))
                target_size = Decimal(str(tp.get("targetSize", "0")))
                max_spread_raw = tp.get("maxSpread")
                max_spread = Decimal(str(max_spread_raw)) if max_spread_raw is not None else None

                start_str = tp.get("start")
                if start_str:
                    start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                else:
                    start = datetime.now(timezone.utc)

                end_str = tp.get("end")
                end = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else None
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
                    )
                )
        return results

class AuthenticatedClient:
    def __init__(self, key_id: str = "", secret_key: str = ""):
        self.key_id = key_id
        self.secret_key = secret_key
        self._sdk = None
        if key_id and secret_key and PolymarketUS is not None:
            self._sdk = PolymarketUS(key_id=key_id, secret_key=secret_key)

    @property
    def is_available(self) -> bool:
        return self._sdk is not None
