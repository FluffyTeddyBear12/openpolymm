from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import re
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
        OrderMarketCancelParams,
        OrderScoringParams,
        TradeParams,
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
        TradeParams,
    )
    from py_clob_client.order_builder.constants import BUY, SELL
    OrderMarketCancelParams = None
    IS_V2 = False

from pmus.config import Settings
from pmus.models import (
    Book,
    ConditionMarket,
    IncentiveWindow,
    Level,
    LiveOrder,
    Market,
    Position,
    Quote,
    ShadowFill,
    Side,
)
from pmus.strategy.fill_risk import fill_risk_category

DATA_API_POSITIONS_URL = "https://data-api.polymarket.com/positions"
POSITIONS_PAGE_SIZE = 500
POSITIONS_CACHE_TTL_SEC = 15.0
MIN_DAYS_TO_EXPIRY = timedelta(days=14)
SERIES_MIN_DAYS_TO_EXPIRY = timedelta(days=30)
MIN_MARKET_AGE = timedelta(hours=48)
BUYING_POWER_CACHE_TTL_SEC = 5.0
MAX_SAMPLING_PAGES = 40
SAMPLING_CACHE_TTL_SEC = 300.0
SAMPLING_STALE_CACHE_TTL_SEC = 1800.0
MAX_MARKET_CACHE_SIZE = 10000
CHAMPIONSHIP_SERIES_PATTERN = re.compile(
    r"\b(?:world series|japan series|nba finals|stanley cup(?: finals)?|mlb postseason|korean series|world baseball classic)\b"
)
EXCLUDED_MARKET_TAGS = frozenset({
    "sports",
    "nfl",
    "nba",
    "mlb",
    "nhl",
    "ncaa",
    "ufc",
    "soccer",
    "tennis",
    "esports",
})

logger = logging.getLogger(__name__)


def _parse_iso_ts(val: Any) -> datetime | None:
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    if not val or not isinstance(val, str):
        return None
    s = val.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _fee_usd(fee_rate_bps: Any, price: Decimal, qty: Decimal) -> Decimal:
    raw = str(fee_rate_bps or "").strip()
    if not raw:
        return Decimal("0")
    return Decimal(raw) / Decimal("10000") * min(price, Decimal("1") - price) * qty


def parse_trade_fills(trade: dict, proxy_address: str) -> list[ShadowFill]:
    """Return our fills in one /data/trades record.

    For our maker fills the top-level fields describe the taker, so our legs are
    the maker_orders entries whose maker_address is our proxy wallet.
    """
    if str(trade.get("status", "")).upper() == "FAILED":
        return []
    ts_ms = int(trade["match_time"]) * 1000
    if trade.get("trader_side") == "TAKER":
        taker_asset = str(trade["asset_id"])
        total_qty = Decimal("0")
        total_usdc = Decimal("0")
        for m in trade.get("maker_orders") or []:
            if not isinstance(m, dict) or m.get("matched_amount") in (None, "") or m.get("price") in (None, ""):
                continue
            m_qty = Decimal(str(m["matched_amount"]))
            raw_m_px = Decimal(str(m["price"]))
            if m_qty > Decimal("0") and raw_m_px > Decimal("0"):
                m_asset = str(m.get("asset_id") or "").strip()
                m_px = (Decimal("1") - raw_m_px) if (m_asset and m_asset != taker_asset) else raw_m_px
                if m_px > Decimal("0"):
                    total_qty += m_qty
                    total_usdc += m_qty * m_px
        if total_qty > Decimal("0"):
            taker_qty: Any = total_qty
            taker_price: Any = total_usdc / total_qty
        else:
            taker_qty = trade["size"]
            taker_price = trade["price"]
        legs = [(
            trade["taker_order_id"], trade["asset_id"], trade["side"],
            taker_qty, taker_price, trade.get("fee_rate_bps"), False,
        )]
    else:
        proxy = proxy_address.lower()
        legs = [
            (m["order_id"], m["asset_id"], m["side"], m["matched_amount"], m["price"], m.get("fee_rate_bps"), True)
            for m in trade.get("maker_orders") or []
            if str(m.get("maker_address", "")).lower() == proxy
        ]

    fills = []
    for order_id, asset_id, side, qty, price, fee_bps, is_maker in legs:
        px = price if isinstance(price, Decimal) else Decimal(str(price))
        size = qty if isinstance(qty, Decimal) else Decimal(str(qty))
        fills.append(
            ShadowFill(
                fill_id=f"{trade['id']}:{order_id}",
                order_id=str(order_id),
                slug=str(asset_id),
                side="BUY_LONG" if str(side).upper() == "BUY" else "SELL_LONG",
                price=px,
                qty=size,
                fee=_fee_usd(fee_bps, px, size),
                is_maker=is_maker,
                ts_ms=ts_ms,
            )
        )
    return fills


