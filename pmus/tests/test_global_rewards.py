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


def test_bid_price_ignores_dust_spoof_bid_below_target_size():
    b = book([("0.48", "1"), ("0.47", "100")], [("0.51", "100")])
    w = window(target="50")
    assert global_rewards.bid_price(b, w, Decimal("0.01")) == Decimal("0.47")


def test_bid_price_steps_behind_top_qualifying_bid_on_multi_level_book():
    b_cent = book([("0.48", "100"), ("0.47", "100")], [("0.50", "100")])
    w_cent = window(target="50", max_spread=Decimal("0.04"))
    assert global_rewards.bid_price(b_cent, w_cent, Decimal("0.01")) == Decimal("0.47")

    b_milli = book([("0.401", "100"), ("0.395", "100")], [("0.415", "100")])
    w_milli = window(target="50", max_spread=Decimal("0.030"))
    assert global_rewards.bid_price(b_milli, w_milli, Decimal("0.001")) == Decimal("0.400")

    b_wide_cent = book([("0.59", "100"), ("0.58", "100")], [("0.62", "100")])
    w_wide_cent = window(target="50", max_spread=Decimal("0.03"))
    assert global_rewards.bid_price(b_wide_cent, w_wide_cent, Decimal("0.01")) == Decimal("0.59")


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
    assert abs(snap.total_bid_score - weight * 100) < Decimal("1e-20")
    assert snap.bid_qualifies is True
    assert abs(snap.reward_usd - global_rewards.reward_rate(Decimal("100"), weight * 50 / 3, weight * 50)) < Decimal("1e-20")


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


def test_strip_orders_from_book_removes_own_resting_bid_and_preserves_other_levels():
    b = Book(
        slug="mkt-1",
        bids=(
            Level(price=Decimal("0.48"), qty=Decimal("100")),
            Level(price=Decimal("0.47"), qty=Decimal("100")),
        ),
        asks=(
            Level(price=Decimal("0.52"), qty=Decimal("100")),
            Level(price=Decimal("0.53"), qty=Decimal("100")),
        ),
        ts_ms=1000,
    )
    orders = [
        LiveOrder(order_id="o1", slug="mkt-1", side="BUY_LONG", price=Decimal("0.48"), qty_open=Decimal("100")),
        LiveOrder(order_id="o2", slug="mkt-1", side="SELL_LONG", price=Decimal("0.52"), qty_open=Decimal("40")),
        LiveOrder(order_id="o3", slug="other-slug", side="BUY_LONG", price=Decimal("0.47"), qty_open=Decimal("100")),
    ]
    stripped = global_rewards.strip_orders_from_book(b, orders)
    assert stripped.slug == "mkt-1"
    assert stripped.bids == (Level(price=Decimal("0.47"), qty=Decimal("100")),)
    assert stripped.asks == (
        Level(price=Decimal("0.52"), qty=Decimal("60")),
        Level(price=Decimal("0.53"), qty=Decimal("100")),
    )
    assert global_rewards.strip_orders_from_book(b, []) == b


def test_bid_price_returns_none_when_no_qualifying_bids():
    b = book([("0.48", "10")], [("0.52", "100")])
    w = window(target="50")
    assert global_rewards.bid_price(b, w, Decimal("0.01")) is None


def test_bid_price_steps_back_when_single_qualifying_bid_under_2x_target_size():
    b = book([("0.48", "60")], [("0.50", "100")])
    w = window(target="50", max_spread=Decimal("0.04"))
    assert global_rewards.bid_price(b, w, Decimal("0.01")) == Decimal("0.47")

    b_thick = book([("0.48", "120")], [("0.50", "100")])
    assert global_rewards.bid_price(b_thick, w, Decimal("0.01")) == Decimal("0.48")


def test_pair_quote_prices_steps_back_when_sum_exceeds_098():
    w = window(target="50", max_spread=Decimal("0.03"))
    yes_b = book([("0.49", "200")], [("0.51", "100")])
    no_b = book([("0.50", "200")], [("0.51", "100")])
    prices = global_rewards.pair_quote_prices(yes_b, no_b, w, Decimal("0.01"), Decimal("0.01"))
    assert prices is not None
    p_yes, p_no = prices
    assert p_yes + p_no <= Decimal("0.98")
    assert p_yes == Decimal("0.49")
    assert p_no == Decimal("0.49")


