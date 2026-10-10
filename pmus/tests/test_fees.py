from decimal import Decimal
import pytest

from pmus.fees import taker_fee, maker_rebate, combo_taker_fee, round_cents

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
