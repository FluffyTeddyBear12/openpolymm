# Bot 1 (`pmus`) Full Forensic Review & Fine-Tuning Specification

- **Date**: October 24, 2026
- **Target System**: Bot 1 (`pmus`, Polymarket Global Two-Sided LIP Market Maker)
- **Production Host**: AWS Ireland VPS `52.16.177.132` (`pmus.service`, baseline commit `50ace40`, untouched pending deployment approval)
- **Proxy Wallet**: `0xe7d565d58c61e2b96adb0e4518e7c33978402e7f`
- **Verification Status**: `463 passed in 7.53s` (`0 failed`) across `pmus/tests`; live 3,000-book allocation verified (`$144.25` deployed across 3 uncorrelated two-sided conditions at `$15.94/day` expected LIP)

---

## 1. Executive Summary & Wallet Ground-Truth Reconciliation

Bot 1 (`pmus`) quotes two-sided maker liquidity (`BUY YES` + `BUY NO` with `p_yes + p_no <= 0.98`) on Polymarket Global to capture daily quadratic Liquidity Incentive Program (`LIP`) payouts. Across the full on-chain history (`375` traded tokens, `1,593` fills, `357` closed positions, and `18` open positions), Bot 1's LIP reward engine works as designed (`+$275.71` total non-trade income `[measured]`), while net profitability was dragged negative by adverse fills on fast-jumping markets, unseasoned `<48h` listings, correlated sibling concentration, and a dead breakeven-only unwind loop.

| Metric / Operating Era | Token Count | Realized + MTM Trade PnL | Settled + Accrued LIP & Rebates | Net Strategy PnL | Primary Driver |
|---|---|---|---|---|---|
| **Pre-LIP Testing (`Oct 06-10`)** | `237` | `-$190.24` `[measured]` | `$0.00` `[measured]` | `-$190.24` | Early directional/taker experiments prior to LIP maker engine |
| **Single-Sided LIP (`Oct 11-15`)** | `34` | `-$144.25` `[measured]` | `+$62.80` `[measured]` | `-$81.45` | `1/3` single-sided penalty and naked directional fills |
| **Two-Sided Global (`Oct 16-24`, excl. PHP)** | `102` | `-$352.68` `[measured]` | `+$183.45` (`$22.39/d` full days) | `-$169.23` | `-$219.89` early Oct 16-19 toxic fills + `$39.54` open bags |
| **Two-Sided Global (`Oct 16-24`, incl. PHP)** | `104` | `-$411.83` `[measured]` | `+$183.45` `[measured]` | `-$228.38` | Includes `$59.15` locked in 2 halted PHP security-flaw legs |
| **Post-Wave-19 (`>= Oct 21`, excl. PHP)** | `32` | `-$91.48` `[measured]` | `+$81.90` `[measured]` | **`-$9.58`** | Near breakeven once sports/weather/crypto-airdrop filters landed |

**Non-Trade Yield Breakdown (`+$275.7101` `[measured]`)**
- On-chain settled `REWARD` payouts (`Oct 12-23` accruals, exact `T+1` match to 4 decimal places): `+$234.2300`
- Official accrued Oct 24 LIP payout: `+$12.0198`
- Settled `MAKER_REBATE`: `+$19.4603`
- Settled `TAKER_REBATE`: `+$10.0000`

---

## 2. What Bot 1 Was Doing Right (Preserved Strengths)

1. **Exact Quadratic LIP Math (`pmus/strategy/global_rewards.py`)**
   Models Polymarket Global's exact quadratic scoring curve `((v - s) / v)^2`, `c = 3.0` two-sided scaling (`min` + `max / 3`), and extreme midpoint (`[0.10, 0.90]`) single-sided disqualification `[measured]`.
2. **Concave Marginal-Efficiency Waterfilling (`pmus/strategy/condition_selector.py`)**
   Allocates budget across discrete `$36..$85` tiers by marginal reward per incremental dollar rather than dumping the entire bankroll into a single crowded pool `[measured]`.
3. **Cross-Book Synthetic Arbitrage Protection (`pmus/strategy/book_guard.py`, `pmus/engine.py`)**
   Enforces `price + opp_bid <= 0.99` both pre-trade and mid-tick, immediately canceling resting bids before cross-book takers pick off stale pairs `[measured]`.
4. **Self-Order Stripping & Anti-Chase Bid Caps (`pmus/engine.py`)**
   Strips Bot 1's own resting and recently canceled orders before computing `adjusted_mid` and `support_price`, preventing self-referential upward bid chasing `[measured]`.

---

## 3. What Bot 1 Was Doing Wrong & The 7 Structural Fixes (`pmus/`)

