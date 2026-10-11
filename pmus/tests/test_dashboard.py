from decimal import Decimal
import time
from unittest.mock import MagicMock, patch

from pmus.config import Settings
from pmus.dashboard import (
    _get_official_daily_earnings,
    compute_kpis,
    format_fills_table,
    format_market_overview,
    get_active_positions,
    get_profile_links,
    is_database_empty,
    resolve_market_title,
    sync_ledger_from_vps,
)
from pmus.ledger import Ledger
from pmus.models import LipSnapshot, LiveOrder, Position, Quote, ShadowFill


def test_is_database_empty_missing_file(tmp_path):
    missing_db = tmp_path / "nonexistent.db"
    ledger = Ledger.__new__(Ledger)
    ledger.db_path = missing_db
    assert is_database_empty(ledger) is True


def test_is_database_empty_fresh_db(tmp_path):
    ledger = Ledger(tmp_path / "fresh.db")
    assert is_database_empty(ledger) is True


def test_is_database_empty_with_quote(tmp_path):
    ledger = Ledger(tmp_path / "data.db")
    assert is_database_empty(ledger) is True
    ledger.record_quote(Quote(slug="mkt-1", side="BUY_LONG", price=Decimal("0.50"), qty=Decimal("10")), int(time.time() * 1000))
    assert is_database_empty(ledger) is False


def test_compute_kpis_empty(tmp_path):
    ledger = Ledger(tmp_path / "empty_kpi.db")
    kpis = compute_kpis(ledger, since_ms=0, buying_power=Decimal("100.50"), max_collateral=Decimal("70.00"))
    assert kpis["balance_usd"] == Decimal("100.50")
    assert kpis["collateral_cap_usd"] == Decimal("70.00")
    assert kpis["total_fills"] == 0
    assert kpis["total_volume"] == Decimal("0")
    assert kpis["qualifying_seconds"] == 0
    assert kpis["lip_reward_total"] == Decimal("0")
    assert kpis["realized_pnl"] == Decimal("0")
    assert kpis["maker_rebates"] == Decimal("0")
    assert kpis["taker_fees"] == Decimal("0")
    assert kpis["net_pnl"] == Decimal("0")


