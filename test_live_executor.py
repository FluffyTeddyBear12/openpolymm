import unittest
from unittest.mock import patch, MagicMock
from paper_trader import LiveExecutor, MissedReason

@patch.dict("os.environ", {
    "POLYMARKET_PRIVATE_KEY": "0x" + "1" * 64,
    "POLYMARKET_ADDRESS": "0x" + "2" * 40,
    "POLYGON_RPC_URL": "https://1rpc.io/matic"
})
class TestLiveExecutor(unittest.TestCase):
    def setUp(self):
        self.dash = MagicMock()
        self.dash.state = {
            "execution_mode": "Live Trading",
            "live_wager_cap": 10.0,
            "markets": {}
        }
        self.dash.add_activity_log = MagicMock()
        self.dash.add_trade = MagicMock()
        self.dash.clear_market_edge = MagicMock()

        self.risk = MagicMock()
        self.risk.available_cash = 100.0
        self.risk.capital = 100.0
        self.risk.max_exposure_pct = 0.10
        self.risk.can_trade.return_value = True
        self.risk.open_position = MagicMock(return_value=True)

        market_token_map = {
            "0xmarket1": {
                "token_yes": "111",
                "token_no": "222",
                "question": "Test Market",
                "outcomes": ["Yes", "No"]
            }
        }

        with patch("paper_trader.ClobClient", create=True):
            self.executor = LiveExecutor(self.risk, market_token_map, self.dash)
            self.executor.client = MagicMock()
            self.executor.trade_lock = MagicMock()
            self.executor.trade_lock.__enter__ = MagicMock(return_value=None)
            self.executor.trade_lock.__exit__ = MagicMock(return_value=None)
            self.executor.lock = MagicMock()
            self.executor.lock.__enter__ = MagicMock(return_value=None)
            self.executor.lock.__exit__ = MagicMock(return_value=None)
            self.executor.risk.lock = MagicMock()
            self.executor.risk.lock.__enter__ = MagicMock(return_value=None)
            self.executor.risk.lock.__exit__ = MagicMock(return_value=None)
            self.executor.market_books = {"0xmarket1": {"YES": 0.49, "NO": 0.49}}
            self.executor.market_depths = {"0xmarket1": {"YES": 100.0, "NO": 100.0}}
            self.executor.last_processed_quotes = {}
            self.executor.shadow_tracker = MagicMock()
        self.dash.add_activity_log.reset_mock()

        self.opp = {
            "market_id": "0xmarket1",
            "trade_size": 10.0,
            "edge": 0.02,
            "ask_yes": 0.49,
            "ask_no": 0.49
        }

    def test_execute_arbitrage_unfilled_fok(self):
        """Test execute_arbitrage when Polymarket kills FOK orders (returns errorMsg)."""
        mock_resp = [
            {
                "errorMsg": "order couldn't be fully filled. FOK orders are fully filled or killed.",
                "takingAmount": "",
                "status": "",
                "success": True
            },
            {
                "errorMsg": "order couldn't be fully filled. FOK orders are fully filled or killed.",
                "takingAmount": "",
                "status": "",
                "success": True
            }
        ]
        self.executor.client.post_orders.return_value = mock_resp

        result = self.executor.execute_arbitrage(self.opp)

        self.assertFalse(result)
        self.risk.open_position.assert_not_called()
        self.dash.add_trade.assert_not_called()
        self.dash.add_activity_log.assert_called_once()
        log_msg = self.dash.add_activity_log.call_args[0][0]
        self.assertIn("⚠️ Live Orders Unfilled", log_msg)
        self.executor.shadow_tracker.record_missed.assert_called_once()
        args, kwargs = self.executor.shadow_tracker.record_missed.call_args
        self.assertEqual(args[1], MissedReason.CLOB_ORDER_KILLED)

    def test_execute_arbitrage_zero_taking_amount(self):
        """Test execute_arbitrage when takingAmount is 0 without errorMsg."""
        mock_resp = [
            {"takingAmount": "0.0", "status": "FILLED", "success": True},
            {"takingAmount": "10.0", "status": "FILLED", "success": True}
        ]
        self.executor.client.post_orders.return_value = mock_resp

        result = self.executor.execute_arbitrage(self.opp)

        self.assertFalse(result)
        self.risk.open_position.assert_not_called()
        self.dash.add_trade.assert_not_called()
        self.assertTrue(self.dash.add_activity_log.called)
        self.executor.shadow_tracker.record_missed.assert_called_once()

    @patch("web3.Web3")
    def test_execute_arbitrage_merge_failure(self, mock_web3_class):
        """Test execute_arbitrage when orders fill but token merge reverts or fails."""
        mock_resp = [
            {"takingAmount": "20.4", "status": "FILLED", "success": True},
            {"takingAmount": "20.4", "status": "FILLED", "success": True}
        ]
        self.executor.client.post_orders.return_value = mock_resp

        mock_w3 = MagicMock()
        mock_web3_class.return_value = mock_w3
        mock_web3_class.to_checksum_address = lambda x: x
        mock_web3_class.to_bytes = lambda hexstr: b"0" * 32
        mock_web3_class.to_hex = lambda x: "0xhash"

        mock_contract = MagicMock()
        mock_w3.eth.contract.return_value = mock_contract
        mock_contract.functions.balanceOf().call.side_effect = [0, 0]

        result = self.executor.execute_arbitrage(self.opp)

        self.assertTrue(result)
        self.risk.open_position.assert_called_once()
        self.dash.add_trade.assert_called_once()
        self.dash.add_activity_log.assert_any_call("⚠️ Token Merge Failed or pending on-chain: Position held for auto-unwind")

    @patch("web3.Web3")
    def test_execute_arbitrage_merge_revert_receipt(self, mock_web3_class):
        """Test execute_arbitrage when merge transaction reverts (receipt.status == 0)."""
        mock_resp = [
            {"takingAmount": "20.4", "status": "FILLED", "success": True},
            {"takingAmount": "20.4", "status": "FILLED", "success": True}
        ]
        self.executor.client.post_orders.return_value = mock_resp

        mock_w3 = MagicMock()
        mock_web3_class.return_value = mock_w3
        mock_web3_class.to_checksum_address = lambda x: x
        mock_web3_class.to_bytes = lambda hexstr: b"0" * 32
        mock_web3_class.to_hex = lambda x: "0xhash"

        mock_contract = MagicMock()
        mock_w3.eth.contract.return_value = mock_contract
        mock_contract.functions.balanceOf().call.side_effect = [1000000, 1000000]

        receipt = MagicMock()
        receipt.status = 0
        mock_w3.eth.wait_for_transaction_receipt.return_value = receipt

        result = self.executor.execute_arbitrage(self.opp)

        self.assertTrue(result)
        self.risk.open_position.assert_called_once()
        self.dash.add_trade.assert_called_once()
        self.dash.add_activity_log.assert_any_call("⚠️ Token Merge Failed or pending on-chain: Position held for auto-unwind")

    @patch("web3.Web3")
    def test_execute_arbitrage_success(self, mock_web3_class):
        """Test execute_arbitrage when orders fill and merge confirms on-chain."""
        mock_resp = [
            {"takingAmount": "20.4", "status": "FILLED", "success": True},
            {"takingAmount": "20.4", "status": "FILLED", "success": True}
        ]
        self.executor.client.post_orders.return_value = mock_resp

        mock_w3 = MagicMock()
        mock_web3_class.return_value = mock_w3
        mock_web3_class.to_checksum_address = lambda x: x
        mock_web3_class.to_bytes = lambda hexstr: b"0" * 32
        mock_web3_class.to_hex = lambda x: "0xhash"

        mock_contract = MagicMock()
        mock_w3.eth.contract.return_value = mock_contract
        mock_contract.functions.balanceOf().call.side_effect = [1000000, 1000000]

        receipt = MagicMock()
        receipt.status = 1
        mock_w3.eth.wait_for_transaction_receipt.return_value = receipt

        result = self.executor.execute_arbitrage(self.opp)

        self.assertTrue(result)
        self.risk.open_position.assert_called_once()
        self.dash.add_trade.assert_called_once()
        self.dash.clear_market_edge.assert_called_once_with("0xmarket1")

if __name__ == "__main__":
    unittest.main()