### Root Cause 1: Missing Lifecycle & Delay Gates on `/sampling-markets`
- **Problem**: `clob_venue.py` parsed only 4 fields from `/sampling-markets` and synthesized fake `now - 1h` / `now + 23h` timestamps, admitting `<48h` unseasoned listings (`-$156.59` historical losses incl. `$59.15` PHP security-flaw halt), `<14d` expiring markets (`-$91.20` losses), and live delayed-matching sports markets (`seconds_delay > 0`) `[measured]`.
- **Fix (`pmus/clob_venue.py`, `pmus/models.py`)**: Extracted native `accepting_order_timestamp`, `neg_risk_market_id`, `tags`, `seconds_delay`, `accepting_orders`, `archived`, and `gameStartTime`/`eventStartTime` with zero extra HTTP calls. Enforced `MIN_MARKET_AGE = 48h`, `MIN_DAYS_TO_EXPIRY = 14d` (`30d` for championship futures lacking `game_start_time`), `seconds_delay == 0`, active order-book flags, and `SAMPLING_CACHE_TTL_SEC = 300.0`.

### Root Cause 2: Correlation Blindness & Single-Theme Concentration
- **Problem**: Multi-outcome ladders without `neg_risk_market_id` received distinct slugs, letting Bot 1 put `83.8%` of budget into correlated AI Arena ladders and `86.9%` into Israeli Minister markets `[measured]`.
- **Fix (`pmus/strategy/condition_selector.py`)**: Added dual correlation keys (`negrisk:<id>` + normalized `stem:<question_stem>` stripping terminal deadlines and numeric/percentage strike comparators), a `42%` single-condition budget share cap (`$36` floor) whenever `>= 3` independent groups qualify (across both waterfill and `< $100` fallback paths), `1.3x` `ADMISSION_HYSTERESIS`, and continuous `risk_adjustment_multiplier` weighting (`comp_q`, `TTE`, price symmetry, merge margin, and bid/ask balance).

### Root Cause 3: Poisoned `keep_negatives.json` Fixture & Jump-Event Regex Gaps
- **Problem**: `keep_negatives.json` contained `43` mislabeled esports, MLB postseason, IPO, and box-office questions that forced weak regexes in `fill_risk.py`, leaking `16` jump-event markets on Oct 24 (`LiveBench`, `Text Arena`, `HLE-Diamond`, `Global Power Rankings`, `valuation`, `Metascore`, `domestic gross`, `PHP security flaw`) `[measured]`.
- **Fix (`pmus/strategy/fill_risk.py`, `pmus/tests/fixtures/fill_risk/keep_negatives.json`)**: Surgically pruned only the `43` mislabeled entries (preserving `334` valid institutional/political test cases), normalized smart quotes, and expanded `FILL_RISK_RULES` to block `51/54` (`96.31%` USD) historical two-sided losers and `100%` of Oct 24 + live-pool jump leaks.

### Root Cause 4: Dead `0.01`-Tick Front Shield, Ask Overhangs, and Mid-Tick Price Drift
- **Problem**: `bid_price` only stepped back on `len(qual_levels) >= 2` and blocked 1-tick step-backs on `0.03` `max_spread` books at half-cent mids (`0.03 * 0.625 = 0.01875 < 0.02`); `quote_guard` lacked an ask-wall overhang check; `lip_quoter.py` lacked live `[0.22, 0.78]` pair leg bounds (`Markus Ruck` filled at `0.218`) `[measured]`.
- **Fix (`pmus/strategy/global_rewards.py`, `pmus/strategy/book_guard.py`, `pmus/strategy/lip_quoter.py`)**: Added `_has_front_shield` (`>= $25` front notional on multi-level `0.01`-tick books) with half-cent `0.03`-spread step-back support, rejected `>12.5x` ask-wall overhangs (`>= $500` ask wall vs `max(top_bid_usd, near_bid_usd)`), and enforced live `[0.22, 0.78]` paired bid bounds on every tick.

### Root Cause 5: Dead Breakeven-Only `SELL_LONG` Unwind & Matched-Pair Liquidation Bug
- **Problem**: Once an unhedged fill dropped `3c` below `avg_price` (`Alibaba` `0.45 -> 0.23`, `Kimi K3.1 Pro` `0.46 -> 0.16`, `Markus Ruck` `0.218 -> 0.01`), `lip_quoter.py` only posted asks at `>= avg_price` (never filling) and canceled manual operator `SELL` orders below `avg_price`. Conversely, `/interrogate` caught that naive stop-unwinding after `prepare()` rotation would liquidate `$1.00`-redeemable matched pairs `[measured]`.
- **Fix (`pmus/strategy/lip_quoter.py`, `pmus/engine.py`)**: Implemented 4-state `UnwindRegime` (`HEDGED_PAIR_HOLD`, `IN_SPREAD_SCRATCH`, `LIQUID_STOP_UNWIND` down to `max(0.12, 0.55 * avg_price)` when `ask >= stop_floor` and `near_bid_usd >= $75`, and `COLLAPSED_STUB_HOLD` refusing to panic-dump `$0.01` stubs), persisted `known_sibling_by_slug` across reallocations so matched pairs are never stop-sold, bypassed `SELL_HYSTERESIS_BAND` on downward stop-unwinds, and preserved manual operator `SELL` orders (including ledger fallback on startup).

