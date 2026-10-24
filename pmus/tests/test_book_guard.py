import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from pmus.clob_venue import ClobVenueClient
from pmus.config import Settings
from pmus.engine import LiveEngine
from pmus.models import Book, ConditionMarket, IncentiveWindow, Level, LiveOrder, Market, Position
from pmus.risk import RiskGateResult
from pmus.strategy import global_rewards
from pmus.strategy.book_guard import quote_guard
from pmus.strategy.condition_selector import evaluate_condition
from pmus.strategy.lip_quoter import desired_quotes

D = Decimal
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def book(slug, bids, asks, tick=None, last=None):
    return Book(
        slug=slug,
        bids=tuple(Level(D(p), D(q)) for p, q in bids),
        asks=tuple(Level(D(p), D(q)) for p, q in asks),
        ts_ms=0,
        tick_size=D(tick) if tick else None,
        last_trade_price=D(last) if last else None,
    )


def window(target="20", max_spread="0.045", pool="100"):
    return IncentiveWindow(
        slug="w", program_id="p", pool_usd=D(pool), discount=D("0"), target_size=D(target),
        max_spread=D(max_spread), start=NOW - timedelta(hours=1), end=None, status="active",
    )


def market(slug, tick):
    return Market(slug=slug, event_id="ev", category="x", tick=D(tick), min_qty=D("5"), active=True)


def condition(name, w, market_tick="0.01"):
    return ConditionMarket(
        condition_id=name, event_id="ev", question=name,
        yes=market(name + "-y", market_tick), no=market(name + "-n", market_tick), window=w,
    )


HOLLOW_YES = ([("0.47", "30"), ("0.32", "100"), ("0.31", "188.5"), ("0.30", "107.5")], [("0.49", "30"), ("0.53", "20"), ("0.57", "32.8")])
HOLLOW_NO = ([("0.51", "30"), ("0.47", "20"), ("0.43", "32.8")], [("0.53", "30"), ("0.68", "100"), ("0.69", "188.5"), ("0.70", "107.5")])


def test_parse_book_payload_reads_tick_size_and_last_trade_price():
    payload = {"bids": [{"price": "0.47", "size": "30"}], "asks": [], "tick_size": "0.01", "last_trade_price": "0.57"}
    parsed = ClobVenueClient._parse_book_payload("tok", payload)
    assert parsed.tick_size == D("0.01")
    assert parsed.last_trade_price == D("0.57")

    blank = ClobVenueClient._parse_book_payload("tok", {"bids": [], "asks": [], "last_trade_price": "0"})
    assert blank.tick_size is None
    assert blank.last_trade_price is None


def test_strip_orders_keeps_tick_size_and_last_trade_price():
    b = book("tok", [("0.47", "100")], [("0.49", "100")], tick="0.01", last="0.48")
    stripped = global_rewards.strip_orders_from_book(b, [LiveOrder("o", "tok", "BUY_LONG", D("0.47"), D("40"))])
    assert stripped.bids == (Level(D("0.47"), D("60")),)
    assert stripped.tick_size == D("0.01")
    assert stripped.last_trade_price == D("0.48")


def test_guard_rejects_lone_bid_above_gap():
    yes_b = book("y", *HOLLOW_YES, tick="0.01")
    reason = quote_guard(yes_b, window(target="20", max_spread="0.065"), D("0.46"), D("0.01"))
    assert reason is not None and "support" in reason


def test_selector_skips_hollow_book():
    w = window(target="20", max_spread="0.065", pool="28")
    c = condition("hollow", w)
    yes_b = book(c.yes.slug, *HOLLOW_YES, tick="0.01", last="0.45")
    no_b = book(c.no.slug, *HOLLOW_NO, tick="0.01", last="0.45")
    assert evaluate_condition(c, yes_b, no_b, D("65"), D("600"), D("0")) is None


def test_selector_never_bids_above_stale_last_trade():
    w = window(target="20", max_spread="0.045", pool="28")
    c = condition("stale", w)
    yes_b = book(c.yes.slug, [("0.25", "50"), ("0.23", "100"), ("0.20", "100")], [("0.28", "60"), ("0.29", "100"), ("0.30", "120")], tick="0.01", last="0.43")
    no_b = book(c.no.slug, [("0.72", "60"), ("0.71", "100"), ("0.70", "120"), ("0.50", "200")], [("0.75", "50"), ("0.77", "100")], tick="0.01", last="0.43")

    reason = quote_guard(no_b, w, D("0.71"), D("0.01"))
    assert reason is not None and "last trade" in reason

    t = evaluate_condition(c, yes_b, no_b, D("65"), D("600"), D("0"))
    assert t is None or t.no_price is None


