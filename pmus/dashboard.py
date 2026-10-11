import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import time
from typing import Any

# Ensure repository root is on sys.path
_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import pandas as pd
import streamlit as st

from pmus.config import Settings
from pmus.ledger import Ledger
from pmus.models import LipSnapshot, LiveOrder, Position, ShadowFill


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
    lip_snaps = ledger.get_lip_snapshots_since(since_ms)

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

    lip_reward_total = Decimal("0")
    qualifying_seconds = 0
    qualifying_bid_seconds = 0
    qualifying_ask_seconds = 0

    for s in lip_snaps:
        if s.qualifies:
            qualifying_seconds += 1
        if s.bid_qualifies:
            qualifying_bid_seconds += 1
        if s.ask_qualifies:
            qualifying_ask_seconds += 1
        lip_reward_total += s.reward_usd

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
    snaps = ledger.get_lip_snapshots_since(recent_ms)

    latest_snaps: dict[str, LipSnapshot] = {}
    for s in snaps:
        curr = latest_snaps.get(s.slug)
        if curr is None or s.ts_ms >= curr.ts_ms:
            latest_snaps[s.slug] = s

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

        rows.append({
            "slug": slug,
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


def format_fills_table(fills: list[ShadowFill]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in sorted(fills, key=lambda x: x.ts_ms, reverse=True):
        dt = datetime.fromtimestamp(f.ts_ms / 1000, tz=timezone.utc)
        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        if f.fee < Decimal("0"):
            fee_str = f"+${abs(f.fee):.4f}"
        else:
            fee_str = f"-${f.fee:.4f}"
        rows.append({
            "Timestamp": ts_str,
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


def _get_live_buying_power(settings: Settings) -> Decimal:
    if settings.venue == "global" and settings.polygon_private_key:
        try:
            from pmus.clob_venue import ClobVenueClient
            client = ClobVenueClient(settings)
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


@st.fragment(run_every="2s")
def render_live_hud(ledger: Ledger, settings: Settings) -> None:
    empty = is_database_empty(ledger)

    if empty:
        st.info("Waiting for first tick... Trading bot has not recorded any quotes, orders, or LIP snapshots yet.")

    buying_power = _get_live_buying_power(settings) if not empty else Decimal("0")
    kpis = compute_kpis(ledger, since_ms=0, buying_power=buying_power, max_collateral=settings.max_collateral)

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
            display_data.append({
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
        st.dataframe(df_markets, use_container_width=True, hide_index=True)
    else:
        st.markdown("_No active quoting markets found._")

    st.markdown("### Fills & Execution Blotter")
    fills = ledger.get_fills_since(0)
    blotter_rows = format_fills_table(fills)
    if blotter_rows:
        df_fills = pd.DataFrame(blotter_rows)
        st.dataframe(df_fills, use_container_width=True, hide_index=True)
    else:
        st.markdown("_No fills recorded yet._")

    positions = ledger.get_all_positions()
    active_positions = [p for p in positions if p.net_qty != Decimal("0")]
    if active_positions:
        st.markdown("### Open Positions")
        pos_data = [
            {
                "Slug": p.slug,
                "Net Qty": f"{p.net_qty:,}",
                "Avg Price": f"${p.avg_price:.4f}",
                "Cost (USD)": f"${(p.net_qty * p.avg_price):,.2f}",
            }
            for p in active_positions
        ]
        st.dataframe(pd.DataFrame(pos_data), use_container_width=True, hide_index=True)

    st.caption(f"Last updated: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} (auto-refreshing every 2s)")


def main() -> None:
    st.set_page_config(page_title="PMUS Trading HUD", layout="wide", initial_sidebar_state="collapsed")

    settings = Settings.from_env()
    db_path = settings.data_dir / "ledger.db"
    ledger = Ledger(db_path)

    header_col, links_col = st.columns([3, 2])
    with header_col:
        st.title("PMUS Trading HUD")
        venue_badge = f"{settings.venue.upper()} | {settings.mode.upper()}"
        st.caption(f"Venue Mode: **{venue_badge}** | Data: `{db_path.name}`")

    with links_col:
        links = get_profile_links(settings.polymarket_proxy_address)
        l_col1, l_col2 = st.columns(2)
        with l_col1:
            st.link_button("Polymarket Profile", links["polymarket"], use_container_width=True)
        with l_col2:
            st.link_button("Polygonscan", links["polygonscan"], use_container_width=True)
        if settings.polymarket_proxy_address:
            addr = settings.polymarket_proxy_address
            short_addr = f"{addr[:6]}...{addr[-4:]}" if len(addr) > 10 else addr
            st.caption(f"Proxy: `{short_addr}`")

    render_live_hud(ledger, settings)


if __name__ == "__main__" or (hasattr(st, "runtime") and st.runtime.exists()):
    main()
