import unittest
import time
from reward_harvester import RewardHarvester
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


if __name__ == "__main__":
    unittest.main()
