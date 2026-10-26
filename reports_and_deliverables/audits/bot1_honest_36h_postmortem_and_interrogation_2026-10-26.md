# Bot 1 (`pmus`) Honest 48-Hour Postmortem, Swarm Forensics & Adversarial Interrogation (`2026-10-26`)

## Executive Summary & Direct Answer

You were right, and you were not imagining things. Over October 25 and October 26 (`43.5 hours` through `19:28 UTC`), Bot 1 (`pmus.service` on `52.16.177.132`, proxy wallet `0xe7d565d58c61e2b96adb0e4518e7c33978402e7f`) performed poorly and lost real money while both its hourly status webhook and our October 25 summary gave you a misleading picture of gross theoretical rewards.

- **Today (`Oct 26 UTC`, through `19:28 UTC`):** Settled LIP reward payout at `00:00:17 UTC` (from Oct 25 quoting) was **`+$10.00`** (`$10.0033`). Realized CLOB trading losses today totaled **`-$17.42`** (`-$16.70` in SQLite `ledger.db` due to a `$0.72` multi-level taker slippage under-count). **Today's net realized PnL is `-$7.42`.**
- **Yesterday (`Oct 25 UTC`):** Settled LIP reward payout at `00:00 UTC` (from Oct 24 quoting) was **`+$15.90`** (`$15.8994`). Realized CLOB sell losses totaled **`-$20.93`**, AND at `18:07:47 UTC` (live during Sunday 1 PM ET NFL games) Bot 1 bought `45` shares of `Will the Miami Dolphins be the last winless team of the 2026-27 NFL regular season? [Yes]` @ `$0.430` (`$19.35`), which resolved to `$0.00` (`redeemable=True`) with **`$0.00` recorded in `ledger.get_realized_loss_today()`**. **Yesterday's true economic net PnL was `-$24.38`.**
- **Combined 48-Hour True Net PnL (`Oct 25` to `Oct 26`):** `+$25.90` settled LIP rewards minus `-$38.35` closed sell losses (`-$28.87` from Bot 1 `maker=1` `LIQUID_STOP_UNWIND` orders + `-$12.03` from manual UI `maker=0` market exits, offset by `+$2.54` scratches) minus `-$19.35` Miami Dolphins `$0.00` resolution = **`-$31.80` true net loss**.
- **14-Day Structural Reality (`Oct 13` to `Oct 26`):** `+$260.13` settled LIP rewards (`+$18.35` maker rebates) vs **`-$489.23`** realized trading losses (`-$444.90` CLOB sells + `-$44.33` redeemed `$0` loss), plus `-$19.39` unredeemed `$0.00` wipeouts, `-$21.96` unrealized drawdown on live positions, and `$59.15` frozen in two archived Oct 21 `PHP` cybersecurity markets. Net negative on **11 of 14 days**.
- **Current Live Account State (`19:28 UTC`):** Bot 1 tripped its `$15.00` daily loss stop at **`18:59:24 UTC`** (`17 minutes` before you messaged us) and canceled all orders (`0` open orders). Visible mark-to-market equity is **`$202.02`** (`$176.52` free USDC cash + `$24.70` `Alibaba [Yes]` + `$0.65` `Markus Ruck [Yes]` + `$0.16` `Tyreek Hill [No]` dust), plus **`130` frozen CTF shares (`$59.15` cost)** still sitting on-chain (`payoutDenominator = 0`) inside the two archived Oct 21 `Major company hacked via PHP flaw by Nov 30 / Dec 31, 2026` markets (NOT "Philippine election shares", which we falsely stated on Oct 25).

---

## 1. Unified On-Chain & Ledger Reconciliation (`Oct 13` to `Oct 26, 2026`)

