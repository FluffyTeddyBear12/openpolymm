import re
import time
from decimal import Decimal

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
) -> str:
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
    for s in lip_snaps:
        if s.qualifies:
            qualifying_seconds += 1
            lip_reward_total += s.reward_usd

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

    lines = [
        "============================================================",
        f"  POLYMARKET US PERFORMANCE REPORT (SINCE {since_str.upper()})",
        "============================================================",
        f"  Total Fills:             {len(fills)}",
        f"  Total Trade Volume:      ${total_volume:.2f}",
        f"  Maker Rebates Earned:    ${maker_rebates:.4f}",
        f"  Taker Fees Paid:         ${taker_fees:.4f}",
        f"  Realized P&L:            ${realized_pnl:.4f}",
        "------------------------------------------------------------",
        f"  LIP Snapshots Recorded:  {len(lip_snaps)}",
        f"  LIP Qualifying Seconds:  {qualifying_seconds}",
        f"  Estimated LIP Accrued:   ${lip_reward_total:.4f} (estimate)",
        "------------------------------------------------------------",
        f"  Active Positions:        {open_positions_count}",
        f"  Inventory Net Cost:      ${total_inventory_cost:.2f}",
        f"  Inventory Mark-to-Market:${total_inventory_market_val:.2f}",
        f"  Unrealized MTM P&L:      ${unrealized_mtm:.4f}",
        "------------------------------------------------------------",
        f"  ESTIMATED NET EARNINGS:  ${net_pnl:.4f}",
        "============================================================",
    ]
    return "\n".join(lines)
