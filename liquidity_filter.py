"""
Strict Liquidity & Spread Gating Engine for Polymarket Arbitrage Trading.

Eliminates leg-out execution risk and thin-market traps where spreads reach 50-70%
(e.g., best bid $0.01 vs ask $0.04) and available book depth is under $5.

Core Components:
1. validate_order_book_liquidity:
   Verifies bid-ask spreads for both YES and NO books do not exceed max_spread (default 1.5%),
   and top-3 ask depth meets minimum capital threshold min_depth_usd (default $50).
2. is_market_eligible:
   Rejects illiquid prop betting patterns (soccer exact/correct scores, obscure ITF tennis)
   and low 24h volume markets (< $1,000), while passing liquid crypto, politics, and sports.
3. validate_arbitrage_execution:
   Production integration helper for paper_trader.py dropped directly before execute_arbitrage
   and inside check_market_parity.
"""

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("PolyPaperTrader.LiquidityFilter")

DEFAULT_MAX_SPREAD: float = 0.015          # 1.5 cents max bid-ask spread
DEFAULT_MIN_DEPTH_USD: float = 100.0        # $100.00 minimum executable ask depth
DEFAULT_MIN_VOLUME_24H: float = 1000.0      # $1,000 minimum 24h market volume
DEFAULT_MIN_HOURS_TO_EXPIRY: float = 4.0     # 4.0 hours safety horizon before resolution

TURBO_AND_SHORT_DURATION_PATTERNS: List[str] = [
    "up or down",
    "up/down",
    "updown",
    "15m",
    "5m",
    "10m",
    "30m",
    "1h ",
    "halftime",
    "half-time",
    "1st half",
    "2nd half",
    "minute",
    "overtime",
    "quarter",
    "set 1",
    "set 2",
    "set 3",
    "set 4",
    "set 5",
    "innings",
    "inning",
]

TIME_RANGE_REGEX = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)", re.IGNORECASE)