### Root Cause 6: Reallocation Churn (`13` Rotations in `3h41m`) & Collateral-Safe Sizing
- **Problem**: `CAPITAL_UTILIZATION_RATIO = 0.92` rotated the entire portfolio every `2-17` minutes whenever one leg paused, breaking Polymarket's 60-second continuous LIP scoring cycles `[measured]`.
- **Fix (`pmus/engine.py`)**: Lowered `CAPITAL_UTILIZATION_RATIO` to `0.75` (with dynamic `%` log formatting), raised `CAPITAL_UNDERDEPLOYED_TICKS = 30` and `YIELD_COLLAPSE_COOLDOWN_SECONDS = 180.0`, and added asymmetric `_qty_within_tolerance` in `reconcile_quotes` (keeping resting `BUY` queue priority on minor `<= +20%` / `<= 15`-share upward resizes while immediately canceling on downsizes so USDC collateral is never over-committed).

### Root Cause 7: Midnight Cost-Basis Blind Spot & Windowed LIP Telemetry Skew
- **Problem**: `get_realized_loss_today()` ignored pre-midnight inventory cost basis (`$0.00` tracked vs `$16.90` actual loss), `PMUS_DAILY_LOSS_BASELINE=72.62` never expired across days, `shadow.py` ignored `SELL_LONG` cross-side LIP scoring, and `get_lip_stats(since_ms > 0)` counted rows (`1s`) instead of `dt_sec` `[measured]`.
- **Fix (`pmus/ledger.py`, `pmus/config.py`, `pmus/engine.py`, `pmus/shadow.py`, `pmus/report.py`)**: Seeded pre-window inventory cost basis in `get_realized_pnl_since`, added UTC date-scoped `daily_loss_baseline_date` and `dynamic_daily_loss_stop` enforcement, stripped mirrored opposite-book asks in `estimate_global_pair_score`, persisted `dt_sec` in `lip_snapshots` (with idempotent `ALTER TABLE` migration), and wired `realized_pnl` into `report.py`.

---

## 4. Before vs. After Benchmark & Live 3,000-Book Verification

| Verification Metric | Baseline (`50ace40`) | Tuned `pmus/` (`HEAD` Worktree) |
|---|---|---|
| **Automated Unit & Regression Suite (`pmus/tests`)** | `442 passed` | **`463 passed` (`0 failed` in `7.53s`)** |
| **Historical 54 Two-Sided Losers Blocked** | `24 / 54` (`53.82%` loss USD) | **`51 / 54` (`96.31%` loss USD)** |
| **Oct 24 Leaked Jump Markets Blocked** | `0 / 16` (`0.0%`) | **`16 / 16` (`100.0%`) + 3 Live Rank Leaks** |
| **Valid `keep_negatives.json` Cases Preserved** | `377` (incl. `43` poisoned) | **`334 / 334` (`100%` clean cases)** |
| **Overnight Realized Loss Accounting** | `$0.00` tracked (blind spot) | **`$16.90` tracked (exact FIFO/avg cost)** |
| **Live 3,000-Book Deployment (`$148.07` Budget)** | Correlated AI/sports jump concentration | **`$144.25` (`97.42%`) across 3 uncorrelated pairs** |
| **Live 3,000-Book Expected LIP Yield** | Churned every `2-17 min` | **`$15.94 / day` (`5c-8c` pair merge buffers)** |

---

## 5. Operator Action Plan & Safe VPS Deployment Sequence

1. **Handling Existing Underwater Inventory Before Restart**
   - **`Alibaba` (`-$16.58` MTM, expires Oct 31) & `Kimi K3.1 Pro` (`-$9.40` MTM)**: Both books are liquid above `0.55 * avg_price`. Starting the tuned engine without manual orders will trigger `LIQUID_STOP_UNWIND` to free trapped capital back into `$15.94/day` two-sided LIP pools. If you prefer to hold either position for a higher bounce price, place a manual limit `SELL` on Polymarket UI before starting `pmus.service`; the new manual-sell adoption code will preserve your order untouched.
   - **`Markus Ruck` (`-$13.52` MTM, `$0.01` stub)**: Automatically classified as `COLLAPSED_STUB_HOLD`; the bot will NOT dump shares at `$0.01`.
   - **Halted `PHP` Security-Flaw Shares (`$59.15` cost)**: Order book is halted (`enable_order_book=False`); leave untouched until UMA oracle resolution settles on-chain.
2. **Clean Up Stale `/etc/pmus.env` Override on VPS**
   - Remove `PMUS_DAILY_LOSS_BASELINE=72.62` (or pair it with `PMUS_DAILY_LOSS_BASELINE_DATE=YYYY-MM-DD`) in `/etc/pmus.env` so the `$15.00` / `8%` dynamic daily loss stop operates cleanly from `0` each UTC day.
3. **Selective Git Commit & Zero-Lock VPS Deployment (Run Only After Approval)**
   - Stage strictly `pmus/` (`git add pmus/`) so the `24` modified legacy root files outside `pmus/` are excluded.
   - Stop `pmus.service` on `52.16.177.132` **before** pulling/restarting (`sudo systemctl stop pmus.service`) so `Ledger._init_db()` applies `ALTER TABLE lip_snapshots ADD COLUMN dt_sec` cleanly without contending with a live writer on the `2.6 GB` SQLite WAL file, then start `sudo systemctl start pmus.service`.
