# OpenPolyMM: High-Velocity Liquidity Provisioning, Spread Protection & Parity Engine for Polymarket on Polygon

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests Passing](https://img.shields.io/badge/tests-259%2F259%20passing-brightgreen.svg)]()
[![Network: Polygon PoS](https://img.shields.io/badge/network-Polygon%20PoS%20(137)-8247E5.svg)](https://polygon.technology/)
[![Exchange: Polymarket CLOB](https://img.shields.io/badge/exchange-Polymarket%20CLOB-00F0FF.svg)](https://polymarket.com/)

OpenPolyMM is an institutional-grade algorithmic liquidity provisioning and binary parity execution engine built specifically for Polymarket's Central Limit Order Book (CLOB) on Polygon PoS.

Prediction market arbitrage offers guaranteed mathematical resolution at $1.00 when buying complementary binary outcome tokens (YES + NO) below par ($P_{YES} + P_{NO} < 1.00$). However, naive trading bots face severe real-world execution risks. These include non-atomic execution, asymmetric order fill failures, wide spreads, and phantom order book depth. OpenPolyMM solves these problems through an asymmetric maker-taker state machine, strict liquidity filters, loss-free rollback protection, and protocol liquidity mining rewards harvesting.

---

## Problem Statement

Trading binary parity opportunities across decentralized order books presents distinct mechanical hazards:

1. **Leg-Out Execution Failures**: Firing simultaneous dual-taker market orders is non-atomic. If Leg 1 fills while Leg 2 slips, gets killed, or exhausts book liquidity, the trader is left holding directional exposure.
2. **Catastrophic Liquidation Dumps**: Conventional bots attempt emergency exits by dumping unhedged Leg 1 tokens into the best bid. In thin markets, bids can drop to $0.010 on a token bought for $0.038, triggering immediate 60% to 90% capital destruction.
3. **Spread Traps and Phantom Depth**: Illiquid prop markets frequently display artificial parity opportunities. Their bid-ask spreads often exceed 50%, with executable depth falling below $5.00.
4. **Capital Drag**: Capital resting in limit orders incurs opportunity cost unless integrated with daily liquidity rewards.

---

## Core Architecture

OpenPolyMM decouples binary market execution into modular, deterministic components designed for zero-loss operation:

### 1. Dual-Leg Maker-Taker Execution Engine (`maker_taker_engine.py`)
- Replaces risky dual-taker execution with an asymmetric sequence.
- **Leg 1 (Maker)**: Posts a passive Good-Til-Cancelled (GTC) limit order inside the spread with 0% taker fee and zero slippage.
- **Zero-Loss Timeout Cancellation**: If Leg 1 does not fill within the configurable timeout (default 5.0 seconds), the engine cancels the order with $0 cost and $0 loss.
- **Leg 2 (Taker)**: Only after Leg 1 is 100% filled and verified by the sequencer, the engine submits an instant Fill-Or-Kill (FOK) order on the complementary leg.
- **Dynamic Re-pricing**: Re-fetches the live order book immediately prior to Leg 2 execution to account for microsecond price updates.

### 2. Rollback & Price Protection Guard (`rollback_protector.py`)
- Protects capital if Leg 2 fails to execute.
- **Immediate Safe Exit**: If the current best bid satisfies `best_bid >= buy_price - max_loss_cents` (default 0.5 cents), the guard executes an instant market exit.
- **Loss-Free Limit Sell**: If the best bid falls below the acceptable floor or hits penny bids, dumping is prohibited. The guard automatically places a passive GTC limit sell order at the original purchase price to achieve recovery at par.
- **Settlement Delay Resilience**: Features built-in exponential backoff and re-polling for transient Polygon token allowance and balance credit states.

### 3. Spread & Liquidity Filter (`liquidity_filter.py`)
- Evaluates order book conditions prior to execution.
- **Spread Gating**: Rejects markets where the bid-ask spread on either YES or NO exceeds 1.5 cents (`max_spread = 0.015`).
- **Depth Verification**: Enforces minimum executable liquidity across the top three ask levels ($250.00 floor and at least 2.5x desired position size).
- **Volume & Expiry Filters**: Requires at least $1,000 in 24-hour volume and rejects markets resolving within 4.0 hours to avoid settlement halts.
- **Toxicity Exclusion**: Filters out highly volatile in-play sports games, halftime intervals, and obscure prop markets.

### 4. Daily Liquidity Rewards Harvester (`reward_harvester.py`)
- Tracks active Polymarket daily liquidity reward pools (`rewards_daily_rate > 0`).
- Dynamically augments parity scoring by combining raw arbitrage edge with daily pool yield.
- Provides projected USDC yields for resting maker limit orders.

### 5. High-Velocity Telemetry Dashboard (`dashboard.py`)
- Interactive trading terminal interface built with Streamlit and Plotly.
- Real-time updates for active market spreads, collateral recycling, net PnL, and socket worker health.
- Low-latency Inter-Process Communication (IPC) for adjusting risk sizing, exposure percentages, and parity thresholds during live runs.

---

## Execution State Flow

```
                      +-----------------------------+
                      |   Polymarket CLOB Stream    |
                      |  16 Sockets across 2 Pools  |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |    Spread & Liquidity Gate   |
                      | - Spread <= 1.5c            |
                      | - Depth >= $250.00          |
                      | - 24h Volume >= $1,000      |
                      +--------------+--------------+
                                     | Passes Filter
                                     v
                      +-----------------------------+
                      |   Parity & Edge Detection   |
                      | Ask_YES + Ask_NO < Parity   |
                      +--------------+--------------+
                                     | Edge Confirmed
                                     v
                      +-----------------------------+
                      |      Leg 1: Maker Limit     |
                      |   Passive GTC Buy at Bid+1  |
                      +--------------+--------------+
                                     |
                    +----------------+----------------+
                    | Unfilled                        | 100% Filled
                    v                                 v
        +-----------------------+         +-----------------------+
        |   Cancel Maker Order  |         |   Leg 2: Taker FOK    |
        | $0 Cost / $0 Loss     |         | Instant Buy Counter   |
        +-----------------------+         +-----------+-----------+
                                                      |
                                    +-----------------+-----------------+
                                    | Fill Confirmed                    | Missed / Rejected
                                    v                                   v
                        +-----------------------+           +-----------------------+
                        |   Arbitrage Locked    |           |   Rollback Protector  |
                        | Redeemable at $1.00   |           | Bid >= Floor: Mkt Exit|
                        +-----------------------+           | Bid < Floor: Limit Par|
                                                            +-----------------------+
```

---

## Quickstart Guide

### Prerequisites
- Python 3.11 or higher
- Polygon PoS RPC endpoint (Infura, Alchemy, or public node)
- Polymarket CLOB API credentials or wallet private key

### 1. Clone the Repository
```bash
git clone https://github.com/FluffyTeddyBear12/openpolymm.git
cd openpolymm
```

### 2. Set Up Virtual Environment
```bash
python -m venv venv

# Windows
.\venv\Scripts\activate

# Linux / macOS
source venv/bin/activate

pip install -r requirements.txt
```

### 3. Configure Environment Variables
Copy `.env.example` to `.env` and fill in your keys:
```bash
cp .env.example .env
```
Ensure your `.env` contains:
```env
POLYMARKET_PRIVATE_KEY="your_private_key_without_0x"
POLYMARKET_ADDRESS="your_proxy_wallet_address"
POLYMARKET_SIGNATURE_TYPE=2
POLYMARKET_HOST="https://clob.polymarket.com"
POLYMARKET_CHAIN_ID=137
POLYGON_RPC_URL="https://polygon-bor-rpc.publicnode.com"
```

### 4. Run Test Suite
Run all 259 unit and integration tests:
```bash
python -m pytest -v
```

### 5. Launch Telemetry Dashboard
```bash
streamlit run dashboard.py
```
Open your browser at `http://localhost:8501` to view live market feeds, order books, and bot telemetry.

### 6. Start the Supervisor Engine
```bash
python start_bot.py
```
The supervisor manages single-instance socket locks, enforces CPU core affinity, monitors memory budgets, and auto-restarts background workers upon failure.

---

## Test Coverage & Verification

OpenPolyMM maintains complete test coverage with 259 passing tests across five test suites:

- **`test_maker_taker_engine.py`**: Validates happy path fills, zero-loss cancellations on maker timeouts, and delayed sequencer state handling.
- **`test_rollback_protector.py`**: Verifies negative slippage bounds, penny bid rejection, limit sell placement, and balance credit retry logic.
- **`test_liquidity_filter.py`**: Tests spread thresholds, top-3 ask depth checks, sports start time boundaries, and expiry safety horizons.
- **`test_dashboard.py`**: Verifies Streamlit state hydration, heartbeat tracking, search filtering, and scaling up to 1,000 active markets.
- **`test_paper_trader.py`**: Tests the risk sizing engine, circuit breaker triggers, collateral recycling lifecycles, and multi-socket pool dispatching.

---

## Security & Risk Disclosures

- **Non-Custodial Design**: OpenPolyMM never sends private keys to external services. All transactions are signed locally via `py_clob_client`.
- **Trading Risks**: Trading binary prediction markets carries financial risk. Always test thoroughly in paper trading mode (`paper_trader.py`) before committing real capital.
- **Credential Protection**: Never commit your `.env` file or wallet private keys to source control.

---

## Contributing & License

Contributions are welcome. Please open an issue or pull request on GitHub.

Distributed under the MIT License. See [LICENSE](LICENSE) for full details.
