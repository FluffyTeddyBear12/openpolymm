import sys

with open(r'd:\neststock\scripts\polymarket_bot\test_paper_trader.py', 'r', encoding='utf-8') as f:
    content = f.read()

live_executor_tests = '''
import pytest
from unittest.mock import MagicMock, patch
from paper_trader import LiveExecutor, RiskSizingEngine, DashboardState

def test_live_executor_initialization_no_key():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': ''}):
        risk_engine = MagicMock(spec=RiskSizingEngine)
        executor = LiveExecutor(risk_engine=risk_engine)
        assert executor.client is None

def test_live_executor_initialization_with_key():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc'}):
        with patch('py_clob_client.client.ClobClient') as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.create_or_derive_api_creds.return_value = 'mock_creds'
            risk_engine = MagicMock(spec=RiskSizingEngine)
            executor = LiveExecutor(risk_engine=risk_engine)
            assert executor.client is not None
            mock_instance.set_api_creds.assert_called_with('mock_creds')

def test_live_executor_execute_arbitrage_success():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc'}):
        with patch('py_clob_client.client.ClobClient') as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.create_or_derive_api_creds.return_value = 'mock_creds'
            mock_instance.post_orders.return_value = "success"
            
            risk_engine = MagicMock(spec=RiskSizingEngine)
            risk_engine.capital = 1000
            risk_engine.max_exposure_pct = 0.1
            risk_engine.available_cash = 1000
            risk_engine.can_trade.return_value = True
            risk_engine.open_position.return_value = True
            
            dash_state = MagicMock(spec=DashboardState)
            dash_state.state = {"execution_mode": "Live Trading", "live_wager_cap": 5.0}
            
            m_map = {"0xmarket": {"token_yes": "0xYes", "token_no": "0xNo"}}
            executor = LiveExecutor(risk_engine=risk_engine, market_token_map=m_map, dash_state=dash_state)
            
            executor.market_books = {"0xmarket": {"0xYes": 0.40, "0xNo": 0.40}}
            executor.market_depths = {"0xmarket": {"0xYes": 1000.0, "0xNo": 1000.0}}
            
            opp = {
                "market_id": "0xmarket",
                "trade_size": 10.0,
                "ask_yes": 0.40,
                "ask_no": 0.40,
                "effective_cost": 0.80,
                "edge": 0.20
            }
            
            # Wager cap is 5.0, so trade_size should be capped at 5.0
            res = executor.execute_arbitrage(opp)
            
            assert res is True
            mock_instance.post_orders.assert_called_once()
            risk_engine.open_position.assert_called_once_with("0xmarket", 5.0, 5.0 * 0.20)
'''

with open(r'd:\neststock\scripts\polymarket_bot\test_paper_trader.py', 'a', encoding='utf-8') as f:
    f.write('\n' + live_executor_tests + '\n')
print("Added LiveExecutor tests")