def test_selector_prices_on_book_tick_when_market_tick_is_stale():
    w = window(target="20", max_spread="0.045")
    c = condition("tick", w, market_tick="0.001")
    yes_b = book(c.yes.slug, [("0.48", "25"), ("0.47", "300"), ("0.46", "400")], [("0.50", "25"), ("0.51", "300")], tick="0.01")
    no_b = book(c.no.slug, [("0.50", "25"), ("0.49", "300")], [("0.52", "25"), ("0.53", "300")], tick="0.01")
    t = evaluate_condition(c, yes_b, no_b, D("65"), D("600"), D("0"))
    assert t is not None and t.mode == "two_sided"
    assert t.yes_price % D("0.01") == 0
    assert t.no_price % D("0.01") == 0


def test_paired_requote_pulls_bid_when_book_turns_hollow():
    w = window(target="20", max_spread="0.065")
    m = market("y", "0.01")
    flat = Position("y", D("0"), D("0"))
    healthy = book("y", [("0.47", "300"), ("0.46", "300")], [("0.49", "300")], tick="0.01")
    hollow = book("y", *HOLLOW_YES, tick="0.01")
    kwargs = dict(position=flat, budget=D("50"), now=NOW, bid_budget=D("50"), is_global=True, paired=True, quote_size=D("20"))

    assert [q.side for q in desired_quotes(m, healthy, w, **kwargs)] == ["BUY_LONG"]
    assert desired_quotes(m, hollow, w, **kwargs) == []


def test_requote_uses_book_tick_over_stale_market_tick():
    w = window(target="20", max_spread="0.045")
    m = market("y", "0.001")
    b = book("y", [("0.48", "25"), ("0.47", "300"), ("0.46", "400")], [("0.50", "25"), ("0.51", "300")], tick="0.01")
    quotes = desired_quotes(m, b, w, Position("y", D("0"), D("0")), D("50"), now=NOW, bid_budget=D("50"), is_global=True, paired=True, quote_size=D("20"))
    assert [q.side for q in quotes] == ["BUY_LONG"]
    assert quotes[0].price % D("0.01") == 0


def test_engine_submits_with_book_tick():
    w = IncentiveWindow("y1", "p1", D("100"), D("0.30"), D("50"), D("0.05"), datetime.now(timezone.utc), None, "active")
    auth_client = MagicMock()
    auth_client.create_orders_batch.return_value = []
    risk_manager = MagicMock()
    risk_manager.check_pre_submit.return_value = RiskGateResult(True)
    engine = LiveEngine(
        settings=Settings(venue="global", total_budget=D("100"), max_per_market=D("50")),
        ledger=MagicMock(),
        public_client=MagicMock(),
        auth_client=auth_client,
        risk_manager=risk_manager,
        logger=logging.getLogger("test_book_tick"),
    )
    engine.ledger.get_open_orders.return_value = []
    engine.markets["y1"] = market("y1", "0.001")
    engine.active_windows["y1"] = w
    engine.side_budgets[("y1", "bid")] = D("50")
    engine.side_qtys[("y1", "bid")] = D("50")
    b = book("y1", [("0.49", "200")], [("0.51", "200")], tick="0.01")

    engine._quote_market(
        slug="y1",
        now_dt=datetime.now(timezone.utc),
        now_ts=1000.0,
        now_ms=1000000,
        exchange_open=[],
        live_positions={},
        buying_power=D("500"),
        realized_loss_today=D("0"),
        book=b,
    )

    quotes, ticks, _ = auth_client.create_orders_batch.call_args[0]
    assert ticks == {"y1": D("0.01")}
    assert [q.price % D("0.01") for q in quotes] == [0]


def test_last_trade_reference_reads_shared_market_price_on_this_books_side():
    w = window(target="20", max_spread="0.065")
    no_b = book("n", [("0.61", "300"), ("0.60", "300")], [("0.63", "300")], tick="0.01", last="0.35")
    assert quote_guard(no_b, w, D("0.61"), D("0.01")) is None
    yes_b = book("y", [("0.34", "300"), ("0.33", "300")], [("0.36", "300")], tick="0.01", last="0.35")
    assert quote_guard(yes_b, w, D("0.34"), D("0.01")) is None


def test_guard_rejects_procter_and_gamble_cliff_book():
    w = window(target="20", max_spread="0.045")
    pg_yes = book(
        "pg-y",
        [("0.26", "40"), ("0.25", "58.7"), ("0.03", "27"), ("0.02", "306")],
        [("0.28", "100")],
        tick="0.01",
    )
    reason = quote_guard(pg_yes, w, D("0.25"), D("0.01"))
    assert reason is not None and ("support" in reason or "cliff book" in reason)


def test_guard_rejects_walmart_single_order_bait_book():
    w = window(target="50", max_spread="0.045")
    wmt_no = book(
        "wmt-n",
        [("0.52", "65"), ("0.24", "200")],
        [("0.54", "65")],
        tick="0.01",
        last="0.26",
    )
    reason = quote_guard(wmt_no, w, D("0.51"), D("0.01"))
    assert reason is not None and ("support" in reason or "last trade" in reason)


