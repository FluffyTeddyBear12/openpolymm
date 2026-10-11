from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pmus.models import Book, IncentiveWindow, Level, LiveOrder, Market, Position, Quote
from pmus.shadow import estimate_global_lip_score
from pmus.strategy import global_rewards
from pmus.strategy.lip_quoter import desired_quotes
from pmus.strategy.market_selector import greedy_allocate

NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
V = Decimal("0.03")


def window(slug="tok", pool="100", target="50", max_spread=V) -> IncentiveWindow:
    return IncentiveWindow(
        slug=slug,
        program_id=f"p-{slug}",
        pool_usd=Decimal(pool),
        discount=Decimal("0.30"),
        target_size=Decimal(target),
        max_spread=max_spread,
        start=NOW - timedelta(hours=1),
        end=NOW + timedelta(hours=23),
        status="active",
    )


def book(bids, asks, slug="tok") -> Book:
    return Book(
        slug,
        tuple(Level(Decimal(p), Decimal(q)) for p, q in bids),
        tuple(Level(Decimal(p), Decimal(q)) for p, q in asks),
        0,
    )


def market(slug="tok", tick="0.01") -> Market:
    return Market(slug=slug, event_id=f"ev-{slug}", category="global", tick=Decimal(tick), min_qty=Decimal("5"), active=True)


def flat(slug="tok") -> Position:
    return Position(slug, Decimal("0"), Decimal("0"))


def test_expected_reward_is_quadratic_single_sided_share():
    b = book([("0.49", "100"), ("0.47", "100")], [("0.51", "100")])
    w = window()

    rate = global_rewards.expected_bid_reward_sec(b, w, Decimal("0.49"), Decimal("50"))

    weight = ((V - Decimal("0.01")) / V) ** 2
    ours = weight * 50 / 3
    competitors = weight * 100
    expected = Decimal("100") / Decimal("86400") * ours / (competitors + ours)
    assert abs(rate - expected) < Decimal("1e-20")


def test_order_outside_max_spread_or_below_min_size_scores_zero():
    b = book([("0.49", "100")], [("0.51", "100")])
    w = window()
    assert global_rewards.expected_bid_reward_sec(b, w, Decimal("0.47"), Decimal("50")) == 0
    assert global_rewards.expected_bid_reward_sec(b, w, Decimal("0.49"), Decimal("49")) == 0


def test_single_sided_scores_zero_outside_reward_band():
    s = Decimal("10")
    assert global_rewards.combined_score(s, Decimal("0"), Decimal("0.50")) == s / 3
    assert global_rewards.combined_score(s, Decimal("0"), Decimal("0.05")) == 0
    assert global_rewards.combined_score(s, Decimal("4"), Decimal("0.95")) == Decimal("4")


def test_adjusted_mid_ignores_levels_below_min_size():
    b = book([("0.50", "1"), ("0.40", "100")], [("0.52", "100")])
    assert global_rewards.adjusted_mid(b, Decimal("50")) == Decimal("0.46")


def test_bid_price_joins_best_bid_on_market_tick_within_spread():
    w = window()
    assert global_rewards.bid_price(book([("0.495", "100")], [("0.52", "100")]), w, Decimal("0.01")) == Decimal("0.49")
    assert global_rewards.bid_price(book([("0.44", "100")], [("0.56", "100")]), w, Decimal("0.01")) is None


def test_selector_skips_tail_and_one_sided_books():
    windows = [window("tail-low"), window("tail-high"), window("one-sided")]
    books = {
        "tail-low": book([("0.04", "10")], [("0.06", "10")], "tail-low"),
        "tail-high": book([("0.94", "10")], [("0.96", "10")], "tail-high"),
        "one-sided": book([("0.50", "10")], [], "one-sided"),
    }

    res = greedy_allocate(windows, books, total_budget=Decimal("60"), now=NOW, is_global=True)

    assert res.selected_slugs == []
    assert "3 outside 0.10-0.90 mid or one-sided" in res.unfunded_reason


def test_selector_applies_one_dollar_daily_floor_to_global():
    b = book([("0.49", "100")], [("0.51", "100")])
    w_small = window(pool="1")

    res = greedy_allocate([w_small], {"tok": b}, total_budget=Decimal("60"), now=NOW, is_global=True)

    assert res.selected_slugs == []
    assert "below $1/day" in res.unfunded_reason


