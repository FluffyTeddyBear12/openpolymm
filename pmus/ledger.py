import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from pmus.models import LipSnapshot, LiveOrder, Position, Quote, ShadowFill

class Ledger:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quotes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER NOT NULL,
                    slug TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price TEXT NOT NULL,
                    qty TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price TEXT NOT NULL,
                    qty_open TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    closed_at_ms INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price TEXT NOT NULL,
                    qty TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    is_maker INTEGER NOT NULL,
                    ts_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lip_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER NOT NULL,
                    slug TEXT NOT NULL,
                    program_id TEXT NOT NULL,
                    trader_bid_score TEXT NOT NULL,
                    total_bid_score TEXT NOT NULL,
                    trader_ask_score TEXT NOT NULL,
                    total_ask_score TEXT NOT NULL,
                    qualifies INTEGER NOT NULL,
                    reward_usd TEXT NOT NULL,
                    bid_qualifies INTEGER DEFAULT 0,
                    ask_qualifies INTEGER DEFAULT 0
                )
                """
            )
            cursor = conn.execute("PRAGMA table_info(lip_snapshots)")
            cols = [row["name"] for row in cursor.fetchall()]
            if cols and "bid_qualifies" not in cols:
                conn.execute("ALTER TABLE lip_snapshots ADD COLUMN bid_qualifies INTEGER DEFAULT 0")
            if cols and "ask_qualifies" not in cols:
                conn.execute("ALTER TABLE lip_snapshots ADD COLUMN ask_qualifies INTEGER DEFAULT 0")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    slug TEXT PRIMARY KEY,
                    net_qty TEXT NOT NULL,
                    avg_price TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )

    def record_quote(self, quote: Quote, ts_ms: int) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO quotes (ts_ms, slug, side, price, qty) VALUES (?, ?, ?, ?, ?)",
                (ts_ms, quote.slug, quote.side, str(quote.price), str(quote.qty)),
            )

    def record_order(self, order: LiveOrder, status: str = "OPEN", ts_ms: int | None = None) -> None:
        created_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO orders (order_id, slug, side, price, qty_open, status, created_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (order.order_id, order.slug, order.side, str(order.price), str(order.qty_open), status, created_ms),
            )

    def close_order(self, order_id: str, status: str = "CANCELED", ts_ms: int | None = None) -> None:
        closed_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE orders SET status = ?, closed_at_ms = ? WHERE order_id = ?",
                (status, closed_ms, order_id),
            )

    def get_open_orders(self, slug: str | None = None) -> list[LiveOrder]:
        query = "SELECT order_id, slug, side, price, qty_open FROM orders WHERE status = 'OPEN'"
        params: tuple = ()
        if slug:
            query += " AND slug = ?"
            params = (slug,)
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [
                LiveOrder(
                    order_id=r["order_id"],
                    slug=r["slug"],
                    side=r["side"],
                    price=Decimal(r["price"]),
                    qty_open=Decimal(r["qty_open"]),
                )
                for r in rows
            ]

    def record_fill(self, fill: ShadowFill) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO fills (fill_id, order_id, slug, side, price, qty, fee, is_maker, ts_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.order_id,
                    fill.slug,
                    fill.side,
                    str(fill.price),
                    str(fill.qty),
                    str(fill.fee),
                    1 if fill.is_maker else 0,
                    fill.ts_ms,
                ),
            )

    def record_lip_snapshot(self, snap: LipSnapshot) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO lip_snapshots (
                    ts_ms, slug, program_id, trader_bid_score, total_bid_score,
                    trader_ask_score, total_ask_score, qualifies, reward_usd,
                    bid_qualifies, ask_qualifies
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snap.ts_ms,
                    snap.slug,
                    snap.program_id,
                    str(snap.trader_bid_score),
                    str(snap.total_bid_score),
                    str(snap.trader_ask_score),
                    str(snap.total_ask_score),
                    1 if snap.qualifies else 0,
                    str(snap.reward_usd),
                    1 if snap.bid_qualifies else 0,
                    1 if snap.ask_qualifies else 0,
                ),
            )

    def get_position(self, slug: str) -> Position:
        with self._get_connection() as conn:
            row = conn.execute("SELECT net_qty, avg_price FROM positions WHERE slug = ?", (slug,)).fetchone()
            if row:
                return Position(slug=slug, net_qty=Decimal(row["net_qty"]), avg_price=Decimal(row["avg_price"]))
            return Position(slug=slug, net_qty=Decimal("0"), avg_price=Decimal("0"))

    def update_position(self, position: Position, ts_ms: int | None = None) -> None:
        updated_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO positions (slug, net_qty, avg_price, updated_at_ms)
                VALUES (?, ?, ?, ?)
                """,
                (position.slug, str(position.net_qty), str(position.avg_price), updated_ms),
            )

    def get_all_positions(self) -> list[Position]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT slug, net_qty, avg_price FROM positions").fetchall()
            return [
                Position(slug=r["slug"], net_qty=Decimal(r["net_qty"]), avg_price=Decimal(r["avg_price"]))
                for r in rows
            ]

    def get_fills_since(self, since_ms: int) -> list[ShadowFill]:
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT fill_id, order_id, slug, side, price, qty, fee, is_maker, ts_ms
                FROM fills WHERE ts_ms >= ? ORDER BY ts_ms ASC
                """,
                (since_ms,),
            ).fetchall()
            return [
                ShadowFill(
                    fill_id=r["fill_id"],
                    order_id=r["order_id"],
                    slug=r["slug"],
                    side=r["side"],
                    price=Decimal(r["price"]),
                    qty=Decimal(r["qty"]),
                    fee=Decimal(r["fee"]),
                    is_maker=bool(r["is_maker"]),
                    ts_ms=r["ts_ms"],
                )
                for r in rows
            ]

    def get_lip_snapshots_since(self, since_ms: int) -> list[LipSnapshot]:
        with self._get_connection() as conn:
            cursor = conn.execute("PRAGMA table_info(lip_snapshots)")
            cols = [row["name"] for row in cursor.fetchall()]
            has_side_cols = "bid_qualifies" in cols and "ask_qualifies" in cols

            if has_side_cols:
                query = """
                    SELECT ts_ms, slug, program_id, trader_bid_score, total_bid_score,
                           trader_ask_score, total_ask_score, qualifies, reward_usd,
                           bid_qualifies, ask_qualifies
                    FROM lip_snapshots WHERE ts_ms >= ? ORDER BY ts_ms ASC
                """
            else:
                query = """
                    SELECT ts_ms, slug, program_id, trader_bid_score, total_bid_score,
                           trader_ask_score, total_ask_score, qualifies, reward_usd
                    FROM lip_snapshots WHERE ts_ms >= ? ORDER BY ts_ms ASC
                """

            rows = conn.execute(query, (since_ms,)).fetchall()
            return [
                LipSnapshot(
                    ts_ms=r["ts_ms"],
                    slug=r["slug"],
                    program_id=r["program_id"],
                    trader_bid_score=Decimal(r["trader_bid_score"]),
                    total_bid_score=Decimal(r["total_bid_score"]),
                    trader_ask_score=Decimal(r["trader_ask_score"]),
                    total_ask_score=Decimal(r["total_ask_score"]),
                    qualifies=bool(r["qualifies"]),
                    reward_usd=Decimal(r["reward_usd"]),
                    bid_qualifies=bool(r["bid_qualifies"]) if has_side_cols else bool(r["qualifies"]),
                    ask_qualifies=bool(r["ask_qualifies"]) if has_side_cols else bool(r["qualifies"]),
                )
                for r in rows
            ]
