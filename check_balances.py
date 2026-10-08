import os
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

rpc_url = os.environ.get('POLYGON_RPC_URL', 'https://1rpc.io/matic')
w3 = Web3(Web3.HTTPProvider(rpc_url))

raw_addr = os.environ.get('POLYMARKET_ADDRESS') or os.environ.get('POLYGON_ADDRESS', '0x0000000000000000000000000000000000000000')
addr = w3.to_checksum_address(raw_addr)
print(f"Checking balances for address: {addr}")
print('POL:', w3.eth.get_balance(addr) / 10**18)
abi = [{'inputs':[{'internalType':'address','name':'account','type':'address'}],'name':'balanceOf','outputs':[{'internalType':'uint256','name':'','type':'uint256'}],'stateMutability':'view','type':'function'}]
usdc_e = w3.eth.contract(address=w3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'), abi=abi)
usdc_native = w3.eth.contract(address=w3.to_checksum_address('0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359'), abi=abi)
print('USDC.e:', usdc_e.functions.balanceOf(addr).call() / 10**6)
print('USDC Native:', usdc_native.functions.balanceOf(addr).call() / 10**6)