def test_selector_reports_nonzero_expected_reward_and_respects_per_market_cap():
    b = book([("0.49", "100")], [("0.51", "100")])
    w = window(pool="100")

    res = greedy_allocate([w], {"tok": b}, total_budget=Decimal("60"), now=NOW, is_global=True)
    capped = greedy_allocate([w], {"tok": b}, total_budget=Decimal("60"), now=NOW, is_global=True, max_cost_per_market=Decimal("20"))

    cand = res.selected_candidates[0]
    assert cand.expected_reward_sec * 86400 >= Decimal("1")
    assert cand.cost == Decimal("50") * Decimal("0.49")
    assert capped.selected_slugs == []


def test_selector_and_quoter_post_the_same_price_on_market_tick():
    b = book([("0.4937", "100")], [("0.5100", "100")])
    w = window()

    res = greedy_allocate([w], {"tok": b}, total_budget=Decimal("60"), now=NOW, is_global=True, ticks={"tok": Decimal("0.01")})
    quotes = desired_quotes(market(), b, w, flat(), budget=Decimal("30"), bid_budget=res.side_budgets[("tok", "bid")], ask_budget=Decimal("0"), is_global=True)

    assert res.selected_candidates[0].price == Decimal("0.49")
    assert quotes == [Quote("tok", "BUY_LONG", Decimal("0.49"), Decimal("50"))]


def test_quoter_posts_no_bid_in_tail_market_but_keeps_inventory_exit():
    b = book([("0.05", "100")], [("0.07", "100")])
    w = window()

    assert desired_quotes(market(), b, w, flat(), budget=Decimal("30"), is_global=True) == []
    long_quotes = desired_quotes(market(), b, w, Position("tok", Decimal("50"), Decimal("0.06")), budget=Decimal("30"), is_global=True)
    assert long_quotes == [Quote("tok", "SELL_LONG", Decimal("0.07"), Decimal("50"))]


def test_quoter_never_posts_sell_without_inventory():
    b = book([("0.49", "100")], [("0.51", "100")])
    quotes = desired_quotes(market(), b, window(), flat(), budget=Decimal("100"), is_global=True)
    assert [q.side for q in quotes] == ["BUY_LONG"]


def test_global_snapshot_scores_single_sided_bid_at_one_third():
    b = book([("0.49", "100")], [("0.51", "100")])
    ours = [LiveOrder("o1", "tok", "BUY_LONG", Decimal("0.49"), Decimal("50"))]

    snap = estimate_global_lip_score(b, window(), ours, ts_ms=1)

    weight = ((V - Decimal("0.01")) / V) ** 2
    assert abs(snap.trader_bid_score - weight * 50) < Decimal("1e-20")
    assert abs(snap.total_bid_score - weight * 150) < Decimal("1e-20")
    assert snap.bid_qualifies is True
    assert abs(snap.reward_usd - global_rewards.reward_rate(Decimal("100"), weight * 50 / 3, weight * 100)) < Decimal("1e-20")


def test_global_snapshot_single_sided_tail_market_earns_nothing():
    b = book([("0.05", "100")], [("0.07", "100")])
    ours = [LiveOrder("o1", "tok", "BUY_LONG", Decimal("0.05"), Decimal("50"))]

    snap = estimate_global_lip_score(b, window(), ours, ts_ms=1)

    assert snap.qualifies is False
    assert snap.reward_usd == 0


def test_selector_honors_configured_global_floor(monkeypatch):
    b = book([("0.49", "100")], [("0.51", "100")])
    w = window(pool="5")

    default = greedy_allocate([w], {"tok": b}, total_budget=Decimal("60"), now=NOW, is_global=True)
    monkeypatch.setattr(global_rewards, "MIN_DAILY_PAYOUT_USD", Decimal("0.50"))
    lowered = greedy_allocate([w], {"tok": b}, total_budget=Decimal("60"), now=NOW, is_global=True)

    assert default.selected_slugs == []
    assert lowered.selected_slugs == ["tok"]
