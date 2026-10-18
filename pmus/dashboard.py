from datetime import datetime, timezone
from decimal import Decimal
import html
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any

# Ensure repository root is on sys.path
_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import pandas as pd
import requests
import streamlit as st

from pmus.config import Settings
from pmus.ledger import Ledger
from pmus.models import LipSnapshot, LiveOrder, Position, ShadowFill

_TITLE_CACHE: dict[str, str] = {}

DEFAULT_RESET_EPOCH_MS = 1792281600000  # 2026-10-18T00:00:00Z (Oct 17, 7:00 PM CDT)
FRESH_START_TODAY_0700_UTC_MS = 1792308000000  # 2026-10-18T07:00:00Z (Post-manual sells fresh baseline)


def get_effective_reset_epoch_ms(settings: Settings | None = None) -> int:
    if settings is not None:
        try:
            reset_file = settings.data_dir / "dashboard_reset.json"
            if reset_file.exists():
                data = json.loads(reset_file.read_text(encoding="utf-8"))
                val = int(data.get("reset_epoch_ms", 0))
                if val > 0:
                    return val
        except Exception:
            pass
    return DEFAULT_RESET_EPOCH_MS


def set_custom_reset_epoch_ms(settings: Settings, ts_ms: int) -> None:
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        reset_file = settings.data_dir / "dashboard_reset.json"
        reset_file.write_text(json.dumps({"reset_epoch_ms": int(ts_ms)}), encoding="utf-8")
    except Exception:
        pass


def check_password(settings: Settings) -> bool:
    if not settings.dashboard_password:
        return True
    if st.session_state.get("authenticated", False):
        return True

    st.title("PMUS Trading HUD")
    st.subheader("Restricted Access - Authentication Required")
    with st.form("login_form"):
        pwd = st.text_input("Dashboard Password", type="password", key="login_pwd")
        if st.form_submit_button("Unlock Dashboard"):
            if pwd == settings.dashboard_password or (pwd.strip() and pwd.strip() == settings.dashboard_password.strip()):
                st.session_state["authenticated"] = True
                st.rerun()
            elif pwd:
                st.error("Invalid password")
    return False


def resolve_market_title(slug: str, fetch: bool = False) -> str:
    if not slug:
        return ""
    if slug in _TITLE_CACHE:
        cached = _TITLE_CACHE[slug]
        if not fetch or "..." not in cached:
            return cached

    short_slug = f"{slug[:8]}...{slug[-6:]}" if len(slug) > 14 else slug
    if not fetch:
        return short_slug

    try:
        url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={slug}"
        resp = requests.get(url, timeout=0.5)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0 and "question" in data[0]:
                q = str(data[0]["question"]).strip()
                if q:
                    _TITLE_CACHE[slug] = q
                    return q
    except Exception:
        pass

    _TITLE_CACHE[slug] = short_slug
    return short_slug


def is_database_empty(ledger: Ledger) -> bool:
    if not ledger.db_path.exists():
        return True
    try:
        with ledger._get_connection() as conn:
            cur = conn.cursor()
            for tbl in ("lip_snapshots", "orders", "fills", "quotes"):
                try:
                    res = cur.execute(f"SELECT 1 FROM {tbl} LIMIT 1").fetchone()
                    if res is not None:
                        return False
                except sqlite3.OperationalError:
                    continue
        return True
    except Exception:
        return True


def _fill_maker_rebate(f: ShadowFill, is_global: bool = False) -> Decimal:
    if f.fee < Decimal("0"):
        return -f.fee
    if (
        not is_global
        and f.is_maker
        and f.fee == Decimal("0")
        and Decimal("0") < f.price < Decimal("1")
        and f.qty > Decimal("0")
    ):
        return (Decimal("0.0125") * f.qty * f.price * (Decimal("1") - f.price)).quantize(Decimal("0.0001"))
    return Decimal("0")


def compute_kpis(
    ledger: Ledger,
    since_ms: int = 0,
    buying_power: Decimal | None = None,
    max_collateral: Decimal | None = None,
    is_global: bool = False,
    fills: list[ShadowFill] | None = None,
    held_positions_usd: Decimal = Decimal("0"),
    official_rewards: Decimal | None = None,
) -> dict[str, Any]:
    if fills is None:
        fills = ledger.get_fills_since(since_ms)
    lip_stats = ledger.get_lip_stats(since_ms)

    total_volume = Decimal("0")
    maker_rebates = Decimal("0")
    taker_fees = Decimal("0")
    realized_pnl = Decimal("0")

    for f in fills:
        total_volume += (f.price * f.qty)
        reb = _fill_maker_rebate(f, is_global=is_global)
        if reb > Decimal("0"):
            maker_rebates += reb
        elif f.fee > Decimal("0"):
            taker_fees += f.fee

    market_fills: dict[str, list[ShadowFill]] = {}
    for f in fills:
        market_fills.setdefault(f.slug, []).append(f)

    for _, m_fills in market_fills.items():
        pos_qty = Decimal("0")
        avg_px = Decimal("0")
        for f in m_fills:
            if f.side in ("BUY_LONG", "BUY_SHORT"):
                if pos_qty >= Decimal("0"):
                    new_qty = pos_qty + f.qty
                    if new_qty > Decimal("0"):
                        avg_px = (pos_qty * avg_px + f.qty * f.price) / new_qty
                    pos_qty = new_qty
                else:
                    close_qty = min(f.qty, abs(pos_qty))
                    realized_pnl += close_qty * (avg_px - f.price)
                    pos_qty += f.qty
                    if pos_qty > Decimal("0"):
                        avg_px = f.price
            else:
                if pos_qty <= Decimal("0"):
                    new_qty = abs(pos_qty) + f.qty
                    if new_qty > Decimal("0"):
                        avg_px = (abs(pos_qty) * avg_px + f.qty * f.price) / new_qty
                    pos_qty = -new_qty
                else:
                    close_qty = min(f.qty, pos_qty)
                    realized_pnl += close_qty * (f.price - avg_px)
                    pos_qty -= f.qty
                    if pos_qty < Decimal("0"):
                        avg_px = f.price

    lip_reward_total = lip_stats["lip_reward_total"]
    qualifying_seconds = lip_stats["qualifying_seconds"]
    qualifying_bid_seconds = lip_stats["qualifying_bid_seconds"]
    qualifying_ask_seconds = lip_stats["qualifying_ask_seconds"]

    effective_balance = buying_power if buying_power is not None else Decimal("0")
    if max_collateral is not None and effective_balance > Decimal("0") and max_collateral > effective_balance:
        collateral_cap = effective_balance
    else:
        collateral_cap = max_collateral if max_collateral is not None else Decimal("70.00")

    portfolio_value_usd = effective_balance + held_positions_usd
    active_lip_rewards = (
        official_rewards
        if (official_rewards is not None and official_rewards > Decimal("0"))
        else lip_reward_total
    )
    net_pnl = realized_pnl + maker_rebates + lip_reward_total - taker_fees
    total_exit_value_usd = portfolio_value_usd + active_lip_rewards + maker_rebates + realized_pnl - taker_fees

    return {
        "balance_usd": effective_balance,
        "held_positions_usd": held_positions_usd,
        "portfolio_value_usd": portfolio_value_usd,
        "collateral_cap_usd": collateral_cap,
        "lip_reward_total": lip_reward_total,
        "active_lip_rewards": active_lip_rewards,
        "qualifying_seconds": qualifying_seconds,
        "qualifying_bid_seconds": qualifying_bid_seconds,
        "qualifying_ask_seconds": qualifying_ask_seconds,
        "total_fills": len(fills),
        "total_volume": total_volume,
        "realized_pnl": realized_pnl,
        "maker_rebates": maker_rebates,
        "taker_fees": taker_fees,
        "net_pnl": net_pnl,
        "total_exit_value_usd": total_exit_value_usd,
    }


