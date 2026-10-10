import re
import time
from decimal import Decimal

from pmus.ledger import Ledger

def parse_duration_ms(since_str: str) -> int:
    match = re.match(r"^(\d+)([smhd])$", since_str.strip().lower())
    if not match:
        return 3600 * 1000
    val, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return val * multipliers[unit] * 1000

def generate_report(ledger: Ledger, since_str: str = "1h") -> str:
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
    for p in positions:
        if p.net_qty != 0:
            open_positions_count += 1
            total_inventory_cost += p.net_qty * p.avg_price

    net_pnl = realized_pnl + maker_rebates + lip_reward_total - taker_fees

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
        f"  Estimated LIP Accrued:   ${lip_reward_total:.4f}",
        "------------------------------------------------------------",
        f"  Active Positions:        {open_positions_count}",
        f"  Inventory Net Cost:      ${total_inventory_cost:.2f}",
        "------------------------------------------------------------",
        f"  ESTIMATED NET EARNINGS:  ${net_pnl:.4f}",
        "============================================================",
    ]
    return "\n".join(lines)
