import sqlite3
from datetime import datetime, timezone
import time
from decimal import Decimal
import pytest

from pmus.ledger import Ledger
from pmus.models import LipSnapshot, LiveOrder, Position, Quote, ShadowFill

@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "test_ledger.db")

def test_ledger_orders_lifecycle(ledger):
    order = LiveOrder(
        order_id="ord-1",
        slug="test-slug",
        side="BUY_LONG",
        price=Decimal("0.450"),
        qty_open=Decimal("10"),
    )
    ledger.record_order(order, status="OPEN")
    open_orders = ledger.get_open_orders("test-slug")
    assert len(open_orders) == 1
    assert open_orders[0].order_id == "ord-1"

    ledger.close_order("ord-1", status="CANCELED")
    assert len(ledger.get_open_orders("test-slug")) == 0

def test_ledger_fills_and_positions(ledger):
    fill = ShadowFill(
        fill_id="f-1",
        order_id="ord-1",
        slug="test-slug",
        side="BUY_LONG",
        price=Decimal("0.450"),
        qty=Decimal("10"),
        fee=Decimal("-0.03"),
        is_maker=True,
        ts_ms=1000,
    )
    ledger.record_fill(fill)
    fills = ledger.get_fills_since(0)
    assert len(fills) == 1
    assert fills[0].fill_id == "f-1"

    pos = Position(slug="test-slug", net_qty=Decimal("10"), avg_price=Decimal("0.450"))
    ledger.update_position(pos)
    retrieved = ledger.get_position("test-slug")
    assert retrieved.net_qty == Decimal("10")
    assert retrieved.avg_price == Decimal("0.450")

def test_ledger_lip_snapshots(ledger):
    snap = LipSnapshot(
        ts_ms=1000,
        slug="test-slug",
        program_id="prog-1",
        trader_bid_score=Decimal("10"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("5"),
        total_ask_score=Decimal("50"),
        qualifies=True,
        reward_usd=Decimal("0.005"),
    )
    ledger.record_lip_snapshot(snap)
    snaps = ledger.get_lip_snapshots_since(0)
    assert len(snaps) == 1
    assert snaps[0].qualifies is True
    assert snaps[0].reward_usd == Decimal("0.005")

def test_ledger_realized_loss_today(ledger):
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    # Buy 10 @ 0.50, Sell 10 @ 0.40 -> loss of $1.00
    fill_buy = ShadowFill("f-b", "o-b", "mkt-1", "BUY_LONG", Decimal("0.50"), Decimal("10"), Decimal("0"), True, now_ms)
    fill_sell = ShadowFill("f-s", "o-s", "mkt-1", "SELL_LONG", Decimal("0.40"), Decimal("10"), Decimal("0"), True, now_ms + 100)
    ledger.record_fill(fill_buy)
    ledger.record_fill(fill_sell)

    loss = ledger.get_realized_loss_today(now)
    assert loss == Decimal("1.00")


def test_ledger_lip_stats_and_latest(ledger):
    snap1 = LipSnapshot(
        ts_ms=1000,
        slug="mkt-1",
        program_id="p1",
        trader_bid_score=Decimal("10"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("5"),
        total_ask_score=Decimal("100"),
        qualifies=True,
        reward_usd=Decimal("0.0025"),
        bid_qualifies=True,
        ask_qualifies=False,
    )
    snap2 = LipSnapshot(
        ts_ms=2000,
        slug="mkt-1",
        program_id="p1",
        trader_bid_score=Decimal("15"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("10"),
        total_ask_score=Decimal("100"),
        qualifies=True,
        reward_usd=Decimal("0.0035"),
        bid_qualifies=True,
        ask_qualifies=True,
    )
    snap3 = LipSnapshot(
        ts_ms=1500,
        slug="mkt-2",
        program_id="p2",
        trader_bid_score=Decimal("0"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("0"),
        total_ask_score=Decimal("100"),
        qualifies=False,
        reward_usd=Decimal("0"),
        bid_qualifies=False,
        ask_qualifies=False,
    )
    ledger.record_lip_snapshot(snap1)
    ledger.record_lip_snapshot(snap2)
    ledger.record_lip_snapshot(snap3)

    stats = ledger.get_lip_stats(0)
    assert stats["qualifying_seconds"] == 2
    assert stats["qualifying_bid_seconds"] == 2
    assert stats["qualifying_ask_seconds"] == 1
    assert stats["lip_reward_total"] == Decimal("0.0060")

    stats_recent = ledger.get_lip_stats(2000)
    assert stats_recent["qualifying_seconds"] == 1
    assert stats_recent["lip_reward_total"] == Decimal("0.0035")

    latest = ledger.get_latest_lip_snapshots()
    assert len(latest) == 2
    assert latest["mkt-1"].ts_ms == 2000
    assert latest["mkt-1"].reward_usd == Decimal("0.0035")
    assert latest["mkt-2"].ts_ms == 1500


def test_ledger_seeds_materialized_lip_tables_from_existing_db(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE lip_snapshots (
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
    conn.execute(
        """
        INSERT INTO lip_snapshots (
            ts_ms, slug, program_id, trader_bid_score, total_bid_score,
            trader_ask_score, total_ask_score, qualifies, reward_usd,
            bid_qualifies, ask_qualifies
        ) VALUES
        (1000, 'mkt-1', 'p1', '10', '100', '5', '50', 1, '0.0050', 1, 0),
        (2000, 'mkt-1', 'p1', '20', '100', '10', '50', 1, '0.0075', 1, 1),
        (1500, 'mkt-2', 'p2', '0', '100', '0', '50', 0, '0.0000', 0, 0)
        """
    )
    conn.commit()
    conn.close()

    ledger = Ledger(db_path)

    stats = ledger.get_lip_stats(0)
    assert stats["qualifying_seconds"] == 2
    assert stats["qualifying_bid_seconds"] == 2
    assert stats["qualifying_ask_seconds"] == 1
    assert stats["lip_reward_total"] == Decimal("0.0125")

    latest = ledger.get_latest_lip_snapshots()
    assert len(latest) == 2
    assert latest["mkt-1"].ts_ms == 2000
    assert latest["mkt-1"].reward_usd == Decimal("0.0075")
    assert latest["mkt-1"].qualifies is True
    assert latest["mkt-1"].bid_qualifies is True
    assert latest["mkt-1"].ask_qualifies is True
    assert latest["mkt-2"].ts_ms == 1500
    assert latest["mkt-2"].reward_usd == Decimal("0.0000")

    new_snap = LipSnapshot(
        ts_ms=3000,
        slug="mkt-1",
        program_id="p1",
        trader_bid_score=Decimal("30"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("15"),
        total_ask_score=Decimal("50"),
        qualifies=True,
        reward_usd=Decimal("0.0100"),
        bid_qualifies=True,
        ask_qualifies=True,
    )
    ledger.record_lip_snapshot(new_snap)

    stats2 = ledger.get_lip_stats(0)
    assert stats2["qualifying_seconds"] == 3
    assert stats2["qualifying_bid_seconds"] == 3
    assert stats2["qualifying_ask_seconds"] == 2
    assert stats2["lip_reward_total"] == Decimal("0.0225")

    latest2 = ledger.get_latest_lip_snapshots()
    assert latest2["mkt-1"].ts_ms == 3000
    assert latest2["mkt-1"].reward_usd == Decimal("0.0100")


def test_ledger_pragmas_and_indexes(ledger):
    with ledger._get_connection() as conn:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        assert timeout == 5000
        assert sync == 1

        indexes = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()]
        assert "idx_orders_status" in indexes
        assert "idx_fills_ts" in indexes