def test_compute_kpis_with_fills_and_lip(tmp_path):
    ledger = Ledger(tmp_path / "active.db")
    now_ms = 1_000_000

    ledger.record_fill(
        ShadowFill(
            fill_id="f1",
            order_id="o1",
            slug="mkt-1",
            side="BUY_LONG",
            price=Decimal("0.40"),
            qty=Decimal("100"),
            fee=Decimal("-0.05"),
            is_maker=True,
            ts_ms=now_ms,
        )
    )
    ledger.record_fill(
        ShadowFill(
            fill_id="f2",
            order_id="o2",
            slug="mkt-1",
            side="SELL_LONG",
            price=Decimal("0.60"),
            qty=Decimal("100"),
            fee=Decimal("0.02"),
            is_maker=False,
            ts_ms=now_ms + 1000,
        )
    )

    ledger.record_lip_snapshot(
        LipSnapshot(
            ts_ms=now_ms,
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
    )
    ledger.record_lip_snapshot(
        LipSnapshot(
            ts_ms=now_ms + 1000,
            slug="mkt-1",
            program_id="p1",
            trader_bid_score=Decimal("10"),
            total_bid_score=Decimal("100"),
            trader_ask_score=Decimal("10"),
            total_ask_score=Decimal("100"),
            qualifies=True,
            reward_usd=Decimal("0.0035"),
            bid_qualifies=True,
            ask_qualifies=True,
        )
    )

    kpis = compute_kpis(ledger, since_ms=0)
    assert kpis["total_fills"] == 2
    assert kpis["total_volume"] == Decimal("40.00") + Decimal("60.00")
    assert kpis["maker_rebates"] == Decimal("0.05")
    assert kpis["taker_fees"] == Decimal("0.02")
    assert kpis["realized_pnl"] == Decimal("20.00")
    assert kpis["qualifying_seconds"] == 2
    assert kpis["qualifying_bid_seconds"] == 2
    assert kpis["qualifying_ask_seconds"] == 1
    assert kpis["lip_reward_total"] == Decimal("0.0060")
    expected_net = Decimal("20.00") + Decimal("0.05") + Decimal("0.0060") - Decimal("0.02")
    assert kpis["net_pnl"] == expected_net


def test_format_market_overview(tmp_path):
    ledger = Ledger(tmp_path / "overview.db")
    now_ms = 1_000_000

    ledger.record_order(LiveOrder(order_id="o1", slug="mkt-a", side="BUY_LONG", price=Decimal("0.35"), qty_open=Decimal("50")))
    ledger.record_order(LiveOrder(order_id="o2", slug="mkt-a", side="BUY_LONG", price=Decimal("0.37"), qty_open=Decimal("30")))
    ledger.record_order(LiveOrder(order_id="o3", slug="mkt-a", side="SELL_LONG", price=Decimal("0.65"), qty_open=Decimal("40")))

    ledger.record_lip_snapshot(
        LipSnapshot(
            ts_ms=now_ms,
            slug="mkt-a",
            program_id="prog-1",
            trader_bid_score=Decimal("15"),
            total_bid_score=Decimal("500"),
            trader_ask_score=Decimal("10"),
            total_ask_score=Decimal("500"),
            qualifies=True,
            reward_usd=Decimal("0.001"),
            bid_qualifies=True,
            ask_qualifies=True,
        )
    )

    ledger.record_lip_snapshot(
        LipSnapshot(
            ts_ms=now_ms,
            slug="mkt-b",
            program_id="prog-2",
            trader_bid_score=Decimal("0"),
            total_bid_score=Decimal("200"),
            trader_ask_score=Decimal("0"),
            total_ask_score=Decimal("200"),
            qualifies=False,
            reward_usd=Decimal("0"),
            bid_qualifies=False,
            ask_qualifies=False,
        )
    )

    rows = format_market_overview(ledger, recent_ms=0)
    assert len(rows) == 2

    row_a = next(r for r in rows if r["slug"] == "mkt-a")
    assert row_a["status"] == "QUOTING"
    assert row_a["bid_count"] == 2
    assert row_a["best_bid"] == Decimal("0.37")
    assert row_a["bid_qty"] == Decimal("80")
    assert row_a["ask_count"] == 1
    assert row_a["best_ask"] == Decimal("0.65")
    assert row_a["ask_qty"] == Decimal("40")
    assert row_a["qualifies"] is True
    assert row_a["bid_qualifies"] is True
    assert row_a["ask_qualifies"] is True
    assert row_a["target_pool_size"] == Decimal("500")

    row_b = next(r for r in rows if r["slug"] == "mkt-b")
    assert row_b["status"] == "MONITORING"
    assert row_b["bid_count"] == 0
    assert row_b["best_bid"] is None
    assert row_b["ask_count"] == 0
    assert row_b["qualifies"] is False
    assert row_b["target_pool_size"] == Decimal("200")


def test_format_fills_table():
    fills = [
        ShadowFill(
            fill_id="f1",
            order_id="o1",
            slug="slug-old",
            side="BUY_LONG",
            price=Decimal("0.1234"),
            qty=Decimal("500"),
            fee=Decimal("-0.0050"),
            is_maker=True,
            ts_ms=100000,
        ),
        ShadowFill(
            fill_id="f2",
            order_id="o2",
            slug="slug-new",
            side="SELL_LONG",
            price=Decimal("0.8765"),
            qty=Decimal("250"),
            fee=Decimal("0.0025"),
            is_maker=False,
            ts_ms=200000,
        ),
    ]

    table = format_fills_table(fills)
    assert len(table) == 2
    assert table[0]["Order ID"] == "o2"
    assert table[0]["Slug"] == "slug-new"
    assert table[0]["Price"] == "$0.8765"
    assert table[0]["Fee"] == "-$0.0025"
    assert "UTC" in table[0]["Timestamp"]

    assert table[1]["Order ID"] == "o1"
    assert table[1]["Fee"] == "+$0.0050"


def test_get_profile_links():
    links = get_profile_links("0xabcdef1234567890")
    assert links["polymarket"] == "https://polymarket.com/profile/0xabcdef1234567890"
    assert links["polygonscan"] == "https://polygonscan.com/address/0xabcdef1234567890"

    empty_links = get_profile_links("")
    assert empty_links["polymarket"] == "https://polymarket.com"
    assert empty_links["polygonscan"] == "https://polygonscan.com"


def test_get_active_positions_reconstructs_from_fills(tmp_path):
    ledger = Ledger(tmp_path / "recon.db")
    now_ms = 1_000_000

    ledger.record_fill(
        ShadowFill(
            fill_id="f1",
            order_id="o1",
            slug="slug-a",
            side="BUY_LONG",
            price=Decimal("0.38"),
            qty=Decimal("100"),
            fee=Decimal("0"),
            is_maker=True,
            ts_ms=now_ms,
        )
    )
    ledger.record_fill(
        ShadowFill(
            fill_id="f2",
            order_id="o2",
            slug="slug-a",
            side="BUY_LONG",
            price=Decimal("0.40"),
            qty=Decimal("105"),
            fee=Decimal("0"),
            is_maker=True,
            ts_ms=now_ms + 1000,
        )
    )

    positions = get_active_positions(ledger)
    assert len(positions) == 1
    p = positions[0]
    assert p.slug == "slug-a"
    assert p.net_qty == Decimal("205")
    expected_avg = Decimal("80") / Decimal("205")
    assert p.avg_price == expected_avg


def test_get_active_positions_from_positions_table(tmp_path):
    ledger = Ledger(tmp_path / "table.db")
    ledger.update_position(Position(slug="slug-table", net_qty=Decimal("50"), avg_price=Decimal("0.55")), 1000)
    ledger.update_position(Position(slug="slug-zero", net_qty=Decimal("0"), avg_price=Decimal("0.55")), 1000)

    positions = get_active_positions(ledger)
    assert len(positions) == 1
    assert positions[0].slug == "slug-table"
    assert positions[0].net_qty == Decimal("50")
    assert positions[0].avg_price == Decimal("0.55")


def test_resolve_market_title_fallback(monkeypatch):
    import requests

    long_slug = "2174263314346390629056905015582624153306727273685651494952952650494641127760"

    def mock_get_fail(*args, **kwargs):
        raise requests.RequestException("Network error")

    monkeypatch.setattr(requests, "get", mock_get_fail)
    title = resolve_market_title(long_slug)
    assert title == f"{long_slug[:8]}...{long_slug[-6:]}"


def test_resolve_market_title_success(monkeypatch):
    import requests

    test_slug = "success_slug_1234567890"

    class MockResponse:
        status_code = 200

        def json(self):
            return [{"question": "Will candidate X win the election?"}]

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: MockResponse())
    title = resolve_market_title(test_slug, fetch=True)
    assert title == "Will candidate X win the election?"


