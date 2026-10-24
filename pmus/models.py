from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

Side = Literal["BUY_LONG", "SELL_LONG", "BUY_SHORT", "SELL_SHORT"]
MarketState = Literal["IDLE", "QUOTING", "PAUSED", "FLATTENING"]

@dataclass(frozen=True)
class Market:
    slug: str
    event_id: str | None
    category: str
    tick: Decimal
    min_qty: Decimal
    active: bool
    mutually_exclusive: bool = False
    question: str = ""

@dataclass(frozen=True)
class Level:
    price: Decimal
    qty: Decimal

@dataclass(frozen=True)
class Book:
    slug: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    ts_ms: int
    tick_size: Decimal | None = None
    last_trade_price: Decimal | None = None

@dataclass(frozen=True)
class IncentiveWindow:
    slug: str
    program_id: str
    pool_usd: Decimal
    discount: Decimal
    target_size: Decimal
    max_spread: Decimal | None
    start: datetime
    end: datetime | None
    status: str
    period: str = "early"
    event_start_time: datetime | None = None
    accepting_order_timestamp: datetime | None = None
    seconds_delay: int = 0
    accepting_orders: bool = True
    archived: bool = False

@dataclass(frozen=True)
class ConditionMarket:
    condition_id: str
    event_id: str
    question: str
    yes: Market
    no: Market
    window: IncentiveWindow
    neg_risk_market_id: str = ""
    tags: tuple[str, ...] = ()
    accepting_order_timestamp: datetime | None = None
    seconds_delay: int = 0
    accepting_orders: bool = True
    archived: bool = False

@dataclass(frozen=True)
class Quote:
    slug: str
    side: Side
    price: Decimal
    qty: Decimal

@dataclass(frozen=True)
class LiveOrder:
    order_id: str
    slug: str
    side: Side
    price: Decimal
    qty_open: Decimal

    @property
    def remaining_qty(self) -> Decimal:
        return self.qty_open

@dataclass(frozen=True)
class Position:
    slug: str
    net_qty: Decimal
    avg_price: Decimal

@dataclass(frozen=True)
class BasketOpp:
    event_id: str
    direction: Literal["SHORT_ALL", "BUY_ALL"]
    legs: tuple[tuple[str, Decimal], ...]
    gross_edge: Decimal
    fees: Decimal
    net_edge: Decimal

@dataclass(frozen=True)
class ShadowFill:
    fill_id: str
    order_id: str
    slug: str
    side: Side
    price: Decimal
    qty: Decimal
    fee: Decimal
    is_maker: bool
    ts_ms: int

@dataclass(frozen=True)
class LipSnapshot:
    ts_ms: int
    slug: str
    program_id: str
    trader_bid_score: Decimal
    total_bid_score: Decimal
    trader_ask_score: Decimal
    total_ask_score: Decimal
    qualifies: bool
    reward_usd: Decimal
    bid_qualifies: bool = False
    ask_qualifies: bool = False
    dt_sec: int = 1
