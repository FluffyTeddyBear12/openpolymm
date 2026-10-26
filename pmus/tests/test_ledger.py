from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from unittest.mock import patch
import pytest

from pmus.ledger import Ledger
from pmus.models import LipSnapshot, LiveOrder, Position, Quote, ShadowFill


@pytest.fixture
def ledger(tmp_path):
    inst = Ledger(tmp_path / "test_ledger.db")
    yield inst
    inst.close()

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


def test_get_realized_loss_today_tracks_overnight_cost_basis_without_double_counting_prior_day(ledger):
    now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    today_00 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ts_yest_open = int((today_00 - timedelta(hours=8)).timestamp() * 1000)
    ts_yest_closed_buy = int((today_00 - timedelta(hours=7)).timestamp() * 1000)
    ts_yest_closed_sell = int((today_00 - timedelta(hours=6)).timestamp() * 1000)
    ts_today_sell = int((today_00 + timedelta(minutes=30)).timestamp() * 1000)

    ledger.record_fill(ShadowFill("f-prev-b", "o-pb", "closed-yest", "BUY_LONG", Decimal("0.50"), Decimal("100"), Decimal("0.50"), True, ts_yest_closed_buy))
    ledger.record_fill(ShadowFill("f-prev-s", "o-ps", "closed-yest", "SELL_LONG", Decimal("0.30"), Decimal("100"), Decimal("0.50"), True, ts_yest_closed_sell))

    ledger.record_fill(ShadowFill("f-bag-b", "o-bb", "bag-slug", "BUY_LONG", Decimal("0.51"), Decimal("65"), Decimal("1.25"), True, ts_yest_open))
    ledger.record_fill(ShadowFill("f-bag-s", "o-bs", "bag-slug", "SELL_LONG", Decimal("0.25"), Decimal("65"), Decimal("0"), True, ts_today_sell))

    assert ledger.get_realized_loss_today(now) == Decimal("16.90")


