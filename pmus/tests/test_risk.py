from decimal import Decimal
from pathlib import Path
import pytest

from pmus.config import Settings
from pmus.models import LiveOrder, Position, Quote
from pmus.risk import RiskManager

@pytest.fixture
def risk_settings(tmp_path):
    return Settings(
        total_budget=Decimal("60.00"),
        max_collateral=Decimal("70.00"),
        max_collateral_per_market=Decimal("30.00"),
        daily_loss_stop=Decimal("10.00"),
        max_open_orders=5,
        data_dir=tmp_path / "data",
    )

def test_sell_long_requires_zero_collateral(risk_settings):
    rm = RiskManager(risk_settings)
    q_sell_long = [Quote(slug="mkt-1", side="SELL_LONG", price=Decimal("0.800"), qty=Decimal("100"))]
    res = rm.check_pre_submit(
        new_quotes=q_sell_long,
        open_orders=[],
        positions={"mkt-1": Position("mkt-1", Decimal("100"), Decimal("0.700"))},
        buying_power=Decimal("1.00"),
        realized_loss_today=Decimal("0"),
    )
    assert res.allowed is True

def test_kill_switch_triggers_from_data_dir_and_parent(risk_settings):
    rm = RiskManager(risk_settings)
    assert rm.is_kill_switch_active() is False

    stop_file = risk_settings.data_dir / "STOP"
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.touch()
    assert rm.is_kill_switch_active() is True
    stop_file.unlink()

    parent_stop = risk_settings.data_dir.parent / "STOP"
    parent_stop.touch()
    assert rm.is_kill_switch_active() is True
    parent_stop.unlink()

def test_exceeds_total_budget_and_max_collateral(risk_settings):
    rm = RiskManager(risk_settings)
    # 6500 @ 0.010 = $65.00 > $60.00 total budget
    quotes = [Quote(slug="mkt-1", side="BUY_LONG", price=Decimal("0.010"), qty=Decimal("6500"))]
    res = rm.check_pre_submit(quotes, [], {}, Decimal("100.00"), Decimal("0"))
    assert res.allowed is False
    assert "Exceeds total budget collateral" in res.reason

def test_daily_loss_stop_blocks_trading(risk_settings):
    rm = RiskManager(risk_settings)
    res = rm.check_pre_submit([], [], {}, Decimal("100.00"), Decimal("10.50"))
    assert res.allowed is False
    assert "Daily realized loss stop reached" in res.reason

def test_exceeds_max_collateral_per_market(risk_settings):
    rm = RiskManager(risk_settings)
    # 3500 @ 0.010 = $35.00 > $30.00 max_collateral_per_market
    quotes = [Quote(slug="mkt-1", side="BUY_LONG", price=Decimal("0.010"), qty=Decimal("3500"))]
    res = rm.check_pre_submit(quotes, [], {}, Decimal("100.00"), Decimal("0"))
    assert res.allowed is False
    assert "Exceeds max collateral per market" in res.reason


def test_sell_long_closing_position_does_not_exceed_contract_cap(tmp_path):
    settings = Settings(
        total_budget=Decimal("200.00"),
        max_collateral=Decimal("200.00"),
        max_collateral_per_market=Decimal("60.00"),
        max_contracts_per_market=400,
        daily_loss_stop=Decimal("10.00"),
        max_open_orders=10,
        data_dir=tmp_path / "data",
    )
    rm = RiskManager(settings)
    q_sell = [Quote(slug="mkt-1", side="SELL_LONG", price=Decimal("0.171"), qty=Decimal("320"))]
    res = rm.check_pre_submit(
        new_quotes=q_sell,
        open_orders=[],
        positions={"mkt-1": Position("mkt-1", Decimal("320"), Decimal("0.171"))},
        buying_power=Decimal("10.00"),
        realized_loss_today=Decimal("0"),
    )
    assert res.allowed is True
