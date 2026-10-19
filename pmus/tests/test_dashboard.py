from datetime import datetime, timezone
from decimal import Decimal
import time
from unittest.mock import MagicMock, patch

from pmus.config import Settings
from pmus.dashboard import (
    _get_clob_venue_client,
    _get_live_buying_power,
    _get_official_daily_earnings,
    check_password,
    compute_kpis,
    compute_maker_rebate_summary,
    format_fills_table,
    format_market_overview,
    get_active_positions,
    get_profile_links,
    is_database_empty,
    render_live_hud,
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


def test_compute_kpis_collateral_cap_uses_effective_balance_when_max_collateral_higher(tmp_path):
    ledger = Ledger(tmp_path / "high_cap.db")
    kpis = compute_kpis(
        ledger,
        since_ms=0,
        buying_power=Decimal("176.18"),
        max_collateral=Decimal("500.00"),
    )
    assert kpis["balance_usd"] == Decimal("176.18")
    assert kpis["collateral_cap_usd"] == Decimal("176.18")


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


def test_format_market_overview_zeroes_stale_rewards_when_monitoring(tmp_path):
    ledger = Ledger(tmp_path / "stale_lip.db")
    now_ms = 1_000_000
    ledger.record_lip_snapshot(
        LipSnapshot(
            ts_ms=now_ms,
            slug="mkt-stale",
            program_id="prog-stale",
            trader_bid_score=Decimal("45"),
            total_bid_score=Decimal("500"),
            trader_ask_score=Decimal("0"),
            total_ask_score=Decimal("500"),
            qualifies=True,
            reward_usd=Decimal("0.00047884"),
            bid_qualifies=True,
            ask_qualifies=False,
        )
    )
    rows = format_market_overview(ledger, recent_ms=0)
    assert len(rows) == 1
    row = rows[0]
    assert row["slug"] == "mkt-stale"
    assert row["status"] == "MONITORING"
    assert row["qualifies"] is False
    assert row["bid_qualifies"] is False
    assert row["ask_qualifies"] is False
    assert row["trader_bid_score"] == Decimal("0")
    assert row["trader_ask_score"] == Decimal("0")
    assert row["est_reward_rate"] == Decimal("0")
    assert row["target_pool_size"] == Decimal("500")


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
    with patch("pmus.dashboard.Path.exists", return_value=False):
        success, msg = sync_ledger_from_vps(target)
    assert success is False
    assert "SSH key not found" in msg


def test_get_official_daily_earnings_not_global():
    _get_official_daily_earnings.clear()
    settings = Settings(venue="us", polygon_private_key="0xabc")
    assert _get_official_daily_earnings(settings) is None

    settings_no_key = Settings(venue="global", polygon_private_key="")
    assert _get_official_daily_earnings(settings_no_key) is None


def test_get_official_daily_earnings_success():
    _get_official_daily_earnings.clear()
    _get_clob_venue_client.clear()
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
    _get_clob_venue_client.clear()
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
    _get_clob_venue_client.clear()
    mock_clob = MagicMock()
    mock_clob.client.get_total_earnings_for_user_for_day.side_effect = RuntimeError("API error")

    with patch("pmus.clob_venue.ClobVenueClient", return_value=mock_clob):
        settings = Settings(venue="global", polygon_private_key="0x123")
        res = _get_official_daily_earnings(settings)
        assert res is None


def test_get_live_buying_power_cached_client():
    _get_live_buying_power.clear()
    _get_clob_venue_client.clear()
    mock_clob = MagicMock()
    mock_clob.get_buying_power.return_value = Decimal("45.67")

    with patch("pmus.clob_venue.ClobVenueClient", return_value=mock_clob):
        settings = Settings(venue="global", polygon_private_key="0x123")
        bp = _get_live_buying_power(settings)
        assert bp == Decimal("45.67")


def test_get_clob_venue_client_caching():
    _get_clob_venue_client.clear()
    mock_clob = MagicMock()
    with patch("pmus.clob_venue.ClobVenueClient", return_value=mock_clob) as mock_cls:
        settings = Settings(venue="global", polygon_private_key="0xabc")
        c1 = _get_clob_venue_client(settings)
        c2 = _get_clob_venue_client(settings)
        assert c1 is c2
        assert mock_cls.call_count == 1


def test_check_password_empty():
    settings = Settings(dashboard_password="")
    assert check_password(settings) is True


def test_check_password_authenticated():
    settings = Settings(dashboard_password="secret_password")
    with patch("pmus.dashboard.st") as mock_st:
        mock_st.session_state = {"authenticated": True}
        assert check_password(settings) is True


def test_check_password_unauthenticated_renders():
    settings = Settings(dashboard_password="secret_password")
    with patch("pmus.dashboard.st") as mock_st:
        mock_st.session_state = {}
        mock_st.text_input.return_value = ""
        mock_st.form_submit_button.return_value = False
        assert check_password(settings) is False
        mock_st.title.assert_called_with("PMUS Trading HUD")
        mock_st.subheader.assert_called_with("Restricted Access - Authentication Required")
        mock_st.text_input.assert_called_with("Dashboard Password", type="password", key="login_pwd")
        mock_st.form_submit_button.assert_called_with("Unlock Dashboard")


def test_check_password_success_submit():
    settings = Settings(dashboard_password="secret_password")
    session = {}
    with patch("pmus.dashboard.st") as mock_st:
        mock_st.session_state = session
        mock_st.text_input.return_value = "secret_password"
        mock_st.form_submit_button.return_value = True
        assert check_password(settings) is False
        assert session.get("authenticated") is True
        mock_st.rerun.assert_called_once()


def test_check_password_accepts_whitespace_padded_password():
    settings = Settings(dashboard_password="secret_password")
    session = {}
    with patch("pmus.dashboard.st") as mock_st:
        mock_st.session_state = session
        mock_st.text_input.return_value = "  secret_password  "
        mock_st.form_submit_button.return_value = True
        assert check_password(settings) is False
        assert session.get("authenticated") is True
        mock_st.rerun.assert_called_once()


def test_check_password_invalid_submit():
    settings = Settings(dashboard_password="secret_password")
    session = {}
    with patch("pmus.dashboard.st") as mock_st:
        mock_st.session_state = session
        mock_st.text_input.return_value = "wrong_password"
        mock_st.form_submit_button.return_value = True
        assert check_password(settings) is False
        assert session.get("authenticated") is not True
        mock_st.error.assert_called_with("Invalid password")


def test_classify_position_phase():
    from datetime import datetime, timezone
    from pmus.dashboard import classify_position_phase

    now = datetime(2026, 10, 13, 18, 0, 0, tzinfo=timezone.utc)
    assert classify_position_phase("2026-10-13T15:00:00Z", now=now) == "In-Play / Awaiting Settlement"
    assert classify_position_phase("2026-10-13T20:00:00Z", now=now) == "Pre-Match / Open"
    assert classify_position_phase("", now=now) == "Open"
    assert classify_position_phase(None, now=now) == "Open"
    assert classify_position_phase("not-a-timestamp", now=now) == "Open"


def test_compute_kpis_estimates_global_clob_maker_rebates_when_fee_zero(tmp_path):
    ledger = Ledger(tmp_path / "rebates_kpi.db")
    now_ms = 1_000_000

    ledger.record_fill(
        ShadowFill(
            fill_id="f1",
            order_id="o1",
            slug="mkt-1",
            side="BUY_LONG",
            price=Decimal("0.40"),
            qty=Decimal("100"),
            fee=Decimal("0"),
            is_maker=True,
            ts_ms=now_ms,
        )
    )

    kpis = compute_kpis(ledger, since_ms=0)
    expected_rebate = Decimal("0.3000")
    assert kpis["maker_rebates"] == expected_rebate
    assert kpis["taker_fees"] == Decimal("0")
    assert kpis["net_pnl"] == expected_rebate


def test_compute_maker_rebate_summary_calculates_rebates_and_projections(tmp_path):
    ledger = Ledger(tmp_path / "rebate_summary.db")
    t0 = 10_000_000
    t1 = t0 + 7_200_000

    ledger.record_fill(
        ShadowFill(
            fill_id="f1",
            order_id="o1",
            slug="mkt-1",
            side="BUY_LONG",
            price=Decimal("0.50"),
            qty=Decimal("100"),
            fee=Decimal("0"),
            is_maker=True,
            ts_ms=t0,
        )
    )
    ledger.record_fill(
        ShadowFill(
            fill_id="f2",
            order_id="o2",
            slug="mkt-1",
            side="SELL_LONG",
            price=Decimal("0.50"),
            qty=Decimal("100"),
            fee=Decimal("0"),
            is_maker=True,
            ts_ms=t1,
        )
    )

    ledger.record_order(
        LiveOrder(
            order_id="o3",
            slug="mkt-1",
            side="BUY_LONG",
            price=Decimal("0.40"),
            qty_open=Decimal("200"),
        )
    )

    ledger.record_lip_snapshot(
        LipSnapshot(
            ts_ms=t1,
            slug="mkt-1",
            program_id="p1",
            trader_bid_score=Decimal("10"),
            total_bid_score=Decimal("100"),
            trader_ask_score=Decimal("0"),
            total_ask_score=Decimal("100"),
            qualifies=True,
            reward_usd=Decimal("0.0001"),
            bid_qualifies=True,
            ask_qualifies=False,
        )
    )

    summary = compute_maker_rebate_summary(ledger, since_ms=0)
    assert summary["current_maker_rebates"] == Decimal("0.6250")
    assert summary["maker_fills_count"] == 2
    assert summary["resting_rebate_potential"] == Decimal("0.6000")
    assert summary["active_daily_lip"] == Decimal("8.64")
    assert summary["projected_daily_rebates"] == Decimal("7.5000")
    assert summary["projected_monthly_rebates"] == Decimal("225.00")
    assert summary["projected_daily_profit"] == Decimal("16.14")
    assert summary["projected_monthly_profit"] == Decimal("484.20")


def test_global_mode_disables_synthetic_us_maker_rebates_and_projects_pool_velocity(tmp_path):
    ledger = Ledger(tmp_path / "global_mode.db")
    now_ms = 1_000_000

    fill = ShadowFill(
        fill_id="f1",
        order_id="o1",
        slug="mkt-1",
        side="BUY_LONG",
        price=Decimal("0.40"),
        qty=Decimal("100"),
        fee=Decimal("0"),
        is_maker=True,
        ts_ms=now_ms,
    )
    ledger.record_fill(fill)

    order = LiveOrder(
        order_id="o2",
        slug="mkt-1",
        side="BUY_LONG",
        price=Decimal("0.45"),
        qty_open=Decimal("100"),
    )
    ledger.record_order(order)

    kpis = compute_kpis(ledger, since_ms=0, is_global=True)
    assert kpis["maker_rebates"] == Decimal("0")

    fills_table = format_fills_table([fill], is_global=True)
    assert fills_table[0]["Fee"] == "-$0.0000"

    market_rows = [
        {
            "slug": "mkt-1",
            "status": "QUOTING",
            "qualifies": True,
            "est_reward_rate": Decimal("0.00010000"),
        },
        {
            "slug": "mkt-2",
            "status": "IDLE",
            "qualifies": False,
            "est_reward_rate": Decimal("0.00005000"),
        },
    ]

    summary = compute_maker_rebate_summary(
        ledger,
        since_ms=0,
        market_rows=market_rows,
        open_orders=[order],
        is_global=True,
    )
    assert summary["current_maker_rebates"] == Decimal("0")
    assert summary["resting_rebate_potential"] == Decimal("0")
    assert summary["active_daily_lip"] == Decimal("8.64")
    assert summary["active_hourly_lip"] == Decimal("0.3600")
    assert summary["active_monthly_lip"] == Decimal("259.20")
    assert summary["deployed_collateral"] == Decimal("45.00")
    assert summary["scoring_markets_count"] == 1
    assert summary["active_buy_orders_count"] == 1


def test_render_live_hud_displays_today_est_and_all_time_lip_yield(tmp_path):
    ledger = Ledger(tmp_path / "hud_test.db")
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ms = int(today_start.timestamp() * 1000)
    yesterday_ms = 1792281600000 - 3_600_000
    today_ms = today_start_ms + 3_600_000

    ledger.record_lip_snapshot(
        LipSnapshot(
            ts_ms=yesterday_ms,
            slug="mkt-old",
            program_id="p-old",
            trader_bid_score=Decimal("10"),
            total_bid_score=Decimal("100"),
            trader_ask_score=Decimal("0"),
            total_ask_score=Decimal("100"),
            qualifies=True,
            reward_usd=Decimal("10.00"),
            bid_qualifies=True,
            ask_qualifies=False,
        )
    )
    ledger.record_lip_snapshot(
        LipSnapshot(
            ts_ms=today_ms,
            slug="mkt-today",
            program_id="p-today",
            trader_bid_score=Decimal("20"),
            total_bid_score=Decimal("100"),
            trader_ask_score=Decimal("0"),
            total_ask_score=Decimal("100"),
            qualifies=True,
            reward_usd=Decimal("5.00"),
            bid_qualifies=True,
            ask_qualifies=False,
        )
    )

    settings = Settings(venue="global", data_dir=tmp_path)
    with patch("pmus.dashboard.st") as mock_st, \
         patch("pmus.dashboard.is_database_empty", return_value=False), \
         patch("pmus.dashboard._get_live_buying_power", return_value=Decimal("100.00")), \
         patch("pmus.dashboard._get_official_daily_earnings", return_value=None):
        mock_st.columns.side_effect = lambda n: [MagicMock() for _ in range(n)]
        fn = getattr(render_live_hud, "__wrapped__", render_live_hud)
        fn(ledger, settings)
        metric_calls = [call.kwargs for call in mock_st.metric.call_args_list]
        pool_metric = next(m for m in metric_calls if m.get("label") == "Pool Rewards Earned")
        assert pool_metric["value"] == "$5.0000"
        assert pool_metric["delta"] == "Today Est: $5.00 | All-Time: $15.00"
        exit_metric = next(m for m in metric_calls if m.get("label") == "Total Exit Value")
        assert exit_metric["value"] == "$105.00"


def test_epoch_cutoff_and_custom_reset_persistence(tmp_path):
    from pmus.dashboard import (
        DEFAULT_RESET_EPOCH_MS,
        FRESH_START_TODAY_0700_UTC_MS,
        get_effective_reset_epoch_ms,
        set_custom_reset_epoch_ms,
    )

    assert DEFAULT_RESET_EPOCH_MS == 1792281600000
    assert FRESH_START_TODAY_0700_UTC_MS == 1792308000000

    settings = Settings(data_dir=tmp_path)
    assert get_effective_reset_epoch_ms(settings) == DEFAULT_RESET_EPOCH_MS

    set_custom_reset_epoch_ms(settings, 1792310000000)
    assert get_effective_reset_epoch_ms(settings) == 1792310000000


def test_compute_kpis_total_exit_value_exact_user_example(tmp_path):
    ledger = Ledger(tmp_path / "exit_value.db")
    t0 = 1792281600000 + 10_000

    ledger.record_fill(
        ShadowFill(
            fill_id="buy-loss",
            order_id="o-buy",
            slug="mkt-loss",
            side="BUY_LONG",
            price=Decimal("0.50"),
            qty=Decimal("100"),
            fee=Decimal("0"),
            is_maker=True,
            ts_ms=t0,
        )
    )
    ledger.record_fill(
        ShadowFill(
            fill_id="sell-loss",
            order_id="o-sell",
            slug="mkt-loss",
            side="SELL_LONG",
            price=Decimal("0.45"),
            qty=Decimal("100"),
            fee=Decimal("0"),
            is_maker=True,
            ts_ms=t0 + 1000,
        )
    )
    ledger.record_lip_snapshot(
        LipSnapshot(
            ts_ms=t0,
            slug="mkt-loss",
            program_id="p-1",
            trader_bid_score=Decimal("50"),
            total_bid_score=Decimal("100"),
            trader_ask_score=Decimal("50"),
            total_ask_score=Decimal("100"),
            qualifies=True,
            reward_usd=Decimal("14.00"),
            bid_qualifies=True,
            ask_qualifies=True,
        )
    )

    kpis = compute_kpis(
        ledger,
        since_ms=1792281600000,
        buying_power=Decimal("150.00"),
        held_positions_usd=Decimal("50.00"),
        is_global=True,
    )
    assert kpis["portfolio_value_usd"] == Decimal("200.00")
    assert kpis["active_lip_rewards"] == Decimal("14.00")
    assert kpis["realized_pnl"] == Decimal("-5.00")
    assert kpis["maker_rebates"] == Decimal("0")
    assert kpis["taker_fees"] == Decimal("0")
    assert kpis["total_exit_value_usd"] == Decimal("209.00")


def test_format_held_positions_table_with_and_without_sell_orders(monkeypatch):
    from pmus.dashboard import _TITLE_CACHE, format_held_positions_table

    _TITLE_CACHE["token-1"] = "Will Market 1 Resolve Yes?"
    _TITLE_CACHE["token-2"] = "Will Market 2 Resolve Yes?"
    _TITLE_CACHE["token-3"] = "Will Market 3 Resolve Yes?"

    positions = [
        Position(slug="token-1", net_qty=Decimal("180.86"), avg_price=Decimal("0.3800")),
        Position(slug="token-2", net_qty=Decimal("100"), avg_price=Decimal("0.5000")),
        Position(slug="token-3", net_qty=Decimal("50"), avg_price=Decimal("0.4000")),
    ]
    open_orders = [
        LiveOrder(
            order_id="sell-1",
            slug="token-1",
            side="SELL_LONG",
            price=Decimal("0.4020"),
            qty_open=Decimal("180.86"),
        ),
        LiveOrder(
            order_id="sell-2",
            slug="token-2",
            side="SELL_LONG",
            price=Decimal("0.5000"),
            qty_open=Decimal("100"),
        ),
    ]

    rows = format_held_positions_table(positions, open_orders)
    assert len(rows) == 3

    r1 = rows[0]
    assert "Slug" not in r1 and "Market Slug" not in r1 and "slug" not in r1
    assert r1["Market Question"] == "Will Market 1 Resolve Yes?"
    assert r1["Shares Held"] == "180.86"
    assert r1["Avg Entry Price"] == "$0.3800"
    assert r1["Cost Paid"] == "$68.73"
    assert r1["Limit Sell Order"] == "SELL 180.86 @ $0.4020"
    assert r1["Break-Even Price"] == "$0.3800"
    assert r1["Projected Revenue"] == "$72.71"
    assert r1["Projected Net Profit / Loss"] == "+$3.98 (+5.8%)"

    r2 = rows[1]
    assert r2["Limit Sell Order"] == "SELL 100 @ $0.5000"
    assert r2["Projected Revenue"] == "$50.00"
    assert r2["Projected Net Profit / Loss"] == "$0.00 (Break-Even)"

    r3 = rows[2]
    assert r3["Limit Sell Order"] == "Pending Unwind Quote"
    assert r3["Projected Revenue"] == "$20.00"
    assert r3["Projected Net Profit / Loss"] == "$0.00 (Break-Even)"


def test_render_mobile_cards_html_and_ui_slug_exclusion(tmp_path):
    from pmus.dashboard import _TITLE_CACHE, render_mobile_cards_html

    _TITLE_CACHE["tok-abc"] = "Will Bitcoin hit $100k?"
    html_out = render_mobile_cards_html(
        [
            {
                "Market Question": "Will Bitcoin hit $100k?",
                "Slug": "tok-abc",
                "Shares Held": "100",
                "Limit Sell Order": "SELL 100 @ $0.5200",
            }
        ],
        title_key="Market Question",
        badge_key="Limit Sell Order",
    )
    assert "Will Bitcoin hit $100k?" in html_out
    assert "SELL 100 @ $0.5200" in html_out
    assert "tok-abc" not in html_out

    ledger = Ledger(tmp_path / "ui_no_slug.db")
    t_now = 1792281600000 + 5000
    ledger.record_order(LiveOrder(order_id="o1", slug="tok-abc", side="BUY_LONG", price=Decimal("0.48"), qty_open=Decimal("50")))
    ledger.record_fill(
        ShadowFill(
            fill_id="f1",
            order_id="o1",
            slug="tok-abc",
            side="BUY_LONG",
            price=Decimal("0.48"),
            qty=Decimal("50"),
            fee=Decimal("0"),
            is_maker=True,
            ts_ms=t_now,
        )
    )
    ledger.update_position(Position(slug="tok-abc", net_qty=Decimal("50"), avg_price=Decimal("0.48")), t_now)

    settings = Settings(venue="global", data_dir=tmp_path)
    with patch("pmus.dashboard.st") as mock_st, \
         patch("pmus.dashboard.is_database_empty", return_value=False), \
         patch("pmus.dashboard._get_live_buying_power", return_value=Decimal("200.00")), \
         patch("pmus.dashboard._get_official_daily_earnings", return_value=Decimal("10.00")):
        mock_st.columns.side_effect = lambda n: [MagicMock() for _ in range(n)]
        fn = getattr(render_live_hud, "__wrapped__", render_live_hud)
        fn(ledger, settings)
        assert mock_st.dataframe.call_count >= 3
        for call in mock_st.dataframe.call_args_list:
            df = call.args[0]
            for col in df.columns:
                assert col not in ("Slug", "slug", "Market Slug")







