from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pmus.ledger import Ledger
from pmus.models import Book, IncentiveWindow, Level, LiveOrder
from pmus.report import generate_report
from pmus.shadow import estimate_global_pair_score

NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)


def make_window(
    slug="cond-1",
    pool="100",
    target="50",
    max_spread=Decimal("0.03"),
    status="active",
) -> IncentiveWindow:
    return IncentiveWindow(
        slug=slug,
        program_id=f"p-{slug}",
        pool_usd=Decimal(pool),
        discount=Decimal("0.30"),
        target_size=Decimal(target),
        max_spread=max_spread,
        start=NOW - timedelta(hours=1),
        end=NOW + timedelta(hours=23),
        status=status,
    )


def make_book(slug: str, bid_px: str, ask_px: str, qty="100") -> Book:
    return Book(
        slug=slug,
        bids=(Level(Decimal(bid_px), Decimal(qty)),),
        asks=(Level(Decimal(ask_px), Decimal(qty)),),
        ts_ms=1000,
    )


def test_symmetric_pair_gives_approx_three_times_single_in_band():
    w = make_window(pool="100", target="50", max_spread=Decimal("0.03"))
    yes_b = make_book("tok-yes", "0.49", "0.51", "100")
    no_b = make_book("tok-no", "0.49", "0.51", "100")

    o_yes = LiveOrder("o-y", "tok-yes", "BUY_LONG", Decimal("0.49"), Decimal("50"))
    o_no = LiveOrder("o-n", "tok-no", "BUY_SHORT", Decimal("0.49"), Decimal("50"))

    snap_single_y, snap_single_n = estimate_global_pair_score(yes_b, no_b, w, [o_yes], ts_ms=2000)
    single_total = snap_single_y.reward_usd + snap_single_n.reward_usd

    expected_single = (Decimal("100") / Decimal("86400")) * (Decimal("1") / Decimal("7"))
    assert abs(single_total - expected_single) < Decimal("1e-20")
    assert abs(snap_single_y.reward_usd - expected_single) < Decimal("1e-20")
    assert snap_single_n.reward_usd == Decimal("0")
    assert abs(snap_single_y.trader_bid_score - Decimal("200") / Decimal("9")) < Decimal("1e-20")
    assert abs(snap_single_y.total_bid_score - Decimal("600") / Decimal("9")) < Decimal("1e-20")
    assert snap_single_n.trader_bid_score == Decimal("0")
    assert abs(snap_single_n.total_bid_score - Decimal("400") / Decimal("9")) < Decimal("1e-20")
    assert snap_single_y.qualifies is True
    assert snap_single_y.bid_qualifies is True
    assert snap_single_n.qualifies is True
    assert snap_single_n.bid_qualifies is False

    snap_pair_y, snap_pair_n = estimate_global_pair_score(yes_b, no_b, w, [o_yes, o_no], ts_ms=2000)
    pair_total = snap_pair_y.reward_usd + snap_pair_n.reward_usd

    expected_pair = (Decimal("100") / Decimal("86400")) * (Decimal("1") / Decimal("3"))
    assert abs(pair_total - expected_pair) < Decimal("1e-20")
    assert abs(snap_pair_y.reward_usd - expected_pair / Decimal("2")) < Decimal("1e-20")
    assert abs(snap_pair_n.reward_usd - expected_pair / Decimal("2")) < Decimal("1e-20")
    assert abs(snap_pair_y.trader_bid_score - Decimal("200") / Decimal("9")) < Decimal("1e-20")
    assert abs(snap_pair_y.total_bid_score - Decimal("600") / Decimal("9")) < Decimal("1e-20")
    assert abs(snap_pair_n.trader_bid_score - Decimal("200") / Decimal("9")) < Decimal("1e-20")
    assert abs(snap_pair_n.total_bid_score - Decimal("600") / Decimal("9")) < Decimal("1e-20")
    assert snap_pair_y.qualifies is True
    assert snap_pair_y.bid_qualifies is True
    assert snap_pair_n.qualifies is True
    assert snap_pair_n.bid_qualifies is True

    ratio = pair_total / single_total
    assert abs(ratio - Decimal("7") / Decimal("3")) < Decimal("1e-20")
    assert Decimal("2") < ratio < Decimal("3.5")


