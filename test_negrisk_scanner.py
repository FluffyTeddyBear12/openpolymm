"""
Unit and integration tests for Multi-Outcome Neg-Risk Basket Scanner and Parity Arb Adapter.
"""

import unittest
from negrisk_scanner import NegRiskBasketScanner, NegRiskAdapter


class TestNegRiskBasketScanner(unittest.TestCase):
    def setUp(self):
        self.scanner = NegRiskBasketScanner()
        self.adapter = NegRiskAdapter()

    def test_basket_indexing(self):
        """Test basket indexing: groups markets correctly by neg_risk_market_id."""
        sample_markets = [
            {
                "condition_id": "0xCOND1",
                "neg_risk": True,
                "neg_risk_market_id": "BASKET_ALPHA",
                "token_yes": "T_ALPHA_1",
                "token_no": "T_ALPHA_1_NO",
                "question": "Candidate 1 wins?",
                "tick_size": 0.001,
            },
            {
                "condition_id": "0xCOND2",
                "neg_risk": True,
                "neg_risk_market_id": "BASKET_ALPHA",
                "token_yes": "T_ALPHA_2",
                "token_no": "T_ALPHA_2_NO",
                "question": "Candidate 2 wins?",
                "tick_size": 0.001,
            },
            {
                "condition_id": "0xCOND3",
                "neg_risk": True,
                "neg_risk_market_id": "BASKET_ALPHA",
                "token_yes": "T_ALPHA_3",
                "token_no": "T_ALPHA_3_NO",
                "question": "Candidate 3 wins?",
                "tick_size": 0.001,
            },
            {
                "condition_id": "0xCOND4",
                "neg_risk": True,
                "neg_risk_market_id": "BASKET_BETA",
                "token_yes": "T_BETA_1",
                "token_no": "T_BETA_1_NO",
                "question": "Team A wins?",
                "tick_size": 0.001,
            },
            {
                "condition_id": "0xCOND5",
                "neg_risk": False,  # Standard binary market, should NOT be indexed
                "neg_risk_market_id": None,
                "token_yes": "T_STD_YES",
                "token_no": "T_STD_NO",
                "question": "Will BTC hit 100k?",
                "tick_size": 0.001,
            },
        ]

        count = self.scanner.index_market_universe(sample_markets)
        self.assertEqual(count, 2)
        self.assertIn("BASKET_ALPHA", self.scanner.baskets)
        self.assertIn("BASKET_BETA", self.scanner.baskets)
        self.assertEqual(len(self.scanner.baskets["BASKET_ALPHA"]), 3)
        self.assertEqual(len(self.scanner.baskets["BASKET_BETA"]), 1)
        self.assertEqual(self.scanner.market_to_basket.get("0xCOND1"), "BASKET_ALPHA")
        self.assertNotIn("0xCOND5", self.scanner.market_to_basket)

    def test_parity_check_success(self):
        """
        3-outcome basket with asks 0.30, 0.30, 0.32 (sum = 0.92, edge = 0.08)
        -> triggers opportunity with correct sizing and profit.
        """
        basket_markets = [
            {
                "condition_id": "0xOUTCOME_A",
                "neg_risk": True,
                "neg_risk_market_id": "BASKET_ELECTION",
                "token_yes": "TOKEN_A",
                "token_no": "TOKEN_A_NO",
                "question": "Party A",
                "tick_size": 0.001,
            },
            {
                "condition_id": "0xOUTCOME_B",
                "neg_risk": True,
                "neg_risk_market_id": "BASKET_ELECTION",
                "token_yes": "TOKEN_B",
                "token_no": "TOKEN_B_NO",
                "question": "Party B",
                "tick_size": 0.001,
            },
            {
                "condition_id": "0xOUTCOME_C",
                "neg_risk": True,
                "neg_risk_market_id": "BASKET_ELECTION",
                "token_yes": "TOKEN_C",
                "token_no": "TOKEN_C_NO",
                "question": "Party C",
                "tick_size": 0.001,
            },
        ]
        self.scanner.index_market_universe(basket_markets)

        market_books = {
            "0xOUTCOME_A": {"TOKEN_A": 0.30},
            "0xOUTCOME_B": {"TOKEN_B": 0.30},
            "0xOUTCOME_C": {"TOKEN_C": 0.32},
        }
        market_depths = {
            "0xOUTCOME_A": {"TOKEN_A": 100.0},
            "0xOUTCOME_B": {"TOKEN_B": 150.0},
            "0xOUTCOME_C": {"TOKEN_C": 80.0},
        }

        opp = self.scanner.check_basket_parity(
            neg_risk_market_id="BASKET_ELECTION",
            market_books=market_books,
            market_depths=market_depths,
            fee_rate=0.0,
            min_edge=0.015,
        )

        self.assertIsNotNone(opp)
        self.assertEqual(opp["execution_type"], "negrisk_basket")
        self.assertEqual(opp["neg_risk_market_id"], "BASKET_ELECTION")
        self.assertEqual(opp["num_outcomes"], 3)
        self.assertEqual(opp["sum_ask"], 0.92)
        self.assertEqual(opp["total_cost"], 0.92)
        self.assertEqual(opp["edge"], 0.08)
        self.assertEqual(opp["max_shares"], 80.0)  # min(100, 150, 80)
        self.assertEqual(opp["trade_size"], round(80.0 * 0.92, 2))  # 73.60
        self.assertEqual(opp["expected_profit"], round(80.0 * 0.08, 4))  # 6.40
        self.assertEqual(len(opp["outcomes"]), 3)

    def test_parity_check_missing_outcome_or_zero_depth(self):
        """Basket with missing outcome or 0 depth -> returns None."""
        basket_markets = [
            {
                "condition_id": "0xM1",
                "neg_risk": True,
                "neg_risk_market_id": "BASKET_DEP",
                "token_yes": "T1",
                "token_no": "T1_NO",
                "question": "Q1",
            },
            {
                "condition_id": "0xM2",
                "neg_risk": True,
                "neg_risk_market_id": "BASKET_DEP",
                "token_yes": "T2",
                "token_no": "T2_NO",
                "question": "Q2",
            },
        ]
        self.scanner.index_market_universe(basket_markets)

        # Case 1: Missing outcome book
        books_missing = {"0xM1": {"T1": 0.40}}
        depths_valid = {"0xM1": {"T1": 50.0}, "0xM2": {"T2": 50.0}}
        self.assertIsNone(
            self.scanner.check_basket_parity("BASKET_DEP", books_missing, depths_valid)
        )

        # Case 2: Zero depth on one outcome
        books_valid = {"0xM1": {"T1": 0.40}, "0xM2": {"T2": 0.45}}
        depths_zero = {"0xM1": {"T1": 50.0}, "0xM2": {"T2": 0.0}}
        self.assertIsNone(
            self.scanner.check_basket_parity(
                "BASKET_DEP", books_valid, depths_zero, min_depth_shares=5.0
            )
        )

        # Case 3: Depth below min_depth_shares (e.g. 2.0 < 5.0)
        depths_low = {"0xM1": {"T1": 50.0}, "0xM2": {"T2": 2.0}}
        self.assertIsNone(
            self.scanner.check_basket_parity(
                "BASKET_DEP", books_valid, depths_low, min_depth_shares=5.0
            )
        )

    def test_parity_check_no_edge(self):
        """Basket with total ask >= 1.00 -> returns None."""
        basket_markets = [
            {
                "condition_id": "0xO1",
                "neg_risk": True,
                "neg_risk_market_id": "BASKET_EXPENSIVE",
                "token_yes": "TO1",
            },
            {
                "condition_id": "0xO2",
                "neg_risk": True,
                "neg_risk_market_id": "BASKET_EXPENSIVE",
                "token_yes": "TO2",
            },
            {
                "condition_id": "0xO3",
                "neg_risk": True,
                "neg_risk_market_id": "BASKET_EXPENSIVE",
                "token_yes": "TO3",
            },
        ]
        self.scanner.index_market_universe(basket_markets)

        # Asks: 0.35 + 0.35 + 0.35 = 1.05 >= 1.00
        books_expensive = {
            "0xO1": {"TO1": 0.35},
            "0xO2": {"TO2": 0.35},
            "0xO3": {"TO3": 0.35},
        }
        depths = {
            "0xO1": {"TO1": 100.0},
            "0xO2": {"TO2": 100.0},
            "0xO3": {"TO3": 100.0},
        }
        self.assertIsNone(
            self.scanner.check_basket_parity(
                "BASKET_EXPENSIVE", books_expensive, depths, min_edge=0.015
            )
        )

        # Asks: 0.33 + 0.33 + 0.33 = 0.99 -> edge = 0.01 < min_edge 0.015 -> returns None
        books_sub_edge = {
            "0xO1": {"TO1": 0.33},
            "0xO2": {"TO2": 0.33},
            "0xO3": {"TO3": 0.33},
        }
        self.assertIsNone(
            self.scanner.check_basket_parity(
                "BASKET_EXPENSIVE", books_sub_edge, depths, min_edge=0.015
            )
        )

    def test_adapter_format_merge_transaction(self):
        """Test NegRiskAdapter.format_merge_transaction outputs correct structure."""
        cid = "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
        tx = self.adapter.format_merge_transaction(cid, amount_shares=10.0)

        self.assertIsInstance(tx, dict)
        self.assertEqual(tx["function"], "mergePositions")
        self.assertEqual(tx["condition_id"], cid)
        self.assertEqual(tx["amount_shares"], 10.0)
        self.assertEqual(tx["amount_raw"], 10_000_000)
        self.assertTrue(tx["calldata"].startswith("0x"))
        self.assertIn("to", tx)
        self.assertEqual(tx["to"], NegRiskAdapter.CTF_EXCHANGE_ADDRESS)


if __name__ == "__main__":
    unittest.main()