| Metric (USD) | Today (`Oct 26 UTC`) | 48h (`Oct 25` to `Oct 26`) | 14-Day (`Oct 13` to `Oct 26`) |
|---|---:|---:|---:|
| Settled LIP `REWARD` paid on-chain | `+$10.00` (`$10.0033`) | `+$25.90` (`$25.9027`) | `+$260.13` (`$260.1327`) |
| Maker fee rebates (`MAKER_REBATE`) | `$0.00` | `$0.00` | `+$18.35` (`$18.3549`) |
| Realized CLOB `SELL` PnL | `-$17.42` (`-$17.4187`) | `-$38.35` (`-$38.3490`) | `-$444.90` (`-$444.9007`) |
| Redeemed `$0.00` resolution (`Oct 18 lukxo`) | `$0.00` | `$0.00` | `-$44.33` (`-$44.3250`) |
| **Net realized cash PnL (`REWARD` + sells)** | **`-$7.42`** | **`-$12.45`** | **`-$229.09`** (`-$210.74` w/ rebates) |
| Unredeemed `$0.00` wipeouts bought in window | `$0.00` | `-$19.35` (`Dolphins`) | `-$19.39` (`Dolphins` + `HK 30C`) |
| Unrealized MTM on live positions | `+$0.01` | `+$0.01` | `-$21.96` (`Alibaba`, `Ruck`) |
| Frozen CTF shares in 2 archived Oct 21 `PHP` markets | `$0.00` | `$0.00` | `-$59.15` (`130` shares, `den=0`) |
| **True economic net PnL (ex-rebates)** | **`-$7.40`** | **`-$31.78`** (`-$31.80` ex-dust) | **`-$329.59`** (`-$311.24` w/ rebates) |

---

## 2. Complete Oct 25–26 Losing Sells & Origin Attribution (`maker=1` vs `maker=0`)

Every losing sell over the last 48 hours was cross-checked across SQLite `ledger.db` (`is_maker`, `order_id`, timestamps), Polymarket Data API (`usdcSize`), and `journalctl` logs:

| Sell UTC | Entry UTC | Hold | `mkr` | Verified Origin | Qty | Entry | Exit (`eff`) | USDC PnL | Market |
|---|---|---:|---:|---|---:|---:|---:|---:|---|
| `10-25 05:20:03` | `10-24 06:12:51` | `23.12h` | `1` | Bot Stop-Unwind | `33.33` | `0.420` | `0.300` | `-$3.99` | Kimi K3.1 Pro HLE-Diamond >=30% [Yes] |
| `10-25 05:26:59` | `10-24 06:12:51` | `23.24h` | `1` | Bot Stop-Unwind | `3.33` | `0.420` | `0.300` | `-$0.40` | Kimi K3.1 Pro HLE-Diamond >=30% [Yes] |
| `10-25 06:01:37` | `10-25 04:28:10` | `1.56h` | `1` | Bot Stop-Unwind | `60.00` | `0.220` | `0.160` | `-$3.60` | Another GTA VI trailer by Nov 30? [Yes] |
| `10-25 06:54:32` | `10-24 06:12:51` | `24.69h` | `1` | Bot Stop-Unwind | `28.00` | `0.420` | `0.300` | `-$3.35` | Kimi K3.1 Pro HLE-Diamond >=30% [Yes] |
| `10-25 08:47:20` | `10-24 20:30:50` | `12.28h` | `0` | Manual UI Dust | `3.72` | `0.310` | `0.299` | `-$0.04` | South Korea win Group E Asian Cup? [No] |
| `10-25 12:43:27` | `10-25 11:13:45` | `1.50h` | `1` | Bot Stop-Unwind | `65.00` | `0.420` | `0.360` | `-$3.90` | Israeli Min. of Foreign Affairs [Yes] |
| `10-25 17:30:21` | `10-25 15:53:11` | `1.62h` | `0` | Manual UI Taker | `60.00` | `0.410` | `0.390` | `-$1.20` | Israeli Min. of Foreign Affairs [Yes] |
| `10-25 17:30:34` | `10-25 15:58:47` | `1.53h` | `0` | Manual UI Dust | `4.69` | `0.600` | `0.580` | `-$0.09` | Israeli Min. of Finance [No] |
| `10-25 21:38:04` | `10-25 09:42:00` | `11.93h` | `1` | Bot Stop-Unwind | `65.00` | `0.262` | `0.160` | `-$6.63` | Milos Vucevic Serbia Pres. [Yes] |
| `10-26 01:00:38` | `10-26 00:06:30` | `0.90h` | `0` | Manual UI Taker | `27.54` | `0.600` | `0.550` | `-$1.38` | Marathon Fusion gold alchemy [Yes] |
| `10-26 01:00:43` | `10-25 23:59:12` | `1.03h` | `0` | Manual UI Taker | `65.00` | `0.430` | `0.378` | `-$3.40` | Israeli Min. of Justice [Yes] |
| `10-26 06:05:26` | `10-26 04:53:48` | `1.19h` | `1` | Bot Stop-Unwind | `60.00` | `0.590` | `0.520` | `-$4.20` | Bank of Brazil Selic no change [No] |
| `10-26 13:01:48` | `10-26 05:13:20` | `7.81h` | `1` | Bot Stop-Unwind | `40.00` | `0.640` | `0.570` | `-$2.80` | UMich Consumer Sentiment 46-48.9 [No] |
| `10-26 13:16:47` | `10-26 04:53:48` | `8.38h` | `0` | Manual UI Dust | `4.99` | `0.590` | `0.410` | `-$0.90` | Bank of Brazil Selic no change [No] |
| `10-26 14:33:15` | `10-26 14:13:04` | `0.34h` | `0` | Manual UI Taker | `41.96` | `0.580` | `0.534` | `-$1.92` | Israeli Min. of Nat. Security [No] |
| `10-26 18:59:09` | `10-26 18:43:21` | `0.26h` | `0` | Manual UI Taker | `55.00` | `0.540` | `0.529` | `-$0.60` | Israeli Min. of Justice [No] |
| `10-26 18:59:24` | `10-26 18:30:11` | `0.49h` | `0` | Manual UI Taker | `59.99` | `0.400` | `0.358` | `-$2.50` | Tyreek Hill sign w/ Chiefs 2026? [Yes] |