def test_bid_price_steps_behind_second_qualifying_level_when_top_level_thin():
    w = window(target="50", max_spread=Decimal("0.040"))
    b_milli_deep = book([("0.401", "50"), ("0.398", "80")], [("0.415", "100")])
    assert global_rewards.bid_price(b_milli_deep, w, Decimal("0.001")) == Decimal("0.397")

    b_milli_thick = book([("0.401", "120"), ("0.398", "60")], [("0.415", "100")])
    assert global_rewards.bid_price(b_milli_thick, w, Decimal("0.001")) == Decimal("0.400")

    b_milli_solo_thin = book([("0.401", "50")], [("0.415", "100")])
    assert global_rewards.bid_price(b_milli_solo_thin, w, Decimal("0.001")) == Decimal("0.400")


def test_bid_price_steps_back_on_cent_tick_with_045_max_spread_and_cliff_shield():
    w = window(target="20", max_spread=Decimal("0.045"))
    b_step = book([("0.68", "25")], [("0.71", "100")])
    assert global_rewards.bid_price(b_step, w, Decimal("0.01")) == Decimal("0.67")

    b_cliff = book([("0.66", "20"), ("0.64", "500")], [("0.67", "500")])
    assert global_rewards.bid_price(b_cliff, w, Decimal("0.01")) == Decimal("0.64")


def test_pair_quote_prices_caps_at_096_on_wide_spread_and_keeps_098_on_tight_spread():
    w_wide = window(target="50", max_spread=Decimal("0.045"))
    yes_b = book([("0.49", "200")], [("0.51", "100")])
    no_b = book([("0.49", "200")], [("0.51", "100")])
    prices_wide = global_rewards.pair_quote_prices(yes_b, no_b, w_wide, Decimal("0.01"), Decimal("0.01"))
    assert prices_wide is not None
    assert prices_wide[0] + prices_wide[1] <= Decimal("0.97")
    assert prices_wide == (Decimal("0.48"), Decimal("0.49"))

    w_tight = window(target="50", max_spread=Decimal("0.03"))
    prices_tight = global_rewards.pair_quote_prices(yes_b, no_b, w_tight, Decimal("0.01"), Decimal("0.01"))
    assert prices_tight is not None
    assert prices_tight[0] + prices_tight[1] <= Decimal("0.98")
    assert prices_tight == (Decimal("0.49"), Decimal("0.49"))

    yes_milli = book([("0.499", "200")], [("0.510", "100")])
    no_milli = book([("0.499", "200")], [("0.510", "100")])
    prices_milli = global_rewards.pair_quote_prices(yes_milli, no_milli, w_wide, Decimal("0.001"), Decimal("0.001"))
    assert prices_milli is not None
    assert prices_milli[0] + prices_milli[1] <= Decimal("0.97")


def test_bid_price_steps_back_behind_thin_10_usd_top_bid_on_standard_004_spread():
    w = window(target="20", max_spread=Decimal("0.04"))
    b_thin = book([("0.25", "40"), ("0.24", "20"), ("0.23", "500")], [("0.26", "500")])
    assert global_rewards.bid_price(b_thin, w, Decimal("0.01")) == Decimal("0.23")

    b_cliff_004 = book([("0.25", "40"), ("0.23", "500")], [("0.26", "500")])
    assert global_rewards.bid_price(b_cliff_004, w, Decimal("0.01")) == Decimal("0.23")


def test_bid_price_steps_back_one_tick_on_thin_top_notional_with_003_max_spread():
    w = window(target="20", max_spread=Decimal("0.03"))
    b = book([("0.50", "30"), ("0.49", "100")], [("0.52", "100")])
    assert global_rewards.adjusted_mid(b, w.target_size) == Decimal("0.51")
    assert global_rewards.bid_price(b, w, Decimal("0.01")) == Decimal("0.49")


def test_side_score_and_global_lip_snapshot_exclude_sub_target_dust_levels():
    w = window(target="50", max_spread=Decimal("0.05"))
    b_clean = book([("0.48", "100")], [("0.52", "100")])
    b_dust = book([("0.49", "49"), ("0.48", "100")], [("0.52", "100")])

    rew_clean = global_rewards.expected_bid_reward_sec(b_clean, w, Decimal("0.48"), Decimal("50"))
    rew_dust = global_rewards.expected_bid_reward_sec(b_dust, w, Decimal("0.48"), Decimal("50"))
    assert rew_dust == rew_clean

    ours = [LiveOrder("o1", "tok", "BUY_LONG", Decimal("0.48"), Decimal("50"))]
    snap_clean = estimate_global_lip_score(b_clean, w, ours, ts_ms=1)
    snap_dust = estimate_global_lip_score(b_dust, w, ours, ts_ms=1)
    assert snap_dust.total_bid_score == snap_clean.total_bid_score
    assert snap_dust.reward_usd == snap_clean.reward_usd