def test_record_lip_snapshot_scales_by_elapsed_dt_and_caps_at_90s(ledger):
    snap1 = LipSnapshot(
        ts_ms=1_000_000,
        slug="mkt-dt",
        program_id="p-dt",
        trader_bid_score=Decimal("50"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("0"),
        total_ask_score=Decimal("100"),
        qualifies=True,
        reward_usd=Decimal("0.01"),
        bid_qualifies=True,
    )
    snap2 = LipSnapshot(
        ts_ms=1_010_000,
        slug="mkt-dt",
        program_id="p-dt",
        trader_bid_score=Decimal("50"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("0"),
        total_ask_score=Decimal("100"),
        qualifies=True,
        reward_usd=Decimal("0.01"),
        bid_qualifies=True,
    )
    snap3 = LipSnapshot(
        ts_ms=1_130_000,
        slug="mkt-dt",
        program_id="p-dt",
        trader_bid_score=Decimal("50"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("0"),
        total_ask_score=Decimal("100"),
        qualifies=True,
        reward_usd=Decimal("0.01"),
        bid_qualifies=True,
    )
    ledger.record_lip_snapshot(snap1)
    ledger.record_lip_snapshot(snap2, elapsed_sec=Decimal("10"))
    ledger.record_lip_snapshot(snap3, elapsed_sec=Decimal("120"))

    stats = ledger.get_lip_stats(0)
    assert stats["lip_reward_total"] == Decimal("1.01")
    assert stats["qualifying_seconds"] == 101
    assert ledger.get_latest_lip_snapshots()["mkt-dt"].reward_usd == Decimal("0.01")


def test_record_lip_snapshot_does_not_extrapolate_wall_clock_gaps(ledger):
    s1 = LipSnapshot(
        ts_ms=1_000_000,
        slug="mkt-gap",
        program_id="p-gap",
        trader_bid_score=Decimal("50"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("0"),
        total_ask_score=Decimal("100"),
        qualifies=True,
        reward_usd=Decimal("0.01"),
        bid_qualifies=True,
    )
    s2 = LipSnapshot(
        ts_ms=1_060_000,
        slug="mkt-gap",
        program_id="p-gap",
        trader_bid_score=Decimal("50"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("0"),
        total_ask_score=Decimal("100"),
        qualifies=True,
        reward_usd=Decimal("0.01"),
        bid_qualifies=True,
    )
    ledger.record_lip_snapshot(s1)
    ledger.record_lip_snapshot(s2)

    stats = ledger.get_lip_stats(0)
    assert stats["qualifying_seconds"] == 2
    assert stats["lip_reward_total"] == Decimal("0.02")


def test_get_realized_pnl_since_respects_include_fees_flag(tmp_path):
    ledger = Ledger(tmp_path / "pnl_fees.db")
    try:
        ledger.record_fill(
            ShadowFill("f1", "o1", "m-fee", "BUY_LONG", Decimal("0.40"), Decimal("50"), Decimal("0.50"), False, 1000)
        )
        ledger.record_fill(
            ShadowFill("f2", "o2", "m-fee", "SELL_LONG", Decimal("0.50"), Decimal("50"), Decimal("0.25"), False, 2000)
        )
        assert ledger.get_realized_pnl_since(1500, include_fees=False) == Decimal("5.00")
        assert ledger.get_realized_pnl_since(1500, include_fees=True) == Decimal("4.75")
    finally:
        ledger.close()


def test_get_lip_stats_since_ms_matches_materialized_summary_with_persisted_dt_sec(ledger):
    s1 = LipSnapshot(
        ts_ms=10_000,
        slug="mkt-sync",
        program_id="p-sync",
        trader_bid_score=Decimal("40"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("0"),
        total_ask_score=Decimal("100"),
        qualifies=True,
        reward_usd=Decimal("0.02"),
        bid_qualifies=True,
        ask_qualifies=False,
    )
    s2 = LipSnapshot(
        ts_ms=25_000,
        slug="mkt-sync",
        program_id="p-sync",
        trader_bid_score=Decimal("40"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("30"),
        total_ask_score=Decimal("100"),
        qualifies=True,
        reward_usd=Decimal("0.03"),
        bid_qualifies=True,
        ask_qualifies=True,
        dt_sec=15,
    )
    s3 = LipSnapshot(
        ts_ms=45_000,
        slug="mkt-sync",
        program_id="p-sync",
        trader_bid_score=Decimal("40"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("30"),
        total_ask_score=Decimal("100"),
        qualifies=True,
        reward_usd=Decimal("0.01"),
        bid_qualifies=True,
        ask_qualifies=True,
    )
    ledger.record_lip_snapshot(s1)
    ledger.record_lip_snapshot(s2)
    ledger.record_lip_snapshot(s3, elapsed_sec=Decimal("20"))

    persisted = ledger.get_lip_snapshots_since(1)
    assert [snap.dt_sec for snap in persisted] == [1, 15, 20]

    stats_all = ledger.get_lip_stats(0)
    stats_since = ledger.get_lip_stats(1)
    assert stats_all["qualifying_seconds"] == stats_since["qualifying_seconds"] == 36
    assert stats_all["qualifying_bid_seconds"] == stats_since["qualifying_bid_seconds"] == 36
    assert stats_all["qualifying_ask_seconds"] == stats_since["qualifying_ask_seconds"] == 35
    assert stats_all["lip_reward_total"] == stats_since["lip_reward_total"] == Decimal("0.67")


def test_ledger_reuses_thread_local_connection_and_skips_pragma_table_info_on_queries(tmp_path):
    real_connect = sqlite3.connect
    with patch("pmus.ledger.sqlite3.connect", wraps=real_connect) as spy_connect:
        ledger = Ledger(tmp_path / "reuse.db")
        try:
            executed_sql: list[str] = []
            with ledger._get_connection() as conn:
                conn.set_trace_callback(executed_sql.append)

            for i in range(5):
                ts = 1000 + i * 100
                ledger.record_quote(Quote(slug="mkt-r", side="BUY_LONG", price=Decimal("0.45"), qty=Decimal("10")), ts)
                ledger.record_lip_snapshot(
                    LipSnapshot(
                        ts_ms=ts,
                        slug="mkt-r",
                        program_id="p-r",
                        trader_bid_score=Decimal("10"),
                        total_bid_score=Decimal("100"),
                        trader_ask_score=Decimal("10"),
                        total_ask_score=Decimal("100"),
                        qualifies=True,
                        reward_usd=Decimal("0.001"),
                        bid_qualifies=True,
                        ask_qualifies=True,
                    ),
                    elapsed_sec=Decimal("2"),
                )
                ledger.get_lip_stats(500)
                ledger.get_lip_snapshots_since(500)

            assert spy_connect.call_count <= 3
            assert not any("PRAGMA table_info" in sql for sql in executed_sql)
        finally:
            ledger.close()


def test_ledger_close_releases_tempdir_and_rolls_back_on_error():
    with tempfile.TemporaryDirectory() as tmp_dir:
        ledger = Ledger(Path(tmp_dir) / "temp_win.db")
        ledger.record_quote(Quote(slug="mkt-c", side="BUY_LONG", price=Decimal("0.50"), qty=Decimal("5")), 1000)

        with pytest.raises(RuntimeError):
            with ledger._get_connection() as conn:
                conn.execute(
                    "INSERT INTO quotes (ts_ms, slug, side, price, qty) VALUES (?, ?, ?, ?, ?)",
                    (2000, "mkt-c", "BUY_LONG", "0.51", "5"),
                )
                raise RuntimeError("boom")

        with ledger._get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
            assert count == 1

        worker_done = threading.Event()

        def worker():
            assert ledger.get_lip_stats(0)["qualifying_seconds"] == 0
            ledger.close()
            worker_done.set()

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2.0)
        assert worker_done.is_set()
        ledger.close()
        ledger.close()


def test_prune_old_snapshots_deletes_expired_rows_and_vacuums(ledger):
    day_ms = 86_400_000
    now_ms = 20 * day_ms
    old_ts = now_ms - 8 * day_ms
    boundary_ts = now_ms - 7 * day_ms
    recent_ts = now_ms - 1 * day_ms

    for ts in (old_ts, boundary_ts, recent_ts):
        ledger.record_quote(Quote(slug="mkt-p", side="BUY_LONG", price=Decimal("0.40"), qty=Decimal("10")), ts)
        ledger.record_lip_snapshot(
            LipSnapshot(
                ts_ms=ts,
                slug="mkt-p",
                program_id="p-p",
                trader_bid_score=Decimal("10"),
                total_bid_score=Decimal("100"),
                trader_ask_score=Decimal("0"),
                total_ask_score=Decimal("100"),
                qualifies=True,
                reward_usd=Decimal("0.01"),
                bid_qualifies=True,
            )
        )

    ledger.record_fill(
        ShadowFill("f-old", "o-old", "mkt-p", "BUY_LONG", Decimal("0.40"), Decimal("10"), Decimal("0"), True, old_ts)
    )

    deleted = ledger.prune_old_snapshots(retention_days=7, now_ms=now_ms, vacuum=True)
    assert deleted == 2

    remaining_snaps = ledger.get_lip_snapshots_since(0)
    assert [s.ts_ms for s in remaining_snaps] == [boundary_ts, recent_ts]

    with ledger._get_connection() as conn:
        quote_ts = [r[0] for r in conn.execute("SELECT ts_ms FROM quotes ORDER BY ts_ms ASC").fetchall()]
    assert quote_ts == [boundary_ts, recent_ts]
    assert len(ledger.get_fills_since(0)) == 1
    assert ledger.get_lip_stats(0)["lip_reward_total"] == Decimal("0.03")


def test_loss_cooldowns_persistence_and_expiry(ledger):
    t0 = 1000.0
    ledger.record_loss_cooldown("slug-1", t0 + 100.0)
    ledger.record_loss_cooldown("slug-2", t0 + 50.0)

    active = ledger.get_active_loss_cooldowns(t0 + 10.0)
    assert active == {"slug-1": 1100.0, "slug-2": 1050.0}

    active_later = ledger.get_active_loss_cooldowns(t0 + 60.0)
    assert active_later == {"slug-1": 1100.0}


def test_has_fill_since(ledger):
    ledger.record_fill(
        ShadowFill("f-1", "o-1", "tok-a", "BUY_LONG", Decimal("0.50"), Decimal("10"), Decimal("0"), True, 10000)
    )
    assert ledger.has_fill_since("tok-a", 5000) is True
    assert ledger.has_fill_since("tok-a", 15000) is False
    assert ledger.has_fill_since("tok-other", 0) is False


def test_ledger_auto_recovers_from_corrupt_database(tmp_path):
    db_file = tmp_path / "corrupt_ledger.db"
    db_file.write_bytes(b"NOT A REAL SQLITE DB - TOTAL CORRUPTION")

    recovered_ledger = Ledger(db_file)
    assert db_file.exists()
    assert recovered_ledger.get_lip_stats(0)["qualifying_seconds"] == 0
    corrupt_backups = list(tmp_path.glob("corrupt_ledger.db.corrupt_*"))
    assert len(corrupt_backups) == 1

