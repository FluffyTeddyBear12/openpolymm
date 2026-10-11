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

    # 30 days away: simple yield = (1 - 0.985)/0.985 = 1.52%, annualized = 1.52% * 365/30 = 18.5% < 25%
    market_low_apy = {"endDate": (now + timedelta(days=30)).isoformat()}
    ok, reason = SatelliteEngine.check_gates(market_low_apy, book, current_time=now)
    assert ok is False
    assert "APY" in reason

    # 3 days away (<= 7 days): passes regardless of APY
    market_short = {"endDate": (now + timedelta(days=3)).isoformat()}
    ok, _ = SatelliteEngine.check_gates(market_short, book, current_time=now)
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
    engine = SatelliteEngine(ledger=temp_ledger, trade_mode="shadow")

    # Record a resolved loss to trip circuit breaker
    t = temp_ledger.record_entry("shadow", "heavy_favorite", "c1", "tok-1", "mkt-1", "q1", "YES", "BUY", 0.97, 10.0, 10.3)
    temp_ledger.record_resolution(t, outcome_won=False)

    res = engine.run_once()
    assert res["status"] == "HALTED"
    assert "Circuit breaker active" in res["reason"]