def test_guard_enforces_wave20_support_notional_and_front_shield():
    from pmus.strategy.book_guard import (
        MIN_NEAR_NOTIONAL_USD,
        MIN_SUPPORT_SHARES,
        MULTI_LEVEL_SUPPORT_TARGETS,
    )
    assert MIN_SUPPORT_SHARES == D("250")
    assert MIN_NEAR_NOTIONAL_USD == D("75")
    assert MULTI_LEVEL_SUPPORT_TARGETS == D("4")

    w = window(target="20", max_spread="0.045")
    shallow_support = book(
        "shallow-sup",
        [("0.40", "40"), ("0.39", "40"), ("0.20", "300")],
        [("0.42", "200")],
        tick="0.01",
    )
    reason_sup = quote_guard(shallow_support, w, D("0.40"), D("0.01"))
    assert reason_sup is not None and "support" in reason_sup

    oxy_shallow_two_level = book(
        "oxy-shallow",
        [("0.47", "50"), ("0.46", "50")],
        [("0.50", "100")],
        tick="0.01",
    )
    reason_oxy = quote_guard(oxy_shallow_two_level, w, D("0.47"), D("0.01"))
    assert reason_oxy is not None and "post-discount support depth" in reason_oxy

    low_notional = book(
        "low-notional",
        [("0.15", "100"), ("0.14", "250")],
        [("0.17", "200")],
        tick="0.01",
    )
    reason_notional = quote_guard(low_notional, w, D("0.15"), D("0.01"))
    assert reason_notional is not None and "cliff book" in reason_notional

    thin_front_shield = book(
        "thin-shield",
        [("0.50", "15"), ("0.49", "20"), ("0.48", "300")],
        [("0.52", "200")],
        tick="0.01",
    )
    reason_shield = quote_guard(thin_front_shield, w, D("0.49"), D("0.01"))
    assert reason_shield is not None and "thin front shield" in reason_shield


def test_guard_rejects_single_whale_top_bid_cliff_and_allows_exact_099_cross_sum():
    w = window(target="20", max_spread="0.045")
    whale_cliff = book(
        "whale-cliff",
        [("0.50", "400"), ("0.15", "100")],
        [("0.52", "200")],
        tick="0.01",
    )
    reason_cliff = quote_guard(whale_cliff, w, D("0.49"), D("0.01"))
    assert reason_cliff is not None and "cliff book" in reason_cliff

    deep_book = book(
        "deep-y",
        [("0.49", "200"), ("0.48", "200")],
        [("0.51", "200")],
        tick="0.01",
    )
    opp_at_051 = book("opp-n", [("0.51", "200")], [("0.53", "200")], tick="0.01")
    assert quote_guard(deep_book, w, D("0.48"), D("0.01"), other_book=opp_at_051) is None

    opp_at_052 = book("opp-n2", [("0.52", "200")], [("0.54", "200")], tick="0.01")
    reason_cross = quote_guard(deep_book, w, D("0.48"), D("0.01"), other_book=opp_at_052)
    assert reason_cross is not None and "cross-book arbitrage" in reason_cross


def test_guard_rejects_ask_overhang_and_enforces_hysteresis_and_thin_usd_stepback():
    from pmus.strategy.global_rewards import bid_price

    w = window(target="50", max_spread="0.05")
    overhang = book(
        "overhang-mkt",
        [("0.46", "110"), ("0.45", "120"), ("0.44", "250")],
        [("0.47", "6500"), ("0.48", "4000")],
        tick="0.01",
    )
    reason_overhang = quote_guard(overhang, w, D("0.45"), D("0.01"))
    assert reason_overhang is not None and "ask overhang" in reason_overhang

    borderline = book(
        "borderline-mkt",
        [("0.30", "100"), ("0.29", "180")],
        [("0.32", "250")],
        tick="0.01",
    )
    w_small = window(target="20", max_spread="0.04")
    assert quote_guard(borderline, w_small, D("0.29"), D("0.01"), hysteresis=D("1.0")) is None
    assert quote_guard(borderline, w_small, D("0.29"), D("0.01"), hysteresis=D("1.3")) is not None

    thin_top = book(
        "thin-top",
        [("0.35", "22"), ("0.34", "18"), ("0.33", "450")],
        [("0.37", "300"), ("0.38", "300")],
        tick="0.01",
    )
    w_wide = window(target="20", max_spread="0.05")
    stepped = bid_price(thin_top, w_wide, D("0.01"))
    assert stepped == D("0.33")
    assert quote_guard(thin_top, w_wide, stepped, D("0.01")) is None


def test_guard_allows_520_ask_wall_when_thin_top_bid_backed_by_deep_second_level():
    w = window(target="20", max_spread="0.03")
    backed = book(
        "backed-mkt",
        [("0.50", "5"), ("0.49", "20000")],
        [("0.52", "1000")],
        tick="0.01",
    )
    assert global_rewards.bid_price(backed, w, D("0.01")) == D("0.49")
    assert quote_guard(backed, w, D("0.49"), D("0.01")) is None
    assert "ask overhang" not in (quote_guard(backed, w, D("0.50"), D("0.01")) or "")