*(Plus `Miami Dolphins last winless team [Yes]`, `45` shares bought `10-25 18:07:47 UTC` @ `0.430` = `-$19.35` resolved to `0.000`, never sold on CLOB.)*

---

## 3. Five Consensus Root Causes (`/swarm` + 3-Model `/interrogate` Verdict)

1. **`max_markets=6` Pair-vs-Leg Unit Bug in Commit `d5b8362` (`engine.py:140-193, 679-698`, `risk.py:171`)**:
   - `allocate_markets()` treats `max_markets = 6` as `3` condition pairs (`~$58/pair`), sizing skewed favorite legs at `$43–$49` (`$48.10` on `OpenAI Image Edit AI`). Meanwhile `prepare()` and `tick()` pass `max_markets = 6` as `6` **pairs** into `calculate_num_pairs`, returning `est_pairs = 4` on `$174` cash and capping `dynamic_max_collateral_per_market` at **`$40.00`**. `RiskManager.check_pre_submit` silently blocks the `$48.10` favorite leg on every tick, leaving the underdog leg quoting single-sided (`S / 3` 67% LIP score penalty, `< 75%` deployed cash), triggering **37 full-portfolio reallocations in 8 hours** (`4.45 min` burst median) and freezing the single-threaded loop inside `prepare()` (`3,000` book fetches) for **`63s` median / `84s` max** (`33.87 min` total freeze) while resting orders sit blind.
2. **Post-Fill Hedge Abandonment & Hair-Trigger `LIQUID_STOP_UNWIND` (`condition_selector.py:228-330`, `engine.py:844-857, 1624-1693`, `lip_quoter.py:61-94, 361-363`)**:
   - When one leg fills, `either_leg_filled = True` plus `pair_preference = 0.0` causes the next reallocation to drop both legs and cancel the unfilled hedge leg instead of completing the `$1.00` synthetic merge. `prepare()` re-adds the held leg with `bid_budget = 0`, forcing `allow_stop_unwind = True` and bypassing the `-15%` crash gate. Any `3c–4c` spread drift (`min_exit - mid >= max_spread`) immediately activates `LIQUID_STOP_UNWIND` down to `0.55 * avg_price` (wiping out `8.4 hours` of portfolio LIP rewards per `-$3.50` stop-unwind).
