import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

@dataclass(frozen=True)
class Settings:
    key_id: str = ""
    secret_key: str = ""
    mode: Literal["shadow", "live"] = "shadow"
    gateway_url: str = "https://gateway.polymarket.us"
    max_collateral: Decimal = Decimal("70.00")
    max_per_market: Decimal = Decimal("25.00")
    max_open_orders: int = 20
    daily_loss_limit: Decimal = Decimal("5.00")
    data_dir: Path = Path(__file__).resolve().parent / "data"
    logs_dir: Path = Path(__file__).resolve().parent / "logs"

    @classmethod
    def from_env(cls) -> "Settings":
        mode_str = os.getenv("PMUS_MODE", "shadow").strip().lower()
        mode: Literal["shadow", "live"] = "live" if mode_str == "live" else "shadow"
        return cls(
            key_id=os.getenv("PMUS_KEY_ID", "").strip(),
            secret_key=os.getenv("PMUS_SECRET_KEY", "").strip(),
            mode=mode,
            gateway_url=os.getenv("PMUS_GATEWAY_URL", "https://gateway.polymarket.us").strip(),
            max_collateral=Decimal(os.getenv("PMUS_MAX_COLLATERAL", "70.00")),
            max_per_market=Decimal(os.getenv("PMUS_MAX_PER_MARKET", "25.00")),
            max_open_orders=int(os.getenv("PMUS_MAX_OPEN_ORDERS", "20")),
            daily_loss_limit=Decimal(os.getenv("PMUS_DAILY_LOSS_LIMIT", "5.00")),
            data_dir=Path(os.getenv("PMUS_DATA_DIR", str(Path(__file__).resolve().parent / "data"))),
            logs_dir=Path(os.getenv("PMUS_LOGS_DIR", str(Path(__file__).resolve().parent / "logs"))),
        )
