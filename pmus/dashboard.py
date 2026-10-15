import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import subprocess
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
            if pwd == settings.dashboard_password:
                st.session_state["authenticated"] = True
                st.rerun()
            elif pwd:
                st.error("Invalid password")
    return False


def resolve_market_title(slug: str, fetch: bool = False) -> str:
    if not slug:
        return ""
    if slug in _TITLE_CACHE:
        return _TITLE_CACHE[slug]

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
                    res = cur.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()
                    if res and res[0] > 0:
                        return False
                except sqlite3.OperationalError:
                    continue
        return True
    except Exception:
        return True


def _fill_maker_rebate(f: ShadowFill) -> Decimal:
    if f.fee < Decimal("0"):
        return -f.fee
    if f.is_maker and f.fee == Decimal("0") and Decimal("0") < f.price < Decimal("1") and f.qty > Decimal("0"):
        return (Decimal("0.0125") * f.qty * f.price * (Decimal("1") - f.price)).quantize(Decimal("0.0001"))
    return Decimal("0")


def compute_kpis(
    ledger: Ledger,
    since_ms: int = 0,
    buying_power: Decimal | None = None,
    max_collateral: Decimal | None = None,
) -> dict[str, Any]:
    fills = ledger.get_fills_since(since_ms)
    lip_stats = ledger.get_lip_stats(since_ms)

    total_volume = Decimal("0")
    maker_rebates = Decimal("0")
    taker_fees = Decimal("0")
    realized_pnl = Decimal("0")

    for f in fills:
        total_volume += (f.price * f.qty)
        reb = _fill_maker_rebate(f)
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

    collateral_cap = max_collateral if max_collateral is not None else Decimal("70.00")
    effective_balance = buying_power if buying_power is not None else Decimal("0")

    return {
        "balance_usd": effective_balance,
        "collateral_cap_usd": collateral_cap,
        "lip_reward_total": lip_reward_total,
        "qualifying_seconds": qualifying_seconds,
        "qualifying_bid_seconds": qualifying_bid_seconds,
        "qualifying_ask_seconds": qualifying_ask_seconds,
        "total_fills": len(fills),
        "total_volume": total_volume,
        "realized_pnl": realized_pnl,
        "maker_rebates": maker_rebates,
        "taker_fees": taker_fees,
        "net_pnl": realized_pnl + maker_rebates + lip_reward_total - taker_fees,
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
        status = "QUOTING" if (bids or asks) else "MONITORING"
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
            "qualifies": snap.qualifies if snap else False,
            "bid_qualifies": snap.bid_qualifies if snap else False,
            "ask_qualifies": snap.ask_qualifies if snap else False,
            "target_pool_size": snap.total_bid_score if snap else Decimal("0"),
            "trader_bid_score": snap.trader_bid_score if snap else Decimal("0"),
            "trader_ask_score": snap.trader_ask_score if snap else Decimal("0"),
            "est_reward_rate": snap.reward_usd if snap else Decimal("0"),
            "program_id": snap.program_id if snap else "",
            "last_update_ms": snap.ts_ms if snap else 0,
        })

    return rows


def compute_maker_rebate_summary(ledger: Ledger, since_ms: int = 0) -> dict[str, Any]:
    fills = ledger.get_fills_since(since_ms)
    open_orders = ledger.get_open_orders()
    market_rows = format_market_overview(ledger, recent_ms=since_ms)
    current_rebates = sum((_fill_maker_rebate(f) for f in fills), Decimal("0.0000"))
    maker_fills = [f for f in fills if f.is_maker or f.fee < Decimal("0")]
    resting_potential = sum(
        (
            (Decimal("0.0125") * o.qty_open * o.price * (Decimal("1") - o.price)).quantize(Decimal("0.0001"))
            for o in open_orders
            if Decimal("0") < o.price < Decimal("1") and o.qty_open > Decimal("0")
        ),
        Decimal("0.0000"),
    )
    active_daily_lip = sum((r["est_reward_rate"] * Decimal("86400") for r in market_rows if r["status"] == "QUOTING"), Decimal("0.00"))
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
        "projected_daily_rebates": projected_daily_rebates,
        "projected_monthly_rebates": projected_monthly_rebates,
        "projected_daily_profit": projected_daily_profit,
        "projected_monthly_profit": projected_monthly_profit,
    }


def format_fills_table(fills: list[ShadowFill], limit: int = 25) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in sorted(fills, key=lambda x: x.ts_ms, reverse=True)[:limit]:
        dt = datetime.fromtimestamp(f.ts_ms / 1000, tz=timezone.utc)
        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        reb = _fill_maker_rebate(f)
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


