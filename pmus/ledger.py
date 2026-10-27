from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator

from pmus.models import LipSnapshot, LiveOrder, Position, Quote, ShadowFill


class Ledger:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._has_dt_sec = True
        self._has_side_qual = True
        self._init_db()

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
            except Exception:
                conn.close()
                raise
            self._local.conn = conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _init_db(self) -> None:
        try:
            self._ensure_tables()
        except sqlite3.DatabaseError as exc:
            err = str(exc).lower()
            if "malformed" in err or "not a database" in err or "corrupt" in err:
                self.close()
                backup = self.db_path.with_name(f"{self.db_path.name}.corrupt_{int(time.time())}")
                try:
                    self.db_path.rename(backup)
                except Exception:
                    pass
                for suffix in ["-wal", "-shm"]:
                    aux = self.db_path.with_name(f"{self.db_path.name}{suffix}")
                    if aux.exists():
                        try:
                            aux.unlink()
                        except Exception:
                            pass
                self._ensure_tables()
            else:
                raise

    def _ensure_tables(self) -> None:
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_quotes_ts ON quotes (ts_ms)")
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status)")
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills (ts_ms)")
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
                    ask_qualifies INTEGER DEFAULT 0,
                    dt_sec INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lip_snapshots_ts ON lip_snapshots (ts_ms)")
            cursor = conn.execute("PRAGMA table_info(lip_snapshots)")
            cols = {row["name"] for row in cursor.fetchall()}
            if cols and "bid_qualifies" not in cols:
                conn.execute("ALTER TABLE lip_snapshots ADD COLUMN bid_qualifies INTEGER DEFAULT 0")
                cols.add("bid_qualifies")
            if cols and "ask_qualifies" not in cols:
                conn.execute("ALTER TABLE lip_snapshots ADD COLUMN ask_qualifies INTEGER DEFAULT 0")
                cols.add("ask_qualifies")
            if cols and "dt_sec" not in cols:
                conn.execute("ALTER TABLE lip_snapshots ADD COLUMN dt_sec INTEGER NOT NULL DEFAULT 1")
                cols.add("dt_sec")
            self._has_dt_sec = "dt_sec" in cols
            self._has_side_qual = "bid_qualifies" in cols and "ask_qualifies" in cols

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lip_latest (
                    slug TEXT PRIMARY KEY,
                    ts_ms INTEGER NOT NULL,
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lip_stats_summary (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    total_reward TEXT NOT NULL DEFAULT '0',
                    qual_sec INTEGER NOT NULL DEFAULT 0,
                    bid_qual_sec INTEGER NOT NULL DEFAULT 0,
                    ask_qual_sec INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            summary_row = conn.execute("SELECT id FROM lip_stats_summary WHERE id = 1").fetchone()
            if summary_row is None:
                stats = conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(CAST(reward_usd AS NUMERIC)), 0) AS total_reward,
                        COALESCE(SUM(CASE WHEN qualifies = 1 THEN MAX(1, COALESCE(dt_sec, 1)) ELSE 0 END), 0) AS qual_sec,
                        COALESCE(SUM(CASE WHEN bid_qualifies = 1 THEN MAX(1, COALESCE(dt_sec, 1)) ELSE 0 END), 0) AS bid_qual_sec,
                        COALESCE(SUM(CASE WHEN ask_qualifies = 1 THEN MAX(1, COALESCE(dt_sec, 1)) ELSE 0 END), 0) AS ask_qual_sec
                    FROM lip_snapshots
                    """
                ).fetchone()
                total_reward = str(stats["total_reward"]) if stats else "0"
                qual_sec = int(stats["qual_sec"]) if stats else 0
                bid_qual_sec = int(stats["bid_qual_sec"]) if stats else 0
                ask_qual_sec = int(stats["ask_qual_sec"]) if stats else 0
                conn.execute(
                    """
                    INSERT INTO lip_stats_summary (id, total_reward, qual_sec, bid_qual_sec, ask_qual_sec)
                    VALUES (1, ?, ?, ?, ?)
                    """,
                    (total_reward, qual_sec, bid_qual_sec, ask_qual_sec),
                )

            has_latest = conn.execute("SELECT 1 FROM lip_latest LIMIT 1").fetchone()
            if has_latest is None:
                has_snapshots = conn.execute("SELECT 1 FROM lip_snapshots LIMIT 1").fetchone()
                if has_snapshots is not None:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO lip_latest (
                            slug, ts_ms, program_id, trader_bid_score, total_bid_score,
                            trader_ask_score, total_ask_score, qualifies, reward_usd,
                            bid_qualifies, ask_qualifies
                        )
                        SELECT
                            s.slug, s.ts_ms, s.program_id, s.trader_bid_score, s.total_bid_score,
                            s.trader_ask_score, s.total_ask_score, s.qualifies, s.reward_usd,
                            COALESCE(s.bid_qualifies, s.qualifies), COALESCE(s.ask_qualifies, s.qualifies)
                        FROM lip_snapshots s
                        INNER JOIN (
                            SELECT slug, MAX(ts_ms) AS max_ts
                            FROM lip_snapshots
                            GROUP BY slug
                        ) m ON s.slug = m.slug AND s.ts_ms = m.max_ts
                        """
                    )

            conn.execute("CREATE INDEX IF NOT EXISTS idx_lip_snapshots_ts ON lip_snapshots(ts_ms)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lip_snapshots_slug_ts ON lip_snapshots(slug, ts_ms DESC)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS loss_cooldowns (
                    slug TEXT PRIMARY KEY,
                    cooldown_until_ts REAL NOT NULL
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

    def record_fill(self, fill: ShadowFill) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO fills (fill_id, order_id, slug, side, price, qty, fee, is_maker, ts_ms)
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
            return cursor.rowcount == 1

    def record_lip_snapshot(self, snap: LipSnapshot, elapsed_sec: Decimal | float | None = None) -> None:
        if elapsed_sec is not None:
            dt_sec = max(1, min(90, int(round(float(elapsed_sec)))))
        else:
            dt_sec = max(1, int(getattr(snap, "dt_sec", 1)))
        accrued_reward = snap.reward_usd * Decimal(dt_sec)
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO lip_snapshots (
                    ts_ms, slug, program_id, trader_bid_score, total_bid_score,
                    trader_ask_score, total_ask_score, qualifies, reward_usd,
                    bid_qualifies, ask_qualifies, dt_sec
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    str(accrued_reward),
                    1 if snap.bid_qualifies else 0,
                    1 if snap.ask_qualifies else 0,
                    dt_sec,
                ),
            )
            conn.execute(
                """
                INSERT INTO lip_latest (
                    slug, ts_ms, program_id, trader_bid_score, total_bid_score,
                    trader_ask_score, total_ask_score, qualifies, reward_usd,
                    bid_qualifies, ask_qualifies
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    ts_ms = excluded.ts_ms,
                    program_id = excluded.program_id,
                    trader_bid_score = excluded.trader_bid_score,
                    total_bid_score = excluded.total_bid_score,
                    trader_ask_score = excluded.trader_ask_score,
                    total_ask_score = excluded.total_ask_score,
                    qualifies = excluded.qualifies,
                    reward_usd = excluded.reward_usd,
                    bid_qualifies = excluded.bid_qualifies,
                    ask_qualifies = excluded.ask_qualifies
                WHERE excluded.ts_ms >= lip_latest.ts_ms
                """,
                (
                    snap.slug,
                    snap.ts_ms,
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
            cur_row = conn.execute("SELECT total_reward FROM lip_stats_summary WHERE id = 1").fetchone()
            if cur_row is not None:
                new_total = str(Decimal(str(cur_row["total_reward"])) + accrued_reward)
                conn.execute(
                    """
                    UPDATE lip_stats_summary
                    SET total_reward = ?,
                        qual_sec = qual_sec + ?,
                        bid_qual_sec = bid_qual_sec + ?,
                        ask_qual_sec = ask_qual_sec + ?
                    WHERE id = 1
                    """,
                    (
                        new_total,
                        dt_sec if snap.qualifies else 0,
                        dt_sec if snap.bid_qualifies else 0,
                        dt_sec if snap.ask_qualifies else 0,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO lip_stats_summary (id, total_reward, qual_sec, bid_qual_sec, ask_qual_sec)
                    VALUES (1, ?, ?, ?, ?)
                    """,
                    (
                        str(accrued_reward),
                        dt_sec if snap.qualifies else 0,
                        dt_sec if snap.bid_qualifies else 0,
                        dt_sec if snap.ask_qualifies else 0,
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
        has_side_cols = self._has_side_qual
        has_dt_col = self._has_dt_sec
        dt_select = "COALESCE(dt_sec, 1) AS dt_sec" if has_dt_col else "1 AS dt_sec"

        if has_side_cols:
            query = f"""
                SELECT ts_ms, slug, program_id, trader_bid_score, total_bid_score,
                       trader_ask_score, total_ask_score, qualifies, reward_usd,
                       bid_qualifies, ask_qualifies, {dt_select}
                FROM lip_snapshots WHERE ts_ms >= ? ORDER BY ts_ms ASC
            """
        else:
            query = f"""
                SELECT ts_ms, slug, program_id, trader_bid_score, total_bid_score,
                       trader_ask_score, total_ask_score, qualifies, reward_usd,
                       {dt_select}
                FROM lip_snapshots WHERE ts_ms >= ? ORDER BY ts_ms ASC
            """

        with self._get_connection() as conn:
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
                    dt_sec=max(1, int(r["dt_sec"])),
                )
                for r in rows
            ]

    def get_lip_stats(self, since_ms: int = 0) -> dict[str, Any]:
        with self._get_connection() as conn:
            if since_ms <= 0:
                row = conn.execute(
                    "SELECT total_reward, qual_sec, bid_qual_sec, ask_qual_sec FROM lip_stats_summary WHERE id = 1"
                ).fetchone()
                if row:
                    return {
                        "lip_reward_total": Decimal(str(row["total_reward"])),
                        "qualifying_seconds": int(row["qual_sec"]),
                        "qualifying_bid_seconds": int(row["bid_qual_sec"]),
                        "qualifying_ask_seconds": int(row["ask_qual_sec"]),
                    }
                return {
                    "lip_reward_total": Decimal("0"),
                    "qualifying_seconds": 0,
                    "qualifying_bid_seconds": 0,
                    "qualifying_ask_seconds": 0,
                }

            has_side_cols = self._has_side_qual
            has_dt_col = self._has_dt_sec
            dt_expr = "MAX(1, COALESCE(dt_sec, 1))" if has_dt_col else "1"

            if has_side_cols:
                query = f"""
                    SELECT
                        COALESCE(SUM(CAST(reward_usd AS NUMERIC)), 0) AS total_reward,
                        COALESCE(SUM(CASE WHEN qualifies = 1 THEN {dt_expr} ELSE 0 END), 0) AS qual_sec,
                        COALESCE(SUM(CASE WHEN bid_qualifies = 1 THEN {dt_expr} ELSE 0 END), 0) AS bid_qual_sec,
                        COALESCE(SUM(CASE WHEN ask_qualifies = 1 THEN {dt_expr} ELSE 0 END), 0) AS ask_qual_sec
                    FROM lip_snapshots
                    WHERE ts_ms >= ?
                """
            else:
                query = f"""
                    SELECT
                        COALESCE(SUM(CAST(reward_usd AS NUMERIC)), 0) AS total_reward,
                        COALESCE(SUM(CASE WHEN qualifies = 1 THEN {dt_expr} ELSE 0 END), 0) AS qual_sec,
                        COALESCE(SUM(CASE WHEN qualifies = 1 THEN {dt_expr} ELSE 0 END), 0) AS bid_qual_sec,
                        COALESCE(SUM(CASE WHEN qualifies = 1 THEN {dt_expr} ELSE 0 END), 0) AS ask_qual_sec
                    FROM lip_snapshots
                    WHERE ts_ms >= ?
                """

            row = conn.execute(query, (since_ms,)).fetchone()
            if row:
                return {
                    "lip_reward_total": Decimal(str(row["total_reward"])),
                    "qualifying_seconds": int(row["qual_sec"]),
                    "qualifying_bid_seconds": int(row["bid_qual_sec"]),
                    "qualifying_ask_seconds": int(row["ask_qual_sec"]),
                }
            return {
                "lip_reward_total": Decimal("0"),
                "qualifying_seconds": 0,
                "qualifying_bid_seconds": 0,
                "qualifying_ask_seconds": 0,
            }

    get_lip_summary_since = get_lip_stats

    def get_latest_lip_snapshots(self) -> dict[str, LipSnapshot]:
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT ts_ms, slug, program_id, trader_bid_score, total_bid_score,
                       trader_ask_score, total_ask_score, qualifies, reward_usd,
                       bid_qualifies, ask_qualifies
                FROM lip_latest
                """
            ).fetchall()
            if not rows:
                has_snapshots = conn.execute("SELECT 1 FROM lip_snapshots LIMIT 1").fetchone()
                if has_snapshots:
                    has_side_cols = self._has_side_qual

                    if has_side_cols:
                        query = """
                            SELECT s.ts_ms, s.slug, s.program_id, s.trader_bid_score, s.total_bid_score,
                                   s.trader_ask_score, s.total_ask_score, s.qualifies, s.reward_usd,
                                   s.bid_qualifies, s.ask_qualifies
                            FROM lip_snapshots s
                            INNER JOIN (
                                SELECT slug, MAX(ts_ms) AS max_ts
                                FROM lip_snapshots
                                GROUP BY slug
                            ) m ON s.slug = m.slug AND s.ts_ms = m.max_ts
                        """
                    else:
                        query = """
                            SELECT s.ts_ms, s.slug, s.program_id, s.trader_bid_score, s.total_bid_score,
                                   s.trader_ask_score, s.total_ask_score, s.qualifies, s.reward_usd
                            FROM lip_snapshots s
                            INNER JOIN (
                                SELECT slug, MAX(ts_ms) AS max_ts
                                FROM lip_snapshots
                                GROUP BY slug
                            ) m ON s.slug = m.slug AND s.ts_ms = m.max_ts
                        """

                    rows = conn.execute(query).fetchall()
                    result: dict[str, LipSnapshot] = {}
                    for r in rows:
                        result[r["slug"]] = LipSnapshot(
                            ts_ms=r["ts_ms"],
                            slug=r["slug"],
                            program_id=r["program_id"],
                            trader_bid_score=Decimal(str(r["trader_bid_score"])),
                            total_bid_score=Decimal(str(r["total_bid_score"])),
                            trader_ask_score=Decimal(str(r["trader_ask_score"])),
                            total_ask_score=Decimal(str(r["total_ask_score"])),
                            qualifies=bool(r["qualifies"]),
                            reward_usd=Decimal(str(r["reward_usd"])),
                            bid_qualifies=bool(r["bid_qualifies"]) if has_side_cols else bool(r["qualifies"]),
                            ask_qualifies=bool(r["ask_qualifies"]) if has_side_cols else bool(r["qualifies"]),
                        )
                    return result
                return {}

            result: dict[str, LipSnapshot] = {}
            for r in rows:
                result[r["slug"]] = LipSnapshot(
                    ts_ms=r["ts_ms"],
                    slug=r["slug"],
                    program_id=r["program_id"],
                    trader_bid_score=Decimal(str(r["trader_bid_score"])),
                    total_bid_score=Decimal(str(r["total_bid_score"])),
                    trader_ask_score=Decimal(str(r["trader_ask_score"])),
                    total_ask_score=Decimal(str(r["total_ask_score"])),
                    qualifies=bool(r["qualifies"]),
                    reward_usd=Decimal(str(r["reward_usd"])),
                    bid_qualifies=bool(r["bid_qualifies"]),
                    ask_qualifies=bool(r["ask_qualifies"]),
                )
            return result

    def get_realized_pnl_since(self, since_ms: int, include_fees: bool = True) -> Decimal:
        recent_fills = self.get_fills_since(since_ms)
        if not recent_fills:
            return Decimal("0")

        slugs = sorted({f.slug for f in recent_fills})
        placeholders = ",".join("?" for _ in slugs)
        with self._get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT fill_id, order_id, slug, side, price, qty, fee, is_maker, ts_ms
                FROM fills WHERE slug IN ({placeholders}) ORDER BY ts_ms ASC, rowid ASC
                """,
                tuple(slugs),
            ).fetchall()

        market_fills: dict[str, list[ShadowFill]] = {}
        for r in rows:
            fill = ShadowFill(
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
            market_fills.setdefault(fill.slug, []).append(fill)

        total_realized_pnl = Decimal("0")
        for m_fills in market_fills.values():
            pos_qty = Decimal("0")
            avg_px = Decimal("0")
            for f in m_fills:
                in_window = f.ts_ms >= since_ms
                if f.side in ("BUY_LONG", "BUY_SHORT"):
                    if pos_qty >= Decimal("0"):
                        new_qty = pos_qty + f.qty
                        if new_qty > Decimal("0"):
                            avg_px = (pos_qty * avg_px + f.qty * f.price) / new_qty
                        pos_qty = new_qty
                    else:
                        close_qty = min(f.qty, abs(pos_qty))
                        if in_window:
                            total_realized_pnl += close_qty * (avg_px - f.price)
                        pos_qty += f.qty
                        if pos_qty > Decimal("0"):
                            avg_px = f.price
                else:
                    if pos_qty <= Decimal("0"):
                        new_qty = abs(pos_qty) + f.qty
                        if new_qty > Decimal("0"):
                            avg_px = (abs(pos_qty) * avg_px + f.qty * f.price) / new_qty
                        pos_qty = -new_qty
                    else:
                        close_qty = min(f.qty, pos_qty)
                        if in_window:
                            total_realized_pnl += close_qty * (f.price - avg_px)
                        pos_qty -= f.qty
                        if pos_qty < Decimal("0"):
                            avg_px = f.price

                if in_window and include_fees:
                    total_realized_pnl -= f.fee

        return total_realized_pnl

    def get_realized_loss_today(self, now: datetime) -> Decimal:
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(start_of_day.timestamp() * 1000)
        total_realized_pnl = self.get_realized_pnl_since(start_ms, include_fees=True)
        if total_realized_pnl < Decimal("0"):
            return abs(total_realized_pnl)
        return Decimal("0")

    def prune_old_snapshots(
        self,
        retention_days: int = 7,
        now_ms: int | None = None,
        vacuum: bool = False,
    ) -> int:
        base_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        cutoff_ms = base_ms - int(retention_days * 86_400_000)
        with self._get_connection() as conn:
            cur_lip = conn.execute("DELETE FROM lip_snapshots WHERE ts_ms < ?", (cutoff_ms,))
            cur_quotes = conn.execute("DELETE FROM quotes WHERE ts_ms < ?", (cutoff_ms,))
            deleted = max(0, cur_lip.rowcount) + max(0, cur_quotes.rowcount)
            if vacuum:
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
            return deleted

    def record_loss_cooldown(self, slug: str, cooldown_until_ts: float) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO loss_cooldowns (slug, cooldown_until_ts) VALUES (?, ?)",
                (slug, float(cooldown_until_ts)),
            )

    def get_active_loss_cooldowns(self, now_ts: float) -> dict[str, float]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT slug, cooldown_until_ts FROM loss_cooldowns WHERE cooldown_until_ts > ?",
                (float(now_ts),),
            ).fetchall()
            return {row["slug"]: float(row["cooldown_until_ts"]) for row in rows}

    def has_fill_since(self, slug: str, since_ms: int) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM fills WHERE slug = ? AND ts_ms >= ? LIMIT 1",
                (slug, since_ms),
            ).fetchone()
            return row is not None
