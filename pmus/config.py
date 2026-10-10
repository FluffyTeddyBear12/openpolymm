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
    venue: Literal["global", "us"] = "global"
    gateway_url: str = "https://gateway.polymarket.us"
    max_collateral: Decimal = Decimal("70.00")
    max_per_market: Decimal = Decimal("25.00")
    max_collateral_per_market: Decimal = Decimal("30.00")
    total_budget: Decimal = Decimal("60.00")
    max_markets: int = 4
    max_contracts_per_market: int = 6000
    max_open_orders: int = 20
    pre_start_cancel_seconds: int = 300
    quote_live_periods: bool = False
    daily_loss_limit: Decimal = Decimal("5.00")
    daily_loss_stop: Decimal = Decimal("10.00")
    webhook_url: str = ""
    data_dir: Path = Path(__file__).resolve().parent / "data"
    logs_dir: Path = Path(__file__).resolve().parent / "logs"
    polygon_private_key: str = ""
    polymarket_proxy_address: str = ""
    polymarket_signature_type: int = 2
    clob_api_key: str = ""
    clob_secret: str = ""
    clob_passphrase: str = ""
    clob_host: str = "https://clob.polymarket.com"
    chain_id: int = 137

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            import dotenv
            dotenv.load_dotenv()
        except Exception:
            pass

        mode_str = os.getenv("PMUS_MODE", "shadow").strip().lower()
        mode: Literal["shadow", "live"] = "live" if mode_str == "live" else "shadow"

        polygon_private_key = (
            os.getenv("POLYGON_PRIVATE_KEY") or os.getenv("POLYMARKET_PRIVATE_KEY") or ""
        ).strip()
        polymarket_proxy_address = (
            os.getenv("POLYMARKET_PROXY_ADDRESS") or os.getenv("POLYMARKET_ADDRESS") or ""
        ).strip()

        raw_sig = (os.getenv("POLYMARKET_SIGNATURE_TYPE") or "2").strip()
        try:
            polymarket_signature_type = int(raw_sig)
        except ValueError:
            polymarket_signature_type = 2
        if polymarket_signature_type == 3:
            polymarket_signature_type = 2

        clob_api_key = os.getenv("CLOB_API_KEY", "").strip()
        clob_secret = os.getenv("CLOB_SECRET", "").strip()
        clob_passphrase = os.getenv("CLOB_PASSPHRASE", "").strip()
        clob_host = (
            os.getenv("CLOB_HOST") or os.getenv("POLYMARKET_HOST") or "https://clob.polymarket.com"
        ).strip()

        raw_chain = (os.getenv("POLYMARKET_CHAIN_ID") or "137").strip()
        try:
            chain_id = int(raw_chain)
        except ValueError:
            chain_id = 137

        venue_env = (os.getenv("PMUS_VENUE") or os.getenv("VENUE", "")).strip().lower()
        if venue_env in ("global", "us"):
            venue: Literal["global", "us"] = venue_env  # type: ignore
        elif polygon_private_key:
            venue = "global"
        elif os.getenv("PMUS_KEY_ID", "").strip():
            venue = "us"
        else:
            venue = "global"

        webhook_url = (
            os.getenv("HOMEASSISTANT_CLOUDHOOK_URL") or os.getenv("PMUS_WEBHOOK_URL") or ""
        ).strip()

        return cls(
            key_id=os.getenv("PMUS_KEY_ID", "").strip(),
            secret_key=os.getenv("PMUS_SECRET_KEY", "").strip(),
            mode=mode,
            venue=venue,
            gateway_url=os.getenv("PMUS_GATEWAY_URL", "https://gateway.polymarket.us").strip(),
            max_collateral=Decimal(os.getenv("PMUS_MAX_COLLATERAL", "70.00")),
            max_per_market=Decimal(os.getenv("PMUS_MAX_PER_MARKET", "25.00")),
            max_collateral_per_market=Decimal(os.getenv("PMUS_MAX_COLLATERAL_PER_MARKET", "30.00")),
            total_budget=Decimal(os.getenv("PMUS_TOTAL_BUDGET", "60.00")),
            max_markets=int(os.getenv("PMUS_MAX_MARKETS", "4")),
            max_contracts_per_market=int(os.getenv("PMUS_MAX_CONTRACTS_PER_MARKET", "6000")),
            max_open_orders=int(os.getenv("PMUS_MAX_OPEN_ORDERS", "20")),
            pre_start_cancel_seconds=int(os.getenv("PMUS_PRE_START_CANCEL_SECONDS", "300")),
            quote_live_periods=os.getenv("PMUS_QUOTE_LIVE_PERIODS", "false").strip().lower() in ("true", "1"),
            daily_loss_limit=Decimal(os.getenv("PMUS_DAILY_LOSS_LIMIT", "5.00")),
            daily_loss_stop=Decimal(os.getenv("PMUS_DAILY_LOSS_STOP", "10.00")),
            webhook_url=webhook_url,
            data_dir=Path(os.getenv("PMUS_DATA_DIR", str(Path(__file__).resolve().parent / "data"))),
            logs_dir=Path(os.getenv("PMUS_LOGS_DIR", str(Path(__file__).resolve().parent / "logs"))),
            polygon_private_key=polygon_private_key,
            polymarket_proxy_address=polymarket_proxy_address,
            polymarket_signature_type=polymarket_signature_type,
            clob_api_key=clob_api_key,
            clob_secret=clob_secret,
            clob_passphrase=clob_passphrase,
            clob_host=clob_host,
            chain_id=chain_id,
        )