def get_active_positions(ledger: Ledger) -> list[Position]:
    positions = ledger.get_all_positions()
    active = [p for p in positions if p.net_qty != Decimal("0")]
    if active:
        return active

    fills = ledger.get_fills_since(0)
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
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Plus+Jakarta+Sans:wght@500;600;700;800&display=swap');

        html, body, [class*="css"] {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }

        .stApp {
            background-color: #0c0a09;
            background-image:
                radial-gradient(ellipse at 50% -5%, rgba(245, 158, 11, 0.15) 0%, transparent 60%),
                radial-gradient(ellipse at 88% 35%, rgba(16, 185, 129, 0.11) 0%, transparent 48%),
                radial-gradient(ellipse at 12% 80%, rgba(56, 189, 248, 0.08) 0%, transparent 52%),
                linear-gradient(to right, rgba(255, 255, 255, 0.025) 1px, transparent 1px),
                linear-gradient(to bottom, rgba(255, 255, 255, 0.025) 1px, transparent 1px);
            background-size: 100% 100%, 100% 100%, 100% 100%, 64px 64px, 64px 64px;
            background-attachment: fixed;
            color: #f1f5f9;
        }

        [data-testid="stMetric"] {
            background: linear-gradient(145deg, rgba(28, 25, 23, 0.78), rgba(16, 14, 13, 0.88)) !important;
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(87, 83, 78, 0.45) !important;
            border-radius: 16px !important;
            padding: 18px 20px !important;
            box-shadow: 0 12px 32px -12px rgba(0, 0, 0, 0.6);
            transition: border-color 0.25s ease, transform 0.2s ease;
        }

        [data-testid="stMetric"]:hover {
            border-color: rgba(245, 158, 11, 0.5) !important;
            transform: translateY(-2px);
        }

        [data-testid="stMetricLabel"] {
            font-family: 'Plus Jakarta Sans', sans-serif !important;
            font-size: 0.82rem !important;
            font-weight: 600 !important;
            letter-spacing: 0.02em !important;
            text-transform: uppercase !important;
            color: #94a3b8 !important;
        }

        [data-testid="stMetricValue"] {
            font-family: 'JetBrains Mono', monospace !important;
            font-size: 1.65rem !important;
            font-weight: 700 !important;
            color: #f8fafc !important;
            letter-spacing: -0.02em !important;
        }

        .nestpulse-hero {
            background: linear-gradient(145deg, rgba(28, 25, 23, 0.75), rgba(18, 16, 15, 0.85));
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid rgba(87, 83, 78, 0.4);
            border-radius: 18px;
            padding: 16px 22px;
            margin-bottom: 20px;
            box-shadow: 0 16px 40px -16px rgba(0, 0, 0, 0.7);
        }

        .nestpulse-pill {
            display: inline-flex;
            align-items: center;
            padding: 3px 10px;
            border-radius: 9999px;
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            background: linear-gradient(135deg, rgba(245, 158, 11, 0.2), rgba(16, 185, 129, 0.2));
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.35);
        }

        .stTabs [data-baseweb="tab-list"] {
            gap: 8px;
            background: rgba(28, 25, 23, 0.5);
            padding: 6px;
            border-radius: 12px;
            border: 1px solid rgba(87, 83, 78, 0.3);
        }

        .stTabs [data-baseweb="tab"] {
            border-radius: 8px;
            font-family: 'Plus Jakarta Sans', sans-serif;
            font-weight: 600;
            font-size: 0.9rem;
            color: #94a3b8;
            padding: 8px 16px;
        }

        .stTabs [aria-selected="true"] {
            background: rgba(245, 158, 11, 0.15) !important;
            color: #fbbf24 !important;
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


@st.fragment(run_every="10s")
def render_live_hud(ledger: Ledger, settings: Settings) -> None:
    empty = is_database_empty(ledger)

    if empty:
        st.info("Waiting for first tick... Trading bot has not recorded any quotes, orders, or LIP snapshots yet.")

    buying_power = _get_live_buying_power(settings) if not empty else Decimal("0")
    kpis = compute_kpis(ledger, since_ms=0, buying_power=buying_power, max_collateral=settings.max_collateral)
    official_rewards = _get_official_daily_earnings(settings)

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric(
            label="Available Cash",
            value=f"${kpis['balance_usd']:,.2f}",
            delta=f"Limit: ${kpis['collateral_cap_usd']:,.2f}",
            delta_color="off",
            border=True,
        )
    with c2:
        if official_rewards is not None and official_rewards > Decimal("0"):
            st.metric(
                label="Pool Rewards Earned",
                value=f"${official_rewards:,.4f}",
                delta=f"Est Yield: ${kpis['lip_reward_total']:,.4f}",
                delta_color="off",
                border=True,
            )
        else:
            st.metric(
                label="Pool Rewards Earned",
                value=f"${kpis['lip_reward_total']:,.4f}",
                delta="LIP Yield",
                delta_color="off",
                border=True,
            )
    with c3:
        st.metric(
            label="Active Scoring Time",
            value=f"{kpis['qualifying_seconds']:,}s",
            delta=f"Bid: {kpis['qualifying_bid_seconds']}s | Ask: {kpis['qualifying_ask_seconds']}s",
            delta_color="off",
            border=True,
        )
    with c4:
        st.metric(
            label="Matched Orders",
            value=f"{kpis['total_fills']}",
            delta=f"${kpis['total_volume']:,.2f} Vol",
            delta_color="off",
            border=True,
        )
    with c5:
        pnl_val = kpis["realized_pnl"]
        st.metric(
            label="Net Exit Profit",
            value=f"${pnl_val:+,.4f}",
            delta=f"Rebates: +${kpis['maker_rebates']:,.4f}",
            delta_color="normal" if pnl_val != Decimal("0") else "off",
            border=True,
        )

    st.markdown("#### Maker Rebates & Projected Income")
    reb_summary = compute_maker_rebate_summary(ledger, since_ms=0)
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
    market_rows = format_market_overview(ledger, recent_ms=0)
    if market_rows:
        display_data = []
        for r in market_rows:
            best_bid_str = f"${r['best_bid']:.3f}" if r["best_bid"] is not None else "-"
            best_ask_str = f"${r['best_ask']:.3f}" if r["best_ask"] is not None else "-"
            qual_symbol = "YES" if r["qualifies"] else "NO"
            b_qual_symbol = "YES" if r["bid_qualifies"] else "NO"
            a_qual_symbol = "YES" if r["ask_qualifies"] else "NO"
            title = r.get("title") or resolve_market_title(r["slug"])
            display_data.append({
                "Title": title,
                "Market Slug": r["slug"],
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
        df_markets = pd.DataFrame(display_data)
        st.dataframe(df_markets, width="stretch", hide_index=True)
    else:
        st.markdown("_No active quoting markets found._")

    st.markdown("#### Recent Order Matches")
    fills = ledger.get_fills_since(0)
    blotter_rows = format_fills_table(fills)
    if blotter_rows:
        df_fills = pd.DataFrame(blotter_rows)
        st.dataframe(df_fills, width="stretch", hide_index=True)
    else:
        st.markdown("_No fills recorded yet._")

    active_positions = get_active_positions(ledger)
    if active_positions:
        st.markdown("#### Held Shares (Auto-Unwinding)")
        pos_data = [
            {
                "Title": resolve_market_title(p.slug),
                "Slug": p.slug,
                "Net Qty": f"{p.net_qty:,}",
                "Avg Price": f"${p.avg_price:.4f}",
                "Cost (USD)": f"${(p.net_qty * p.avg_price):,.2f}",
            }
            for p in active_positions
        ]
        st.dataframe(pd.DataFrame(pos_data), width="stretch", hide_index=True)

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
    allocated_capital = sum(float(t.get("size_usd", 0.0)) for t in open_trades)
    virtual_cash = max(0.0, bankroll_usd - allocated_capital)

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
        if st.sidebar.button("Lock Dashboard"):
            st.session_state["authenticated"] = False
            st.rerun()

    db_path = settings.data_dir / "ledger.db"
    ledger = Ledger(db_path)

    venue_badge = f"{settings.venue.upper()} | {settings.mode.upper()}"
    header_col, links_col = st.columns([3, 2])
    with header_col:
        st.markdown(
            f"""
            <div class="nestpulse-hero">
                <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 4px;">
                    <span style="font-size: 2.1rem; font-weight: 800; letter-spacing: -0.03em; color: #f8fafc; font-family: 'Plus Jakarta Sans', sans-serif;">NestPulse</span>
                    <span class="nestpulse-pill">LIVE CLOB</span>
                </div>
                <div style="font-size: 0.92rem; color: #94a3b8; font-weight: 500;">
                    Autonomous Reward Pool &amp; Certainty Studio &nbsp;•&nbsp; <span style="color: #cbd5e1;">Venue: {venue_badge}</span> &nbsp;•&nbsp; <span style="color: #64748b; font-family: 'JetBrains Mono', monospace;">{db_path.name}</span>
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
            if st.button("Sync with VPS", use_container_width=True, help="Download latest ledger.db from Ireland VPS"):
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
