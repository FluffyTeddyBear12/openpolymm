from decimal import Decimal
from types import SimpleNamespace
import pytest

from pmus.fees import (
    taker_fee,
    maker_rebate,
    combo_taker_fee,
    round_cents,
    exact_maker_rebate,
    project_maker_rebates,
    FOUR_DECIMALS,
    CENT,
)

@pytest.mark.parametrize(
    "contracts,price,expected_taker,expected_maker",
    [
        (Decimal("1000"), Decimal("0.10"), Decimal("6.26"), Decimal("1.12")),
        (Decimal("1000"), Decimal("0.65"), Decimal("15.81"), Decimal("2.84")),
        (Decimal("1000"), Decimal("0.30"), Decimal("14.60"), Decimal("2.62")),
        (Decimal("1000"), Decimal("0.90"), Decimal("6.26"), Decimal("1.12")),
        (Decimal("1000"), Decimal("0.50"), Decimal("17.38"), Decimal("3.12")),
    ],
)
def test_standard_examples_from_docs(contracts, price, expected_taker, expected_maker):
    assert taker_fee(contracts, price) == expected_taker
    assert maker_rebate(contracts, price) == expected_maker

def test_table_tennis_example_from_docs():
    assert taker_fee(Decimal("1000"), Decimal("0.50"), is_table_tennis=True) == Decimal("25.00")
    assert maker_rebate(Decimal("1000"), Decimal("0.50")) == Decimal("3.12")

@pytest.mark.parametrize(
    "contracts,price,expected_combo_taker",
    [
        (Decimal("1000"), Decimal("0.10"), Decimal("10.19")),
        (Decimal("1000"), Decimal("0.50"), Decimal("19.25")),
    ],
)
def test_combo_examples_from_docs(contracts, price, expected_combo_taker):
    assert combo_taker_fee(contracts, price) == expected_combo_taker

@pytest.mark.parametrize(
    "price,expected_taker_100,expected_maker_100",
    [
        (Decimal("0.01"), Decimal("0.07"), Decimal("0.01")),
        (Decimal("0.05"), Decimal("0.33"), Decimal("0.06")),
        (Decimal("0.10"), Decimal("0.63"), Decimal("0.11")),
        (Decimal("0.25"), Decimal("1.30"), Decimal("0.23")),
        (Decimal("0.50"), Decimal("1.74"), Decimal("0.31")),
        (Decimal("0.75"), Decimal("1.30"), Decimal("0.23")),
        (Decimal("0.90"), Decimal("0.63"), Decimal("0.11")),
        (Decimal("0.99"), Decimal("0.07"), Decimal("0.01")),
    ],
)
def test_standard_schedule_100_lot(price, expected_taker_100, expected_maker_100):
    c = Decimal("100")
    assert taker_fee(c, price) == expected_taker_100
    assert maker_rebate(c, price) == expected_maker_100

def test_bankers_rounding():
    assert round_cents(Decimal("0.025")) == Decimal("0.02")
    assert round_cents(Decimal("0.035")) == Decimal("0.04")

def test_exact_maker_rebate_global_and_recorded_fee():
    assert exact_maker_rebate(Decimal("1000"), Decimal("0.50")) == Decimal("3.1250")
    assert exact_maker_rebate(Decimal("1000"), Decimal("0.10")) == Decimal("1.1250")
    assert exact_maker_rebate(Decimal("100"), Decimal("0.25")) == Decimal("0.2344")

    assert exact_maker_rebate(Decimal("1000"), Decimal("0.50"), recorded_fee=Decimal("-1.23456")) == Decimal("1.2346")
    assert exact_maker_rebate(Decimal("0"), Decimal("0"), recorded_fee=Decimal("-0.50")) == Decimal("0.5000")

    assert exact_maker_rebate(Decimal("1000"), Decimal("0.50"), is_maker=False) == Decimal("0.0000")
    assert exact_maker_rebate(Decimal("0"), Decimal("0.50")) == Decimal("0.0000")
    assert exact_maker_rebate(Decimal("-10"), Decimal("0.50")) == Decimal("0.0000")
    assert exact_maker_rebate(Decimal("1000"), Decimal("0")) == Decimal("0.0000")
    assert exact_maker_rebate(Decimal("1000"), Decimal("1")) == Decimal("0.0000")
    assert exact_maker_rebate(Decimal("1000"), Decimal("1.05")) == Decimal("0.0000")

def test_project_maker_rebates_combines_resting_and_lip():
    orders = [
        SimpleNamespace(qty_open=Decimal("1000"), price=Decimal("0.50")),
        SimpleNamespace(qty_open=Decimal("1000"), price=Decimal("0.10")),
    ]
    res_resting = project_maker_rebates([], orders, active_daily_lip_usd=Decimal("10.00"))
    assert res_resting["current_maker_rebates"] == Decimal("0.0000")
    assert res_resting["maker_fills_count"] == 0
    assert res_resting["resting_rebate_potential"] == Decimal("4.2500")
    assert res_resting["projected_daily_rebates"] == Decimal("4.2500")
    assert res_resting["projected_monthly_rebates"] == Decimal("127.50")
    assert res_resting["projected_daily_profit"] == Decimal("14.25")
    assert res_resting["projected_monthly_profit"] == Decimal("427.50")

    fills = [
        SimpleNamespace(qty=Decimal("1000"), price=Decimal("0.50"), fee=Decimal("0"), is_maker=True, ts_ms=10_000_000),
        SimpleNamespace(qty=Decimal("1000"), price=Decimal("0.50"), fee=Decimal("0"), is_maker=True, ts_ms=17_200_000),
    ]
    res_paced = project_maker_rebates(fills, orders, active_daily_lip_usd=Decimal("25.00"))
    assert res_paced["current_maker_rebates"] == Decimal("6.2500")
    assert res_paced["maker_fills_count"] == 2
    assert res_paced["resting_rebate_potential"] == Decimal("4.2500")
    assert res_paced["projected_daily_rebates"] == Decimal("75.0000")
    assert res_paced["projected_monthly_rebates"] == Decimal("2250.00")
    assert res_paced["projected_daily_profit"] == Decimal("100.00")
    assert res_paced["projected_monthly_profit"] == Decimal("3000.00")
