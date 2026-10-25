import logging
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import requests

from pmus.config import Settings

logger = logging.getLogger("pmus.notify")

class Notifier:
    def __init__(self, settings: Settings, timeout: float = 5.0):
        self.webhook_url = settings.webhook_url
        self.timeout = timeout

    @staticmethod
    def format_payload(
        title: str,
        message: str,
        level: str = "info",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "title": title,
            "message": message,
            "level": level,
            "text": f"[{level.upper()}] {title}: {message}",
            "content": f"[{level.upper()}] {title}: {message}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "extra": extra or {},
        }

    def send(
        self,
        title: str,
        message: str,
        level: str = "info",
        extra: dict[str, Any] | None = None,
    ) -> threading.Thread | None:
        if not self.webhook_url:
            return None

        payload = self.format_payload(title=title, message=message, level=level, extra=extra)

        def _dispatch():
            try:
                requests.post(self.webhook_url, json=payload, timeout=self.timeout)
            except Exception as exc:
                logger.warning("Failed to dispatch webhook notification: %s", exc)

        thread = threading.Thread(target=_dispatch, daemon=True)
        thread.start()
        return thread

    def notify_fill(self, fill: Any) -> threading.Thread | None:
        title = "Order Fill"
        msg = f"Filled {fill.qty} of {fill.slug} ({fill.side}) @ ${fill.price}"
        extra = {
            "fill_id": str(getattr(fill, "fill_id", "")),
            "order_id": str(getattr(fill, "order_id", "")),
            "slug": str(getattr(fill, "slug", "")),
            "side": str(getattr(fill, "side", "")),
            "price": str(getattr(fill, "price", "")),
            "qty": str(getattr(fill, "qty", "")),
            "fee": str(getattr(fill, "fee", "0")),
        }
        return self.send(title=title, message=msg, level="info", extra=extra)

    def notify_daily_loss_stop(self, loss: Decimal, stop: Decimal) -> threading.Thread | None:
        title = "Daily Loss Stop Tripped"
        msg = f"Daily realized loss stop reached: ${loss:.2f} >= ${stop:.2f}. Trading halted."
        extra = {
            "loss": str(loss),
            "stop": str(stop),
        }
        return self.send(title=title, message=msg, level="error", extra=extra)

    def notify_kill_switch(self, active: bool) -> threading.Thread | None:
        if active:
            title = "Kill Switch Activated"
            msg = "Kill switch STOP file detected. All orders canceled and trading halted."
            level = "warning"
        else:
            title = "Kill Switch Deactivated"
            msg = "Kill switch STOP file removed. Normal trading resumed."
            level = "info"
        extra = {"active": active}
        return self.send(title=title, message=msg, level=level, extra=extra)

    def notify_hourly_status(
        self,
        qualifying_sec_1h: int,
        accrued_usd_1h: Decimal,
        qualifying_sec_today: int,
        accrued_usd_today: Decimal,
        projected_payout_today: Decimal,
        open_pairs: int,
        buying_power: Decimal,
        extra: dict[str, Any] | None = None,
    ) -> threading.Thread | None:
        title = "Hourly Status Ping"
        msg = (
            f"1h Accrual: ${accrued_usd_1h:.4f} ({qualifying_sec_1h}s qual) | "
            f"Today: ${accrued_usd_today:.2f} (Proj 24h: ${projected_payout_today:.2f}) | "
            f"Active: {open_pairs} pairs | Cash: ${buying_power:.2f}"
        )
        combined_extra = {
            "qualifying_sec_1h": qualifying_sec_1h,
            "accrued_usd_1h": str(accrued_usd_1h),
            "qualifying_sec_today": qualifying_sec_today,
            "accrued_usd_today": str(accrued_usd_today),
            "projected_payout_today": str(projected_payout_today),
            "open_pairs": open_pairs,
            "buying_power": str(buying_power),
        }
        if extra:
            combined_extra.update(extra)
        return self.send(title=title, message=msg, level="info", extra=combined_extra)
