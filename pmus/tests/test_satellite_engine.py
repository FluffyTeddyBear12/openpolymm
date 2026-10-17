from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest

from pmus.certainty_ledger import CertaintyLedger
from pmus.models import Book, Level
from pmus.satellite_engine import SatelliteEngine


@pytest.fixture
def temp_ledger(tmp_path):
    db_file = tmp_path / "test_certainty.db"
    return CertaintyLedger(db_path=db_file)


def test_certainty_ledger_entry_and_open_trades(temp_ledger):
    trade_id = temp_ledger.record_entry(
        trade_mode="shadow",
        strategy_type="heavy_favorite",
        condition_id="0xcond1",
        token_id="tok-yes-1",
        market_slug="market-one",
        question="Will X happen?",
        outcome="YES",
        side="BUY",
        entry_price=0.970,
        size_usd=15.0,
        shares=15.4639,
    )
    assert trade_id == 1

    open_trades = temp_ledger.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["id"] == 1
    assert open_trades[0]["status"] == "FILLED"
    assert open_trades[0]["entry_price"] == 0.970


def test_certainty_ledger_resolution_win(temp_ledger):
    trade_id = temp_ledger.record_entry(
        trade_mode="shadow",
        strategy_type="heavy_favorite",
        condition_id="0xcond1",
        token_id="tok-yes-1",
        market_slug="market-one",
        question="Will X happen?",
        outcome="YES",
        side="BUY",
        entry_price=0.970,
        size_usd=15.0,
        shares=15.4639,
    )
    temp_ledger.record_resolution(trade_id, outcome_won=True, payout_per_share=1.0)

    trade = temp_ledger.get_trade(trade_id)
    assert trade["status"] == "RESOLVED_WIN"
    assert trade["exit_price"] == 1.0
    # pnl = (1.0 - 0.970) * 15.4639 = 0.463917
    assert trade["pnl_usd"] > 0.46
    assert len(temp_ledger.get_open_trades()) == 0


def test_certainty_ledger_resolution_loss_and_circuit_breaker(temp_ledger):
    trade_id = temp_ledger.record_entry(
        trade_mode="shadow",
        strategy_type="heavy_favorite",
        condition_id="0xcond1",
        token_id="tok-yes-1",
        market_slug="market-one",
        question="Will X happen?",
        outcome="YES",
        side="BUY",
        entry_price=0.970,
        size_usd=15.0,
        shares=15.4639,
    )
    temp_ledger.record_resolution(trade_id, outcome_won=False, payout_per_share=0.0)

    trade = temp_ledger.get_trade(trade_id)
    assert trade["status"] == "RESOLVED_LOSS"
    assert trade["exit_price"] == 0.0
    assert trade["pnl_usd"] == -15.0

    # Circuit breaker should be active
    is_active, rem_hours = temp_ledger.is_circuit_breaker_active(48.0)
    assert is_active is True
    assert 47.0 < rem_hours <= 48.0


def test_certainty_ledger_clob_exit(temp_ledger):
    trade_id = temp_ledger.record_entry(
        trade_mode="shadow",
        strategy_type="heavy_favorite",
        condition_id="0xcond1",
        token_id="tok-yes-1",
        market_slug="market-one",
        question="Will X happen?",
        outcome="YES",
        side="BUY",
        entry_price=0.970,
        size_usd=15.0,
        shares=15.4639,
    )
    temp_ledger.record_clob_exit(trade_id, exit_price=0.993, notes="Secondary CLOB exit")

    trade = temp_ledger.get_trade(trade_id)
    assert trade["status"] == "EXITED_CLOB"
    assert trade["exit_price"] == 0.993
    # pnl = (0.993 - 0.970) * 15.4639 = 0.3556697
    assert trade["pnl_usd"] > 0.35


def test_certainty_ledger_performance_summary(temp_ledger):
    # 1 win
    t1 = temp_ledger.record_entry("shadow", "heavy_favorite", "c1", "tok1", "m1", "q1", "YES", "BUY", 0.97, 10.0, 10.309)
    temp_ledger.record_resolution(t1, outcome_won=True)

    # 1 clob exit win
    t2 = temp_ledger.record_entry("shadow", "heavy_favorite", "c2", "tok2", "m2", "q2", "YES", "BUY", 0.97, 10.0, 10.309)
    temp_ledger.record_clob_exit(t2, exit_price=0.993)

    summary = temp_ledger.get_performance_summary()
    assert summary["total_trades"] == 2
    assert summary["wins"] == 2
    assert summary["losses"] == 0
    assert summary["win_rate"] == 1.0
    assert summary["total_pnl"] > 0