def format_market_overview(ledger: Ledger, recent_ms: int = 0) -> list[dict[str, Any]]:
    open_orders = ledger.get_open_orders()
    latest_snaps = ledger.get_latest_lip_snapshots()
    if recent_ms > 0:
        latest_snaps = {k: s for k, s in latest_snaps.items() if s.ts_ms >= recent_ms}

    all_slugs = sorted(list({o.slug for o in open_orders} | set(latest_snaps.keys())))
    rows: list[dict[str, Any]] = []

    for slug in all_slugs:
        slug_orders = [o for o in open_orders if o.slug == slug]
        bids = [o for o in slug_orders if o.side.startswith("BUY")]
        asks = [o for o in slug_orders if o.side.startswith("SELL")]

        best_bid = max((b.price for b in bids), default=None)
        total_bid_qty = sum((b.qty_open for b in bids), Decimal("0"))
        best_ask = min((a.price for a in asks), default=None)
        total_ask_qty = sum((a.qty_open for a in asks), Decimal("0"))

        snap = latest_snaps.get(slug)
        is_quoting = bool(bids or asks)
        status = "QUOTING" if is_quoting else "MONITORING"
        title = resolve_market_title(slug)

        rows.append({
            "slug": slug,
            "title": title,
            "status": status,
            "bid_count": len(bids),
            "best_bid": best_bid,
            "bid_qty": total_bid_qty,
            "ask_count": len(asks),
            "best_ask": best_ask,
            "ask_qty": total_ask_qty,
            "qualifies": (snap.qualifies if snap else False) if is_quoting else False,
            "bid_qualifies": (snap.bid_qualifies if snap else False) if is_quoting else False,
            "ask_qualifies": (snap.ask_qualifies if snap else False) if is_quoting else False,
            "target_pool_size": snap.total_bid_score if snap else Decimal("0"),
            "trader_bid_score": (snap.trader_bid_score if snap else Decimal("0")) if is_quoting else Decimal("0"),
            "trader_ask_score": (snap.trader_ask_score if snap else Decimal("0")) if is_quoting else Decimal("0"),
            "est_reward_rate": (snap.reward_usd if snap else Decimal("0")) if is_quoting else Decimal("0"),
            "program_id": snap.program_id if snap else "",
            "last_update_ms": snap.ts_ms if snap else 0,
        })

    return rows


def compute_maker_rebate_summary(
    ledger: Ledger,
    since_ms: int = 0,
    market_rows: list[dict[str, Any]] | None = None,
    fills: list[ShadowFill] | None = None,
    open_orders: list[LiveOrder] | None = None,
    is_global: bool = False,
) -> dict[str, Any]:
    if fills is None:
        fills = ledger.get_fills_since(since_ms)
    if open_orders is None:
        open_orders = ledger.get_open_orders()
    if market_rows is None:
        market_rows = format_market_overview(ledger, recent_ms=since_ms)
    current_rebates = sum((_fill_maker_rebate(f, is_global=is_global) for f in fills), Decimal("0.0000"))
    maker_fills = [f for f in fills if f.is_maker or f.fee < Decimal("0")]
    active_daily_lip = sum((r["est_reward_rate"] * Decimal("86400") for r in market_rows if r["status"] == "QUOTING"), Decimal("0.00"))
    active_hourly_lip = (active_daily_lip / Decimal("24")).quantize(Decimal("0.0001"))
    active_monthly_lip = (active_daily_lip * Decimal("30")).quantize(Decimal("0.01"))
    deployed_collateral = sum((o.price * o.qty_open for o in open_orders if o.side.startswith("BUY")), Decimal("0.00"))
    scoring_markets_count = sum(1 for r in market_rows if r["status"] == "QUOTING" and r["qualifies"])
    active_buy_orders_count = sum(1 for o in open_orders if o.side.startswith("BUY"))

    if is_global:
        resting_potential = Decimal("0.0000")
        projected_daily_rebates = current_rebates
    else:
        resting_potential = sum(
            (
                (Decimal("0.0125") * o.qty_open * o.price * (Decimal("1") - o.price)).quantize(Decimal("0.0001"))
                for o in open_orders
                if Decimal("0") < o.price < Decimal("1") and o.qty_open > Decimal("0")
            ),
            Decimal("0.0000"),
        )
        if len(maker_fills) >= 2:
            ts_vals = [f.ts_ms for f in maker_fills if f.ts_ms > 0]
            span_ms = max(ts_vals) - min(ts_vals) if len(ts_vals) >= 2 else 0
            if span_ms >= 3_600_000:
                pace = (current_rebates * Decimal("86400000") / Decimal(str(span_ms))).quantize(Decimal("0.0001"))
                projected_daily_rebates = max(pace, resting_potential)
            else:
                projected_daily_rebates = current_rebates + resting_potential
        else:
            projected_daily_rebates = current_rebates + resting_potential

    projected_monthly_rebates = (projected_daily_rebates * Decimal("30")).quantize(Decimal("0.01"))
    projected_daily_profit = (active_daily_lip + projected_daily_rebates).quantize(Decimal("0.01"))
    projected_monthly_profit = (projected_daily_profit * Decimal("30")).quantize(Decimal("0.01"))
    return {
        "current_maker_rebates": current_rebates,
        "maker_fills_count": len(maker_fills),
        "resting_rebate_potential": resting_potential,
        "active_daily_lip": active_daily_lip,
        "active_hourly_lip": active_hourly_lip,
        "active_monthly_lip": active_monthly_lip,
        "deployed_collateral": deployed_collateral,
        "scoring_markets_count": scoring_markets_count,
        "active_buy_orders_count": active_buy_orders_count,
        "projected_daily_rebates": projected_daily_rebates,
        "projected_monthly_rebates": projected_monthly_rebates,
        "projected_daily_profit": projected_daily_profit,
        "projected_monthly_profit": projected_monthly_profit,
    }