def test_tail_market_single_zero_pair_positive():
    w = make_window(slug="cond-tail", pool="100", target="50", max_spread=Decimal("0.02"))
    yes_b = make_book("tok-yes", "0.04", "0.06", "100")
    no_b = make_book("tok-no", "0.94", "0.96", "100")

    o_yes = LiveOrder("o-y", "tok-yes", "BUY_LONG", Decimal("0.04"), Decimal("50"))
    o_no = LiveOrder("o-n", "tok-no", "BUY_SHORT", Decimal("0.94"), Decimal("50"))

    snap_single_y, snap_single_n = estimate_global_pair_score(yes_b, no_b, w, [o_yes])
    assert snap_single_y.reward_usd == Decimal("0")
    assert snap_single_n.reward_usd == Decimal("0")
    assert snap_single_y.qualifies is False
    assert snap_single_y.bid_qualifies is False
    assert snap_single_n.qualifies is False
    assert snap_single_n.bid_qualifies is False

    snap_pair_y, snap_pair_n = estimate_global_pair_score(yes_b, no_b, w, [o_yes, o_no])
    assert snap_pair_y.qualifies is True
    assert snap_pair_n.qualifies is True
    assert snap_pair_y.bid_qualifies is True
    assert snap_pair_n.bid_qualifies is True
    assert snap_pair_y.reward_usd > Decimal("0")
    assert snap_pair_n.reward_usd > Decimal("0")

    expected_pair = (Decimal("100") / Decimal("86400")) * (Decimal("1") / Decimal("3"))
    assert abs((snap_pair_y.reward_usd + snap_pair_n.reward_usd) - expected_pair) < Decimal("1e-20")


def test_reward_split_proportional():
    w = make_window(pool="100", target="50", max_spread=Decimal("0.03"))
    yes_b = make_book("tok-yes", "0.49", "0.51", "100")
    no_b = make_book("tok-no", "0.49", "0.51", "100")

    o_yes = LiveOrder("o-y", "tok-yes", "BUY_LONG", Decimal("0.49"), Decimal("100"))
    o_no = LiveOrder("o-n", "tok-no", "BUY_SHORT", Decimal("0.49"), Decimal("50"))

    snap_y, snap_n = estimate_global_pair_score(yes_b, no_b, w, [o_yes, o_no])
    assert abs(snap_y.trader_bid_score - Decimal("400") / Decimal("9")) < Decimal("1e-20")
    assert abs(snap_n.trader_bid_score - Decimal("200") / Decimal("9")) < Decimal("1e-20")
    assert abs(snap_y.reward_usd - (Decimal("2") * snap_n.reward_usd)) < Decimal("1e-20")

    total_r = snap_y.reward_usd + snap_n.reward_usd
    assert snap_y.reward_usd == (total_r * Decimal("2")) / Decimal("3")
    assert snap_n.reward_usd == (total_r * Decimal("1")) / Decimal("3")

    snap_only_no_y, snap_only_no_n = estimate_global_pair_score(yes_b, no_b, w, [o_no])
    assert snap_only_no_y.reward_usd == Decimal("0")
    assert snap_only_no_n.reward_usd > Decimal("0")
    assert snap_only_no_y.bid_qualifies is False
    assert snap_only_no_n.bid_qualifies is True


def test_orders_below_target_size_ignored():
    w = make_window(pool="100", target="50", max_spread=Decimal("0.03"))
    yes_b = make_book("tok-yes", "0.49", "0.51", "100")
    no_b = make_book("tok-no", "0.49", "0.51", "100")

    o_small_y = LiveOrder("o-sy", "tok-yes", "BUY_LONG", Decimal("0.49"), Decimal("49.99"))
    o_small_n = LiveOrder("o-sn", "tok-no", "BUY_SHORT", Decimal("0.49"), Decimal("10"))

    snap_y, snap_n = estimate_global_pair_score(yes_b, no_b, w, [o_small_y, o_small_n])
    assert snap_y.trader_bid_score == Decimal("0")
    assert snap_n.trader_bid_score == Decimal("0")
    assert abs(snap_y.total_bid_score - Decimal("400") / Decimal("9")) < Decimal("1e-20")
    assert abs(snap_n.total_bid_score - Decimal("400") / Decimal("9")) < Decimal("1e-20")
    assert snap_y.reward_usd == Decimal("0")
    assert snap_n.reward_usd == Decimal("0")
    assert snap_y.qualifies is False
    assert snap_n.qualifies is False
    assert snap_y.bid_qualifies is False
    assert snap_n.bid_qualifies is False


