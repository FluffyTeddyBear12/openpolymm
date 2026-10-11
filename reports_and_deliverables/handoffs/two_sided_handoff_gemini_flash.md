# Handoff: PMUS two-sided quoting (Polymarket Global)

Paste everything below the line into a new Gemini 3.8 Flash (High) session with workspace `d:\neststock\scripts\polymarket_bot`.

---

You are taking over a live Polymarket Global liquidity-rewards bot. Read this whole prompt before acting.

## Ground rules

- Repo: `d:\neststock\scripts\polymarket_bot`, branch `pmus-rebuild`, remote `https://github.com/FluffyTeddyBear12/openpolymm.git`. HEAD is `c83b822` and is pushed.
- Only modify `pmus/`, `deploy/` and `reports_and_deliverables/`. Never read or print `.env`, `/etc/pmus.env` values, or any private key.
- Python: `.\venv\Scripts\python.exe` (3.14). Tests: `.\venv\Scripts\python.exe -m pytest pmus/tests -q`. Baseline is 190 passing. Keep it green.
- Shell is PowerShell. Use `;`, not `&&`. Native stderr shows as `NativeCommandError` and exit code 1 even on success; read the output, not the exit code.
- Simple code, no narrating comments, a unit test for every behavior change, no network in tests.
- Never run `--mode live` locally. Never deploy to the VPS or restart `pmus.service` without the user saying yes in this session.
- Label every number you report as measured, estimated or guessed. Never invent wallet addresses, links or payouts. The proxy wallet is `0xe7d565d58c61e2b96adb0e4518e7c33978402e7f`.
- Write user deliverables as PDFs in `reports_and_deliverables\<category>\`.

## What the bot is

- Package `pmus/`. Venue adapter `pmus/clob_venue.py` (`ClobVenueClient`, py-clob-client-v2, signature type 3 deposit wallet). Loop in `pmus/engine.py` (`LiveEngine.prepare/tick/shutdown`, `ShadowEngine`). Quoting in `pmus/strategy/lip_quoter.py`. Reward math in `pmus/strategy/global_rewards.py`. Risk gate `pmus/risk.py`. Ledger `pmus/ledger.py` (SQLite).
- Global reward rules: order score `S = ((v-s)/v)^2 * size`, `v` = max_spread, `s` = distance from size-adjusted mid, size >= min_size. Maker score `max(min(Q_one,Q_two), max(Q_one,Q_two)/3)` with YES mid in [0.10, 0.90], else `min(Q_one,Q_two)`. One pool per condition.

## Live state (measured 2026-10-11 15:23 UTC, unchanged since)

- VPS `52.16.177.132` (AWS Ireland). SSH: `ssh -i "C:\Users\redwi\.ssh\LightsailDefaultKey-eu-west-1.pem" -o StrictHostKeyChecking=no ubuntu@52.16.177.132`. App dir `/home/ubuntu/polymarket_bot`, venv `./venv/bin/python`, service `pmus.service`, env `/etc/pmus.env`.
- The VPS runs commit `d555969`, which is single-sided only. The two-sided commits are NOT deployed.
- One resting order: Netanyahu-out-before-2027 YES, BUY 200 @ 0.39 (about $78), scoring true. Buying power was about $90.76.
- Env: `PMUS_MODE=live`, `PMUS_VENUE=global`, `PMUS_MAX_MARKETS=1`, `PMUS_MAX_PER_MARKET=80`, `PMUS_MAX_COLLATERAL_PER_MARKET=80`, `PMUS_TOTAL_BUDGET=85`, `PMUS_MAX_COLLATERAL=85`, `PMUS_MAX_CONTRACTS_PER_MARKET=400`, `PMUS_GLOBAL_MIN_DAILY_REWARD=0.50`.
- SSH quoting trick: write a bash script to a scratch file, then `(Get-Content -Raw f).Replace("`r","") | ssh ... "bash -s"`. A trailing `$'\r'` error is harmless.

## What was just built (commits 76c3967..c83b822)

- `ConditionMarket` in `pmus/models.py`. `ClobVenueClient.fetch_reward_conditions()` groups YES/NO tokens per condition.
- Pure pair math in `global_rewards.py`: `pair_quote_prices`, `pair_size`, `competitor_q`, `expected_pair_reward_sec`, `expected_single_reward_sec`.
- `pmus/strategy/condition_selector.py`: `evaluate_condition` scores two_sided, yes_only and no_only per condition and keeps the highest estimated $/day above the floor. `allocate_conditions` picks greedily within budget. `targets_to_allocation` maps targets into the existing `AllocationResult` (`side_budgets`, `side_qtys`, new `paired_slugs`).
- `engine.allocate_markets` uses the condition path for Global. `desired_quotes(paired=True, quote_size=...)` bids both tokens, works in tail markets, stops bidding a token once net >= quote_size, and never sells paired inventory.
- `shadow.estimate_global_pair_score` scores both tokens together. `ShadowEngine` uses it for paired slugs.

## Measured results

- 190 tests pass.
- A forced two-sided paper run picked "Will Bernie endorse James Talarico" with YES 80 @ 0.18 and NO 80 @ 0.79 ($77.60). Paper scoring (about $0.43/day) matched the selector estimate.
- A market scan (estimates, noisy minute to minute) showed that at equal capital single-sided usually wins, because a pair costs about $1 per share versus `p` for one side. Two-sided wins only when the cheaper side costs more than about 1/3, or in tail markets. At $85 the selector still picks single-sided (Netanyahu or Opensea, about $0.7 to $1.7/day estimated). The main benefit of two-sided at this size is hedged fills, not more reward.

## Tasks, in order

1. **Review the two-sided diff** (`git diff d555969..c83b822`). The earlier review was cancelled. Spawn reviewers if you can, otherwise review yourself. Known suspects to confirm or fix with tests:
   - Paired partial fill: `lip_quoter.py` paired branch re-posts the full `quote_size` while `0 < net < quote_size`. That can push a token to `quote_size + net` (bounded by `max_contracts_per_market` and the risk gate). Decide whether to post `quote_size - net` (it must stay >= min_size to score) or stop.
   - Reallocation every 30 min (`REALLOCATE_SECONDS`) with held pair inventory. Confirm `total_budget` and buying power stop new bids from stacking on top of locked inventory.
   - `engine._pairs` relies on dict insertion order (YES first). Make the YES/NO order explicit, for example by storing the pair in `AllocationResult`.
   - The log line "Allocated N markets" counts tokens, not conditions. Cosmetic.
   - `reconcile_quotes` churn: confirm paired bids are not cancelled and re-created every tick when the price is unchanged.
2. **Ask the user** whether they want a preference for hedged two-sided quoting, for example a new setting `PMUS_PAIR_PREFERENCE=0.8` that picks two_sided when its estimate is at least 80% of the best single-sided estimate. Implement it only if they say yes, in `condition_selector.evaluate_condition` plus `pmus/config.py`, with tests.
3. **Verify** with a paper run on Global using the live env values and a temp data dir:
   `$env:PMUS_DATA_DIR="$env:TEMP\pmus_ts"; <set the env values above>; .\venv\Scripts\python.exe -m pmus run --mode shadow --venue global --seconds 90`
   Allocation takes about 15s, so use 90s or more. Report the "Condition target" log lines.
4. **Deploy only after an explicit user yes.** On the VPS: `git pull` in the app dir, run `./venv/bin/python -m pytest pmus/tests -q`, `sudo systemctl restart pmus`, then check `journalctl -u pmus -n 50`. Confirm orders and `is_order_scoring` the same way `scratch/vps_go_live2.sh` did (the script lives in `C:\Users\redwi\.gemini\antigravity\brain\74d20cd8-160d-489f-8663-614775e7a8e7\scratch\`).
5. **On 2026-10-12, check real earnings** for 2026-10-11 on the VPS with `client.get_total_earnings_for_user_for_day('2026-10-11')` (a ClobVenueClient's `.client`). Compare against the about $1.7 to $2.5/day estimate. Earlier calibration: the old bot earned $0.005322 on 2026-10-09. Report honestly. The user is considering raising capital to $250 based on this number.
6. **Follow-up, not started:** merge filled YES+NO pairs back to USDC through the CTF contract so pair capital recycles instead of sitting until resolution.

## Reporting back to the user

Short and plain. What changed, what was measured, what is still an estimate, and what you need them to decide. No hype, no invented numbers.
