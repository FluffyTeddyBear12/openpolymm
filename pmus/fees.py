from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any

CENT = Decimal("0.01")
FOUR_DECIMALS = Decimal("0.0001")
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

def exact_maker_rebate(
    contracts: Decimal,
    price: Decimal,
    recorded_fee: Decimal = Decimal("0"),
    is_maker: bool = True,
) -> Decimal:
    if not isinstance(recorded_fee, Decimal):
        recorded_fee = Decimal(str(recorded_fee))
    if recorded_fee < Decimal("0"):
        return (-recorded_fee).quantize(FOUR_DECIMALS, rounding=ROUND_HALF_EVEN)
    if not isinstance(contracts, Decimal):
        contracts = Decimal(str(contracts))
    if not isinstance(price, Decimal):
        price = Decimal(str(price))
    if not is_maker or contracts <= Decimal("0") or price <= Decimal("0") or price >= Decimal("1"):
        return Decimal("0.0000")
    raw = MAKER_REBATE_THETA * contracts * price * (Decimal("1") - price)
    return raw.quantize(FOUR_DECIMALS, rounding=ROUND_HALF_EVEN)

def project_maker_rebates(
    fills: list[Any],
    open_orders: list[Any],
    active_daily_lip_usd: Decimal = Decimal("0"),
) -> dict[str, Any]:
    if not isinstance(active_daily_lip_usd, Decimal):
        active_daily_lip_usd = Decimal(str(active_daily_lip_usd))

    current_rebates = sum(
        (
            exact_maker_rebate(
                f.qty,
                f.price,
                getattr(f, "fee", Decimal("0")),
                getattr(f, "is_maker", True),
            )
            for f in fills
        ),
        Decimal("0.0000"),
    )
    maker_fills = [
        f for f in fills
        if getattr(f, "is_maker", False) or getattr(f, "fee", Decimal("0")) < Decimal("0")
    ]
    maker_fills_count = len(maker_fills)
    resting_potential = sum(
        (exact_maker_rebate(o.qty_open, o.price, Decimal("0"), True) for o in open_orders),
        Decimal("0.0000"),
    )

    if maker_fills_count >= 2:
        ts_vals = [int(getattr(f, "ts_ms", 0)) for f in maker_fills if getattr(f, "ts_ms", 0) > 0]
        span_ms = max(ts_vals) - min(ts_vals) if len(ts_vals) >= 2 else 0
        if span_ms >= 3_600_000:
            pace = (current_rebates * Decimal("86400000") / Decimal(str(span_ms))).quantize(
                FOUR_DECIMALS, rounding=ROUND_HALF_EVEN
            )
            projected_daily_rebates = max(pace, resting_potential)
        else:
            projected_daily_rebates = (current_rebates + resting_potential).quantize(
                FOUR_DECIMALS, rounding=ROUND_HALF_EVEN
            )
    else:
        projected_daily_rebates = (current_rebates + resting_potential).quantize(
            FOUR_DECIMALS, rounding=ROUND_HALF_EVEN
        )

    projected_monthly_rebates = (projected_daily_rebates * Decimal("30")).quantize(
        CENT, rounding=ROUND_HALF_EVEN
    )
    projected_daily_profit = (active_daily_lip_usd + projected_daily_rebates).quantize(
        CENT, rounding=ROUND_HALF_EVEN
    )
    projected_monthly_profit = (projected_daily_profit * Decimal("30")).quantize(
        CENT, rounding=ROUND_HALF_EVEN
    )

    return {
        "current_maker_rebates": current_rebates,
        "maker_fills_count": maker_fills_count,
        "resting_rebate_potential": resting_potential,
        "projected_daily_rebates": projected_daily_rebates,
        "projected_monthly_rebates": projected_monthly_rebates,
        "projected_daily_profit": projected_daily_profit,
        "projected_monthly_profit": projected_monthly_profit,
    }
