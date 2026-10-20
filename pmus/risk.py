from datetime import datetime, time as dtime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from pmus.config import Settings
from pmus.models import LiveOrder, Position, Quote

class RiskGateResult:
    def __init__(self, allowed: bool, reason: str = ""):
        self.allowed = allowed
        self.reason = reason

class RiskManager:
    def __init__(self, settings: Settings, stop_file_path: Path | None = None):
        self.settings = settings
        self.stop_file = stop_file_path or (settings.data_dir.parent / "STOP")
        self.dynamic_total_budget: Decimal | None = None
        self.dynamic_max_collateral: Decimal | None = None
        self.dynamic_max_collateral_per_market: Decimal | None = None
        self.dynamic_daily_loss_stop: Decimal | None = None

    def update_dynamic_limits(
        self,
        total_budget: Decimal | None = None,
        max_collateral: Decimal | None = None,
        max_collateral_per_market: Decimal | None = None,
        daily_loss_stop: Decimal | None = None,
    ) -> None:
        if total_budget is not None:
            self.dynamic_total_budget = total_budget
        if max_collateral is not None:
            self.dynamic_max_collateral = max_collateral
        if max_collateral_per_market is not None:
            self.dynamic_max_collateral_per_market = max_collateral_per_market
        if daily_loss_stop is not None:
            self.dynamic_daily_loss_stop = daily_loss_stop

    def is_maintenance_active(self, now: datetime | None = None) -> bool:
        if now is None:
            now = datetime.now(ZoneInfo("America/New_York"))
        else:
            now = now.astimezone(ZoneInfo("America/New_York"))
        if now.weekday() == 3:
            t = now.time()
            if dtime(5, 55) <= t < dtime(8, 5):
                return True
        return False

    def is_kill_switch_active(self) -> bool:
        if self.stop_file and self.stop_file.exists():
            return True
        data_stop = self.settings.data_dir / "STOP"
        parent_stop = self.settings.data_dir.parent / "STOP"
        return data_stop.exists() or parent_stop.exists()

    def check_daily_loss(self, realized_loss_today: Decimal) -> RiskGateResult:
        loss_limit = (
            self.dynamic_daily_loss_stop
            if self.dynamic_daily_loss_stop is not None
            else self.settings.daily_loss_stop
        )
        if realized_loss_today >= loss_limit:
            return RiskGateResult(
                False,
                f"Daily realized loss stop reached: ${realized_loss_today} >= ${loss_limit}",
            )
        return RiskGateResult(True)

    def check_pre_submit(
        self,
        new_quotes: list[Quote],
        open_orders: list[LiveOrder],
        positions: dict[str, Position],
        buying_power: Decimal,
        realized_loss_today: Decimal,
        now: datetime | None = None,
    ) -> RiskGateResult:
        if self.is_kill_switch_active():
            return RiskGateResult(False, "Kill switch active (STOP file present)")

        if self.is_maintenance_active(now):
            return RiskGateResult(False, "Exchange weekly maintenance window active (Thursday 05:55-08:05 ET)")

        loss_res = self.check_daily_loss(realized_loss_today)
        if not loss_res.allowed:
            return loss_res

        total_orders_count = len(open_orders) + len(new_quotes)
        if total_orders_count > self.settings.max_open_orders:
            return RiskGateResult(
                False,
                f"Exceeds max open orders: {total_orders_count} > {self.settings.max_open_orders}",
            )

        total_collateral = Decimal("0")
        market_collateral: dict[str, Decimal] = {}

        for o in open_orders:
            px = o.price
            if o.side in ("BUY_LONG", "BUY_SHORT"):
                col = px
            elif o.side == "SELL_LONG":
                col = Decimal("0")
            else:
                col = Decimal("1") - px
            order_col = col * o.qty_open
            total_collateral += order_col
            market_collateral[o.slug] = market_collateral.get(o.slug, Decimal("0")) + order_col

        new_collateral = Decimal("0")
        for q in new_quotes:
            px = q.price
            if q.side in ("BUY_LONG", "BUY_SHORT"):
                col = px
            elif q.side == "SELL_LONG":
                col = Decimal("0")
            else:
                col = Decimal("1") - px
            quote_col = col * q.qty
            new_collateral += quote_col
            total_collateral += quote_col
            market_collateral[q.slug] = market_collateral.get(q.slug, Decimal("0")) + quote_col

        if new_collateral > Decimal("0"):
            budget_limit = (
                self.dynamic_total_budget
                if self.dynamic_total_budget is not None
                else self.settings.total_budget
            )
            if budget_limit <= Decimal("0"):
                budget_limit = buying_power
            if total_collateral > budget_limit:
                return RiskGateResult(
                    False,
                    f"Exceeds total budget collateral: ${total_collateral} > ${budget_limit}",
                )

            collateral_limit = (
                self.dynamic_max_collateral
                if self.dynamic_max_collateral is not None
                else self.settings.max_collateral
            )
            if collateral_limit <= Decimal("0"):
                collateral_limit = buying_power
            if total_collateral > collateral_limit:
                return RiskGateResult(
                    False,
                    f"Exceeds max collateral: ${total_collateral} > ${collateral_limit}",
                )
            if total_collateral > buying_power:
                return RiskGateResult(
                    False,
                    f"Exceeds account buying power: ${total_collateral} > ${buying_power}",
                )

        max_market_col = (
            self.dynamic_max_collateral_per_market
            if self.dynamic_max_collateral_per_market is not None
            else getattr(self.settings, "max_collateral_per_market", None)
        )
        if max_market_col is not None:
            buy_quote_slugs = {q.slug for q in new_quotes if q.side in ("BUY_LONG", "BUY_SHORT")}
            for slug, m_col in market_collateral.items():
                pos = positions.get(slug)
                pos_cost = (
                    abs(pos.net_qty) * pos.avg_price
                    if (slug in buy_quote_slugs and pos is not None and pos.avg_price > Decimal("0"))
                    else Decimal("0")
                )
                if (m_col + pos_cost) > max_market_col:
                    return RiskGateResult(
                        False,
                        f"Exceeds max collateral per market for {slug}: ${m_col + pos_cost} > ${max_market_col}",
                    )

        for q in new_quotes:
            if q.side == "SELL_LONG":
                pos = positions.get(q.slug, Position(q.slug, Decimal("0"), Decimal("0")))
                resting_sell = sum(
                    o.qty_open for o in open_orders
                    if o.slug == q.slug and o.side in ("SELL", "SELL_LONG")
                )
                new_sell = sum(
                    n.qty for n in new_quotes
                    if n.slug == q.slug and n.side == "SELL_LONG"
                )
                if pos.net_qty <= Decimal("0") or (resting_sell + new_sell) > pos.net_qty:
                    return RiskGateResult(
                        False,
                        f"SELL_LONG exceeds held inventory for {q.slug}: {resting_sell + new_sell} > {pos.net_qty}",
                    )

        for q in new_quotes:
            pos = positions.get(q.slug, Position(q.slug, Decimal("0"), Decimal("0")))
            if q.side == "SELL_LONG" and pos.net_qty > Decimal("0"):
                projected_qty = abs(pos.net_qty - q.qty)
            elif q.side == "BUY_SHORT" and pos.net_qty < Decimal("0"):
                projected_qty = abs(pos.net_qty + q.qty)
            else:
                projected_qty = abs(pos.net_qty) + q.qty
            if projected_qty > self.settings.max_contracts_per_market:
                return RiskGateResult(
                    False,
                    f"Exceeds per-market contract cap for {q.slug}",
                )

        return RiskGateResult(True)
