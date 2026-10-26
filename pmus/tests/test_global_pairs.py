from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pmus.models import Book, IncentiveWindow, Level
from pmus.strategy import global_rewards

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


def test_pair_prices_join_best_bids():
    yes_b = book([("0.48", "100")], [("0.52", "100")])
    no_b = book([("0.47", "100")], [("0.53", "100")])
    w = window(max_spread=Decimal("0.04"))

    prices = global_rewards.pair_quote_prices(yes_b, no_b, w, Decimal("0.01"), Decimal("0.01"))
    assert prices == (Decimal("0.48"), Decimal("0.47"))


def test_pair_prices_none_when_side_cannot_score():
    w = window(max_spread=Decimal("0.03"))
    valid_yes = book([("0.48", "100")], [("0.52", "100")])
    valid_no = book([("0.47", "100")], [("0.53", "100")])
    unscoring_yes = book([("0.45", "100")], [("0.55", "100")])
    unscoring_no = book([("0.45", "100")], [("0.55", "100")])
    empty_book = book([], [("0.50", "100")])

    assert global_rewards.pair_quote_prices(unscoring_yes, valid_no, w, Decimal("0.01"), Decimal("0.01")) is None
    assert global_rewards.pair_quote_prices(valid_yes, unscoring_no, w, Decimal("0.01"), Decimal("0.01")) is None
    assert global_rewards.pair_quote_prices(empty_book, valid_no, w, Decimal("0.01"), Decimal("0.01")) is None


def test_pair_prices_none_when_sum_ge_one():
    w = window(max_spread=Decimal("0.03"))
    yes_b = book([("0.51", "100")], [("0.53", "100")])
    no_b = book([("0.49", "100")], [("0.51", "100")])
    assert global_rewards.pair_quote_prices(yes_b, no_b, w, Decimal("0.01"), Decimal("0.01")) is None

    yes_b_high = book([("0.52", "100")], [("0.54", "100")])
    assert global_rewards.pair_quote_prices(yes_b_high, no_b, w, Decimal("0.01"), Decimal("0.01")) is None


def test_pair_size_lot_rounding_and_caps():
    size = global_rewards.pair_size(
        cost_cap=Decimal("100"),
        p_yes=Decimal("0.45"),
        p_no=Decimal("0.45"),
        min_size=Decimal("50"),
        lot=Decimal("10"),
        max_contracts=Decimal("1000"),
    )
    assert size == Decimal("110")

    size_exact = global_rewards.pair_size(
        cost_cap=Decimal("89.99"),
        p_yes=Decimal("0.45"),
        p_no=Decimal("0.45"),
        min_size=Decimal("50"),
        lot=Decimal("10"),
        max_contracts=Decimal("1000"),
    )
    assert size_exact == Decimal("90")

    size_capped = global_rewards.pair_size(
        cost_cap=Decimal("500"),
        p_yes=Decimal("0.45"),
        p_no=Decimal("0.45"),
        min_size=Decimal("50"),
        lot=Decimal("10"),
        max_contracts=Decimal("75"),
    )
    assert size_capped == Decimal("70")

    size_below_cost = global_rewards.pair_size(
        cost_cap=Decimal("40"),
        p_yes=Decimal("0.45"),
        p_no=Decimal("0.45"),
        min_size=Decimal("50"),
        lot=Decimal("10"),
        max_contracts=Decimal("1000"),
    )
    assert size_below_cost == Decimal("0")

    size_below_cap = global_rewards.pair_size(
        cost_cap=Decimal("100"),
        p_yes=Decimal("0.45"),
        p_no=Decimal("0.45"),
        min_size=Decimal("50"),
        lot=Decimal("10"),
        max_contracts=Decimal("40"),
    )
    assert size_below_cap == Decimal("0")


def test_competitor_q_in_band_and_tail():
    w = window(target="50", max_spread=Decimal("0.04"))
    yes_book = book([("0.48", "100")], [("0.52", "100")])
    no_book = book([("0.49", "200")], [("0.51", "200")])

    q_in_band = global_rewards.competitor_q(yes_book, no_book, w)
    expected_in_band = Decimal("112.5") / Decimal("3")
    assert abs(q_in_band - expected_in_band) < Decimal("1e-20")

    w_tail = window(target="50", max_spread=Decimal("0.02"))
    yes_tail = book([("0.04", "100")], [("0.06", "100")])
    no_tail = book([("0.94", "200")], [("0.96", "200")])

    q_tail = global_rewards.competitor_q(yes_tail, no_tail, w_tail)
    assert q_tail == Decimal("25")

    w_none = window(target="50", max_spread=None)
    assert global_rewards.competitor_q(yes_tail, no_tail, w_none) == Decimal("0")


