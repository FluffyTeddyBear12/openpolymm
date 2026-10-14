from datetime import datetime, timezone
from decimal import Decimal
import re
import time

from pmus.ledger import Ledger
from pmus.venue import PublicGatewayClient

def parse_duration_ms(since_str: str) -> int:
    match = re.match(r"^(\d+)([smhd])$", since_str.strip().lower())
    if not match:
        return 3600 * 1000
    val, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return val * multipliers[unit] * 1000

def generate_report(
    ledger: Ledger,
    since_str: str = "1h",
    client: PublicGatewayClient | None = None,
    venue: str = "us",
) -> str:
    is_global = venue == "global"
    window_ms = parse_duration_ms(since_str)
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - window_ms

    fills = ledger.get_fills_since(since_ms)
    lip_snaps = ledger.get_lip_snapshots_since(since_ms)
    positions = ledger.get_all_positions()

    total_volume = Decimal("0")
    maker_rebates = Decimal("0")
    taker_fees = Decimal("0")
    realized_pnl = Decimal("0")

    for f in fills:
        trade_val = f.price * f.qty
        total_volume += trade_val
        if f.fee < 0:
            maker_rebates += (-f.fee)
        else:
            taker_fees += f.fee

    lip_reward_total = Decimal("0")
    qualifying_seconds = 0
    qualifying_bid_seconds = 0
    qualifying_ask_seconds = 0
    market_snaps: dict[str, list] = {}

    for s in lip_snaps:
        market_snaps.setdefault(s.slug, []).append(s)
        if s.qualifies:
            qualifying_seconds += 1
        if s.bid_qualifies:
            qualifying_bid_seconds += 1
        if s.ask_qualifies:
            qualifying_ask_seconds += 1
        lip_reward_total += s.reward_usd

    windows_map = {}
    if client is not None:
        try:
            page_size = 0 if is_global else 200
            for w in client.fetch_incentives(status="active", page_size=page_size):
                windows_map[w.slug] = w
        except Exception:
            pass

    market_breakdown_lines = []
    if market_snaps:
        now_dt = datetime.now(timezone.utc)
        for slug, snaps in market_snaps.items():
            bid_secs = sum(1 for s in snaps if s.bid_qualifies)
            ask_secs = sum(1 for s in snaps if s.ask_qualifies)
            mkt_est = sum(s.reward_usd for s in snaps)

            shares = []
            for s in snaps:
                b_sh = (s.trader_bid_score / s.total_bid_score) if s.total_bid_score > 0 else Decimal("0")
                a_sh = (s.trader_ask_score / s.total_ask_score) if s.total_ask_score > 0 else Decimal("0")
                if s.total_bid_score > 0 and s.total_ask_score > 0:
                    sh = (b_sh + a_sh) / Decimal("2")
                elif s.total_bid_score > 0:
                    sh = b_sh
                elif s.total_ask_score > 0:
                    sh = a_sh
                else:
                    sh = Decimal("0")
                shares.append(sh)
            avg_share = (sum(shares) / Decimal(str(len(shares)))) * 100 if shares else Decimal("0")

            win = windows_map.get(slug)
            if is_global:
                pool_str = f"${win.pool_usd:.2f}/day" if win else "N/A"
                market_breakdown_lines.append(
                    f"  - Market {slug}: Pool={pool_str}, Share={avg_share:.1f}%, Est=${mkt_est:.4f} (Qual Secs: Bid={bid_secs}, Ask={ask_secs})"
                )
                continue
            if win:
                period_str = getattr(win, "period", "early")
                pool_str = f"${win.pool_usd:.2f}"
                if win.end is not None:
                    dur_secs = int((win.end - win.start).total_seconds())
                    dur_str = f"{dur_secs}s ({dur_secs / 3600:.1f}h)"
                else:
                    dur_secs = 86400
                    dur_str = "86400s (24.0h)"
            else:
                period_str = "early"
                pool_str = "N/A"
                dur_secs = 86400
                dur_str = "N/A"

            # Compute completion cost
            cost = Decimal("0")
            if client is not None:
                try:
                    bk = client.fetch_book(slug)
                    tgt = win.target_size if win else Decimal("5000")
                    ex_b = sum(lvl.qty for lvl in bk.bids)
                    need_b = max(Decimal("0"), tgt - ex_b)
                    b_px = bk.bids[0].price if bk.bids else Decimal("0.01")
                    cost_b = need_b * b_px

                    ex_a = sum(lvl.qty for lvl in bk.asks)
                    need_a = max(Decimal("0"), tgt - ex_a)
                    a_px = bk.asks[0].price if bk.asks else Decimal("0.02")
                    cost_a = need_a * (Decimal("1") - a_px)

                    valid_costs = [c for c in (cost_b, cost_a) if c > Decimal("0")]
                    cost = min(valid_costs) if valid_costs else Decimal("0")
                except Exception:
                    pass

            rem_est = Decimal("0")
            if win and win.end and win.end > now_dt:
                rem_secs = Decimal(str(max(0, int((win.end - now_dt).total_seconds()))))
                share_dec = avg_share / Decimal("100")
                rate_sec = (win.pool_usd / Decimal(str(max(1, dur_secs)))) * Decimal("0.5") * share_dec
                rem_est = rate_sec * rem_secs

            proj_total = mkt_est + rem_est
            floor_flag = "[< $1.00 PAYOUT FLOOR]" if proj_total < Decimal("1.00") else "[ELIGIBLE FOR PAYOUT]"

            market_breakdown_lines.append(
                f"  - Market {slug} ({period_str}): Pool={pool_str}, Dur={dur_str}, Share={avg_share:.1f}%, Est=${mkt_est:.4f} (Qual Secs: Bid={bid_secs}, Ask={ask_secs}) | Completion Cost=${cost:.2f}, Remainder Est=${rem_est:.4f}, Projected Total=${proj_total:.4f} {floor_flag}"
            )
    else:
        market_breakdown_lines.append("  - (No LIP activity recorded)")

    open_positions_count = 0
    total_inventory_cost = Decimal("0")
    total_inventory_market_val = Decimal("0")
    unrealized_mtm = Decimal("0")

    for p in positions:
        if p.net_qty != Decimal("0"):
            open_positions_count += 1
            cost = p.net_qty * p.avg_price
            total_inventory_cost += cost

            mid = p.avg_price
            if client is not None:
                try:
                    bid, ask = client.fetch_bbo(p.slug)
                    if bid and ask:
                        mid = (bid.price + ask.price) / Decimal("2")
                    elif bid:
                        mid = bid.price
                    elif ask:
                        mid = ask.price
                except Exception:
                    pass

            market_val = p.net_qty * mid
            total_inventory_market_val += market_val
            unrealized_mtm += (market_val - cost)

    net_pnl = realized_pnl + maker_rebates + lip_reward_total - taker_fees + unrealized_mtm

    venue_name = "POLYMARKET GLOBAL" if is_global else "POLYMARKET US"
    caveat = (
        "  * Polymarket Global pays liquidity rewards daily; days under $1.00 pay $0.00. LIP figures are model estimates."
        if is_global
        else "  * Minimum Payout Caveat: Polymarket US enforces a $1.00 minimum payout per market-period total; periods earning <$1.00 pay $0.00."
    )

    lines = [
        "============================================================",
        f"  {venue_name} PERFORMANCE REPORT (SINCE {since_str.upper()})",
        "============================================================",
        f"  Total Fills:             {len(fills)}",
        f"  Total Trade Volume:      ${total_volume:.2f}",
        f"  Maker Rebates Earned:    ${maker_rebates:.4f}",
        f"  Taker Fees Paid:         ${taker_fees:.4f}",
        f"  Realized P&L:            ${realized_pnl:.4f}",
        "------------------------------------------------------------",
        f"  LIP Snapshots Recorded:  {len(lip_snaps)}",
        f"  LIP Qualifying Seconds:  {qualifying_seconds} (Bid: {qualifying_bid_seconds}, Ask: {qualifying_ask_seconds})",
        f"  Estimated LIP Accrued:   ${lip_reward_total:.4f} (estimate)",
        "------------------------------------------------------------",
        "  LIP MARKET BREAKDOWN:",
        *market_breakdown_lines,
        "------------------------------------------------------------",
        f"  Active Positions:        {open_positions_count}",
        f"  Inventory Net Cost:      ${total_inventory_cost:.2f}",
        f"  Inventory Mark-to-Market:${total_inventory_market_val:.2f}",
        f"  Unrealized MTM P&L:      ${unrealized_mtm:.4f}",
        "------------------------------------------------------------",
        f"  ESTIMATED NET EARNINGS:  ${net_pnl:.4f}",
        "============================================================",
        caveat,
        "============================================================",
    ]
    return "\n".join(lines)
