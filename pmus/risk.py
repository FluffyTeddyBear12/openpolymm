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
        return self.stop_file.exists()

    def check_daily_loss(self, realized_loss_today: Decimal) -> RiskGateResult:
        if realized_loss_today >= self.settings.daily_loss_stop:
            return RiskGateResult(
                False,
                f"Daily realized loss stop reached: ${realized_loss_today} >= ${self.settings.daily_loss_stop}",
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
        for o in open_orders:
            px = o.price
            col = px if o.side in ("BUY_LONG", "BUY_SHORT") else (Decimal("1") - px)
            total_collateral += col * o.qty_open
        for q in new_quotes:
            px = q.price
            col = px if q.side in ("BUY_LONG", "BUY_SHORT") else (Decimal("1") - px)
            total_collateral += col * q.qty

        if total_collateral > self.settings.total_budget:
            return RiskGateResult(
                False,
                f"Exceeds total budget collateral: ${total_collateral} > ${self.settings.total_budget}",
            )
        if total_collateral > buying_power:
            return RiskGateResult(
                False,
                f"Exceeds account buying power: ${total_collateral} > ${buying_power}",
            )

        for q in new_quotes:
            pos = positions.get(q.slug, Position(q.slug, Decimal("0"), Decimal("0")))
            if abs(pos.net_qty) + q.qty > self.settings.max_contracts_per_market:
                return RiskGateResult(
                    False,
                    f"Exceeds per-market contract cap for {q.slug}",
                )

        return RiskGateResult(True)