def test_gate_wide_spread_rejected():
    market = {"endDate": "2026-10-15T00:00:00Z"}
    book = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.945"), Decimal("1000")),),
        asks=(Level(Decimal("0.975"), Decimal("100")),),
        ts_ms=1000,
    )
    ok, reason = SatelliteEngine.check_gates(market, book)
    assert ok is False
    assert "Spread" in reason


def test_gate_thin_depth_rejected():
    market = {"endDate": "2026-10-15T00:00:00Z"}
    # Bid depth 0.97 * 100 = $97 < $250
    book_thin_bid = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.970"), Decimal("100")),),
        asks=(Level(Decimal("0.975"), Decimal("100")),),
        ts_ms=1000,
    )
    ok, reason = SatelliteEngine.check_gates(market, book_thin_bid)
    assert ok is False
    assert "Bid depth" in reason

    # Ask depth 0.975 * 10 = $9.75 < $15
    book_thin_ask = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.970"), Decimal("1000")),),
        asks=(Level(Decimal("0.975"), Decimal("10")),),
        ts_ms=1000,
    )
    ok, reason = SatelliteEngine.check_gates(market, book_thin_ask)
    assert ok is False
    assert "Ask depth" in reason


def test_gate_price_velocity_rejected():
    market = {"endDate": "2026-10-15T00:00:00Z"}
    book = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.965"), Decimal("1000")),),
        asks=(Level(Decimal("0.975"), Decimal("100")),),
        ts_ms=1000,
    )
    now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    # Price was 0.940 (diff = 0.035 > 0.02) 20s ago
    history = [(now.timestamp() - 20.0, 0.940)]

    ok, reason = SatelliteEngine.check_gates(market, book, price_history=history, current_time=now)
    assert ok is False
    assert "Price velocity" in reason


def test_gate_expiry_and_apy():
    now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    book = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.965"), Decimal("1000")),),
        asks=(Level(Decimal("0.975"), Decimal("100")),),
        ts_ms=1000,
    )

    # 30 days away (> 14 days) rejected unconditionally
    market_long = {"endDate": (now + timedelta(days=30)).isoformat()}
    ok, reason = SatelliteEngine.check_gates(market_long, book, current_time=now)
    assert ok is False
    assert "exceeds horizon" in reason

    # 2 days away (<= 3 days): passes with short-dated band
    market_short = {"endDate": (now + timedelta(days=2)).isoformat()}
    ok, _ = SatelliteEngine.check_gates(market_short, book, current_time=now)
    assert ok is True


def test_gate_calibrated_spread_and_depth():
    now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    market = {"endDate": (now + timedelta(days=2)).isoformat()}
    # Spread exactly 0.025: 0.975 - 0.950 = 0.025 -> passes
    # Depth: 0.95 * 300 = $285 >= $250 -> passes
    book = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.950"), Decimal("300")),),
        asks=(Level(Decimal("0.975"), Decimal("20")),),
        ts_ms=1000,
    )
    ok, reason = SatelliteEngine.check_gates(market, book, current_time=now)
    assert ok is True


def test_gate_price_ceiling():
    market = {"endDate": "2026-10-15T00:00:00Z"}
    book = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.980"), Decimal("1000")),),
        asks=(Level(Decimal("0.990"), Decimal("100")),),
        ts_ms=1000,
    )
    ok, reason = SatelliteEngine.check_gates(market, book)
    assert ok is False
    assert "ceiling" in reason


def test_gate_valid_97c_passes():
    now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    market = {"endDate": (now + timedelta(days=2)).isoformat()}
    book = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.965"), Decimal("1000")),),
        asks=(Level(Decimal("0.975"), Decimal("100")),),
        ts_ms=1000,
    )
    ok, reason = SatelliteEngine.check_gates(market, book, current_time=now)
    assert ok is True
    assert "All microstructure gates passed" in reason


