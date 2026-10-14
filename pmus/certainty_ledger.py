"""SQLite ledger for tracking satellite paper and live trades."""

from datetime import datetime, timezone
import math
from pathlib import Path
import sqlite3
from typing import Any

DEFAULT_LEDGER_PATH = Path(__file__).resolve().parent / "data" / "certainty_ledger.db"


class CertaintyLedger:
    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            self.db_path = DEFAULT_LEDGER_PATH
        else:
            self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS satellite_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_mode TEXT NOT NULL,
                    strategy_type TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    market_slug TEXT NOT NULL,
                    question TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    size_usd REAL NOT NULL,
                    shares REAL NOT NULL,
                    entry_timestamp TEXT NOT NULL,
                    expected_resolution TEXT,
                    status TEXT NOT NULL,
                    exit_price REAL,
                    exit_timestamp TEXT,
                    pnl_usd REAL,
                    notes TEXT DEFAULT ''
                )
                """
            )

    def record_entry(
        self,
        trade_mode: str,
        strategy_type: str,
        condition_id: str,
        token_id: str,
        market_slug: str,
        question: str,
        outcome: str,
        side: str,
        entry_price: float,
        size_usd: float,
        shares: float,
        entry_timestamp: str | None = None,
        expected_resolution: str | None = None,
        status: str = "FILLED",
        notes: str = "",
    ) -> int:
        ts = entry_timestamp or datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO satellite_trades (
                    trade_mode, strategy_type, condition_id, token_id,
                    market_slug, question, outcome, side, entry_price,
                    size_usd, shares, entry_timestamp, expected_resolution,
                    status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_mode,
                    strategy_type,
                    condition_id,
                    token_id,
                    market_slug,
                    question,
                    outcome,
                    side,
                    float(entry_price),
                    float(size_usd),
                    float(shares),
                    ts,
                    expected_resolution,
                    status,
                    notes,
                ),
            )
            return cursor.lastrowid

    def get_trade(self, trade_id: int) -> dict[str, Any] | None:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM satellite_trades WHERE id = ?", (trade_id,)
            ).fetchone()
            return dict(row) if row else None

    def record_resolution(
        self,
        trade_id: int,
        outcome_won: bool,
        payout_per_share: float = 1.0,
        notes: str = "",
    ) -> None:
        trade = self.get_trade(trade_id)
        if not trade:
            raise ValueError(f"Trade {trade_id} not found")

        shares = float(trade["shares"])
        entry_price = float(trade["entry_price"])
        size_usd = float(trade["size_usd"])
        exit_ts = datetime.now(timezone.utc).isoformat()

        if outcome_won:
            status = "RESOLVED_WIN"
            exit_price = float(payout_per_share)
            pnl_usd = (exit_price - entry_price) * shares
        else:
            status = "RESOLVED_LOSS"
            exit_price = 0.0
            pnl_usd = -size_usd

        existing_notes = trade.get("notes") or ""
        new_notes = f"{existing_notes} | {notes}".strip(" |") if notes else existing_notes

        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE satellite_trades
                SET status = ?, exit_price = ?, exit_timestamp = ?, pnl_usd = ?, notes = ?
                WHERE id = ?
                """,
                (status, exit_price, exit_ts, pnl_usd, new_notes, trade_id),
            )

    def record_clob_exit(
        self,
        trade_id: int,
        exit_price: float,
        notes: str = "",
    ) -> None:
        trade = self.get_trade(trade_id)
        if not trade:
            raise ValueError(f"Trade {trade_id} not found")

        shares = float(trade["shares"])
        entry_price = float(trade["entry_price"])
        exit_ts = datetime.now(timezone.utc).isoformat()
        pnl_usd = (float(exit_price) - entry_price) * shares
        status = "EXITED_CLOB"

        existing_notes = trade.get("notes") or ""
        new_notes = f"{existing_notes} | {notes}".strip(" |") if notes else existing_notes

        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE satellite_trades
                SET status = ?, exit_price = ?, exit_timestamp = ?, pnl_usd = ?, notes = ?
                WHERE id = ?
                """,
                (status, float(exit_price), exit_ts, pnl_usd, new_notes, trade_id),
            )

    def get_open_trades(self) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM satellite_trades
                WHERE status IN ('PENDING', 'FILLED')
                ORDER BY id ASC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_trades(self) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM satellite_trades ORDER BY id ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_last_loss_timestamp(self) -> datetime | None:
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT exit_timestamp FROM satellite_trades
                WHERE status = 'RESOLVED_LOSS' OR (status = 'EXITED_CLOB' AND pnl_usd < 0)
                ORDER BY exit_timestamp DESC LIMIT 1
                """
            ).fetchone()
            if row and row["exit_timestamp"]:
                try:
                    ts_str = row["exit_timestamp"]
                    if ts_str.endswith("Z"):
                        ts_str = ts_str[:-1] + "+00:00"
                    return datetime.fromisoformat(ts_str)
                except Exception:
                    return None
            return None

    def is_circuit_breaker_active(self, cooldown_hours: float = 0.0) -> tuple[bool, float]:
        if cooldown_hours <= 0.0:
            return (False, 0.0)
        last_loss = self.get_last_loss_timestamp()
        if not last_loss:
            return (False, 0.0)
        now = datetime.now(timezone.utc)
        if last_loss.tzinfo is None:
            last_loss = last_loss.replace(tzinfo=timezone.utc)
        elapsed_hours = (now - last_loss).total_seconds() / 3600.0
        if elapsed_hours < cooldown_hours:
            return (True, cooldown_hours - elapsed_hours)
        return (False, 0.0)

    def get_performance_summary(self) -> dict[str, Any]:
        all_trades = self.get_all_trades()
        total_trades = len(all_trades)
        open_trades = [t for t in all_trades if t["status"] in ("PENDING", "FILLED")]
        closed_trades = [t for t in all_trades if t["status"] in ("RESOLVED_WIN", "RESOLVED_LOSS", "EXITED_CLOB")]

        wins = 0
        losses = 0
        total_pnl = 0.0
        returns: list[float] = []

        for t in closed_trades:
            pnl = float(t.get("pnl_usd") or 0.0)
            size = float(t.get("size_usd") or 1.0)
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            elif t["status"] == "RESOLVED_WIN":
                wins += 1
            elif t["status"] == "RESOLVED_LOSS":
                losses += 1

            ret = pnl / size if size > 0 else 0.0
            returns.append(ret)

        decided_trades = wins + losses
        win_rate = (wins / decided_trades) if decided_trades > 0 else 0.0
        avg_return = (sum(returns) / len(returns)) if returns else 0.0

        if len(returns) >= 2:
            mean = avg_return
            var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
            std = math.sqrt(var)
            sharpe_ratio = (mean / std) if std > 0 else 0.0
        else:
            sharpe_ratio = 0.0

        return {
            "total_trades": total_trades,
            "open_trades": len(open_trades),
            "closed_trades": len(closed_trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 4),
            "avg_return": round(avg_return, 4),
            "sharpe_ratio": round(sharpe_ratio, 4),
        }