def test_pair_reward_is_approx_three_times_single_reward_in_band():
    w = window(pool="100", target="50", max_spread=Decimal("0.03"))
    yes_b = book([("0.49", "100")], [("0.51", "100")])
    no_b = book([("0.49", "100")], [("0.51", "100")])
    p_yes = Decimal("0.49")
    p_no = Decimal("0.49")
    size = Decimal("50")

    pair_rate = global_rewards.expected_pair_reward_sec(yes_b, no_b, w, p_yes, p_no, size)
    single_rate = global_rewards.expected_single_reward_sec(yes_b, no_b, w, p_yes, size)

    w_bid = ((Decimal("0.03") - Decimal("0.01")) / Decimal("0.03")) ** 2
    comps = w_bid * Decimal("100")
    ours_pair = w_bid * Decimal("50")
    expected_pair = (Decimal("100") / Decimal("86400")) * (ours_pair / (comps + ours_pair))

    ours_single = ours_pair / Decimal("3")
    expected_single = (Decimal("100") / Decimal("86400")) * (ours_single / (comps + ours_single))

    assert abs(pair_rate - expected_pair) < Decimal("1e-20")
    assert abs(single_rate - expected_single) < Decimal("1e-20")

    ratio = pair_rate / single_rate
    assert Decimal("2") < ratio < Decimal("3.5")


def test_tail_market_single_zero_pair_positive():
    w = window(pool="100", target="50", max_spread=Decimal("0.02"))
    yes_b = book([("0.04", "100")], [("0.06", "100")])
    no_b = book([("0.94", "100")], [("0.96", "100")])
    p_yes = Decimal("0.04")
    p_no = Decimal("0.94")
    size = Decimal("50")

    single_rate = global_rewards.expected_single_reward_sec(yes_b, no_b, w, p_yes, size)
    assert single_rate == Decimal("0")

    pair_rate = global_rewards.expected_pair_reward_sec(yes_b, no_b, w, p_yes, p_no, size)
    assert pair_rate > Decimal("0")

    w_bid = ((Decimal("0.02") - Decimal("0.01")) / Decimal("0.02")) ** 2
    ours_pair = w_bid * Decimal("50")
    comps = w_bid * Decimal("100")
    expected_pair = (Decimal("100") / Decimal("86400")) * (ours_pair / (comps + ours_pair))
    assert abs(pair_rate - expected_pair) < Decimal("1e-20")


def test_size_below_target_returns_zero():
    w = window(pool="100", target="50", max_spread=Decimal("0.03"))
    yes_b = book([("0.49", "100")], [("0.51", "100")])
    no_b = book([("0.49", "100")], [("0.51", "100")])
    empty_b = book([], [("0.51", "100")])
    w_no_spread = window(pool="100", target="50", max_spread=None)

    assert (
        global_rewards.expected_pair_reward_sec(yes_b, no_b, w, Decimal("0.49"), Decimal("0.49"), Decimal("49"))
        == Decimal("0")
    )
    assert (
        global_rewards.expected_single_reward_sec(yes_b, no_b, w, Decimal("0.49"), Decimal("49"))
        == Decimal("0")
    )
    assert (
        global_rewards.expected_pair_reward_sec(empty_b, no_b, w, Decimal("0.49"), Decimal("0.49"), Decimal("50"))
        == Decimal("0")
    )
    assert (
        global_rewards.expected_single_reward_sec(empty_b, no_b, w, Decimal("0.49"), Decimal("50"))
        == Decimal("0")
    )
    assert (
        global_rewards.expected_pair_reward_sec(yes_b, no_b, w_no_spread, Decimal("0.49"), Decimal("0.49"), Decimal("50"))
        == Decimal("0")
    )
    assert (
        global_rewards.expected_single_reward_sec(yes_b, no_b, w_no_spread, Decimal("0.49"), Decimal("50"))
        == Decimal("0")
    )


def test_competitor_q_symmetry_and_sub_target_dust_filtering():
    w = window(pool="100", target="50", max_spread=Decimal("0.04"))
    yes_clean = book([("0.48", "100")], [("0.52", "100")])
    no_clean = book([("0.49", "120")], [("0.51", "120")])
    yes_dusty = book([("0.49", "49"), ("0.48", "100"), ("0.47", "12")], [("0.52", "100")])
    no_dusty = book([("0.495", "30"), ("0.49", "120"), ("0.48", "5")], [("0.51", "120")])

    q_clean = global_rewards.competitor_q(yes_clean, no_clean, w)
    q_dusty = global_rewards.competitor_q(yes_dusty, no_dusty, w)
    q_swapped = global_rewards.competitor_q(no_dusty, yes_dusty, w)

    q_one = global_rewards.side_score(yes_clean.bids, Decimal("0.50"), Decimal("0.04"), Decimal("50"))
    q_two = global_rewards.side_score(no_clean.bids, Decimal("0.50"), Decimal("0.04"), Decimal("50"))
    expected = global_rewards.combined_score(q_one, q_two, Decimal("0.50"))

    assert q_clean == expected
    assert q_dusty == q_clean
    assert q_swapped == q_clean
    assert global_rewards.expected_bid_reward_sec(yes_dusty, w, Decimal("0.48"), Decimal("50")) == global_rewards.expected_bid_reward_sec(
        yes_clean, w, Decimal("0.48"), Decimal("50")
    )