def test_satellite_engine_parity_arbitrage(temp_ledger):
    engine = SatelliteEngine(ledger=temp_ledger, trade_mode="shadow")

    fake_market = {
        "conditionId": "0xparity1",
        "slug": "binary-parity-mkt",
        "question": "Will team win?",
        "tokens": [
            {"token_id": "tok-yes", "outcome": "YES"},
            {"token_id": "tok-no", "outcome": "NO"},
        ],
        "endDate": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
    }

    # Ask Yes: 0.48, Ask No: 0.49 -> Sum = 0.97 <= 0.990
    book_yes = Book(
        "tok-yes",
        bids=(Level(Decimal("0.47"), Decimal("2000")),),
        asks=(Level(Decimal("0.48"), Decimal("100")),),
        ts_ms=1000,
    )
    book_no = Book(
        "tok-no",
        bids=(Level(Decimal("0.48"), Decimal("2000")),),
        asks=(Level(Decimal("0.49"), Decimal("100")),),
        ts_ms=1000,
    )

    with patch.object(engine, "fetch_book", side_effect=lambda t: book_yes if t == "tok-yes" else book_no):
        entered = engine.scan_parity_arbitrage([fake_market])
        assert len(entered) == 2

    trades = temp_ledger.get_all_trades()
    assert len(trades) == 2
    assert all(t["strategy_type"] == "parity_arb" for t in trades)
    assert trades[0]["shares"] == trades[1]["shares"]


def test_secondary_clob_exit_trigger(temp_ledger):
    engine = SatelliteEngine(ledger=temp_ledger, trade_mode="shadow")

    trade_id = temp_ledger.record_entry(
        "shadow", "heavy_favorite", "c1", "tok-1", "mkt-1", "q1", "YES", "BUY", 0.97, 15.0, 15.46
    )

    book_high_bid = Book(
        "tok-1",
        bids=(Level(Decimal("0.993"), Decimal("500")),),
        asks=(Level(Decimal("0.998"), Decimal("100")),),
        ts_ms=1000,
    )

    with patch.object(engine, "fetch_book", return_value=book_high_bid):
        engine.monitor_positions()

    trade = temp_ledger.get_trade(trade_id)
    assert trade["status"] == "EXITED_CLOB"
    assert trade["exit_price"] == 0.993
    assert trade["pnl_usd"] > 0


def test_circuit_breaker_halts_scan(temp_ledger):
    engine_halted = SatelliteEngine(ledger=temp_ledger, trade_mode="shadow", circuit_breaker_hours=48.0)

    # Record a resolved loss to trip circuit breaker when enabled
    t = temp_ledger.record_entry("shadow", "heavy_favorite", "c1", "tok-1", "mkt-1", "q1", "YES", "BUY", 0.97, 10.0, 10.3)
    temp_ledger.record_resolution(t, outcome_won=False)

    res = engine_halted.run_once()
    assert res["status"] == "HALTED"
    assert "Circuit breaker active" in res["reason"]

    # Default engine has circuit breaker disabled (0.0h) and keeps scanning
    engine_default = SatelliteEngine(ledger=temp_ledger, trade_mode="shadow")
    with patch.object(engine_default, "fetch_public_markets", return_value=[]):
        res_default = engine_default.run_once()
    assert res_default["status"] == "OK"


def test_monitor_positions_resolves_loss_via_clob_market_fallback_when_gamma_empty(temp_ledger):
    engine = SatelliteEngine(ledger=temp_ledger, trade_mode="shadow")

    trade_id = temp_ledger.record_entry(
        trade_mode="shadow",
        strategy_type="heavy_favorite",
        condition_id="0xcond_fam_alv",
        token_id="tok-alv",
        market_slug="por-fam-alv-2026-10-12-spread-away-1pt5",
        question="Spread: Alverca (+1.5)",
        outcome="ALVERCA",
        side="BUY",
        entry_price=0.950,
        size_usd=30.0,
        shares=31.5789,
    )

    def fake_get(url, params=None, timeout=10.0):
        resp = MagicMock()
        resp.status_code = 200
        if "/markets" in url and params and "slug" in params:
            resp.json.return_value = []
        elif "/markets/0xcond_fam_alv" in url:
            resp.json.return_value = {
                "condition_id": "0xcond_fam_alv",
                "closed": True,
                "tokens": [
                    {"token_id": "tok-fam", "outcome": "Famalicao", "price": 1.0, "winner": True},
                    {"token_id": "tok-alv", "outcome": "Alverca", "price": 0.0, "winner": False},
                ],
            }
        else:
            resp.status_code = 404
        return resp

    with patch.object(engine, "fetch_book", side_effect=RuntimeError("404 No orderbook")), patch(
        "pmus.satellite_engine.requests.get", side_effect=fake_get
    ):
        engine.monitor_positions()

    trade = temp_ledger.get_trade(trade_id)
    assert trade["status"] == "RESOLVED_LOSS"
    assert trade["exit_price"] == 0.0
    assert trade["pnl_usd"] == -30.0
    is_active, _ = temp_ledger.is_circuit_breaker_active(48.0)
    assert is_active is True