def _parse_date_value(raw_val: Any) -> Optional[datetime]:
    """Helper to parse datetime, epoch, or ISO/custom string into UTC datetime."""
    if not raw_val:
        return None
    if isinstance(raw_val, datetime):
        if raw_val.tzinfo is None:
            return raw_val.replace(tzinfo=timezone.utc)
        return raw_val.astimezone(timezone.utc)
    if isinstance(raw_val, (int, float)):
        try:
            ts = float(raw_val)
            if ts > 1e11:  # epoch in milliseconds
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(raw_val, str):
        raw_str = raw_val.strip()
        try:
            ts = float(raw_str)
            if ts > 1e11:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except ValueError:
            pass
        iso_str = raw_str
        if iso_str.endswith("Z"):
            iso_str = iso_str[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw_str, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def _parse_market_start_date(market: Any) -> Optional[datetime]:
    """
    Extracts and parses game or event start datetime as a UTC datetime.
    Only inspects gameStartTime, game_start_time, matchStartTime, sportsStartTime, eventStartTime.
    Excludes startDate/startDateIso which represents market creation time in Gamma API.
    """
    if not isinstance(market, dict):
        return None
    raw_val = (
        market.get("gameStartTime") or
        market.get("game_start_time") or
        market.get("matchStartTime") or
        market.get("sportsStartTime") or
        market.get("eventStartTime")
    )
    return _parse_date_value(raw_val)


SPORTS_KEYWORDS: Tuple[str, ...] = (
    "sport", "sports", "nfl", "nba", "mlb", "nhl", "soccer", "football",
    "basketball", "baseball", "tennis", "cricket", "mma", "ufc", "boxing",
    "esports", "f1", "formula 1", "champions league", "premier league",
    "la liga", "serie a", "bundesliga", "uefa", "fifa", "super bowl",
    "atp", "wta", "itf", "challenger", "pga", "lpga", "nascar", "mls"
)


def _is_sports_market(market: dict, text: str = "") -> bool:
    if not isinstance(market, dict):
        return False
    category = str(market.get("category") or "").strip().lower()
    if "sport" in category:
        return True
    if market.get("sportsMarketType"):
        return True
    tags = market.get("tags")
    if isinstance(tags, list):
        for t in tags:
            tag_str = str(t).lower()
            if any(kw in tag_str for kw in ("sport", "sports", "nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball", "baseball", "tennis", "mma", "ufc")):
                return True
    elif isinstance(tags, str) and "sport" in tags.lower():
        return True
    if not text:
        text = " ".join([
            str(market.get("question") or ""),
            str(market.get("title") or ""),
            str(market.get("slug") or market.get("market_slug") or ""),
            category
        ]).lower()
    return any(kw in text for kw in SPORTS_KEYWORDS)



def _parse_market_end_date(market: Any) -> Optional[datetime]:
    """
    Extracts and parses the expiration / resolution datetime as a UTC datetime.
    Inspects endDateIso, endDate, end_date_iso, end_date, gameStartTime,
    handling ISO strings, epoch seconds/ms, and datetime objects.
    """
    if not isinstance(market, dict):
        return None
    raw_val = (
        market.get("endDateIso") or
        market.get("endDate") or
        market.get("end_date_iso") or
        market.get("end_date") or
        market.get("gameStartTime") or
        market.get("game_start_time")
    )
    return _parse_date_value(raw_val)


def compute_dynamic_min_depth(capital: float, desired_size: float = 0.0, floor_override: Optional[float] = None) -> float:
    """
    Computes dynamic minimum required depth.
    If floor_override is provided, respects floor_override directly.
    Otherwise enforces DEFAULT_MIN_DEPTH_USD (10.0) floor and at least 2.5x desired trade size.
    """
    if floor_override is not None:
        return round(float(floor_override), 2)
    return round(max(DEFAULT_MIN_DEPTH_USD, desired_size * 2.5), 2)




ILLIQUID_PATTERNS: List[Tuple[str, str]] = [
    ("exact score", "soccer exact score prop"),
    ("correct score", "soccer correct score prop"),
    ("itf ", "obscure ITF tennis"),
    ("itf-", "obscure ITF tennis"),
    ("itf:", "obscure ITF tennis"),
    ("itf tennis", "obscure ITF tennis"),
    ("itf men", "obscure ITF tennis"),
    ("itf women", "obscure ITF tennis"),
    ("atp ", "tennis match"),
    ("wta ", "tennis match"),
    ("tennis", "tennis match"),
    ("rolex masters", "tennis tournament"),
    ("china open", "tennis tournament"),
    ("challenger", "challenger tennis"),
    ("w15 ", "tennis event"),
    ("w25 ", "tennis event"),
    ("w35 ", "tennis event"),
    ("wuning", "tennis event"),
    ("suzhou", "tennis event"),
    ("maanshan", "tennis event"),
    ("villena", "tennis event"),
    ("samsun", "tennis event"),
    ("antofagasta", "tennis event"),
    ("tiburon", "tennis event"),
    ("corner kick", "soccer corner kick prop"),
    ("corners over", "soccer corner kick prop"),
    ("total corners", "soccer corner kick prop"),
    ("booking points", "soccer cards/booking points prop"),
    ("method of victory", "combat sports method of victory prop"),
    ("both teams to score", "btts prop"),
    ("btts", "btts prop"),
    ("o/u ", "over under total"),
    ("over/under", "over under total"),
    ("first goal", "first goal prop"),
    ("anytime goal", "anytime goal prop"),
    ("touchdown", "nfl touchdown prop"),
    ("points handicap", "handicap prop"),
    ("game handicap", "handicap prop"),
    ("spread:", "sports spread prop"),
    ("total points", "total points prop"),
    ("total goals", "total goals prop"),
    ("total runs", "total runs prop"),
    ("cricket", "cricket match"),
    ("t20", "cricket match"),
    ("ipl", "cricket match"),
    ("odi", "cricket match"),
    ("baseball", "baseball match"),
    ("mlb", "baseball match"),
    ("nba", "basketball match"),
    ("nfl", "football match"),
    ("nhl", "hockey match"),
    ("premier league", "soccer match"),
    ("la liga", "soccer match"),
    ("serie a", "soccer match"),
    ("bundesliga", "soccer match"),
    ("uefa", "soccer match"),
    ("champions league", "soccer match"),
    ("esports", "esports match"),
    ("cs:go", "cs:go esports"),
    ("cs2", "cs2 esports"),
    ("dota", "dota esports"),
    ("valorant", "valorant esports"),
    ("league of legends", "league of legends esports"),
    (" vs ", "in-play head to head match"),
    (" vs. ", "in-play head to head match"),
    (" v ", "in-play head to head match"),
]


def _parse_level(item: Any) -> Optional[Tuple[float, float]]:
    if hasattr(item, "price") and hasattr(item, "size"):
        p = getattr(item, "price")
        s = getattr(item, "size")
        if not isinstance(p, (int, float, str)) or not isinstance(s, (int, float, str)):
            return None
    elif isinstance(item, dict):
        p = item.get("price") or item.get("p")
        s = item.get("size") or item.get("amount") or item.get("qty") or item.get("shares") or item.get("s")
    elif isinstance(item, (list, tuple)) and len(item) >= 2:
        p, s = item[0], item[1]
    else:
        return None

    try:
        p_val = float(p)
        s_val = float(s)
        if p_val > 0 and s_val > 0:
            return (p_val, s_val)
    except (ValueError, TypeError):
        pass
    return None


def _extract_book_metrics(book: Any) -> Tuple[Optional[float], Optional[float], float]:
    if not book:
        return None, None, 0.0
    if hasattr(book, "bids") and isinstance(getattr(book, "bids", None), (list, tuple)):
        raw_bids = getattr(book, "bids", None)
        raw_asks = getattr(book, "asks", None)
    elif isinstance(book, dict):
        raw_bids = book.get("bids")
        raw_asks = book.get("asks")
    else:
        return None, None, 0.0

    valid_bids: List[Tuple[float, float]] = []
    if isinstance(raw_bids, (list, tuple)):
        for item in raw_bids:
            lvl = _parse_level(item)
            if lvl:
                valid_bids.append(lvl)

    if valid_bids:
        best_bid = max(p for p, _ in valid_bids)
    else:
        if isinstance(book, dict):
            raw_bid = book.get("bid") or book.get("best_bid") or book.get("bid_price")
        else:
            raw_bid = getattr(book, "bid", None) or getattr(book, "best_bid", None) or getattr(book, "bid_price", None)
        try:
            best_bid = float(raw_bid) if raw_bid is not None else None
        except (ValueError, TypeError):
            best_bid = None

    valid_asks: List[Tuple[float, float]] = []
    if isinstance(raw_asks, (list, tuple)):
        for item in raw_asks:
            lvl = _parse_level(item)
            if lvl:
                valid_asks.append(lvl)

    depth_usd = 0.0
    if valid_asks:
        sorted_asks = sorted(valid_asks, key=lambda x: x[0])
        best_ask = sorted_asks[0][0]
        top3_asks = sorted_asks[:3]
        depth_usd = sum(p * s for p, s in top3_asks)
    else:
        if isinstance(book, dict):
            raw_ask = book.get("ask") or book.get("best_ask") or book.get("ask_price")
        else:
            raw_ask = getattr(book, "ask", None) or getattr(book, "best_ask", None) or getattr(book, "ask_price", None)
        try:
            best_ask = float(raw_ask) if raw_ask is not None else None
        except (ValueError, TypeError):
            best_ask = None

    if depth_usd <= 0.0:
        if isinstance(book, dict):
            raw_depth = (
                book.get("depth_usd") or
                book.get("available_depth_usd") or
                book.get("depth") or
                book.get("ask_depth") or
                book.get("executable_liquidity_usd")
            )
        else:
            raw_depth = (
                getattr(book, "depth_usd", None) or
                getattr(book, "available_depth_usd", None) or
                getattr(book, "depth", None) or
                getattr(book, "ask_depth", None) or
                getattr(book, "executable_liquidity_usd", None)
            )
        if raw_depth is not None:
            try:
                depth_usd = max(0.0, float(raw_depth))
            except (ValueError, TypeError):
                depth_usd = 0.0

    return best_bid, best_ask, depth_usd


def validate_order_book_liquidity(
    book_yes: Any,
    book_no: Any,
    max_spread: float = DEFAULT_MAX_SPREAD,
    min_depth_usd: float = DEFAULT_MIN_DEPTH_USD
) -> Tuple[bool, str]:
    bid_yes, ask_yes, depth_yes = _extract_book_metrics(book_yes)
    bid_no, ask_no, depth_no = _extract_book_metrics(book_no)

    if ask_yes is None or bid_yes is None:
        return False, f"Missing quote in YES book (bid: {bid_yes}, ask: {ask_yes})"
    if ask_no is None or bid_no is None:
        return False, f"Missing quote in NO book (bid: {bid_no}, ask: {ask_no})"

    spread_yes = ask_yes - bid_yes
    if spread_yes - max_spread > 1e-7:
        return False, f"YES spread ({spread_yes:.3f}) exceeds max ({max_spread})"

    spread_no = ask_no - bid_no
    if spread_no - max_spread > 1e-7:
        return False, f"NO spread ({spread_no:.3f}) exceeds max ({max_spread})"

    if (depth_yes + 1e-7 < min_depth_usd) or (depth_no + 1e-7 < min_depth_usd):
        return False, f"Insufficient depth (YES: ${depth_yes:.2f}, NO: ${depth_no:.2f} < ${min_depth_usd})"

    return True, "OK"


def is_market_eligible(
    market: dict,
    min_volume_24h: float = DEFAULT_MIN_VOLUME_24H,
    min_hours_to_expiry: float = DEFAULT_MIN_HOURS_TO_EXPIRY
) -> Tuple[bool, str]:
    if not isinstance(market, dict):
        return False, "Invalid market data"

    # 1. StartTime / In-Play Sports Gating
    start_dt = _parse_market_start_date(market)
    is_sports = (
        bool(
            market.get("gameStartTime") or
            market.get("game_start_time") or
            market.get("matchStartTime") or
            market.get("sportsStartTime") or
            market.get("sportsMarketType")
        ) or
        _is_sports_market(market)
    )
    if start_dt is not None and is_sports:
        now_utc = datetime.now(timezone.utc)
        if start_dt <= now_utc:
            return False, "In-play sports match rejected (game already started)"
        if (start_dt - now_utc) < timedelta(hours=1.0):
            hours_to_start = (start_dt - now_utc).total_seconds() / 3600.0
            return False, f"Sports match starts too soon ({hours_to_start:.1f}h < 1.0h safety threshold)"

    # 2. Turbo & Short-Duration Horizon Gating (applied to title / question / slug only)
    market_title = " ".join([
        str(market.get("question") or ""),
        str(market.get("title") or ""),
        str(market.get("slug") or market.get("market_slug") or "")
    ]).lower()

    for pattern in TURBO_AND_SHORT_DURATION_PATTERNS:
        if pattern in market_title:
            return False, f"Turbo/short-duration pattern rejected: '{pattern}'"

    if TIME_RANGE_REGEX.search(market_title):
        return False, "Turbo/short-duration pattern rejected: time interval format"

    # 3. Illiquid / Toxic Prop Pattern Gating (searches all text including description)
    search_text = " ".join([
        market_title,
        str(market.get("description") or ""),
        str(market.get("category") or "")
    ]).lower()

    HEAD_TO_HEAD_PATTERNS = (" vs ", " vs. ", " v ")
    SPORTS_SPECIFIC_PATTERNS = ("ipl", "odi", "t20", "btts", "o/u ", "mlb", "nba", "nfl", "nhl", "uefa", "cs2", "dota")
    for pattern, label in ILLIQUID_PATTERNS:
        if not is_sports and pattern in SPORTS_SPECIFIC_PATTERNS:
            continue
        if pattern in HEAD_TO_HEAD_PATTERNS and not is_sports:
            continue
        clean_pat = pattern.strip()
        if len(clean_pat) <= 4 and clean_pat.isalnum():
            if re.search(r"\b" + re.escape(clean_pat) + r"\b", search_text):
                return False, f"Illiquid prop pattern rejected: {label} ('{pattern}')"
        else:
            if pattern in search_text:
                return False, f"Illiquid prop pattern rejected: {label} ('{pattern}')"

    # 4. Expiration Horizon Gating
    end_dt = _parse_market_end_date(market)
    if end_dt is not None:
        now_utc = datetime.now(timezone.utc)
        hours_left = (end_dt - now_utc).total_seconds() / 3600.0
        if hours_left <= 0:
            return False, "Market has already expired or is resolving"
        if hours_left < min_hours_to_expiry:
            return False, f"Market expires too soon ({hours_left:.1f}h < {min_hours_to_expiry}h safety threshold)"

    # 5. Volume Gating
    vol_val = (
        market.get("volume24hr") or
        market.get("volume_24h") or
        market.get("volume24h") or
        market.get("volume") or
        0.0
    )
    try:
        vol = float(vol_val)
    except (ValueError, TypeError):
        vol = 0.0

    vol_key_present = any(k in market for k in ("volume24hr", "volume_24h", "volume24hr", "volume24h", "volume"))
    if vol_key_present and vol < min_volume_24h:
        return False, f"Volume 24h (${vol:.2f}) below minimum (${min_volume_24h:.2f})"

    return True, "OK"


def validate_arbitrage_execution(
    opp: dict,
    book_yes: Optional[Any] = None,
    book_no: Optional[Any] = None,
    market_meta: Optional[dict] = None,
    max_spread: float = DEFAULT_MAX_SPREAD,
    min_depth_usd: float = DEFAULT_MIN_DEPTH_USD,
    min_volume_24h: float = DEFAULT_MIN_VOLUME_24H,
    min_hours_to_expiry: float = DEFAULT_MIN_HOURS_TO_EXPIRY
) -> Tuple[bool, str]:
    if not isinstance(opp, dict):
        return False, "Invalid arbitrage opportunity object"

    # Direct start-time check if opp contains date metadata
    start_dt = _parse_market_start_date(opp)
    meta = market_meta or opp.get("market_meta") or opp.get("market") or opp
    is_sports = (
        bool(
            (meta.get("gameStartTime") if isinstance(meta, dict) else None) or
            (meta.get("game_start_time") if isinstance(meta, dict) else None) or
            (meta.get("matchStartTime") if isinstance(meta, dict) else None) or
            (meta.get("sportsStartTime") if isinstance(meta, dict) else None) or
            (meta.get("sportsMarketType") if isinstance(meta, dict) else None)
        ) or
        (_is_sports_market(meta) if isinstance(meta, dict) else False)
    )
    if start_dt is not None and is_sports:
        now_utc = datetime.now(timezone.utc)
        if start_dt <= now_utc:
            return False, "In-play sports match rejected (game already started)"
        if (start_dt - now_utc) < timedelta(hours=1.0):
            hours_to_start = (start_dt - now_utc).total_seconds() / 3600.0
            return False, f"Sports match starts too soon ({hours_to_start:.1f}h < 1.0h safety threshold)"

    # Direct expiration check if opp contains date metadata
    end_dt = _parse_market_end_date(opp)
    if end_dt is not None:
        now_utc = datetime.now(timezone.utc)
        hours_left = (end_dt - now_utc).total_seconds() / 3600.0
        if hours_left <= 0:
            return False, "Market has already expired or is resolving"
        if hours_left < min_hours_to_expiry:
            return False, f"Market expires too soon ({hours_left:.1f}h < {min_hours_to_expiry}h safety threshold)"

    meta = market_meta or opp.get("market_meta") or opp.get("market")
    if isinstance(meta, dict):
        eligible, reason = is_market_eligible(
            meta,
            min_volume_24h=min_volume_24h,
            min_hours_to_expiry=min_hours_to_expiry
        )
        if not eligible:
            return False, f"Market eligibility gate failed: {reason}"

    b_yes = book_yes or opp.get("book_yes")
    b_no = book_no or opp.get("book_no")
    has_bids_y = (isinstance(b_yes, dict) and "bids" in b_yes) or (hasattr(b_yes, "bids") and isinstance(getattr(b_yes, "bids", None), (list, tuple)))
    has_bids_n = (isinstance(b_no, dict) and "bids" in b_no) or (hasattr(b_no, "bids") and isinstance(getattr(b_no, "bids", None), (list, tuple)))
    if b_yes and b_no and has_bids_y and has_bids_n:
        return validate_order_book_liquidity(b_yes, b_no, max_spread=max_spread, min_depth_usd=min_depth_usd)

    available_depth = opp.get("available_depth_usd") or opp.get("executable_liquidity_usd")
    if available_depth is not None:
        try:
            depth_val = float(available_depth)
            if depth_val + 1e-7 < min_depth_usd:
                return False, f"Insufficient depth (${depth_val:.2f} < ${min_depth_usd})"
        except (ValueError, TypeError):
            pass

    ask_yes = opp.get("ask_yes")
    bid_yes = opp.get("bid_yes")
    if ask_yes is not None and bid_yes is not None:
        spread_yes = float(ask_yes) - float(bid_yes)
        if spread_yes - max_spread > 1e-7:
            return False, f"YES spread ({spread_yes:.3f}) exceeds max ({max_spread})"

    ask_no = opp.get("ask_no")
    bid_no = opp.get("bid_no")
    if ask_no is not None and bid_no is not None:
        spread_no = float(ask_no) - float(bid_no)
        if spread_no - max_spread > 1e-7:
            return False, f"NO spread ({spread_no:.3f}) exceeds max ({max_spread})"

    return True, "OK"


def check_market_parity_liquidity_gate(
    market_id: str,
    market_meta: Optional[dict] = None,
    depth_yes: Optional[float] = None,
    depth_no: Optional[float] = None,
    book_yes: Optional[dict] = None,
    book_no: Optional[dict] = None,
    max_spread: float = DEFAULT_MAX_SPREAD,
    min_depth_usd: float = DEFAULT_MIN_DEPTH_USD,
    min_volume_24h: float = DEFAULT_MIN_VOLUME_24H,
    min_hours_to_expiry: float = DEFAULT_MIN_HOURS_TO_EXPIRY
) -> Tuple[bool, str]:
    if market_meta:
        eligible, reason = is_market_eligible(
            market_meta,
            min_volume_24h=min_volume_24h,
            min_hours_to_expiry=min_hours_to_expiry
        )
        if not eligible:
            return False, reason

    if depth_yes is not None and depth_no is not None:
        if (depth_yes + 1e-7 < min_depth_usd) or (depth_no + 1e-7 < min_depth_usd):
            return False, f"Insufficient depth (YES: ${depth_yes:.2f}, NO: ${depth_no:.2f} < ${min_depth_usd})"

    if book_yes and book_no:
        return validate_order_book_liquidity(book_yes, book_no, max_spread=max_spread, min_depth_usd=min_depth_usd)

    return True, "OK"
