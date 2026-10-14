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
        bids=(Level(Decimal("0.95"), Decimal("1000")),),
        asks=(Level(Decimal("0.98"), Decimal("100")),),
        ts_ms=1000,
    )
    ok, reason = SatelliteEngine.check_gates(market, book)
    assert ok is False
    assert "Spread" in reason


def test_gate_thin_depth_rejected():
    market = {"endDate": "2026-10-15T00:00:00Z"}
    # Bid depth 0.97 * 100 = $97 < $500
    book_thin_bid = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.97"), Decimal("100")),),
        asks=(Level(Decimal("0.98"), Decimal("100")),),
        ts_ms=1000,
    )
    ok, reason = SatelliteEngine.check_gates(market, book_thin_bid)
    assert ok is False
    assert "Bid depth" in reason

    # Ask depth 0.98 * 10 = $9.80 < $15
    book_thin_ask = Book(
        slug="tok-1",
        bids=(Level(Decimal("0.97"), Decimal("1000")),),
        asks=(Level(Decimal("0.98"), Decimal("10")),),
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
        bids=(Level(Decimal("0.975"), Decimal("1000")),),
        asks=(Level(Decimal("0.985"), Decimal("100")),),
        ts_ms=1000,
    )

    # 30 days away (> 14 days) rejected unconditionally
    market_long = {"endDate": (now + timedelta(days=30)).isoformat()}
    ok, reason = SatelliteEngine.check_gates(market_long, book, current_time=now)
    assert ok is False
    assert "exceeds horizon" in reason

    # 10 days away with low APY: simple yield = (1 - 0.985)/0.985 = 1.52%, annualized = 1.52% * 365/10 = 55% > 20%
    # Test low APY at 12 days: price 0.985 -> 1.52% * 365/12 = 46%
    # If price is 0.984 at 12 days: 1.62% * 365/12 = 49%
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

    # Ask Yes: 0.48, Ask No: 0.49 -> Sum = 0.97 < 1.00
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


def test_secondary_clob_exit_trigger(temp_ledger):
    engine = SatelliteEngine(ledger=temp_ledger, trade_mode="shadow")

    trade_id = temp_ledger.record_entry(
        "shadow", "heavy_favorite", "c1", "tok-1", "mkt-1", "q1", "YES", "BUY", 0.97, 15.0, 15.46
    )

    # Secondary CLOB exit requires best_bid >= 0.992
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