def test_gate_rejects_past_resolution_timestamp():
    now = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
    market_past = {"endDate": (now - timedelta(hours=3)).isoformat()}
    book = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.965"), Decimal("1000")),),
        asks=(Level(Decimal("0.975"), Decimal("100")),),
        ts_ms=1000,
    )
    ok, reason = SatelliteEngine.check_gates(market_past, book, current_time=now)
    assert ok is False
    assert "Resolution timestamp in the past" in reason


def test_scan_parity_arbitrage_skips_when_max_positions_reached(temp_ledger):
    engine = SatelliteEngine(ledger=temp_ledger, trade_mode="shadow", max_open_positions=1)
    temp_ledger.record_entry(
        "shadow", "heavy_favorite", "c1", "tok-1", "mkt-1", "q1", "YES", "BUY", 0.97, 30.0, 30.9278
    )

    fake_market = {
        "conditionId": "0xparity1",
        "slug": "binary-parity-mkt",
        "question": "Will team win?",
        "tokens": [
            {"token_id": "tok-yes", "outcome": "YES"},
            {"token_id": "tok-no", "outcome": "NO"},
        ],
    }

    with patch.object(engine, "fetch_book") as mock_fetch:
        entered = engine.scan_parity_arbitrage([fake_market])
        assert entered == []
        mock_fetch.assert_not_called()


def test_toxic_archetype_rejected():
    now = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
    book = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.960"), Decimal("1000")),),
        asks=(Level(Decimal("0.970"), Decimal("100")),),
        ts_ms=1000,
    )
    for bad_slug in (
        "bra-cha- bah-2026-10-15-exact-score-0-4",
        "por-fam-alv-2026-10-12-spread-away-1pt5",
        "btc-updown-15m-1776009600",
        "arg-crdo- dyj-2026-10-13-first-half-total-1pt5",
    ):
        mkt = {"slug": bad_slug, "endDate": (now + timedelta(days=1)).isoformat()}
        ok, reason = SatelliteEngine.check_gates(mkt, book, current_time=now)
        assert ok is False
        assert "Toxic market archetype" in reason


def test_minimum_horizon_and_live_in_play_rejected():
    now = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
    book = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.960"), Decimal("1000")),),
        asks=(Level(Decimal("0.970"), Decimal("100")),),
        ts_ms=1000,
    )
    mkt_1h = {"slug": "clean-match", "endDate": (now + timedelta(hours=1)).isoformat()}
    ok, reason = SatelliteEngine.check_gates(mkt_1h, book, current_time=now)
    assert ok is False
    assert "high-gamma window" in reason

    mkt_live = {
        "slug": "clean-match",
        "endDate": (now + timedelta(days=1)).isoformat(),
        "events": [{"live": True}],
    }
    ok_live, reason_live = SatelliteEngine.check_gates(mkt_live, book, current_time=now)
    assert ok_live is False
    assert "live in-play" in reason_live


def test_event_correlation_deduplication(temp_ledger):
    engine = SatelliteEngine(ledger=temp_ledger, trade_mode="shadow")
    now = datetime.now(timezone.utc)
    exp = (now + timedelta(days=1)).isoformat()

    m1 = {
        "conditionId": "c1",
        "slug": "arg-crdo-dyj-2026-10-18-total-2pt5",
        "question": "O/U 2.5",
        "clobTokenIds": '["tok-1"]',
        "outcomePrices": '["0.96"]',
        "outcomes": '["UNDER"]',
        "endDate": exp,
    }
    m2 = {
        "conditionId": "c2",
        "slug": "arg-crdo-dyj-2026-10-18-total-3pt5",
        "question": "O/U 3.5",
        "clobTokenIds": '["tok-2"]',
        "outcomePrices": '["0.965"]',
        "outcomes": '["UNDER"]',
        "endDate": exp,
    }
    good_book = Book(
        "tok",
        bids=(Level(Decimal("0.955"), Decimal("1000")),),
        asks=(Level(Decimal("0.965"), Decimal("100")),),
        ts_ms=1000,
    )
    with patch.object(engine, "fetch_book", return_value=good_book):
        entered = engine.scan_heavy_favorites([m1, m2])
    assert len(entered) == 1