def format_fills_table(fills: list[ShadowFill], limit: int = 25, is_global: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in sorted(fills, key=lambda x: x.ts_ms, reverse=True)[:limit]:
        dt = datetime.fromtimestamp(f.ts_ms / 1000, tz=timezone.utc)
        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        reb = _fill_maker_rebate(f, is_global=is_global)
        if f.fee < Decimal("0"):
            fee_str = f"+${abs(f.fee):.4f}"
        elif reb > Decimal("0"):
            fee_str = f"+${reb:.4f}"
        else:
            fee_str = f"-${f.fee:.4f}"
        title = resolve_market_title(f.slug, fetch=False)
        rows.append({
            "Timestamp": ts_str,
            "Title": title,
            "Slug": f.slug,
            "Side": f.side,
            "Price": f"${f.price:.4f}",
            "Qty": f"{f.qty:,}",
            "Fee": fee_str,
            "Order ID": f.order_id,
        })
    return rows


def get_profile_links(proxy_address: str) -> dict[str, str]:
    proxy = (proxy_address or "").strip()
    if proxy:
        return {
            "polymarket": f"https://polymarket.com/profile/{proxy}",
            "polygonscan": f"https://polygonscan.com/address/{proxy}",
        }
    return {
        "polymarket": "https://polymarket.com",
        "polygonscan": "https://polygonscan.com",
    }


def get_active_positions(ledger: Ledger, since_ms: int = 0) -> list[Position]:
    positions = ledger.get_all_positions()
    active = [p for p in positions if p.net_qty != Decimal("0")]
    if active:
        return active

    fills = ledger.get_fills_since(since_ms)
    if not fills:
        return []

    market_fills: dict[str, list[ShadowFill]] = {}
    for f in fills:
        market_fills.setdefault(f.slug, []).append(f)

    reconstructed: list[Position] = []
    for slug, m_fills in market_fills.items():
        pos_qty = Decimal("0")
        avg_px = Decimal("0")
        sorted_fills = sorted(m_fills, key=lambda x: x.ts_ms)
        for f in sorted_fills:
            if f.side in ("BUY_LONG", "BUY_SHORT"):
                if pos_qty >= Decimal("0"):
                    new_qty = pos_qty + f.qty
                    if new_qty > Decimal("0"):
                        avg_px = (pos_qty * avg_px + f.qty * f.price) / new_qty
                    pos_qty = new_qty
                else:
                    pos_qty += f.qty
                    if pos_qty > Decimal("0"):
                        avg_px = f.price
            else:
                if pos_qty <= Decimal("0"):
                    new_qty = abs(pos_qty) + f.qty
                    if new_qty > Decimal("0"):
                        avg_px = (abs(pos_qty) * avg_px + f.qty * f.price) / new_qty
                    pos_qty = -new_qty
                else:
                    pos_qty -= f.qty
                    if pos_qty < Decimal("0"):
                        avg_px = f.price

        if pos_qty != Decimal("0") and abs(pos_qty * avg_px) >= Decimal("0.50"):
            reconstructed.append(Position(slug=slug, net_qty=pos_qty, avg_price=avg_px))

    return reconstructed


def format_held_positions_table(
    positions: list[Any],
    open_orders: list[LiveOrder] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in positions:
        if abs(p.net_qty) <= Decimal("0"):
            continue
        qty = abs(p.net_qty)
        avg_px = p.avg_price
        cost_paid = (qty * avg_px).quantize(Decimal("0.01"))
        title = resolve_market_title(p.slug, fetch=True)

        slug_sells = [
            o
            for o in (open_orders or [])
            if o.slug == p.slug and o.side.upper().startswith("SELL") and o.qty_open > Decimal("0")
        ]
        if slug_sells:
            total_sell_qty = sum((o.qty_open for o in slug_sells), Decimal("0"))
            weighted_sell_px = sum((o.price * o.qty_open for o in slug_sells), Decimal("0")) / total_sell_qty
            limit_sell_str = f"SELL {total_sell_qty:g} @ ${weighted_sell_px:.4f}"
            quoted_qty = min(qty, total_sell_qty)
            unquoted_qty = max(Decimal("0"), qty - quoted_qty)
            projected_rev = (quoted_qty * weighted_sell_px + unquoted_qty * avg_px).quantize(Decimal("0.01"))
        else:
            limit_sell_str = "Pending Unwind Quote"
            projected_rev = cost_paid

        projected_net = (projected_rev - cost_paid).quantize(Decimal("0.01"))
        pct_diff = (
            ((projected_net / cost_paid) * Decimal("100")).quantize(Decimal("0.1"))
            if cost_paid > Decimal("0")
            else Decimal("0.0")
        )
        if projected_net > Decimal("0"):
            net_str = f"+${projected_net:,.2f} (+{pct_diff}%)"
        elif projected_net < Decimal("0"):
            net_str = f"-${abs(projected_net):,.2f} ({pct_diff}%)"
        else:
            net_str = "$0.00 (Break-Even)"

        rows.append({
            "Market Question": title,
            "Shares Held": f"{qty:g}",
            "Avg Entry Price": f"${avg_px:.4f}",
            "Cost Paid": f"${cost_paid:,.2f}",
            "Limit Sell Order": limit_sell_str,
            "Break-Even Price": f"${avg_px:.4f}",
            "Projected Revenue": f"${projected_rev:,.2f}",
            "Projected Net Profit / Loss": net_str,
        })
    return rows


def sync_ledger_from_vps(target_path: Path | None = None) -> tuple[bool, str]:
    key_path = Path(r"C:\Users\redwi\.ssh\LightsailDefaultKey-eu-west-1.pem")
    if not key_path.exists():
        return False, f"SSH key not found at {key_path}"

    if target_path is None:
        settings = Settings.from_env()
        target_path = settings.data_dir / "ledger.db"

    target_path.parent.mkdir(parents=True, exist_ok=True)

    import shutil

    ssh_opts = [
        "-i",
        str(key_path),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
    ]

    try:
        subprocess.run(
            [
                "ssh",
                *ssh_opts,
                "ubuntu@52.16.177.132",
                "python3 -c \"import sqlite3; sqlite3.connect('/home/ubuntu/polymarket_bot/pmus/data/ledger.db').backup(sqlite3.connect('/tmp/ledger_sync.db')); sqlite3.connect('/home/ubuntu/polymarket_bot/pmus/data/certainty_ledger.db').backup(sqlite3.connect('/tmp/certainty_sync.db'))\"",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        pass

    try:
        sat_target = target_path.parent / "certainty_ledger.db"
        sat_temp = sat_target.with_suffix(".tmp")
        sat_cmd = [
            "scp",
            "-C",
            *ssh_opts,
            "ubuntu@52.16.177.132:/tmp/certainty_sync.db",
            str(sat_temp),
        ]
        sat_res = subprocess.run(sat_cmd, capture_output=True, text=True, timeout=15)
        if sat_res.returncode == 0 and sat_temp.exists():
            sat_target.with_name(sat_target.name + "-wal").unlink(missing_ok=True)
            sat_target.with_name(sat_target.name + "-shm").unlink(missing_ok=True)
            shutil.copyfile(str(sat_temp), str(sat_target))
            sat_temp.unlink(missing_ok=True)
    except Exception:
        pass

    temp_target = target_path.with_suffix(".tmp")
    cmd = [
        "scp",
        "-C",
        *ssh_opts,
        "ubuntu@52.16.177.132:/tmp/ledger_sync.db",
        str(temp_target),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if res.returncode == 0:
            target_path.with_name(target_path.name + "-wal").unlink(missing_ok=True)
            target_path.with_name(target_path.name + "-shm").unlink(missing_ok=True)
            shutil.copyfile(str(temp_target), str(target_path))
            try:
                temp_target.unlink(missing_ok=True)
            except Exception:
                pass
            return True, "Synced successfully from Ireland VPS"
        err = res.stderr.strip() or res.stdout.strip() or f"Exit code {res.returncode}"
        return False, f"SCP failed: {err}"
    except subprocess.TimeoutExpired:
        return False, "SCP timed out after 45 seconds"
    except Exception as e:
        return False, f"Sync error: {e}"


@st.cache_resource
def _get_clob_venue_client(settings: Settings) -> Any:
    from pmus.clob_venue import ClobVenueClient
    return ClobVenueClient(settings)


@st.cache_data(ttl=15)
def _get_live_buying_power(settings: Settings) -> Decimal:
    if settings.venue == "global" and settings.polygon_private_key:
        try:
            client = _get_clob_venue_client(settings)
            return client.get_buying_power()
        except Exception:
            return Decimal("0")
    elif settings.venue == "us" and settings.key_id and settings.secret_key:
        try:
            from pmus.venue import AuthenticatedClient
            auth_client = AuthenticatedClient(key_id=settings.key_id, secret_key=settings.secret_key)
            return auth_client.get_buying_power()
        except Exception:
            return Decimal("0")
    return Decimal("0")


@st.cache_data(ttl=60)
def _get_official_daily_earnings(settings: Settings) -> Decimal | None:
    if settings.venue == "global" and settings.polygon_private_key:
        try:
            client = _get_clob_venue_client(settings)
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            results = client.client.get_total_earnings_for_user_for_day(today_str)
            if results:
                if isinstance(results, list):
                    return sum((Decimal(str(item.get("earnings", 0))) for item in results), Decimal("0"))
                if isinstance(results, dict):
                    if "earnings" in results:
                        return Decimal(str(results.get("earnings", 0)))
                    if "data" in results and isinstance(results["data"], list):
                        return sum((Decimal(str(item.get("earnings", 0))) for item in results["data"]), Decimal("0"))
                return Decimal("0")
            return Decimal("0")
        except Exception:
            return None
    return None


def inject_ambient_theme() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@500;600;700;800&display=swap');

        :root {
            --bg-void: #090A0D;
            --bg-base: #0c0a09;
            --bg-surface: #0E1015;
            --border-subtle: #1F2430;
            --text-primary: #F8FAFC;
            --accent-orange: #F97316;
            --accent-amber: #F59E0B;
            --accent-emerald: #10B981;
            --accent-cyan: #0284C7;
            --radius-sm: 4px;
            --max-width: 1760px;
            --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            --font-mono: "JetBrains Mono", "SF Mono", monospace;
        }

        html, body, [class*="css"] {
            font-family: var(--font-sans);
        }

        .stApp {
            min-height: 100dvh;
            background-color: var(--bg-base);
            background-image:
                radial-gradient(ellipse at 50% -5%, rgba(245, 158, 11, 0.14) 0%, transparent 60%),
                radial-gradient(ellipse at 88% 35%, rgba(16, 185, 129, 0.10) 0%, transparent 48%),
                radial-gradient(ellipse at 12% 80%, rgba(2, 132, 199, 0.08) 0%, transparent 52%),
                linear-gradient(to right, rgba(255, 255, 255, 0.02) 1px, transparent 1px),
                linear-gradient(to bottom, rgba(255, 255, 255, 0.02) 1px, transparent 1px);
            background-size: 100% 100%, 100% 100%, 100% 100%, 64px 64px, 64px 64px;
            background-attachment: fixed;
            color: var(--text-primary);
        }

        .block-container {
            max-width: min(var(--max-width), 96vw) !important;
            padding-top: 1.5rem !important;
            container-type: inline-size;
        }

        .system-ticker {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-sm);
            padding: 8px 14px;
            margin-bottom: 14px;
            font-family: var(--font-mono);
            font-size: 0.76rem;
            color: #94a3b8;
        }

        .pulse-dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background-color: var(--accent-emerald);
            margin-right: 8px;
            animation: pulse-ring 2s infinite;
        }

        @keyframes pulse-ring {
            0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
            100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }

        [data-testid="stMetric"] {
            background: linear-gradient(145deg, rgba(18, 20, 26, 0.92), rgba(11, 13, 18, 0.96)) !important;
            border: 1px solid var(--border-subtle) !important;
            border-radius: var(--radius-sm) !important;
            padding: 16px 18px !important;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25), 0 8px 24px -12px rgba(0, 0, 0, 0.65);
            transition: border-color 0.2s ease, transform 0.15s ease;
        }

        [data-testid="stMetric"]:hover {
            border-color: rgba(249, 115, 22, 0.55) !important;
            transform: translateY(-1px);
        }

        [data-testid="stMetricLabel"] {
            font-family: var(--font-sans) !important;
            font-size: 0.80rem !important;
            font-weight: 600 !important;
            letter-spacing: 0.03em !important;
            text-transform: uppercase !important;
            color: #94a3b8 !important;
        }

        [data-testid="stMetricValue"] {
            font-family: var(--font-mono) !important;
            font-size: clamp(1.2rem, 1.8vw, 1.6rem) !important;
            font-weight: 700 !important;
            color: var(--text-primary) !important;
            letter-spacing: -0.02em !important;
        }

        [data-testid="stMetricDelta"] {
            font-family: var(--font-mono) !important;
            font-size: 0.75rem !important;
        }

        .stButton > button, .stLinkButton > a {
            border-radius: var(--radius-sm) !important;
            font-size: 13.5px !important;
            font-weight: 600 !important;
            border: 1px solid rgba(249, 115, 22, 0.4) !important;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12), 0 0 12px -2px rgba(249, 115, 22, 0.25) !important;
            transition: all 0.18s ease !important;
        }

        .stButton > button:hover, .stLinkButton > a:hover {
            border-color: var(--accent-orange) !important;
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.18), 0 0 16px -1px rgba(249, 115, 22, 0.35) !important;
        }

        .nestpulse-hero {
            display: grid;
            grid-template-columns: 1.15fr 0.85fr;
            gap: 16px;
            align-items: center;
            background: linear-gradient(145deg, rgba(20, 22, 29, 0.88), rgba(12, 14, 19, 0.94));
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-sm);
            padding: 16px 20px;
            margin-bottom: 16px;
            box-shadow: 0 12px 32px -16px rgba(0, 0, 0, 0.7);
        }

        .clearinghouse-operations-desk {
            background: rgba(9, 10, 13, 0.75);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-sm);
            padding: 10px 14px;
            font-family: var(--font-mono);
            font-size: 0.76rem;
            color: #cbd5e1;
        }

        .nestpulse-pill {
            display: inline-flex;
            align-items: center;
            padding: 3px 9px;
            border-radius: var(--radius-sm);
            font-family: var(--font-mono);
            font-size: 0.70rem;
            font-weight: 700;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            background: rgba(16, 185, 129, 0.15);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.4);
        }

        .stTabs [data-baseweb="tab-list"] {
            gap: 8px;
            background: rgba(14, 16, 21, 0.75);
            padding: 6px;
            border-radius: var(--radius-sm);
            border: 1px solid var(--border-subtle);
        }

        .stTabs [data-baseweb="tab"] {
            border-radius: var(--radius-sm);
            font-family: var(--font-sans);
            font-weight: 600;
            font-size: 0.88rem;
            color: #94a3b8;
            padding: 8px 16px;
        }

        .stTabs [aria-selected="true"] {
            background: rgba(249, 115, 22, 0.16) !important;
            color: #fb923c !important;
        }

        .mobile-cards-stack {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 12px;
            margin-bottom: 16px;
        }

        .mobile-card-item {
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-sm);
            padding: 14px 16px;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
        }

        .mobile-card-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 8px;
            margin-bottom: 10px;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--border-subtle);
        }

        .mobile-card-title {
            font-weight: 600;
            font-size: 0.90rem;
            color: var(--text-primary);
            line-height: 1.35;
        }

        .mobile-card-badge {
            font-family: var(--font-mono);
            font-size: 0.70rem;
            font-weight: 700;
            padding: 2px 7px;
            border-radius: var(--radius-sm);
            background: rgba(249, 115, 22, 0.15);
            color: #fb923c;
            border: 1px solid rgba(249, 115, 22, 0.35);
            white-space: nowrap;
        }

        .mobile-card-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px 12px;
        }

        .mobile-card-kv {
            display: flex;
            flex-direction: column;
        }

        .mobile-card-k {
            font-size: 0.68rem;
            text-transform: uppercase;
            letter-spacing: 0.03em;
            color: #64748b;
        }

        .mobile-card-v {
            font-family: var(--font-mono);
            font-size: 0.82rem;
            font-weight: 600;
            color: #e2e8f0;
        }

        @container (max-width: 768px) {
            .nestpulse-hero {
                grid-template-columns: 1fr;
            }
        }

        @media (max-width: 768px) {
            .nestpulse-hero {
                grid-template-columns: 1fr;
                padding: 12px 14px;
            }
            [data-testid="stMetric"] {
                padding: 12px 14px !important;
            }
        }

        [data-stale="true"] {
            opacity: 1 !important;
            filter: none !important;
            transition: none !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_mobile_cards_html(
    rows: list[dict[str, Any]],
    title_key: str,
    badge_key: str | None = None,
) -> str:
    if not rows:
        return ""
    cards_html: list[str] = ['<div class="mobile-cards-stack">']
    excluded_keys = {title_key, badge_key, "slug", "Slug", "Market Slug"}
    for r in rows:
        title_val = html.escape(str(r.get(title_key, "Market")))
        badge_html = ""
        if badge_key and r.get(badge_key) is not None:
            badge_val = html.escape(str(r.get(badge_key)))
            badge_html = f'<span class="mobile-card-badge">{badge_val}</span>'
        kv_items: list[str] = []
        for k, v in r.items():
            if k in excluded_keys or k is None:
                continue
            k_esc = html.escape(str(k))
            v_esc = html.escape(str(v))
            kv_items.append(
                f'<div class="mobile-card-kv">'
                f'<span class="mobile-card-k">{k_esc}</span>'
                f'<span class="mobile-card-v">{v_esc}</span>'
                f'</div>'
            )
        cards_html.append(
            f'<div class="mobile-card-item">'
            f'<div class="mobile-card-header">'
            f'<span class="mobile-card-title">{title_val}</span>'
            f'{badge_html}'
            f'</div>'
            f'<div class="mobile-card-grid">{"".join(kv_items)}</div>'
            f'</div>'
        )
    cards_html.append("</div>")
    return "".join(cards_html)


@st.fragment(run_every="10s")
def render_live_hud(ledger: Ledger, settings: Settings) -> None:
    empty = is_database_empty(ledger)

    if empty:
        st.info("Waiting for first tick... Trading bot has not recorded any quotes, orders, or LIP snapshots yet.")

    effective_reset_ms = get_effective_reset_epoch_ms(settings)
    now_ms = int(time.time() * 1000)

    ctrl1, ctrl2, ctrl3, ctrl4 = st.columns(4)
    with ctrl1:
        window_options = [
            "Since Oct 17, 7:00 PM CDT (Default / Custom Cutoff)",
            "Fresh Start Today ($0 Loss Baseline - 07:00 UTC)",
            "Last 1h",
            "Last 24h",
            "All-Time (Historical)",
        ]
        selected_window = st.selectbox(
            "Tracking Window",
            options=window_options,
            index=0,
            help="Filter fills, realized PnL, and pool rewards to your chosen baseline epoch.",
        )
    with ctrl2:
        layout_mode = st.radio(
            "Display Layout",
            options=["Desktop Widescreen", "Mobile Tactile Cards"],
            horizontal=True,
            help="Switch between full widescreen dataframes and mobile-friendly tactile cards.",
        )
    with ctrl3:
        if st.button(
            "Reset Tracking to $0 Now",
            use_container_width=True,
            help="Set a custom tracking baseline right now so prior fills and losses are excluded.",
        ) is True:
            set_custom_reset_epoch_ms(settings, int(time.time() * 1000))
            st.rerun()
    with ctrl4:
        if st.button(
            "Restore Oct 17 7:00 PM Cutoff",
            use_container_width=True,
            help="Restore the default Oct 17, 7:00 PM CDT (Oct 18 00:00 UTC) tracking cutoff.",
        ) is True:
            set_custom_reset_epoch_ms(settings, DEFAULT_RESET_EPOCH_MS)
            st.rerun()

    if not isinstance(selected_window, str):
        active_since_ms = effective_reset_ms
    elif selected_window == "Fresh Start Today ($0 Loss Baseline - 07:00 UTC)":
        active_since_ms = FRESH_START_TODAY_0700_UTC_MS
    elif selected_window == "Last 1h":
        active_since_ms = max(0, now_ms - 3_600_000)
    elif selected_window == "Last 24h":
        active_since_ms = max(0, now_ms - 86_400_000)
    elif selected_window == "All-Time (Historical)":
        active_since_ms = 0
    else:
        active_since_ms = effective_reset_ms

    is_mobile_layout = layout_mode == "Mobile Tactile Cards"

    with st.expander("How to Read This Dashboard (Plain-English Guide)", expanded=False):
        st.markdown(
            """
            - **Available Cash & Portfolio Value**: `Available Cash` is your unallocated USDC on Polymarket CLOB. `Portfolio Value` adds the cost basis of your currently held outcome shares (`Cash + Held Shares`).
            - **Pool Rewards Earned**: Liquidity Incentive Program (LIP) rewards accrued by keeping qualifying resting bids and asks near the midpoint.
            - **Total Exit Value (Card 5)**: What your account is worth if all held positions unwind at break-even, plus earned LIP rewards and maker rebates, minus any realized losses or taker fees in the active window:
              `Total Exit Value = Portfolio Value + LIP Rewards + Maker Rebates + Realized PnL - Taker Fees`.
            - **Held Shares & Unwind Limit Orders**: Whenever a resting buy order is filled, the bot automatically places a `SELL_LONG` limit order at or above your entry price (`Break-Even Price`) to convert the shares back into cash without taking a loss.
            """
        )

    buying_power = _get_live_buying_power(settings) if not empty else Decimal("0")
    is_global = settings.venue == "global"
    all_active_positions = get_active_positions(ledger, since_ms=0)
    held_positions_usd = sum(
        (abs(p.net_qty) * p.avg_price for p in all_active_positions),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"))

    fills = ledger.get_fills_since(active_since_ms)
    open_orders = ledger.get_open_orders()
    official_rewards = _get_official_daily_earnings(settings)
    kpis = compute_kpis(
        ledger,
        since_ms=active_since_ms,
        buying_power=buying_power,
        max_collateral=settings.max_collateral,
        is_global=is_global,
        fills=fills,
        held_positions_usd=held_positions_usd,
        official_rewards=official_rewards,
    )
    today_start_ms = int(
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
        * 1000
    )
    today_lip_stats = ledger.get_lip_stats(today_start_ms)
    all_time_lip_stats = ledger.get_lip_stats(0)
    est_yield_delta = f"Today Est: ${today_lip_stats['lip_reward_total']:,.2f} | All-Time: ${all_time_lip_stats['lip_reward_total']:,.2f}"

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric(
            label="Available Cash",
            value=f"${kpis['balance_usd']:,.2f}",
            delta=f"Portfolio: ${kpis['portfolio_value_usd']:,.2f} (Held: ${kpis['held_positions_usd']:,.2f})",
            delta_color="off",
            border=True,
            help="Free USDC buying power plus total portfolio value including held shares.",
        )
    with c2:
        if official_rewards is not None and official_rewards > Decimal("0"):
            st.metric(
                label="Pool Rewards Earned",
                value=f"${official_rewards:,.4f}",
                delta=est_yield_delta,
                delta_color="off",
                border=True,
                help="Official Polymarket daily liquidity pool rewards earned today.",
            )
        else:
            st.metric(
                label="Pool Rewards Earned",
                value=f"${kpis['lip_reward_total']:,.4f}",
                delta=est_yield_delta,
                delta_color="off",
                border=True,
                help="Estimated Liquidity Incentive Program (LIP) rewards accrued in the active tracking window.",
            )
    with c3:
        st.metric(
            label="Active Scoring Time",
            value=f"{kpis['qualifying_seconds']:,}s",
            delta=f"Bid: {kpis['qualifying_bid_seconds']}s | Ask: {kpis['qualifying_ask_seconds']}s",
            delta_color="off",
            border=True,
            help="Total seconds our resting quotes met Polymarket's minimum score and spread requirements.",
        )
    with c4:
        st.metric(
            label="Matched Orders",
            value=f"{kpis['total_fills']}",
            delta=f"${kpis['total_volume']:,.2f} Vol",
            delta_color="off",
            border=True,
            help="Number of order fills and USD volume matched during the active tracking window.",
        )
    with c5:
        net_trade_pnl = kpis["realized_pnl"] - kpis["taker_fees"]
        c5_delta = (
            f"Portfolio: ${kpis['portfolio_value_usd']:,.2f} | "
            f"LIP: +${kpis['active_lip_rewards']:,.2f} | "
            f"Losses/PnL: ${net_trade_pnl:+,.2f}"
        )
        st.metric(
            label="Total Exit Value",
            value=f"${kpis['total_exit_value_usd']:,.2f}",
            delta=c5_delta,
            delta_color="normal" if kpis["total_exit_value_usd"] >= kpis["portfolio_value_usd"] else "inverse",
            border=True,
            help="Projected total account value when held positions exit at break-even + LIP rewards + maker rebates - realized losses/fees.",
        )

    market_rows = format_market_overview(ledger, recent_ms=active_since_ms)

    reb_summary = compute_maker_rebate_summary(
        ledger,
        since_ms=active_since_ms,
        market_rows=market_rows,
        fills=fills,
        open_orders=open_orders,
        is_global=is_global,
    )
    if is_global:
        st.markdown("#### Pool Rewards Velocity & Projected Income")
        r1, r2, r3, r4 = st.columns(4)
        with r1:
            st.metric(
                label="Live Hourly Pool Pace",
                value=f"+${reb_summary['active_hourly_lip']:,.4f}/hr",
                delta=f"{reb_summary['scoring_markets_count']} Scoring Markets",
                delta_color="normal",
                border=True,
                help="Current hourly LIP reward velocity across all actively quoting markets.",
            )
        with r2:
            st.metric(
                label="Deployed Order Collateral",
                value=f"${reb_summary['deployed_collateral']:,.2f}",
                delta=f"{reb_summary['active_buy_orders_count']} Active Buy Orders",
                delta_color="off",
                border=True,
                help="Total USDC currently committed in open resting BUY limit orders.",
            )
        with r3:
            st.metric(
                label="Projected Daily Rewards",
                value=f"+${reb_summary['active_daily_lip']:,.2f}/day",
                delta="Active Pool Share",
                delta_color="normal",
                border=True,
                help="24-hour projected LIP reward payout at current quoting share.",
            )
        with r4:
            st.metric(
                label="Projected 30d Rewards",
                value=f"+${reb_summary['active_monthly_lip']:,.2f}/30d",
                delta="At Current Pool Pace",
                delta_color="normal",
                border=True,
                help="30-day annualized run-rate from active reward pools.",
            )
    else:
        st.markdown("#### Maker Rebates & Projected Income")
        r1, r2, r3, r4 = st.columns(4)
        with r1:
            st.metric(
                label="Current Maker Rebates",
                value=f"+${reb_summary['current_maker_rebates']:,.4f}",
                delta=f"{reb_summary['maker_fills_count']} Maker Matches",
                delta_color="off",
                border=True,
            )
        with r2:
            st.metric(
                label="Resting Rebate Potential",
                value=f"+${reb_summary['resting_rebate_potential']:,.4f}",
                delta="Per Full Order Turnover",
                delta_color="off",
                border=True,
            )
        with r3:
            st.metric(
                label="Projected Rebate Profit",
                value=f"+${reb_summary['projected_daily_rebates']:,.2f}/day",
                delta=f"+${reb_summary['projected_monthly_rebates']:,.2f} / 30d",
                delta_color="normal",
                border=True,
            )
        with r4:
            st.metric(
                label="Projected Total Income",
                value=f"+${reb_summary['projected_daily_profit']:,.2f}/day",
                delta=f"+${reb_summary['projected_monthly_profit']:,.2f} / 30d (Pools + Rebates)",
                delta_color="normal",
                border=True,
            )

    st.markdown("#### Live Reward Pools")
    if market_rows:
        sorted_rows = sorted(
            market_rows,
            key=lambda r: (0 if r["status"] == "QUOTING" else 1, -r["est_reward_rate"], r["slug"]),
        )
        display_data = []
        for r in sorted_rows:
            best_bid_str = f"${r['best_bid']:.3f}" if r["best_bid"] is not None else "-"
            best_ask_str = f"${r['best_ask']:.3f}" if r["best_ask"] is not None else "-"
            qual_symbol = "YES" if r["qualifies"] else "NO"
            b_qual_symbol = "YES" if r["bid_qualifies"] else "NO"
            a_qual_symbol = "YES" if r["ask_qualifies"] else "NO"
            title = resolve_market_title(r["slug"], fetch=True)
            display_data.append({
                "Title": title,
                "Status": r["status"],
                "Qualifies": qual_symbol,
                "Bid Qual": b_qual_symbol,
                "Ask Qual": a_qual_symbol,
                "Best Bid": best_bid_str,
                "Bid Qty": f"{r['bid_qty']:,}",
                "Best Ask": best_ask_str,
                "Ask Qty": f"{r['ask_qty']:,}",
                "Target Size": f"{r['target_pool_size']:,.0f}",
                "Est LIP Rate/s": f"${r['est_reward_rate']:.8f}",
                "Daily Pace": f"${(r['est_reward_rate'] * Decimal('86400')):,.2f}/day",
            })
        if is_mobile_layout:
            st.markdown(render_mobile_cards_html(display_data, title_key="Title", badge_key="Status"), unsafe_allow_html=True)
        else:
            df_markets = pd.DataFrame(display_data)
            st.dataframe(df_markets, width="stretch", hide_index=True)
    else:
        st.markdown("_No active quoting markets found._")

    st.markdown("#### Held Shares & Unwind Limit Orders")
    held_rows = format_held_positions_table(all_active_positions, open_orders)
    if held_rows:
        if is_mobile_layout:
            st.markdown(
                render_mobile_cards_html(held_rows, title_key="Market Question", badge_key="Limit Sell Order"),
                unsafe_allow_html=True,
            )
        else:
            st.dataframe(pd.DataFrame(held_rows), width="stretch", hide_index=True)
    else:
        st.markdown("_No active held shares currently unwinding._")

    st.markdown("#### Recent Order Matches")
    blotter_rows = format_fills_table(fills, is_global=is_global)
    if blotter_rows:
        clean_fills = []
        for row in blotter_rows:
            resolved_title = resolve_market_title(row.get("Slug", ""), fetch=True) if row.get("Slug") else row.get("Title", "")
            clean_row = {k: v for k, v in row.items() if k not in ("Slug", "slug", "Market Slug")}
            clean_row["Title"] = resolved_title
            clean_fills.append(clean_row)
        if is_mobile_layout:
            st.markdown(render_mobile_cards_html(clean_fills, title_key="Title", badge_key="Side"), unsafe_allow_html=True)
        else:
            df_fills = pd.DataFrame(clean_fills)
            st.dataframe(df_fills, width="stretch", hide_index=True)
    else:
        st.markdown("_No fills recorded in the selected tracking window._")

    st.caption(f"Last updated: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} (auto-refreshing every 10s)")


def classify_position_phase(expected_resolution: str | None, now: datetime | None = None) -> str:
    if not expected_resolution:
        return "Open"
    try:
        res_dt = datetime.fromisoformat(expected_resolution.replace("Z", "+00:00"))
        if res_dt.tzinfo is None:
            res_dt = res_dt.replace(tzinfo=timezone.utc)
        now_dt = now or datetime.now(timezone.utc)
        if res_dt <= now_dt:
            return "In-Play / Awaiting Settlement"
        return "Pre-Match / Open"
    except Exception:
        return "Open"


@st.fragment(run_every="10s")
def render_satellite_hud(settings: Settings) -> None:
    from pmus.certainty_ledger import CertaintyLedger

    db_path = settings.data_dir / "certainty_ledger.db"
    sat_ledger = CertaintyLedger(db_path)
    summary = sat_ledger.get_performance_summary()
    open_trades = sat_ledger.get_open_trades()

    bankroll_usd = 1000.0
    realized_pnl = float(summary.get("total_pnl", 0.0))
    allocated_capital = sum(float(t.get("size_usd", 0.0)) for t in open_trades)
    virtual_cash = max(0.0, bankroll_usd + realized_pnl - allocated_capital)

    cb_active, rem_hours = sat_ledger.is_circuit_breaker_active(0.0)
    if cb_active:
        st.error(f"CIRCUIT BREAKER ACTIVE - Scans halted for next {rem_hours:.1f} hours due to a recorded loss.")

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric(
            label="Virtual Cash / Bankroll",
            value=f"${virtual_cash:,.2f}",
            delta=f"Allocated: ${allocated_capital:,.2f} / ${bankroll_usd:,.0f}",
            delta_color="off",
            border=True,
        )
    with c2:
        st.metric(
            label="Win / Loss Record",
            value=f"{summary['wins']}W - {summary['losses']}L",
            delta=f"Win Rate: {summary['win_rate'] * 100:.1f}% ({summary['closed_trades']} closed)",
            delta_color="normal" if summary['losses'] == 0 else "inverse",
            border=True,
        )
    with c3:
        pnl = summary["total_pnl"]
        st.metric(
            label="Total Realized PnL",
            value=f"${pnl:+,.2f}",
            delta=f"Avg Return: {summary.get('avg_return', 0.0) * 100:+.2f}% / trade",
            delta_color="normal" if pnl >= 0 else "inverse",
            border=True,
        )
    with c4:
        st.metric(
            label="Sharpe Ratio",
            value=f"{summary['sharpe_ratio']:.2f}",
            delta=f"Open Positions: {summary['open_trades']} / 30",
            delta_color="off",
            border=True,
        )
    with c5:
        st.metric(
            label="Circuit Breaker",
            value="HALTED (48h)" if cb_active else "DISABLED",
            delta=f"{rem_hours:.1f}h cooldown left" if cb_active else "Continuous sampling",
            delta_color="inverse" if cb_active else "normal",
            border=True,
        )

    st.markdown("#### Active High-Certainty Positions")
    if open_trades:
        pos_rows = []
        for t in open_trades:
            pos_rows.append({
                "ID": t.get("id"),
                "Phase": classify_position_phase(t.get("expected_resolution")),
                "Market": t.get("market_slug", ""),
                "Outcome": t.get("outcome", ""),
                "Price": f"${float(t.get('entry_price', 0.0)):.4f}",
                "Size": f"${float(t.get('size_usd', 0.0)):.2f}",
                "Shares": f"{float(t.get('shares', 0.0)):.2f}",
                "Entry Time": t.get("entry_timestamp", ""),
                "Expiry": t.get("expected_resolution", ""),
            })
        df_open = pd.DataFrame(pos_rows)
        st.dataframe(df_open, width="stretch", hide_index=True)
    else:
        st.markdown("_No active paper positions in Certainty Ledger._")

    st.markdown("#### Settled Outcomes")
    all_trades = sat_ledger.get_all_trades()
    closed_trades = [t for t in all_trades if t.get("status") not in ("PENDING", "FILLED")]
    if closed_trades:
        closed_rows = []
        for t in sorted(closed_trades, key=lambda x: x.get("exit_timestamp") or "", reverse=True)[:50]:
            pnl_val = float(t.get("pnl_usd", 0.0))
            closed_rows.append({
                "ID": t.get("id"),
                "Status": t.get("status", ""),
                "Market": t.get("market_slug", ""),
                "Outcome": t.get("outcome", ""),
                "Entry Px": f"${float(t.get('entry_price', 0.0)):.4f}",
                "Exit Px": f"${float(t.get('exit_price', 0.0)):.4f}" if t.get("exit_price") is not None else "-",
                "PnL ($)": f"${pnl_val:+,.4f}",
                "Exit Time": t.get("exit_timestamp", ""),
                "Notes": t.get("notes", ""),
            })
        st.dataframe(pd.DataFrame(closed_rows), width="stretch", hide_index=True)

    st.caption(f"Last updated: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} (auto-refreshing every 10s)")


def main() -> None:
    st.set_page_config(page_title="NestPulse | Autonomous Yield Studio", layout="wide", initial_sidebar_state="collapsed")
    inject_ambient_theme()

    settings = Settings.from_env()
    if not check_password(settings):
        st.stop()

    if settings.dashboard_password:
        if st.sidebar.button("Lock Dashboard") is True:
            st.session_state["authenticated"] = False
            st.rerun()

    db_path = settings.data_dir / "ledger.db"
    ledger = Ledger(db_path)

    venue_badge = f"{settings.venue.upper()} | {settings.mode.upper()}"
    st.markdown(
        f"""
        <div class="system-ticker">
            <div><span class="pulse-dot"></span><strong>SYSTEM TELEMETRY:</strong> ACTIVE CLOB QUOTING &amp; AUTO-UNWIND GUARD</div>
            <div>VENUE: {venue_badge} &nbsp;|&nbsp; LEDGER: {db_path.name} &nbsp;|&nbsp; DEFAULT EPOCH: OCT 17 19:00 CDT</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    header_col, links_col = st.columns([3, 2])
    with header_col:
        st.markdown(
            f"""
            <div class="nestpulse-hero">
                <div>
                    <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 4px;">
                        <span style="font-size: 2.0rem; font-weight: 800; letter-spacing: -0.03em; color: #f8fafc;">NestPulse</span>
                        <span class="nestpulse-pill">LIVE CLOB</span>
                    </div>
                    <div style="font-size: 0.90rem; color: #94a3b8; font-weight: 500;">
                        Autonomous Reward Pool &amp; Certainty Studio &nbsp;•&nbsp; <span style="color: #cbd5e1;">Venue: {venue_badge}</span>
                    </div>
                </div>
                <div class="clearinghouse-operations-desk">
                    <div><strong>CLEARINGHOUSE DESK:</strong> BREAK-EVEN FLOOR ACTIVE</div>
                    <div>UNWIND GUARD: SELL_LONG &ge; AVG_ENTRY &nbsp;|&nbsp; SLIPPAGE CAP: $0.00</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with links_col:
        links = get_profile_links(settings.polymarket_proxy_address)
        l_col1, l_col2, l_col3 = st.columns(3)
        with l_col1:
            st.link_button("Polymarket Profile", links["polymarket"], use_container_width=True)
        with l_col2:
            st.link_button("Polygonscan", links["polygonscan"], use_container_width=True)
        with l_col3:
            if st.button("Sync with VPS", use_container_width=True, help="Download latest ledger.db from Ireland VPS") is True:
                success, msg = sync_ledger_from_vps(db_path)
                if success:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
        if settings.polymarket_proxy_address:
            addr = settings.polymarket_proxy_address
            short_addr = f"{addr[:6]}...{addr[-4:]}" if len(addr) > 10 else addr
            st.caption(f"Proxy: `{short_addr}`")

    tab_maker, tab_satellite = st.tabs(["Reward Pools (Engine 1)", "Certainty Harvester (Engine 2)"])
    with tab_maker:
        render_live_hud(ledger, settings)
    with tab_satellite:
        render_satellite_hud(settings)


if __name__ == "__main__" or (hasattr(st, "runtime") and st.runtime.exists()):
    main()
