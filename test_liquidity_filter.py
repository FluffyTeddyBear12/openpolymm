"""
Unit and integration tests for liquidity_filter.py.
Tests order book spread gating, depth verification, market eligibility filtering,
and paper_trader integration helpers.
"""

import pytest
import unittest
from datetime import datetime, timezone, timedelta
from liquidity_filter import (
    validate_order_book_liquidity,
    is_market_eligible,
    validate_arbitrage_execution,
    check_market_parity_liquidity_gate,
    compute_dynamic_min_depth,
)


class TestLiquidityFilter(unittest.TestCase):

    def test_tight_liquid_book_passes(self):
        """Tight liquid book with spread <= 0.015 and depth >= $250 passes."""
        book_yes = {
            "bids": [{"price": 0.495, "size": 300}],
            "asks": [
                {"price": 0.505, "size": 300},
                {"price": 0.510, "size": 200},
                {"price": 0.515, "size": 200}
            ]
        }
        book_no = {
            "bids": [{"price": 0.490, "size": 300}],
            "asks": [
                {"price": 0.500, "size": 300},
                {"price": 0.505, "size": 300}
            ]
        }
        passed, reason = validate_order_book_liquidity(book_yes, book_no)
        self.assertTrue(passed)
        self.assertEqual(reason, "OK")

    def test_wide_spread_on_yes_fails(self):
        """Wide spread on YES (> 0.015) fails with descriptive reason."""
        book_yes = {
            "bids": [{"price": 0.010, "size": 500}],
            "asks": [{"price": 0.040, "size": 2000}]  # spread = 0.030 > 0.015
        }
        book_no = {
            "bids": [{"price": 0.950, "size": 100}],
            "asks": [{"price": 0.960, "size": 100}]   # spread = 0.010 <= 0.015
        }
        passed, reason = validate_order_book_liquidity(book_yes, book_no)
        self.assertFalse(passed)
        self.assertEqual(reason, "YES spread (0.030) exceeds max (0.015)")

    def test_wide_spread_on_no_fails(self):
        """Wide spread on NO (> 0.015) fails with descriptive reason."""
        book_yes = {
            "bids": [{"price": 0.490, "size": 200}],
            "asks": [{"price": 0.500, "size": 200}]   # spread = 0.010 <= 0.015
        }
        book_no = {
            "bids": [{"price": 0.010, "size": 500}],
            "asks": [{"price": 0.050, "size": 2000}]  # spread = 0.040 > 0.015
        }
        passed, reason = validate_order_book_liquidity(book_yes, book_no)
        self.assertFalse(passed)
        self.assertEqual(reason, "NO spread (0.040) exceeds max (0.015)")

    def test_thin_depth_fails(self):
        """Thin depth (< $50) at top 3 ask levels fails."""
        book_yes = {
            "bids": [{"price": 0.495, "size": 100}],
            "asks": [{"price": 0.505, "size": 40}]    # depth = 0.505 * 40 = $20.20 < $50
        }
        book_no = {
            "bids": [{"price": 0.490, "size": 100}],
            "asks": [{"price": 0.500, "size": 200}]   # depth = 0.500 * 200 = $100.00 >= $50
        }
        passed, reason = validate_order_book_liquidity(book_yes, book_no)
        self.assertFalse(passed)
        self.assertIn("Insufficient depth", reason)
        self.assertIn("YES: $20.20", reason)
        self.assertIn("NO: $100.00", reason)

    def test_illiquid_soccer_exact_score_fails(self):
        """Soccer exact / correct score prop markets fail eligibility gate."""
        market_exact = {
            "question": "Real Madrid vs Barcelona: Exact Score 2-1",
            "volume24hr": 50000.0
        }
        passed, reason = is_market_eligible(market_exact)
        self.assertFalse(passed)
        self.assertIn("exact score", reason.lower())

        market_correct = {
            "question": "Arsenal vs Chelsea - Correct Score",
            "volume24hr": 25000.0
        }
        passed, reason = is_market_eligible(market_correct)
        self.assertFalse(passed)
        self.assertIn("correct score", reason.lower())

    def test_illiquid_obscure_itf_tennis_fails(self):
        """Obscure ITF tennis prop markets fail eligibility gate."""
        market = {
            "question": "ITF Men Antalya: Player A vs Player B",
            "volume24hr": 15000.0
        }
        passed, reason = is_market_eligible(market)
        self.assertFalse(passed)
        self.assertIn("itf", reason.lower())

    def test_market_eligibility_low_volume_fails(self):
        """Markets with 24h volume < $1,000 fail eligibility gate."""
        market = {
            "question": "Will BTC reach $150k in 2026?",
            "volume24hr": 450.0
        }
        passed, reason = is_market_eligible(market)
        self.assertFalse(passed)
        self.assertIn("Volume 24h ($450.00) below minimum", reason)

    def test_high_volume_crypto_passes(self):
        """High volume crypto market passes eligibility gate."""
        market = {
            "question": "Will Bitcoin reach $100,000 in 2026?",
            "volume24hr": 350000.0
        }
        passed, reason = is_market_eligible(market)
        self.assertTrue(passed)
        self.assertEqual(reason, "OK")

    def test_major_politics_passes(self):
        """Major political market passes eligibility gate."""
        market = {
            "question": "2028 US Presidential Election Winner",
            "volume24hr": 500000.0
        }
        passed, reason = is_market_eligible(market)
        self.assertTrue(passed)
        self.assertEqual(reason, "OK")

    def test_high_volume_sports_rejected(self):
        """Sports matches are strictly rejected under institutional rules to avoid in-play volatility."""
        market = {
            "question": "Super Bowl LIX: Chiefs vs Eagles Winner",
            "volume24hr": 120000.0
        }
        passed, reason = is_market_eligible(market)
        self.assertFalse(passed)
        self.assertIn("illiquid prop pattern rejected", reason.lower())

    def test_integration_helper_validate_arbitrage_execution(self):
        """Integration helper validate_arbitrage_execution functions seamlessly with $250 depth."""
        liquid_book_yes = {
            "bids": [{"price": 0.495, "size": 300}],
            "asks": [{"price": 0.505, "size": 300}, {"price": 0.510, "size": 300}]
        }
        liquid_book_no = {
            "bids": [{"price": 0.490, "size": 300}],
            "asks": [{"price": 0.500, "size": 300}, {"price": 0.505, "size": 300}]
        }
        m_info = {"question": "Bitcoin price on Friday", "volume24hr": 50000.0}

        opp = {
            "market_id": "0x12345",
            "ask_yes": 0.505,
            "ask_no": 0.500,
            "edge": 0.005,
            "available_depth_usd": 300.0,
            "trade_size": 50.0,
            "expected_profit": 0.25
        }

        passed, reason = validate_arbitrage_execution(
            opp,
            book_yes=liquid_book_yes,
            book_no=liquid_book_no,
            market_meta=m_info
        )
        self.assertTrue(passed)
        self.assertEqual(reason, "OK")

        bad_m_info = {"question": "ITF Women Monastir: Match Winner", "volume24hr": 50000.0}
        passed, reason = validate_arbitrage_execution(opp, book_yes=liquid_book_yes, book_no=liquid_book_no, market_meta=bad_m_info)
        self.assertFalse(passed)
        self.assertIn("Market eligibility gate failed", reason)

        opp_thin = dict(opp)
        opp_thin["available_depth_usd"] = 15.0
        passed, reason = validate_arbitrage_execution(opp_thin)
        self.assertFalse(passed)
        self.assertIn("Insufficient depth", reason)

    def test_fast_check_market_parity_liquidity_gate(self):
        """Fast parity check gate correctly blocks thin depth or ineligible market."""
        passed, reason = check_market_parity_liquidity_gate(
            market_id="0x999",
            market_meta={"question": "ETH > $4000", "volume24hr": 50000.0},
            depth_yes=25.0,
            depth_no=100.0,
            min_depth_usd=250.0
        )
        self.assertFalse(passed)
        self.assertIn("Insufficient depth", reason)

        passed, reason = check_market_parity_liquidity_gate(
            market_id="0x999",
            market_meta={"question": "ETH > $4000", "volume24hr": 500.0},
            depth_yes=300.0,
            depth_no=300.0,
            min_depth_usd=250.0
        )
        self.assertFalse(passed)
        self.assertIn("Volume 24h", reason)

    def test_compute_dynamic_min_depth(self):
        """Test dynamic depth computation logic enforces $250.00 floor and 2.5x desired_size."""
        self.assertEqual(compute_dynamic_min_depth(35.0), 250.0)
        self.assertEqual(compute_dynamic_min_depth(10.0), 250.0)
        self.assertEqual(compute_dynamic_min_depth(1000.0, desired_size=50.0), 250.0)
        self.assertEqual(compute_dynamic_min_depth(35.0, desired_size=120.0), 300.0)
        # Test bankroll floor override
        self.assertEqual(compute_dynamic_min_depth(29.49, desired_size=5.0, floor_override=20.0), 20.0)
        self.assertEqual(compute_dynamic_min_depth(29.49, desired_size=10.0, floor_override=20.0), 25.0)
    def test_is_market_eligible_rejects_turbo_patterns(self):
        """Turbo and short-duration patterns are rejected by is_market_eligible."""
        from liquidity_filter import is_market_eligible

        # 1. 'Bitcoin Up or Down'
        market_updown = {
            "question": "Bitcoin Up or Down - October 6, 10:30PM-10:45PM ET",
            "volume24hr": 50000.0
        }
        passed, reason = is_market_eligible(market_updown)
        self.assertFalse(passed)
        self.assertIn("turbo", reason.lower())

        # 2. '15m'
        market_15m = {
            "question": "ETH Price 15m Candle Close",
            "volume24hr": 30000.0
        }
        passed, reason = is_market_eligible(market_15m)
        self.assertFalse(passed)
        self.assertIn("turbo", reason.lower())

        # 3. '1st Half'
        market_half = {
            "question": "Arsenal vs Chelsea 1st Half Winner",
            "volume24hr": 20000.0
        }
        passed, reason = is_market_eligible(market_half)
        self.assertFalse(passed)
        self.assertIn("turbo", reason.lower())

    def test_is_market_eligible_rejects_imminent_expiry(self):
        """Markets expiring in < 4.0 hours are rejected; markets with > 4.0 hours pass."""
        from datetime import datetime, timezone, timedelta
        from liquidity_filter import is_market_eligible

        now = datetime.now(timezone.utc)

        # 1. Expiring in 30 minutes (< 4.0 hours)
        market_soon = {
            "question": "Will BTC break 100k today?",
            "volume24hr": 50000.0,
            "endDateIso": (now + timedelta(minutes=30)).isoformat()
        }
        passed, reason = is_market_eligible(market_soon, min_hours_to_expiry=4.0)
        self.assertFalse(passed)
        self.assertIn("expires too soon", reason)

        # 2. Expiring in 24 hours (>= 4.0 hours)
        market_future = {
            "question": "Will BTC break 100k tomorrow?",
            "volume24hr": 50000.0,
            "endDateIso": (now + timedelta(hours=24)).isoformat()
        }
        passed, reason = is_market_eligible(market_future, min_hours_to_expiry=4.0)
        self.assertTrue(passed)
        self.assertEqual(reason, "OK")

        # 3. Already expired
        market_expired = {
            "question": "Will BTC break 100k yesterday?",
            "volume24hr": 50000.0,
            "endDateIso": (now - timedelta(hours=1)).isoformat()
        }
        passed, reason = is_market_eligible(market_expired, min_hours_to_expiry=4.0)
        self.assertFalse(passed)
        self.assertIn("already expired", reason)

    def test_is_market_eligible_rejects_in_play_game(self):
        """In-play games with gameStartTime in the past are rejected."""
        now = datetime.now(timezone.utc)
        market_in_play = {
            "question": "Coco Gauff vs Mertens",
            "volume24hr": 50000.0,
            "gameStartTime": (now - timedelta(minutes=45)).isoformat(),
            "endDateIso": (now + timedelta(days=7)).isoformat()
        }
        passed, reason = is_market_eligible(market_in_play)
        self.assertFalse(passed)
        self.assertIn("already started", reason)

    def test_is_market_eligible_rejects_imminent_game(self):
        """Matches starting within 1 hour are rejected for safety."""
        now = datetime.now(timezone.utc)
        market_imminent = {
            "question": "Brewers vs Padres",
            "volume24hr": 50000.0,
            "gameStartTime": (now + timedelta(minutes=20)).isoformat(),
            "endDateIso": (now + timedelta(days=2)).isoformat()
        }
        passed, reason = is_market_eligible(market_imminent)
        self.assertFalse(passed)
        self.assertIn("starts too soon", reason)
        self.assertIn("1.0h", reason)

    def test_is_market_eligible_allows_future_event(self):
        """Events ending well in the future (> 24h) and with volume > $25k are eligible."""
        now = datetime.now(timezone.utc)
        market_future = {
            "question": "Will the Federal Reserve cut rates by December 2026?",
            "volume24hr": 50000.0,
            "endDateIso": (now + timedelta(hours=72)).isoformat()
        }
        passed, reason = is_market_eligible(market_future)
        self.assertTrue(passed)
        self.assertEqual(reason, "OK")


if __name__ == "__main__":
    unittest.main()