def test_parity_arb_loss_leg_excluded_from_circuit_breaker(temp_ledger):
    t_win = temp_ledger.record_entry("shadow", "parity_arb", "c1", "tok-y", "m1", "q1", "YES", "BUY", 0.50, 10.0, 20.0)
    t_loss = temp_ledger.record_entry("shadow", "parity_arb", "c1", "tok-n", "m1", "q1", "NO", "BUY", 0.48, 9.6, 20.0)
    temp_ledger.record_resolution(t_win, outcome_won=True, payout_per_share=1.0)
    temp_ledger.record_resolution(t_loss, outcome_won=False, payout_per_share=0.0)

    is_active, _ = temp_ledger.is_circuit_breaker_active(48.0)
    assert is_active is False


def test_void_50_50_resolution_and_stop_loss_and_overdue_unwind(temp_ledger):
    engine = SatelliteEngine(ledger=temp_ledger, trade_mode="shadow")
    now = datetime.now(timezone.utc)

    # 1. Void 0.50/0.50 resolution (like Trade #174)
    t_void = temp_ledger.record_entry(
        "shadow",
        "heavy_favorite",
        "c_void",
        "tok-void",
        "wta-match-void",
        "Will player win?",
        "NO",
        "BUY",
        0.960,
        30.0,
        31.25,
        expected_resolution=(now - timedelta(hours=2)).isoformat(),
    )

    def fake_gamma_void(url, params=None, timeout=10.0):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [
            {
                "closed": True,
                "clobTokenIds": '["tok-other", "tok-void"]',
                "outcomePrices": '["0.5", "0.5"]',
            }
        ]
        return resp

    with patch("pmus.satellite_engine.requests.get", side_effect=fake_gamma_void):
        engine.monitor_positions()

    settled_void = temp_ledger.get_trade(t_void)
    assert settled_void["status"] == "RESOLVED_LOSS"
    assert settled_void["exit_price"] == 0.5
    assert round(settled_void["pnl_usd"], 4) == round((0.5 - 0.960) * 31.25, 4)

    # 2. Stop-loss CLOB unwind when best_bid <= 0.88
    t_sl = temp_ledger.record_entry(
        "shadow",
        "heavy_favorite",
        "c_sl",
        "tok-sl",
        "mkt-sl",
        "Will team win?",
        "YES",
        "BUY",
        0.965,
        30.0,
        31.0881,
        expected_resolution=(now + timedelta(hours=5)).isoformat(),
    )
    book_sl = Book(
        "tok-sl",
        bids=(Level(Decimal("0.720"), Decimal("500")),),
        asks=(Level(Decimal("0.750"), Decimal("500")),),
        ts_ms=1000,
    )
    with patch.object(engine, "fetch_book", return_value=book_sl), patch.object(
        engine, "_check_and_record_resolution", return_value=False
    ):
        engine.monitor_positions()

    trade_sl = temp_ledger.get_trade(t_sl)
    assert trade_sl["status"] == "EXITED_CLOB"
    assert trade_sl["exit_price"] == 0.720
    assert "Stop-loss" in trade_sl["notes"]

    # 3. Overdue >36h time-stop unwind
    t_overdue = temp_ledger.record_entry(
        "shadow",
        "heavy_favorite",
        "c_over",
        "tok-over",
        "mkt-over",
        "Will event resolve?",
        "YES",
        "BUY",
        0.965,
        30.0,
        31.0881,
        expected_resolution=(now - timedelta(hours=40)).isoformat(),
    )
    book_over = Book(
        "tok-over",
        bids=(Level(Decimal("0.910"), Decimal("500")),),
        asks=(Level(Decimal("0.930"), Decimal("500")),),
        ts_ms=1000,
    )
    with patch.object(engine, "fetch_book", return_value=book_over), patch.object(
        engine, "_check_and_record_resolution", return_value=False
    ):
        engine.monitor_positions()

    trade_over = temp_ledger.get_trade(t_overdue)
    assert trade_over["status"] == "EXITED_CLOB"
    assert trade_over["exit_price"] == 0.910
    assert "Overdue >36h" in trade_over["notes"]


def test_fetch_public_markets_uses_ascending_false(temp_ledger):
    engine = SatelliteEngine(ledger=temp_ledger, trade_mode="shadow")
    captured_params = []

    def fake_get(url, params=None, timeout=10.0):
        if params:
            captured_params.append(dict(params))
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = []
        return resp

    with patch("pmus.satellite_engine.requests.get", side_effect=fake_get):
        engine.fetch_public_markets(limit=50)

    assert len(captured_params) == 6
    for p in captured_params:
        assert p.get("ascending") == "false"
        assert "descending" not in p


