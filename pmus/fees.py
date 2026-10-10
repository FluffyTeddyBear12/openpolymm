from decimal import Decimal, ROUND_HALF_EVEN

CENT = Decimal("0.01")
STANDARD_TAKER_THETA = Decimal("0.0695")
TABLE_TENNIS_TAKER_THETA = Decimal("0.10")
MAKER_REBATE_THETA = Decimal("0.0125")

def round_cents(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_EVEN)

def taker_fee(contracts: Decimal, price: Decimal, is_table_tennis: bool = False) -> Decimal:
    theta = TABLE_TENNIS_TAKER_THETA if is_table_tennis else STANDARD_TAKER_THETA
    raw = theta * contracts * price * (Decimal("1") - price)
    return round_cents(raw)

def maker_rebate(contracts: Decimal, price: Decimal) -> Decimal:
    raw = MAKER_REBATE_THETA * contracts * price * (Decimal("1") - price)
    return round_cents(raw)

def combo_taker_fee(contracts: Decimal, price: Decimal) -> Decimal:
    one_minus_p = Decimal("1") - price
    bracket = STANDARD_TAKER_THETA * one_minus_p + Decimal("0.06") * (one_minus_p ** 4)
    raw = contracts * price * bracket
    return round_cents(raw)