def test_none_or_inactive_window_zero_snapshots():
    yes_b = make_book("tok-yes", "0.49", "0.51", "100")
    no_b = make_book("tok-no", "0.49", "0.51", "100")
    orders = [
        LiveOrder("o-y", "tok-yes", "BUY_LONG", Decimal("0.49"), Decimal("50")),
        LiveOrder("o-n", "tok-no", "BUY_SHORT", Decimal("0.49"), Decimal("50")),
    ]

    snap_y, snap_n = estimate_global_pair_score(yes_b, no_b, None, orders, ts_ms=12345)
    assert snap_y.slug == "tok-yes"
    assert snap_n.slug == "tok-no"
    assert snap_y.program_id == "none"
    assert snap_n.program_id == "none"
    assert snap_y.ts_ms == 12345
    assert snap_n.ts_ms == 12345
    assert snap_y.qualifies is False
    assert snap_n.qualifies is False
    assert snap_y.bid_qualifies is False
    assert snap_n.bid_qualifies is False
    assert snap_y.trader_bid_score == Decimal("0")
    assert snap_n.trader_bid_score == Decimal("0")
    assert snap_y.total_bid_score == Decimal("0")
    assert snap_n.total_bid_score == Decimal("0")
    assert snap_y.reward_usd == Decimal("0")
    assert snap_n.reward_usd == Decimal("0")

    w_inactive = make_window(status="inactive")
    snap_iy, snap_in = estimate_global_pair_score(yes_b, no_b, w_inactive, orders, ts_ms=12345)
    assert snap_iy.program_id == w_inactive.program_id
    assert snap_in.program_id == w_inactive.program_id
    assert snap_iy.reward_usd == Decimal("0")
    assert snap_in.reward_usd == Decimal("0")
    assert snap_iy.qualifies is False
    assert snap_in.qualifies is False

    w_no_spread = make_window(max_spread=None)
    snap_sy, snap_sn = estimate_global_pair_score(yes_b, no_b, w_no_spread, orders)
    assert snap_sy.reward_usd == Decimal("0")
    assert snap_sn.reward_usd == Decimal("0")


def test_missing_mids_or_ask_orders_score_zero():
    w = make_window(pool="100", target="50", max_spread=Decimal("0.03"))
    empty_bids_yes = Book("tok-yes", (), (Level(Decimal("0.51"), Decimal("100")),), 1000)
    normal_no = make_book("tok-no", "0.49", "0.51", "100")
    orders = [
        LiveOrder("o-y", "tok-yes", "BUY_LONG", Decimal("0.49"), Decimal("50")),
        LiveOrder("o-n", "tok-no", "BUY_SHORT", Decimal("0.49"), Decimal("50")),
    ]

    snap_y, snap_n = estimate_global_pair_score(empty_bids_yes, normal_no, w, orders)
    assert snap_y.reward_usd == Decimal("0")
    assert snap_n.reward_usd == Decimal("0")
    assert snap_y.qualifies is False
    assert snap_n.qualifies is False

    yes_b = make_book("tok-yes", "0.49", "0.51", "100")
    ask_orders = [
        LiveOrder("o-ay", "tok-yes", "SELL_LONG", Decimal("0.51"), Decimal("50")),
        LiveOrder("o-an", "tok-no", "SELL_SHORT", Decimal("0.51"), Decimal("50")),
    ]
    snap_ay, snap_an = estimate_global_pair_score(yes_b, normal_no, w, ask_orders)
    assert snap_ay.trader_bid_score == Decimal("0")
    assert snap_an.trader_bid_score == Decimal("0")
    assert snap_ay.trader_ask_score == Decimal("0")
    assert snap_an.trader_ask_score == Decimal("0")
    assert snap_ay.reward_usd == Decimal("0")
    assert snap_an.reward_usd == Decimal("0")


def test_report_sums_both_tokens_without_dedup_drop(tmp_path):
    w = make_window(pool="100", target="50", max_spread=Decimal("0.03"))
    yes_b = make_book("tok-yes", "0.49", "0.51", "100")
    no_b = make_book("tok-no", "0.49", "0.51", "100")
    orders = [
        LiveOrder("o-y", "tok-yes", "BUY_LONG", Decimal("0.49"), Decimal("50")),
        LiveOrder("o-n", "tok-no", "BUY_SHORT", Decimal("0.49"), Decimal("50")),
    ]

    snap_y, snap_n = estimate_global_pair_score(yes_b, no_b, w, orders, ts_ms=NOW.timestamp() * 1000)
    condition_total = snap_y.reward_usd + snap_n.reward_usd

    ledger = Ledger(tmp_path / "global_pair_report.db")
    ledger.record_lip_snapshot(snap_y)
    ledger.record_lip_snapshot(snap_n)

    report_text = generate_report(ledger, since_str="1h", venue="global")
    assert f"Estimated LIP Accrued:   ${condition_total:.4f} (estimate)" in report_text
    assert f"Market {yes_b.slug}:" in report_text
    assert f"Market {no_b.slug}:" in report_text
