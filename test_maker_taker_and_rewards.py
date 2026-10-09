import unittest
import time
from reward_harvester import RewardHarvester, compute_reward_efficiency_index
from maker_taker_engine import check_maker_taker_parity


class TestMakerTakerAndRewards(unittest.TestCase):
    def setUp(self):
        self.sample_markets = {
            "mkt_high_pool": {
                "question": "Market with High Rewards Pool",
                "rewards_daily_rate": 2000.0,
                "liquidity": 15000.0,
                "cost": 1.01,
                "edge": 0.0050
            },
            "mkt_med_pool": {
                "question": "Market with Medium Rewards Pool",
                "rewards_daily_rate": 750.0,
                "liquidity": 8000.0,
                "cost": 1.01,
                "edge": 0.0050
            },
            "mkt_low_pool": {
                "question": "Market with Low Rewards Pool",
                "rewards_daily_rate": 100.0,
                "liquidity": 5000.0,
                "cost": 1.01,
                "edge": 0.0050
            },
            "mkt_zero_pool": {
                "question": "Market with No Rewards Pool",
                "rewards_daily_rate": 0.0,
                "liquidity": 5000.0,
                "cost": 1.01,
                "edge": 0.0050
            }
        }
        self.harvester = RewardHarvester(market_token_map=self.sample_markets, state_file="nonexistent_test_state.json")

    def test_reward_harvester_sorts_and_prioritizes_high_rate_markets(self):
        """Test RewardHarvester sorts and prioritizes high-rate markets correctly."""
        top_mkts = self.harvester.get_top_reward_markets(limit=10)
        self.assertEqual(len(top_mkts), 3)

        rates = [m["rewards_daily_rate"] for m in top_mkts]
        self.assertEqual(rates, [2000.0, 750.0, 100.0])
        self.assertEqual(top_mkts[0]["market_id"], "mkt_high_pool")

        # Test limit slicing
        top_2 = self.harvester.get_top_reward_markets(limit=2)
        self.assertEqual(len(top_2), 2)
        self.assertEqual(top_2[0]["rewards_daily_rate"], 2000.0)
        self.assertEqual(top_2[1]["rewards_daily_rate"], 750.0)

    def test_priority_scoring_favors_reward_markets_over_identical_edge(self):
        """Test that priority scoring favors reward markets over identical-edge non-reward markets."""
        edge = 0.0080
        score_high = self.harvester.calculate_reward_priority("mkt_high_pool", edge=edge)
        score_med = self.harvester.calculate_reward_priority("mkt_med_pool", edge=edge)
        score_zero = self.harvester.calculate_reward_priority("mkt_zero_pool", edge=edge)

        self.assertGreater(score_high, score_med)
        self.assertGreater(score_med, score_zero)
        self.assertAlmostEqual(score_zero, edge, places=5)

    def test_check_maker_taker_parity_finds_opportunity_when_pure_taker_fails(self):
        """
        Test check_maker_taker_parity finds opportunities on markets where
        Ask + Ask > 1.00 but Bid + Ask < 1.00.
        """
        opp = check_maker_taker_parity(
            bid_yes=0.44,
            ask_yes=0.55,
            bid_no=0.42,
            ask_no=0.52,
            min_edge=0.0020
        )
        self.assertIsNotNone(opp)
        self.assertTrue(opp["is_maker_taker"])
        self.assertEqual(opp["maker_side"], "YES")
        self.assertEqual(opp["taker_side"], "NO")
        self.assertGreater(opp["pure_taker_cost"], 1.00)
        self.assertLess(opp["cost"], 1.00)
        self.assertGreater(opp["edge"], 0.02)

    def test_check_maker_taker_parity_reverse_direction(self):
        """Test check_maker_taker_parity correctly selects NO as maker when Bid_NO yields higher edge."""
        opp = check_maker_taker_parity(
            bid_yes=0.46,
            ask_yes=0.54,
            bid_no=0.42,
            ask_no=0.53,
            min_edge=0.0020
        )
        self.assertIsNotNone(opp)
        self.assertEqual(opp["maker_side"], "NO")
        self.assertEqual(opp["taker_side"], "YES")
        self.assertGreater(opp["edge"], 0.0)

    def test_estimate_daily_reward_share(self):
        """Test conservative daily USDC yield calculation from resting capital."""
        est_yield = self.harvester.estimate_daily_reward_share(500.0, "mkt_high_pool")
        self.assertGreater(est_yield, 0.0)

        self.assertEqual(self.harvester.estimate_daily_reward_share(0.0, "mkt_high_pool"), 0.0)
        self.assertEqual(self.harvester.estimate_daily_reward_share(500.0, "mkt_zero_pool"), 0.0)

    def test_dynamic_schema_extraction(self):
        """Test extracting rewards_daily_rate, min_size, and max_spread from various Polymarket schemas."""
        # 1. rewards dict with rates list
        schema_rates = {
            "question": "Schema Rates",
            "rewards": {
                "rates": [{"rewards_daily_rate": 350.0, "min_size": 150.0, "max_spread": 2.5}]
            }
        }
        self.assertEqual(RewardHarvester.extract_daily_rate(schema_rates), 350.0)
        self.assertEqual(RewardHarvester.extract_min_size(schema_rates), 150.0)
        self.assertEqual(RewardHarvester.extract_max_spread(schema_rates), 2.5)

        # 2. market_meta dict
        schema_meta = {
            "question": "Schema Meta",
            "market_meta": {
                "rewards_daily_rate": 500.0,
                "rewards_min_size": 100.0,
                "rewards_max_spread": 2.0
            }
        }
        self.assertEqual(RewardHarvester.extract_daily_rate(schema_meta), 500.0)
        self.assertEqual(RewardHarvester.extract_min_size(schema_meta), 100.0)
        self.assertEqual(RewardHarvester.extract_max_spread(schema_meta), 2.0)

        # 3. rewards list of dicts
        schema_list = {
            "question": "Schema List",
            "rewards": [
                {"rate": 250.0, "min_size": 80.0, "max_spread": 1.5}
            ]
        }
        self.assertEqual(RewardHarvester.extract_daily_rate(schema_list), 250.0)
        self.assertEqual(RewardHarvester.extract_min_size(schema_list), 80.0)
        self.assertEqual(RewardHarvester.extract_max_spread(schema_list), 1.5)

        # 4. Fallbacks when fields absent
        schema_empty = {"question": "Schema Empty"}
        self.assertEqual(RewardHarvester.extract_daily_rate(schema_empty), 0.0)
        self.assertEqual(RewardHarvester.extract_min_size(schema_empty, 200.0), 200.0)
        self.assertEqual(RewardHarvester.extract_max_spread(schema_empty, 3.5), 3.5)

    def test_compute_reward_efficiency_index(self):
        """Test compute_reward_efficiency_index formula and bounding logic."""
        # Baseline: 100 rate, 200 min_size, 0.50 mid_price, 3.5 max_spread
        # capital_commitment = max(200 * 0.50, 1.0) = 100.0
        # spread_factor = max(3.5, 0.5) = 3.5
        # rei = 100 / (100 * 3.5) = 100 / 350 = 0.285714
        rei = compute_reward_efficiency_index(100.0, min_size=200.0, mid_price=0.50, max_spread=3.5)
        self.assertEqual(rei, 0.285714)

        # Extreme low price bounding (price 0.01 -> clamped to 0.05)
        # capital_commitment = max(100 * 0.05, 1.0) = 5.0
        # spread_factor = max(1.0, 0.5) = 1.0
        # rei = 50 / (5.0 * 1.0) = 10.0
        rei_low_price = compute_reward_efficiency_index(50.0, min_size=100.0, mid_price=0.01, max_spread=1.0)
        self.assertEqual(rei_low_price, 10.0)

        # Tight spread clamping (spread 0.1 -> clamped to 0.5)
        # capital_commitment = max(10 * 0.50, 1.0) = 5.0
        # spread_factor = 0.5
        # rei = 10 / (5.0 * 0.5) = 4.0
        rei_tight_spread = compute_reward_efficiency_index(10.0, min_size=10.0, mid_price=0.50, max_spread=0.1)
        self.assertEqual(rei_tight_spread, 4.0)

    def test_can_quote_reward_market_capital_gating(self):
        """Test can_quote_reward_market strictly enforces 40% capital gating rule."""
        markets = {
            "mkt_heavy": {
                "rewards_daily_rate": 1000.0,
                "min_size": 100.0,  # req capital @ $0.50 is $50.00
            },
            "mkt_light": {
                "rewards_daily_rate": 100.0,
                "min_size": 10.0,   # req capital @ $0.50 is $5.00
            }
        }
        harvester = RewardHarvester(market_token_map=markets, state_file="nonexistent_test_state.json")

        # Case 1: $31.49 cash
        # max_allowed = max(31.49 * 0.40, 5.0) = max(12.596, 5.0) = 12.596
        # mkt_heavy requires $50.00 -> REJECT
        # mkt_light requires $5.00 -> ACCEPT
        self.assertFalse(harvester.can_quote_reward_market("mkt_heavy", available_cash=31.49, mid_price=0.50))
        self.assertTrue(harvester.can_quote_reward_market("mkt_light", available_cash=31.49, mid_price=0.50))

        # Case 2: Small balance ($10.00 cash)
        # max_allowed = max(10.0 * 0.40, 5.0) = 5.0
        # mkt_light requires $5.00 -> ACCEPT (hits $5 min threshold)
        self.assertTrue(harvester.can_quote_reward_market("mkt_light", available_cash=10.00, mid_price=0.50))

        # Case 3: Priority calculation with gating
        # If rejected by capital gating, calculate_reward_priority returns base edge without bonus
        base_edge = 0.0050
        gated_score = harvester.calculate_reward_priority("mkt_heavy", edge=base_edge, available_cash=31.49)
        self.assertEqual(gated_score, base_edge)

        ungated_score = harvester.calculate_reward_priority("mkt_light", edge=base_edge, available_cash=31.49)
        self.assertGreater(ungated_score, base_edge)

    def test_get_top_reward_markets_sorting_by_rei_vs_rate(self):
        """Test top reward markets dual sorting: REI efficiency vs absolute rate."""
        markets = {
            "mkt_bloated": {
                # Huge rate, but massive capital requirement -> Low REI
                "question": "Bloated Pool",
                "rewards_daily_rate": 1000.0,
                "min_size": 5000.0,
                "max_spread": 3.5
                # capital = 2500, spread = 3.5 -> REI = 1000 / 8750 = 0.114286
            },
            "mkt_efficient": {
                # Moderate rate, tiny capital requirement -> High REI
                "question": "Efficient Pool",
                "rewards_daily_rate": 200.0,
                "min_size": 50.0,
                "max_spread": 1.0
                # capital = 25, spread = 1.0 -> REI = 200 / 25 = 8.0
            }
        }
        harvester = RewardHarvester(market_token_map=markets, state_file="nonexistent_test_state.json")

        # Sort by REI (default)
        top_by_rei = harvester.get_top_reward_markets(limit=2, sort_by="rei")
        self.assertEqual(top_by_rei[0]["market_id"], "mkt_efficient")
        self.assertEqual(top_by_rei[1]["market_id"], "mkt_bloated")

        # Sort by rate
        top_by_rate = harvester.get_top_reward_markets(limit=2, sort_by="rate")
        self.assertEqual(top_by_rate[0]["market_id"], "mkt_bloated")
        self.assertEqual(top_by_rate[1]["market_id"], "mkt_efficient")


if __name__ == "__main__":
    unittest.main()
