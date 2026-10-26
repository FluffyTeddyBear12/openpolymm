from datetime import datetime, timezone
from decimal import Decimal
import logging
import re
import threading
from typing import Any
from urllib.parse import urlsplit

import requests

from pmus.config import Settings

logger = logging.getLogger("pmus.notify")


class Notifier:
    def __init__(self, settings: Settings, timeout: float = 5.0):
        self.webhook_url = settings.webhook_url
        self.timeout = timeout
        self._semaphore = threading.BoundedSemaphore(4)

    def close(self) -> None:
        return None

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

        def _dispatch() -> None:
            with self._semaphore:
                try:
                    requests.post(self.webhook_url, json=payload, timeout=self.timeout)
                except Exception as exc:
                    redacted_msg = str(exc)
                    if self.webhook_url:
                        redacted_msg = redacted_msg.replace(self.webhook_url, "[REDACTED_URL]")
                    redacted_msg = re.sub(r"https?://\S+", "[REDACTED_URL]", redacted_msg)
                    if self.webhook_url:
                        parsed = urlsplit(self.webhook_url)
                        if parsed.path and parsed.path != "/":
                            redacted_msg = redacted_msg.replace(parsed.path, "[REDACTED_URL]")
                        if parsed.query:
                            redacted_msg = redacted_msg.replace(parsed.query, "[REDACTED_QUERY]")
                    logger.warning(
                        "Failed to dispatch webhook notification (%s): %s",
                        type(exc).__name__,
                        redacted_msg,
                    )

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
        *,
        engine_state: str = "QUOTING",
        open_orders_count: int | None = None,
        realized_pnl_today: Decimal = Decimal("0"),
        unrealized_pnl: Decimal = Decimal("0"),
        mtm_equity: Decimal | None = None,
    ) -> threading.Thread | None:
        title = "Hourly Status Ping"
        net_est_today = accrued_usd_today + realized_pnl_today

        parts = [f"State: {engine_state}"]
        if open_orders_count is not None:
            parts.append(f"Orders: {open_orders_count}")
        parts.extend(
            [
                f"Active: {open_pairs} pairs",
                f"1h LIP: ${accrued_usd_1h:.4f} ({qualifying_sec_1h}s qual)",
                f"Today LIP: ${accrued_usd_today:.2f} (Proj 24h: ${projected_payout_today:.2f})",
                f"Realized PnL: ${realized_pnl_today:+.2f}",
                f"Net Est: ${net_est_today:+.2f}",
                f"Unrealized: ${unrealized_pnl:+.2f}",
            ]
        )
        if mtm_equity is not None:
            parts.append(f"Equity: ${mtm_equity:.2f}")
        parts.append(f"Cash: ${buying_power:.2f}")
        msg = " | ".join(parts)

        level = "warning" if (engine_state != "QUOTING" or net_est_today < Decimal("0")) else "info"
        combined_extra: dict[str, Any] = {
            "qualifying_sec_1h": qualifying_sec_1h,
            "accrued_usd_1h": str(accrued_usd_1h),
            "qualifying_sec_today": qualifying_sec_today,
            "accrued_usd_today": str(accrued_usd_today),
            "projected_payout_today": str(projected_payout_today),
            "open_pairs": open_pairs,
            "buying_power": str(buying_power),
            "engine_state": engine_state,
            "open_orders_count": open_orders_count,
            "realized_pnl_today": str(realized_pnl_today),
            "net_est_today": str(net_est_today),
            "unrealized_pnl": str(unrealized_pnl),
            "mtm_equity": str(mtm_equity) if mtm_equity is not None else None,
        }
        if extra:
            combined_extra.update(extra)
        return self.send(title=title, message=msg, level=level, extra=combined_extra)
