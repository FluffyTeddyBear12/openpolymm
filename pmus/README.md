# Polymarket US Trading Engine (`pmus`)

A production-grade Python package for automated market making, liquidity incentive program (LIP) quoting, and multi-outcome basket arbitrage on the Polymarket US CFTC-regulated exchange.

Designed for small-capitalization accounts (~$70 collateral) with strict risk gating, SQLite append-only audit ledger, and zero unverified execution.

---

## Architecture Overview

- **`pmus.models`**: Immutable frozen dataclasses representing Markets, Books, Quotes, Orders, Positions, Fills, and LIP Snapshots. All prices and values use `Decimal` with banker's rounding (`ROUND_HALF_EVEN`).
- **`pmus.fees`**: Exact schedule implementations for Polymarket US maker rebates (`0.0125*C*p*(1-p)`), standard taker fees (`0.0695*C*p*(1-p)`), table tennis taker fees (`0.10*C*p*(1-p)`), and combo arbitrage calculations.
- **`pmus.venue`**:
  - `PublicGatewayClient`: High-performance public REST client for markets, order books, and active incentive windows with exponential backoff on HTTP 429.
  - `AuthenticatedClient`: Wrapper around the official `polymarket-us` SDK for scoped batch creations, chunked cancellations (<= 20 orders/call), account balance queries, and emergency market flattening.
- **`pmus.strategy.lip_quoter`**: Pure quoting function joining best bid/ask, respecting per-market contract caps, and generating valid side intents (`BUY_LONG`, `BUY_SHORT`, `SELL_LONG`, `SELL_SHORT`).
- **`pmus.strategy.basket_scanner`**: Combinatorial basket scanner detecting short-all and buy-all arbitrage opportunities across mutually exclusive markets net of taker fees.
- **`pmus.strategy.market_selector`**: Incentive program ranker prioritizing active windows by expected $/sec pool rewards per $ collateral.
- **`pmus.risk`**: Pre-submission risk gating covering kill switch file (`pmus/STOP`), America/New_York Thursday maintenance window (05:55-08:05 ET), daily realized loss stop ($10.00), order count limits, and account buying power.
- **`pmus.ledger`**: Append-only SQLite ledger (`ledger.db`) storing quotes, orders, fills, positions, and LIP snapshots.
- **`pmus.engine`**:
  - `ShadowEngine`: Paper simulation loop evaluating real order book depth, generating quotes, simulating fills against adverse crosses, and recording LIP snapshots.
  - `LiveEngine`: Real execution loop submitting batches via authenticated gateway, reconciling state, and enforcing risk controls.
- **`pmus.report`**: P&L and volume reporting with fee accounting, inventory mark-to-market, and qualifying LIP incentive estimates.

---

## Configuration & Environment Variables

All settings can be configured via environment variables or loaded from an environment file:

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `PMUS_KEY_ID` | String | `""` | Polymarket US API Key ID |
| `PMUS_SECRET_KEY` | String | `""` | Polymarket US API Secret Key |
| `PMUS_MODE` | String | `shadow` | Execution mode: `shadow` or `live` |
| `PMUS_GATEWAY_URL` | String | `https://gateway.polymarket.us` | REST API gateway base URL |
| `PMUS_MAX_COLLATERAL` | Decimal | `70.00` | Account total collateral boundary |
| `PMUS_TOTAL_BUDGET` | Decimal | `60.00` | Max active capital deployed across all markets |
| `PMUS_MAX_PER_MARKET` | Decimal | `25.00` | Max capital deployed per individual market |
| `PMUS_MAX_MARKETS` | Integer | `4` | Max concurrent quoted markets |
| `PMUS_MAX_CONTRACTS_PER_MARKET` | Integer | `200` | Max contract position cap per market |
| `PMUS_MAX_OPEN_ORDERS` | Integer | `20` | Max total open orders across all markets |
| `PMUS_DAILY_LOSS_LIMIT` | Decimal | `5.00` | Daily warning loss threshold |
| `PMUS_DAILY_LOSS_STOP` | Decimal | `10.00` | Hard trading halt daily loss stop |
| `PMUS_DATA_DIR` | Path | `pmus/data` | Path to data directory for SQLite ledger |
| `PMUS_LOGS_DIR` | Path | `pmus/logs` | Path to log directory for rotating log files |

---

## CLI Usage

Run commands using the Python module entrypoint:

```bash
# Activate virtual environment
source venv/bin/activate  # Linux/macOS
# or: venv\Scripts\activate on Windows

# 1. Probe live public gateway markets and incentives
python -m pmus probe

# 2. Run in shadow (paper) mode for 10 minutes
python -m pmus run --mode shadow --minutes 10

# Run in shadow mode for specific seconds (e.g. 60 seconds)
python -m pmus run --mode shadow --seconds 60

# 3. Run in live mode (requires PMUS_MODE=live and --confirm-live flag)
PMUS_MODE=live python -m pmus run --mode live --confirm-live --minutes 60

# 4. Generate performance and LIP reward report
python -m pmus report --since 1h
python -m pmus report --since 24h

# 5. Emergency flattening (cancels open orders and closes positions)
# Specific market:
python -m pmus flatten --slug asc-eurolg-bjk-bay-2026-10-16-neg-10pt5
# All active positions:
python -m pmus flatten
```

---

## Safety Controls & Risk Gates

1. **Dual Live Protection**: Live trading requires both `PMUS_MODE=live` in the environment and `--confirm-live` CLI flag. Missing either flag halts startup immediately.
2. **Emergency Kill Switch**: Creating a file named `STOP` inside `pmus/` (or the directory containing `pmus/data`) immediately blocks order creation and triggers position pausing.
3. **Weekly Maintenance Window**: Exchange maintenance occurs every Thursday between 05:55 ET and 08:05 ET. The engine automatically halts quoting during this window.
4. **Daily Realized Loss Stop**: If realized trading losses in the current 24-hour UTC window reach `$10.00`, all quoting halts.
5. **Scoped Order Cancellation**: On startup or market release, orders are canceled only for explicitly managed market slugs to avoid interfering with other trading activities.

---

## Logs & Audit Records

- **Application Logs**: Written to `pmus/logs/pmus.log` with automatic rotation (10 MB size, 5 backups) and mirrored to stdout.
- **SQLite Ledger**: Maintained at `pmus/data/ledger.db` with indexed tables:
  - `quotes`: Desired quotes generated each tick.
  - `orders`: Open, filled, and canceled orders.
  - `fills`: Executed paper or live fills with prices and quantities.
  - `positions`: Real-time net contract positions and volume-weighted average price.
  - `lip_snapshots`: Per-second spread, midpoint, depth, and estimated reward accrual.

---

## Production Deployment

### Systemd Service (Linux)

Install the service file from `deploy/pmus.service`:

```bash
sudo cp deploy/pmus.service /etc/systemd/system/pmus.service
sudo chmod 644 /etc/systemd/system/pmus.service

# Configure environment variables in /etc/pmus.env
sudo nano /etc/pmus.env

# Reload and start service
sudo systemctl daemon-reload
sudo systemctl enable pmus
sudo systemctl start pmus

# Monitor service
sudo systemctl status pmus
journalctl -u pmus -f
```

### Docker Container

Build and run using `deploy/Dockerfile`:

```bash
# Build Docker image
docker build -t pmus-bot -f deploy/Dockerfile .

# Run container in shadow mode
docker run -d \
  --name pmus-shadow \
  -v $(pwd)/pmus/data:/app/pmus/data \
  -v $(pwd)/pmus/logs:/app/pmus/logs \
  pmus-bot run --mode shadow --minutes 1440
```