def test_sync_ledger_from_vps_missing_key(tmp_path):
    target = tmp_path / "ledger.db"
    success, msg = sync_ledger_from_vps(target)
    assert isinstance(success, bool)
    assert isinstance(msg, str)


def test_get_official_daily_earnings_not_global():
    _get_official_daily_earnings.clear()
    settings = Settings(venue="us", polygon_private_key="0xabc")
    assert _get_official_daily_earnings(settings) is None

    settings_no_key = Settings(venue="global", polygon_private_key="")
    assert _get_official_daily_earnings(settings_no_key) is None


def test_get_official_daily_earnings_success():
    _get_official_daily_earnings.clear()
    mock_clob = MagicMock()
    mock_clob.client.get_total_earnings_for_user_for_day.return_value = [
        {"earnings": "1.2345"},
        {"earnings": "0.5000"},
    ]

    with patch("pmus.clob_venue.ClobVenueClient", return_value=mock_clob):
        settings = Settings(venue="global", polygon_private_key="0x123")
        res = _get_official_daily_earnings(settings)
        assert res == Decimal("1.7345")


def test_get_official_daily_earnings_dict_structure():
    _get_official_daily_earnings.clear()
    mock_clob = MagicMock()
    mock_clob.client.get_total_earnings_for_user_for_day.return_value = {
        "earnings": "2.50"
    }

    with patch("pmus.clob_venue.ClobVenueClient", return_value=mock_clob):
        settings = Settings(venue="global", polygon_private_key="0x123")
        res = _get_official_daily_earnings(settings)
        assert res == Decimal("2.50")


def test_get_official_daily_earnings_exception_fallback():
    _get_official_daily_earnings.clear()
    mock_clob = MagicMock()
    mock_clob.client.get_total_earnings_for_user_for_day.side_effect = RuntimeError("API error")

    with patch("pmus.clob_venue.ClobVenueClient", return_value=mock_clob):
        settings = Settings(venue="global", polygon_private_key="0x123")
        res = _get_official_daily_earnings(settings)
        assert res is None


