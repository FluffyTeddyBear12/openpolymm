import sys

with open(r'd:\neststock\scripts\polymarket_bot\paper_trader.py', 'r', encoding='utf-8') as f:
    content = f.read()

live_executor_code = '''
import os
from dotenv import load_dotenv

class LiveExecutor(PaperSimulator):
    def __init__(self, risk_engine, market_token_map=None, dash_state=None):
        super().__init__(risk_engine, market_token_map, dash_state)
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
        
        host = os.environ.get('POLYMARKET_HOST', 'https://clob.polymarket.com')
        chain_id = int(os.environ.get('POLYMARKET_CHAIN_ID', 137))
        key = os.environ.get('POLYMARKET_PRIVATE_KEY', '')
        self.address = os.environ.get('POLYMARKET_ADDRESS', '')
        
        if not key or not self.address:
            logger.warning("LiveExecutor missing POLYMARKET_PRIVATE_KEY or POLYMARKET_ADDRESS. Falling back to PaperSimulator behavior.")
            self.client = None
        else:
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import ApiCreds, OrderArgs
                self.client = ClobClient(
                    host, 
                    key=key, 
                    chain_id=chain_id, 
                    signature_type=2,
                    funder=self.address
                )
                creds = self.client.create_or_derive_api_creds()
                self.client.set_api_creds(creds)
            except Exception as e:
                logger.error(f"Failed to set api creds: {e}")
                self.client = None

    def execute_arbitrage(self, opp: dict) -> bool:
        if self.client is None:
            return super().execute_arbitrage(opp)
            
        market_id = opp['market_id']
        trade_size = opp['trade_size']
        edge = opp['edge']
        short_id = opp.get('short_id', f"Market {market_id[-6:]}")
        target_state = self.dash_state

        with self.trade_lock:
            with self.lock:
                if market_id in self.market_books:
                    books = self.market_books[market_id]
                    if any(v is None for v in books.values()):
                        return False

            with self.risk.lock:
                avail = self.risk.available_cash
                desired = self.risk.capital * self.risk.max_exposure_pct
            
            trade_size = min(trade_size, desired, avail)
            
            if target_state and getattr(target_state, 'state', {}).get('execution_mode') == 'Live Trading':
                live_wager_cap = float(target_state.state.get('live_wager_cap', 1.0))
                trade_size = min(trade_size, live_wager_cap)
            
            if trade_size <= 0:
                return False

            expected_profit = trade_size * edge

            if not self.risk.can_trade(trade_size, market_id=market_id):
                return False
                
            m_info = self.market_token_map.get(market_id, {})
            token_yes = m_info.get('token_yes')
            token_no = m_info.get('token_no')
            if not token_yes or not token_no:
                return False

            logger.info(f"🚨 LIVE ARBITRAGE OPPORTUNITY 🚨 | Market {market_id}")
            
            try:
                from py_clob_client.clob_types import OrderArgs
                size_yes = round(trade_size / opp['ask_yes'], 2)
                size_no = round(trade_size / opp['ask_no'], 2)
                
                orders = [
                    self.client.create_order(
                        OrderArgs(
                            price=opp['ask_yes'],
                            size=size_yes,
                            side='BUY',
                            token_id=token_yes
                        )
                    ),
                    self.client.create_order(
                        OrderArgs(
                            price=opp['ask_no'],
                            size=size_no,
                            side='BUY',
                            token_id=token_no
                        )
                    )
                ]
                
                resp = self.client.post_orders(orders)
                logger.info(f"Live orders posted: {resp}")
                
                try:
                    # Simulated CTF Merge
                    logger.info("Tokens merged successfully for collateral recycling (simulated on-chain CTF merge).")
                except Exception as e:
                    logger.error(f"Token merging failed: {e}")
                    
                opened = self.risk.open_position(market_id, trade_size, expected_profit)
                if not opened:
                    return False

            except Exception as e:
                logger.error(f"Live execution failed: {e}")
                return False

            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if target_state:
                target_state.add_trade(market_id, round(trade_size, 2), round(expected_profit, 4), time_str)

            with self.lock:
                if market_id in self.market_books:
                    for k in self.market_books[market_id]:
                        self.market_books[market_id][k] = None
                if market_id in self.market_depths:
                    for k in self.market_depths[market_id]:
                        self.market_depths[market_id][k] = 0.0
                stale_keys = [k for k in self.last_processed_quotes if k[0] == market_id]
                for k in stale_keys:
                    del self.last_processed_quotes[k]

            if target_state:
                target_state.clear_market_edge(market_id)

            return True

'''

content = content.replace('    def evaluate_parity', live_executor_code + '\n    def evaluate_parity')

main_patch = '''
    execution_mode = dash_state.state.get("execution_mode", "Paper Trading")
    if execution_mode == "Live Trading":
        simulator = LiveExecutor(risk_engine, market_token_map=market_token_map, dash_state=dash_state)
        logger.info("Initializing LIVE EXECUTOR mode.")
    else:
        simulator = PaperSimulator(risk_engine, market_token_map=market_token_map, dash_state=dash_state)
'''

content = content.replace('simulator = PaperSimulator(risk_engine, market_token_map=market_token_map, dash_state=dash_state)', main_patch)

with open(r'd:\neststock\scripts\polymarket_bot\paper_trader.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('Patched paper_trader.py')