3. **Broken Daily Loss Stop, `tick()` Ordering Bug, & `76.8%` CPU Halt Storm (`ledger.py:594-670`, `engine.py:545, 677, 1023-1083`)**:
   - `get_realized_loss_today()` ignores `$0.00` binary resolutions (`Miami Dolphins -$19.35`) and collapsed bags (`Markus Ruck -$13.52`). Inside `tick()`, dynamic cash-shift reallocation (`L1023-1056`) executes *before* the loss-stop gate (`L1070`), triggering an 84-second reallocation `11s` after halting (`18:59:35 UTC`), and `run()` keeps calling `tick()` + `_cancel_targets()` every `10s` indefinitely (`76.8%` CPU). Production `.env` also retains undated `PMUS_DAILY_LOSS_BASELINE=72.62`, which would erase today's halt if `pmus.service` restarted before `00:00 UTC`.
4. **Gaslighting Hourly Webhook & Dual LIP Distortion (`engine.py:926-974, 1011`, `notify.py:92-120`, `global_rewards.py:50, 235`)**:
   - `_dispatch_hourly_status_ping` fires above the loss-stop gate, counts halted + `$0.00`-resolved `preserved_pairs` as `"Active: 5 pairs"`, fabricates up to `84s–90s` of `dt_sec` reward accrual across `prepare()` freezes, never verifies `are_orders_scoring`, and omits realized PnL, MTM drawdown, equity, and `HALTED` status.
5. **Adverse Selection Trap & Blind REST Polling (`clob_venue.py:68-190, 512`, `fill_risk.py:3-140`, `condition_selector.py:516`, `__main__.py:149`)**:
   - Ranking `pool_usd / (competitors + ours)` on a flat `category="global"` feed selects markets specifically when informed makers pull depth ahead of live events (`Miami Dolphins` Sunday NFL window with Jan 2027 `end_date` and null `game_start_time`, `Bank of Brazil Selic`, `UMich Sentiment`, `Tyreek Hill sign with`, and `Israeli Minister` `negRisk` stems rotated 7 times with zero stem-level loss cooldown). With `VenueWebSocket` disabled on `global` and ticks stretched to `2.88s` median by `2.92 GB` SQLite bloat (`6.72M` `lip_snapshots` rows), `<10ms` sweeps fill GTC bids before `book_guard` runs.

*(Note: Bot 2 `pmus-satellite.service` runs strictly with `--shadow` on `certainty_ledger.db`, imports zero CLOB signing clients, and had **0.0% wallet overlap** with your live funds, contributing only `12.5%` background CPU.)*

---

## 4. Recommended Decision Paths

1. **Option A (Recommended Immediate Default): Keep Bot 1 Halted Overnight (`0` Risk) & Fix Core Structural Flaws Before Any Restart.**
   - Do **not** restart `pmus.service` right now (restarting before `00:00 UTC` would also trigger the `PMUS_DAILY_LOSS_BASELINE=72.62` bug unless removed from `.env`, and at `00:00 UTC` tonight `daily_loss_stop_tripped` will automatically reset and resume quoting unless `systemctl stop pmus` is run!). Stopping `pmus.service` cleanly preserves your `$176.52` cash + `$25.51` live shares while we decide how to proceed.
2. **Option B: Structural Overhaul Before Resuming Tight LIP Quoting (All 5 `Act On` Fixes).**
   - Only resume Bot 1 after fixing (1) `max_markets` pair/leg unit alignment + per-leg `RiskManager` cap in selector + async/capped book scan, (2) post-fill hedge completion (`pair_preference > 0` / merge completion) instead of hair-trigger `LIQUID_STOP_UNWIND`, (3) true mark-to-market UTC-midnight equity loss stop at line 1 of `tick()`/`prepare()` + deleting `PMUS_DAILY_LOSS_BASELINE`, (4) honest net-PnL + equity + `HALTED` hourly webhook, (5) positive non-jump category allowlist + 24h stem/negRisk loss cooldown + CLOB WebSocket stream + `ledger.db` pruning.
3. **Option C: Retire Tight `$200` Passive LIP Farming.**
   - On a `$200` bankroll without sub-50ms WebSocket cancel-on-sweep infrastructure, passive top-of-book LIP farming earns `$10–$16/day` gross (`~$0.42–$0.66/hr`) while a single `65`-share informed sweep costs `-$3.50` to `-$19.35` (`8` to `46 hours` of gross rewards). Retiring Bot 1 or pivoting exclusively to wider, back-of-band passive liquidity / hedged merge strategies avoids donating LIP subsidies back to informed takers.
