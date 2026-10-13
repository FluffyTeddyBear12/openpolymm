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
        if f.fee < Decimal("0"):
            maker_rebates += (-f.fee)
        else:
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


def format_fills_table(fills: list[ShadowFill], limit: int = 25) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in sorted(fills, key=lambda x: x.ts_ms, reverse=True)[:limit]:
        dt = datetime.fromtimestamp(f.ts_ms / 1000, tz=timezone.utc)
        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        if f.fee < Decimal("0"):
            fee_str = f"+${abs(f.fee):.4f}"
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

    temp_target = target_path.with_suffix(".tmp")
    cmd = [
        "scp",
        "-i",
        str(key_path),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
        "ubuntu@52.16.177.132:/home/ubuntu/polymarket_bot/pmus/data/ledger.db",
        str(temp_target),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            import shutil
            shutil.copyfile(str(temp_target), str(target_path))
            try:
                temp_target.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                sat_target = target_path.parent / "certainty_ledger.db"
                sat_temp = sat_target.with_suffix(".tmp")
                sat_cmd = [
                    "scp",
                    "-i",
                    str(key_path),
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ConnectTimeout=10",
                    "ubuntu@52.16.177.132:/home/ubuntu/polymarket_bot/pmus/data/certainty_ledger.db",
                    str(sat_temp),
                ]
                sat_res = subprocess.run(sat_cmd, capture_output=True, text=True, timeout=15)
                if sat_res.returncode == 0 and sat_temp.exists():
                    shutil.copyfile(str(sat_temp), str(sat_target))
                    sat_temp.unlink(missing_ok=True)
            except Exception:
                pass
            return True, "Synced successfully from Ireland VPS"
        err = res.stderr.strip() or res.stdout.strip() or f"Exit code {res.returncode}"
        return False, f"SCP failed: {err}"
    except subprocess.TimeoutExpired:
        return False, "SCP timed out after 15 seconds"
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


@st.fragment(run_every="10s")
def render_live_hud(ledger: Ledger, settings: Settings) -> None:
    st.markdown(
        """
        <style>
        [data-stale="true"] {
            opacity: 1 !important;
            filter: none !important;
            transition: none !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    empty = is_database_empty(ledger)

    if empty:
        st.info("Waiting for first tick... Trading bot has not recorded any quotes, orders, or LIP snapshots yet.")

    buying_power = _get_live_buying_power(settings) if not empty else Decimal("0")
    kpis = compute_kpis(ledger, since_ms=0, buying_power=buying_power, max_collateral=settings.max_collateral)
    official_rewards = _get_official_daily_earnings(settings)

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric(
            label="Balance / Collateral",
            value=f"${kpis['balance_usd']:,.2f}",
            delta=f"Cap: ${kpis['collateral_cap_usd']:,.2f}",
            delta_color="off",
            border=True,
        )
    with c2:
        if official_rewards is not None and official_rewards > Decimal("0"):
            st.metric(
                label="Accrued LIP Rewards",
                value=f"${official_rewards:,.4f}",
                delta=f"Est Yield: ${kpis['lip_reward_total']:,.4f}",
                delta_color="off",
                border=True,
            )
        else:
            st.metric(
                label="Accrued LIP Rewards",
                value=f"${kpis['lip_reward_total']:,.4f}",
                delta="LIP Yield",
                delta_color="off",
                border=True,
            )
    with c3:
        st.metric(
            label="Qualifying Seconds",
            value=f"{kpis['qualifying_seconds']:,}s",
            delta=f"Bid: {kpis['qualifying_bid_seconds']}s | Ask: {kpis['qualifying_ask_seconds']}s",
            delta_color="off",
            border=True,
        )
    with c4:
        st.metric(
            label="Total Fills & Volume",
            value=f"{kpis['total_fills']}",
            delta=f"${kpis['total_volume']:,.2f} Vol",
            delta_color="off",
            border=True,
        )
    with c5:
        pnl_val = kpis["realized_pnl"]
        st.metric(
            label="Realized P&L & Rebates",
            value=f"${pnl_val:+,.4f}",
            delta=f"Rebates: +${kpis['maker_rebates']:,.4f}",
            delta_color="normal" if pnl_val != Decimal("0") else "off",
            border=True,
        )

    st.markdown("### Market Quoting Overview")
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
            })
        df_markets = pd.DataFrame(display_data)
        st.dataframe(df_markets, width="stretch", hide_index=True)
    else:
        st.markdown("_No active quoting markets found._")

    st.markdown("### Fills & Execution Blotter")
    fills = ledger.get_fills_since(0)
    blotter_rows = format_fills_table(fills)
    if blotter_rows:
        df_fills = pd.DataFrame(blotter_rows)
        st.dataframe(df_fills, width="stretch", hide_index=True)
    else:
        st.markdown("_No fills recorded yet._")

    active_positions = get_active_positions(ledger)
    if active_positions:
        st.markdown("### Open Positions")
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

    cb_active, rem_hours = sat_ledger.is_circuit_breaker_active(48.0)
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
            value="HALTED (48h)" if cb_active else "NORMAL",
            delta=f"{rem_hours:.1f}h cooldown left" if cb_active else "Scanning active",
            delta_color="inverse" if cb_active else "normal",
            border=True,
        )

    st.markdown("### Active Paper Positions")
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

    st.markdown("### Closed Trades Blotter")
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
    st.set_page_config(page_title="PMUS Trading HUD", layout="wide", initial_sidebar_state="collapsed")

    settings = Settings.from_env()
    if not check_password(settings):
        st.stop()

    if settings.dashboard_password:
        if st.sidebar.button("Lock Dashboard"):
            st.session_state["authenticated"] = False
            st.rerun()

    db_path = settings.data_dir / "ledger.db"
    ledger = Ledger(db_path)

    header_col, links_col = st.columns([3, 2])
    with header_col:
        st.title("PMUS Trading HUD")
        venue_badge = f"{settings.venue.upper()} | {settings.mode.upper()}"
        st.caption(f"Venue Mode: **{venue_badge}** | Data: `{db_path.name}`")

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

    tab_maker, tab_satellite = st.tabs(["Market Maker (Bot 1)", "Satellite Harvester (Bot 2)"])
    with tab_maker:
        render_live_hud(ledger, settings)
    with tab_satellite:
        render_satellite_hud(settings)


if __name__ == "__main__" or (hasattr(st, "runtime") and st.runtime.exists()):
    main()