@dataclass(frozen=True)
class _SamplingMarket:
    cond_id: str
    market_slug: str
    question: str
    tick: Decimal
    min_qty: Decimal
    pool_usd: Decimal
    target_size: Decimal
    max_spread: Decimal
    tokens: tuple[dict[str, Any], ...]
    end_date: datetime | None = None
    game_start_time: datetime | None = None
    accepting_order_timestamp: datetime | None = None
    neg_risk_market_id: str = ""
    tags: tuple[str, ...] = ()
    seconds_delay: int = 0
    accepting_orders: bool = True
    archived: bool = False
    volume_24h: Decimal = Decimal("0")


class ClobVenueClient:
    def __init__(self, settings: Settings, client: Any = None):
        self.settings = settings
        self.host = settings.clob_host.rstrip("/")
        self._market_cache: dict[str, Market] = {}
        self._positions_cache: list[Position] | None = None
        self._positions_cache_ts: float = 0.0
        self._buying_power_cache: Decimal | None = None
        self._buying_power_cache_ts: float = 0.0
        self._resolved_bp_sig_type: int | None = None
        self._sampling_markets_cache: list[_SamplingMarket] | None = None
        self._sampling_markets_cache_ts: float = 0.0
        self._last_fill_risk_skipped: dict[str, int] = {}
        self._sibling_map: dict[str, str] = {}
        self._yes_slugs: set[str] = set()
        self._pair_by_slug: dict[str, tuple[str, str]] = {}
        self._reward_conditions_cache: list[ConditionMarket] | None = None
        self._reward_conditions_cache_ts: float = 0.0
        self._reward_conditions_ttl_seconds: float = 900.0

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
                    if hasattr(self.client, "derive_api_key"):
                        creds = self.client.derive_api_key()
                    elif hasattr(self.client, "create_or_derive_api_key"):
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

    def _record_market_meta(self, token_id: str, market: Market) -> None:
        if token_id not in self._market_cache:
            if len(self._market_cache) >= MAX_MARKET_CACHE_SIZE:
                self._market_cache.pop(next(iter(self._market_cache)))
            self._market_cache[token_id] = market

    def _record_token_pair(self, raw_tokens: Any) -> tuple[str, str] | None:
        if not isinstance(raw_tokens, (list, tuple)):
            return None
        valid = [
            tok for tok in raw_tokens
            if isinstance(tok, dict) and str(tok.get("token_id", "")).strip()
        ]
        if len(valid) != 2:
            return None
        out0 = str(valid[0].get("outcome", "")).strip().lower()
        out1 = str(valid[1].get("outcome", "")).strip().lower()
        if out0 == "yes" or out1 == "no":
            tok_yes, tok_no = valid[0], valid[1]
        elif out0 == "no" or out1 == "yes":
            tok_yes, tok_no = valid[1], valid[0]
        else:
            tok_yes, tok_no = valid[0], valid[1]
        yes_id = str(tok_yes.get("token_id", "")).strip()
        no_id = str(tok_no.get("token_id", "")).strip()
        if not yes_id or not no_id or yes_id == no_id:
            return None
        pair = (yes_id, no_id)
        self._sibling_map[yes_id] = no_id
        self._sibling_map[no_id] = yes_id
        self._yes_slugs.add(yes_id)
        self._pair_by_slug[yes_id] = pair
        self._pair_by_slug[no_id] = pair
        return pair

    def invalidate_sampling_cache(self) -> None:
        self._sampling_markets_cache = None
        self._sampling_markets_cache_ts = 0.0
        self._reward_conditions_cache = None
        self._reward_conditions_cache_ts = 0.0

    def invalidate_sampling_markets_cache(self) -> None:
        self.invalidate_sampling_cache()

    def _fetch_sampling_markets(
        self,
        max_pages: int = MAX_SAMPLING_PAGES,
        force_refresh: bool = False,
    ) -> list[_SamplingMarket]:
        now_mono = time.monotonic()
        if (
            not force_refresh
            and max_pages >= MAX_SAMPLING_PAGES
            and self._sampling_markets_cache is not None
            and (now_mono - self._sampling_markets_cache_ts) < SAMPLING_CACHE_TTL_SEC
        ):
            return list(self._sampling_markets_cache)

        try:
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
        except Exception as exc:
            if (
                max_pages >= MAX_SAMPLING_PAGES
                and self._sampling_markets_cache is not None
                and (now_mono - self._sampling_markets_cache_ts) <= SAMPLING_STALE_CACHE_TTL_SEC
            ):
                logger.warning("First page of sampling-markets failed (%s); returning stale complete cache", exc)
                return list(self._sampling_markets_cache)
            raise

        parsed: list[_SamplingMarket] = []
        seen_conds: set[str] = set()
        skipped_by_risk: dict[str, int] = {}
        now_utc = datetime.now(timezone.utc)

        def _process_market_dicts(raw_items: list[Any]) -> None:
            for m in raw_items:
                if not isinstance(m, dict):
                    continue
                archived = m.get("archived") is True
                accepting_orders = m.get("accepting_orders") is not False
                if (
                    m.get("active") is False
                    or m.get("closed") is True
                    or archived
                    or not accepting_orders
                    or m.get("enable_order_book") is False
                ):
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
                raw_tags = m.get("tags") or ()
                if isinstance(raw_tags, (list, tuple)):
                    tags = tuple(
                        s
                        for s in (
                            str(t.get("slug") or t.get("label") or "").strip()
                            if isinstance(t, dict)
                            else str(t).strip()
                            for t in raw_tags
                        )
                        if s
                    )
                else:
                    tags = ()
                question = str(m.get("question", ""))
                q_lower = question.lower()
                risk_cat = fill_risk_category(question)
                if risk_cat is None and any(t.lower() in EXCLUDED_MARKET_TAGS for t in tags):
                    risk_cat = "sports_tag"

                raw_tokens = m.get("tokens") or []
                if isinstance(raw_tokens, (list, tuple)):
                    self._record_token_pair(raw_tokens)
                    for tok in raw_tokens:
                        if isinstance(tok, dict):
                            tid = str(tok.get("token_id", "")).strip()
                            if tid:
                                self._record_market_meta(
                                    tid,
                                    Market(
                                        slug=tid,
                                        event_id=market_slug,
                                        category="global",
                                        tick=tick,
                                        min_qty=min_qty,
                                        active=True,
                                        mutually_exclusive=False,
                                        question=question,
                                    ),
                                )

                if risk_cat:
                    skipped_by_risk[risk_cat] = skipped_by_risk.get(risk_cat, 0) + 1
                    continue

                try:
                    seconds_delay = int(m.get("seconds_delay") or 0)
                except Exception:
                    seconds_delay = 0
                if seconds_delay != 0:
                    skipped_by_risk["delayed_matching"] = skipped_by_risk.get("delayed_matching", 0) + 1
                    continue

                accepting_order_ts = _parse_iso_ts(
                    m.get("accepting_order_timestamp")
                    or m.get("acceptingOrdersTimestamp")
                    or m.get("created_at")
                    or m.get("createdAt")
                )
                if accepting_order_ts is not None and (now_utc - accepting_order_ts) < MIN_MARKET_AGE:
                    skipped_by_risk["unseasoned"] = skipped_by_risk.get("unseasoned", 0) + 1
                    continue

                end_date_raw = (
                    m.get("end_date_iso")
                    or m.get("endDateIso")
                    or m.get("end_date")
                    or m.get("endDate")
                    or m.get("game_end_time")
                )
                explicit_game_raw = (
                    m.get("game_start_time")
                    or m.get("gameStartTime")
                    or m.get("event_start_time")
                    or m.get("eventStartTime")
                )
                game_start_raw = explicit_game_raw or m.get("start_date") or m.get("startDate")
                explicit_game_start = _parse_iso_ts(explicit_game_raw)
                end_date_dt = _parse_iso_ts(end_date_raw)
                game_start_dt = _parse_iso_ts(game_start_raw)
                min_tte = (
                    SERIES_MIN_DAYS_TO_EXPIRY
                    if CHAMPIONSHIP_SERIES_PATTERN.search(q_lower)
                    else MIN_DAYS_TO_EXPIRY
                )
                if end_date_dt is not None and (end_date_dt - now_utc) < min_tte:
                    skipped_by_risk["short_tte"] = skipped_by_risk.get("short_tte", 0) + 1
                    continue
                if explicit_game_start is not None and (explicit_game_start - now_utc) < min_tte:
                    skipped_by_risk["short_tte"] = skipped_by_risk.get("short_tte", 0) + 1
                    continue

                tokens = m.get("tokens", [])
                if not isinstance(tokens, list):
                    continue

                if cond_id:
                    if cond_id in seen_conds:
                        continue
                    seen_conds.add(cond_id)

                neg_risk_market_id = str(m.get("neg_risk_market_id") or m.get("negRiskMarketID") or "").strip()
                raw_vol = m.get("volume24hr") or m.get("volume_24hr")
                try:
                    init_vol = Decimal(str(raw_vol)) if raw_vol is not None else Decimal("0")
                except Exception:
                    init_vol = Decimal("0")

                token_copies = tuple(dict(t) for t in tokens if isinstance(t, dict))
                parsed.append(
                    _SamplingMarket(
                        cond_id=cond_id,
                        market_slug=market_slug,
                        question=question,
                        tick=tick,
                        min_qty=min_qty,
                        pool_usd=pool_usd,
                        target_size=target_size,
                        max_spread=max_spread,
                        tokens=token_copies,
                        end_date=end_date_dt,
                        game_start_time=game_start_dt,
                        accepting_order_timestamp=accepting_order_ts,
                        neg_risk_market_id=neg_risk_market_id,
                        tags=tags,
                        seconds_delay=seconds_delay,
                        accepting_orders=accepting_orders,
                        archived=archived,
                        volume_24h=init_vol,
                    )
                )

        pages_fetched = 1
        crawl_completed = False
        crawl_error = False

        if isinstance(raw_resp, dict):
            page_data_list = list(raw_resp.get("data") or [])
            _process_market_dicts(page_data_list)
            del page_data_list
            next_cursor = raw_resp.get("next_cursor")
            del raw_resp

            seen_cursors: set[str] = set()
            pages_left = max_pages - 1
            while (
                pages_left > 0
                and isinstance(next_cursor, str)
                and next_cursor
                and next_cursor != "LTE="
            ):
                if next_cursor in seen_cursors:
                    logger.warning(
                        "Repeated cursor %s in sampling-markets pagination; stopping after %d pages",
                        next_cursor,
                        pages_fetched,
                    )
                    break
                seen_cursors.add(next_cursor)
                pages_left -= 1
                try:
                    resp = requests.get(
                        f"{self.host}/sampling-markets",
                        params={"next_cursor": next_cursor},
                        timeout=10.0,
                    )
                    resp.raise_for_status()
                    page_data = resp.json()
                except Exception as exc:
                    crawl_error = True
                    logger.warning(
                        "Sampling markets fetch failed after %d pages: %s",
                        pages_fetched,
                        exc,
                    )
                    break
                if isinstance(page_data, dict):
                    page_items = list(page_data.get("data") or [])
                    _process_market_dicts(page_items)
                    del page_items
                    pages_fetched += 1
                    next_cursor = page_data.get("next_cursor")
                    del page_data
                else:
                    break

            if not crawl_error and (not next_cursor or next_cursor == "LTE="):
                crawl_completed = True
        elif isinstance(raw_resp, list):
            _process_market_dicts(raw_resp)
            del raw_resp
            crawl_completed = True
        else:
            del raw_resp
            crawl_completed = True

        self._last_fill_risk_skipped = dict(skipped_by_risk)
        logger.info("Fill-risk skipped markets: %s", skipped_by_risk)

        if parsed:
            cond_ids = list(dict.fromkeys(pm.cond_id for pm in parsed if pm.cond_id and pm.volume_24h <= Decimal("0")))
            gamma_vols: dict[str, Decimal] = {}
            if cond_ids:
                for i in range(0, len(cond_ids), 50):
                    chunk = cond_ids[i : i + 50]
                    try:
                        resp = requests.get(
                            "https://gamma-api.polymarket.com/markets",
                            params={"condition_ids": chunk},
                            timeout=5.0,
                        )
                        resp.raise_for_status()
                        data = resp.json()
                        items = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else [])
                        if isinstance(items, list):
                            for it in items:
                                if not isinstance(it, dict):
                                    continue
                                cid = str(it.get("conditionId") or it.get("condition_id") or "").strip()
                                raw_v = it.get("volume24hr")
                                if raw_v is None:
                                    raw_v = it.get("volume_24hr")
                                if cid and raw_v is not None:
                                    try:
                                        v = Decimal(str(raw_v))
                                    except Exception:
                                        v = Decimal("0")
                                    gamma_vols[cid] = max(gamma_vols.get(cid, Decimal("0")), v)
                    except Exception as exc:
                        logger.warning("Gamma API volume enrichment failed: %s", exc)

            enriched: list[_SamplingMarket] = []
            for pm in parsed:
                vol = gamma_vols.get(pm.cond_id, pm.volume_24h)
                if self.settings.min_24h_volume > Decimal("0") and vol < self.settings.min_24h_volume:
                    continue
                enriched.append(replace(pm, volume_24h=vol))
            parsed = enriched

        if crawl_completed:
            if max_pages >= MAX_SAMPLING_PAGES:
                self._sampling_markets_cache = list(parsed)
                self._sampling_markets_cache_ts = now_mono
            return parsed
        else:
            if (
                max_pages >= MAX_SAMPLING_PAGES
                and self._sampling_markets_cache is not None
                and (now_mono - self._sampling_markets_cache_ts) <= SAMPLING_STALE_CACHE_TTL_SEC
            ):
                logger.warning(
                    "Sampling markets crawl incomplete after %d pages; returning stale complete cache",
                    pages_fetched,
                )
                return list(self._sampling_markets_cache)
            logger.warning(
                "Sampling markets crawl incomplete after %d pages with no stale cache; returning %d markets without caching",
                pages_fetched,
                len(parsed),
            )
            return parsed

    def fetch_reward_markets(self, limit: int = 20) -> list[tuple[Market, IncentiveWindow]]:
        now_utc = datetime.now(timezone.utc)
        results: list[tuple[Market, IncentiveWindow]] = []

        for pm in self._fetch_sampling_markets():
            for tok in pm.tokens:
                if not isinstance(tok, dict):
                    continue
                token_id = str(tok.get("token_id", ""))
                if not token_id:
                    continue

                market = Market(
                    slug=token_id,
                    event_id=pm.market_slug,
                    category="global",
                    tick=pm.tick,
                    min_qty=pm.min_qty,
                    active=True,
                    mutually_exclusive=False,
                    question=pm.question,
                )
                self._record_market_meta(token_id, market)

                window = IncentiveWindow(
                    slug=token_id,
                    program_id=pm.cond_id,
                    pool_usd=pm.pool_usd,
                    discount=Decimal("0.30"),
                    target_size=pm.target_size,
                    max_spread=pm.max_spread,
                    start=pm.accepting_order_timestamp or (now_utc - timedelta(hours=1)),
                    end=pm.end_date,
                    status="active",
                    event_start_time=pm.game_start_time,
                    accepting_order_timestamp=pm.accepting_order_timestamp,
                    seconds_delay=pm.seconds_delay,
                    accepting_orders=pm.accepting_orders,
                    archived=pm.archived,
                    volume_24h=pm.volume_24h,
                )
                results.append((market, window))
                if limit > 0 and len(results) >= limit:
                    return results

        return results

    def fetch_reward_conditions(self, limit: int = 0, force_refresh: bool = False) -> list[ConditionMarket]:
        now_mono = time.monotonic()
        if (
            not force_refresh
            and self._reward_conditions_cache is not None
            and (now_mono - self._reward_conditions_cache_ts) < self._reward_conditions_ttl_seconds
        ):
            if limit > 0:
                return list(self._reward_conditions_cache[:limit])
            return list(self._reward_conditions_cache)

        now_utc = datetime.now(timezone.utc)
        results: list[ConditionMarket] = []

        for pm in self._fetch_sampling_markets(force_refresh=force_refresh):
            pair = self._record_token_pair(pm.tokens)
            if pair is None:
                continue
            yes_id, no_id = pair

            market_yes = Market(
                slug=yes_id,
                event_id=pm.market_slug,
                category="global",
                tick=pm.tick,
                min_qty=pm.min_qty,
                active=True,
                mutually_exclusive=False,
                question=pm.question,
            )
            market_no = Market(
                slug=no_id,
                event_id=pm.market_slug,
                category="global",
                tick=pm.tick,
                min_qty=pm.min_qty,
                active=True,
                mutually_exclusive=False,
                question=pm.question,
            )
            self._record_market_meta(yes_id, market_yes)
            self._record_market_meta(no_id, market_no)

            window = IncentiveWindow(
                slug=pm.cond_id,
                program_id=pm.cond_id,
                pool_usd=pm.pool_usd,
                discount=Decimal("0.30"),
                target_size=pm.target_size,
                max_spread=pm.max_spread,
                start=pm.accepting_order_timestamp or (now_utc - timedelta(hours=1)),
                end=pm.end_date,
                status="active",
                event_start_time=pm.game_start_time,
                accepting_order_timestamp=pm.accepting_order_timestamp,
                seconds_delay=pm.seconds_delay,
                accepting_orders=pm.accepting_orders,
                archived=pm.archived,
                volume_24h=pm.volume_24h,
            )

            results.append(
                ConditionMarket(
                    condition_id=pm.cond_id,
                    event_id=pm.market_slug,
                    question=pm.question,
                    yes=market_yes,
                    no=market_no,
                    window=window,
                    neg_risk_market_id=pm.neg_risk_market_id,
                    tags=pm.tags,
                    accepting_order_timestamp=pm.accepting_order_timestamp,
                    seconds_delay=pm.seconds_delay,
                    accepting_orders=pm.accepting_orders,
                    archived=pm.archived,
                    volume_24h=pm.volume_24h,
                )
            )

        self._reward_conditions_cache = list(results)
        self._reward_conditions_cache_ts = now_mono

        if limit > 0:
            return results[:limit]
        return results


    @staticmethod
    def _parse_book_payload(token_id: str, data: Any) -> Book:
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
        try:
            ts_ms = int(ts_raw) if ts_raw is not None else int(time.time() * 1000)
        except Exception:
            ts_ms = int(time.time() * 1000)

        def positive_decimal(key: str) -> Decimal | None:
            raw = data.get(key) if isinstance(data, dict) else None
            if raw in (None, ""):
                return None
            value = Decimal(str(raw))
            return value if value > Decimal("0") else None

        return Book(
            slug=token_id,
            bids=tuple(bids),
            asks=tuple(asks),
            ts_ms=ts_ms,
            tick_size=positive_decimal("tick_size"),
            last_trade_price=positive_decimal("last_trade_price"),
        )

    def fetch_book(self, token_id: str) -> Book:
        resp = requests.get(f"{self.host}/book?token_id={token_id}", timeout=10.0)
        resp.raise_for_status()
        return self._parse_book_payload(token_id, resp.json())

    def fetch_books_batch(self, token_ids: list[str], chunk_size: int = 200) -> dict[str, Book]:
        if not token_ids:
            return {}
        start_time = time.monotonic()
        results: dict[str, Book] = {}
        for chunk_idx, i in enumerate(range(0, len(token_ids), chunk_size)):
            chunk = token_ids[i : i + chunk_size]
            payload = [{"token_id": tid} for tid in chunk]
            batch_data = None
            last_exc = None
            for attempt in range(2):
                if attempt > 0:
                    time.sleep(1.0)
                try:
                    resp = requests.post(f"{self.host}/books", json=payload, timeout=15.0)
                    resp.raise_for_status()
                    data = resp.json()
                    if isinstance(data, list):
                        batch_data = data
                        break
                    last_exc = ValueError(f"Non-list response: {type(data)}")
                except Exception as exc:
                    last_exc = exc

            if batch_data is None:
                logger.warning("Batch books chunk %d failed after retry: %s", chunk_idx, last_exc)
                continue

            for item in batch_data:
                if isinstance(item, dict):
                    tid = str(item.get("asset_id") or item.get("token_id") or "").strip()
                    if tid:
                        try:
                            results[tid] = self._parse_book_payload(tid, item)
                        except Exception as parse_exc:
                            logger.warning("Failed to parse book payload for %s: %s", tid, parse_exc)

            if len(token_ids) <= 20:
                for tid in chunk:
                    if tid not in results:
                        try:
                            results[tid] = self.fetch_book(tid)
                        except Exception as exc:
                            logger.warning("Fallback fetch_book failed for %s: %s", tid, exc)

        elapsed = time.monotonic() - start_time
        logger.info(
            "fetch_books_batch: %d tokens requested, %d returned in %.2f seconds",
            len(token_ids),
            len(results),
            elapsed,
        )
        return results

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
                price=round(float(quote.price), 4),
                size=round(float(quote.qty), 4),
                side=side,
            )
            try:
                resp = self.client.create_and_post_order(args)
            except Exception as exc:
                logger.warning("Order post failed for %s %s @ %s: %s", quote.slug, quote.side, quote.price, exc)
                continue
            if isinstance(resp, dict):
                err_msg = str(resp.get("errorMsg") or resp.get("error") or "").strip()
                success_flag = resp.get("success")
                status_str = str(resp.get("status") or "").strip().lower()
                if err_msg or (success_flag is False) or (status_str in ("killed", "canceled", "cancelled", "unmatched", "rejected")):
                    logger.warning("Order for %s %s @ %s rejected by exchange: %r", quote.slug, quote.side, quote.price, resp)
                    continue
                order_id = str(resp.get("orderID") or resp.get("order_id") or resp.get("id") or "")
            elif hasattr(resp, "orderID"):
                order_id = str(resp.orderID)
            elif hasattr(resp, "id"):
                order_id = str(resp.id)
            else:
                order_id = ""

            if not order_id:
                logger.warning("Order for %s %s @ %s not acknowledged: %r", quote.slug, quote.side, quote.price, resp)
                continue

            created.append(
                LiveOrder(
                    order_id=order_id,
                    slug=quote.slug,
                    side=quote.side,
                    price=quote.price,
                    qty_open=quote.qty,
                )
            )
        if created:
            self.invalidate_buying_power_cache()
        return created

    def cancel_orders(self, order_ids: list[str]) -> bool:
        if not order_ids:
            return True
        if not self.is_authenticated:
            raise RuntimeError("Polymarket Global client not authenticated")
        self.client.cancel_orders(order_ids)
        self.invalidate_buying_power_cache()
        return True

    def cancel_orders_batch(self, orders: list[LiveOrder]) -> list[str]:
        order_ids = [o.order_id for o in orders if o.order_id]
        self.cancel_orders(order_ids)
        return order_ids

    def cancel_all_scoped(self, slugs: list[str]) -> list[str]:
        if not slugs:
            return []
        try:
            open_orders = self.get_open_orders(slugs=slugs)
        except Exception as exc:
            logger.warning("Open-orders read failed during scoped cancel (%s); canceling by market", exc)
            self._cancel_by_market(slugs)
            return []
        order_ids = [o.order_id for o in open_orders if o.order_id]
        self.cancel_orders(order_ids)
        return order_ids

    def _cancel_by_market(self, slugs: list[str]) -> None:
        try:
            for slug in slugs:
                if IS_V2:
                    self.client.cancel_market_orders(OrderMarketCancelParams(asset_id=slug))
                else:
                    self.client.cancel_market_orders(asset_id=slug)
            self.invalidate_buying_power_cache()
        except Exception as exc:
            logger.warning("Per-market cancel failed (%s); canceling all orders", exc)
            self.client.cancel_all()
            self.invalidate_buying_power_cache()

    def get_open_orders(self, slugs: list[str] | None = None) -> list[LiveOrder]:
        if not self.is_authenticated:
            return []
        if hasattr(self.client, "get_open_orders"):
            raw_orders = self.client.get_open_orders(OpenOrderParams())
        else:
            raw_orders = self.client.get_orders(OpenOrderParams())

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

    def invalidate_buying_power_cache(self) -> None:
        self._buying_power_cache = None
        self._buying_power_cache_ts = 0.0

    def get_buying_power(self) -> Decimal:
        if not self.is_authenticated:
            return Decimal("0")
        now_mono = time.monotonic()
        if (
            self._buying_power_cache is not None
            and (now_mono - self._buying_power_cache_ts) < BUYING_POWER_CACHE_TTL_SEC
        ):
            return self._buying_power_cache

        if self._resolved_bp_sig_type is not None:
            sig_candidates = [self._resolved_bp_sig_type]
        else:
            candidates = [3, getattr(getattr(self.client, "builder", None), "sig_type", 2), 2, 1, 0]
            sig_candidates = []
            for s in candidates:
                if s is not None and s not in sig_candidates:
                    sig_candidates.append(s)

        saw_zero = False
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
                if self._resolved_bp_sig_type is not None and sig == self._resolved_bp_sig_type:
                    if val >= Decimal("0"):
                        self._buying_power_cache = val
                        self._buying_power_cache_ts = now_mono
                        return val
                elif val > Decimal("0"):
                    self._resolved_bp_sig_type = sig
                    self._buying_power_cache = val
                    self._buying_power_cache_ts = now_mono
                    return val
                elif val == Decimal("0"):
                    saw_zero = True
            except Exception:
                continue

        if saw_zero:
            self._buying_power_cache = Decimal("0")
            self._buying_power_cache_ts = now_mono
            return Decimal("0")
        if self._buying_power_cache is not None:
            return self._buying_power_cache
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

    def invalidate_positions_cache(self) -> None:
        self._positions_cache = None
        self._positions_cache_ts = 0.0
        self.invalidate_buying_power_cache()

    def get_positions(self) -> list[Position]:
        if not self.is_authenticated:
            return []
        proxy = self.settings.polymarket_proxy_address.strip()
        if not proxy:
            raise RuntimeError("POLYMARKET_PROXY_ADDRESS is required to read positions")
        now_mono = time.monotonic()
        if self._positions_cache is not None and (now_mono - self._positions_cache_ts) < POSITIONS_CACHE_TTL_SEC:
            return list(self._positions_cache)
        positions: list[Position] = []
        offset = 0
        try:
            while True:
                resp = requests.get(
                    DATA_API_POSITIONS_URL,
                    params={"user": proxy, "sizeThreshold": 0, "limit": POSITIONS_PAGE_SIZE, "offset": offset},
                    timeout=10,
                )
                resp.raise_for_status()
                page = resp.json()
                if not page:
                    break
                positions.extend(
                    Position(
                        slug=str(p["asset"]),
                        net_qty=Decimal(str(p["size"])),
                        avg_price=Decimal(str(p["avgPrice"])),
                        cur_price=Decimal(str(p["curPrice"])) if p.get("curPrice") not in (None, "") else None,
                        redeemable=bool(p.get("redeemable", False)),
                        archived=bool(p.get("archived", False)),
                    )
                    for p in page
                )
                if len(page) < POSITIONS_PAGE_SIZE:
                    break
                offset += len(page)
            self._positions_cache = list(positions)
            self._positions_cache_ts = time.monotonic()
            return positions
        except Exception:
            if self._positions_cache is not None:
                return list(self._positions_cache)
            raise

    def get_fills(self, since: datetime | None = None) -> list[ShadowFill]:
        if not self.is_authenticated:
            return []
        params = TradeParams(after=int(since.timestamp())) if since is not None else TradeParams()
        trades = self.client.get_trades(params)
        proxy = self.settings.polymarket_proxy_address
        return [fill for trade in trades for fill in parse_trade_fills(trade, proxy)]
