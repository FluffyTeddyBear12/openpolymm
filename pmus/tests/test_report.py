from decimal import Decimal
import pytest

from pmus.ledger import Ledger
from pmus.models import LipSnapshot, Position, ShadowFill
from pmus.report import generate_report, parse_duration_ms

def test_parse_duration_ms():
    assert parse_duration_ms("10s") == 10000
    assert parse_duration_ms("5m") == 300000
    assert parse_duration_ms("1h") == 3600000
    assert parse_duration_ms("2d") == 172800000

def test_generate_report(tmp_path):
    import time
    ledger = Ledger(tmp_path / "test_report_ledger.db")
    now_ms = int(time.time() * 1000)

    fill = ShadowFill(
        fill_id="f-1",
        order_id="o-1",
        slug="mkt-1",
        side="BUY_LONG",
        price=Decimal("0.50"),
        qty=Decimal("10"),
        fee=Decimal("-0.0312"),
        is_maker=True,
        ts_ms=now_ms,
    )
    ledger.record_fill(fill)

    snap = LipSnapshot(
        ts_ms=now_ms,
        slug="mkt-1",
        program_id="p-1",
        trader_bid_score=Decimal("10"),
        total_bid_score=Decimal("100"),
        trader_ask_score=Decimal("10"),
        total_ask_score=Decimal("100"),
        qualifies=True,
        reward_usd=Decimal("0.05"),
    )
    ledger.record_lip_snapshot(snap)

    ledger.update_position(Position(slug="mkt-1", net_qty=Decimal("10"), avg_price=Decimal("0.50")))

    report_text = generate_report(ledger, since_str="1h")
    assert "Total Fills:             1" in report_text
    assert "Maker Rebates Earned:    $0.0312" in report_text
    assert "Estimated LIP Accrued:   $0.0500 (estimate)" in report_text
    assert "Inventory Mark-to-Market:$5.00" in report_text
