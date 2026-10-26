# Bot 1 (`pmus`) Structural Remediation & Hardening Verification Report
**Date:** October 26, 2026  
**Branch:** `pmus-rebuild`  
**Commits:** `e3dc7c5`, `fa2f0ec`  
**Verification Suite:** `533 passed in 5.49s` (`0 failed`, `0 deselected`)

---

## 1. $59.00 USDC Cash Increase Provenance (`13:59 CT` / `18:59 UTC`)

The `$59` cash jump on October 26 was not an external bank deposit. It came from three on-chain USDC inflows into proxy wallet `0xacde6b44ed91cb334fbef01d03ef3fc4db0ca8f4`:
1. **`13:59:11 CT` (`$21.50` USDC)**: Manual UI `SELL` exit of `59.99` shares of `Will Tyreek Hill sign with the Kansas City Chiefs in 2026?` across 3 price levels (`5.00 @ $0.370`, `5.00 @ $0.360`, `49.99 @ $0.357`).
2. **`13:59:09 CT` (`$29.10` USDC)**: Manual UI `SELL` exit of `55.00` shares of `Will no next Israeli Minister of Justice be appointed by February 28, 2027?` across 2 price levels (`5.00 @ $0.540`, `50.00 @ $0.528`).
   - Combined `13:59 CT` liquidation proceeds: **`$50.60` USDC**.
3. **Earlier Intraday Inflows (`$12.05` USDC)**: `$2.05` from a 5-share `Tyreek Hill` sell at `13:33 CT` (`@ $0.410`) plus `$10.00` USDC from the daily LIP liquidity reward payout at `00:00:58 UTC`.

---

## 2. Forensic Gap Review & Residual Risks Addressed

During the deep forensic review, four critical operational hazards were uncovered and structurally resolved:

1. **Historical $0.00 Bag Leakage Eliminated**:
   - Live query of proxy wallet `0xe7d565d58c61e2b96adb0e4518e7c33978402e7f` revealed 16 unredeemed $0.00 positions dating back to October 6 (`Albania vs San Marino`, `Spain`, `Scotland vs Slovenia`, etc.) totaling `$38.00+` in sunken losses. Indiscriminately summing these would cause an immediate halt on tick 1 every day.
   - `_compute_redeemable_loss()` was updated to gate losses strictly to positions active or traded *today* (`ledger.has_fill_since()` since midnight UTC, `local_fill_positions`, or `target_slugs`), shielding the bot from ancient dead bag halts.
2. **Persistent 24-Hour Toxic Market Cooldowns**:
   - `loss_cooldown_until` was previously stored only in RAM. Process restarts or reboot bounces wiped the table, immediately re-admitting toxic markets.
   - Added persistent SQLite storage in `loss_cooldowns` table. `LiveEngine.__init__` now seeds active cooldowns directly from the database on startup.
3. **Automated SQLite Corruption Recovery**:
   - `Ledger.__init__` was hardened to catch `sqlite3.DatabaseError` (e.g. malformed disk image), auto-backup the corrupted file to `ledger.db.corrupt_<timestamp>`, clear stale lock files, and cleanly re-initialize the schema without unhandled process termination.
4. **Timing-Attack Hardened Dashboard Authentication**:
   - Updated `dashboard.py` password verification to use constant-time `hmac.compare_digest`.

---

## 3. Comprehensive Defect Remediation Summary

- **Post-Fill Synthetic Pair Merge Completion**: `allocate_conditions` and `_waterfill_conditions` enforce `max_leg_collateral`, cap post-fill opposite-leg hedges at `1.00 - filled_avg_price` down to `HEDGE_LEG_MIN_PRICE = $0.05` (with matching `$0.05` floor in `lip_quoter.desired_quotes`), and verify `p_yes + p_no <= $0.99`.
- **Hair-Trigger Stop-Unwind Abolished**: `LIQUID_STOP_UNWIND` requires `allow_stop_unwind=True` and a `>= 15%` drawdown from entry (`mid <= avg_price * 0.85`). Realized loss exits trigger an 86,400s (`24h`) cooldown on both sibling slugs and zero out active bid budgets immediately.
- **Redeemable $0.00 Loss Recognition & Risk-Stop Ordering**: `LiveEngine.tick()` evaluates `daily_loss_stop` before cash-shift reallocation, incorporates sibling-netted `_compute_redeemable_loss(live_positions)` (excluding `archived` positions and null `cur_price` values) inside `daily_loss_baseline` subtraction, and enforces `PMUS_DAILY_LOSS_BASELINE_DATE`.
- **Taker Multi-Maker VWAP & LIP Scoring Symmetry**: `parse_trade_fills` computes true matched notional across maker legs (including complement pricing `1.00 - maker_price` on cross-asset fills); `side_score` filters sub-`min_size` dust levels; `estimate_global_pair_score` strips own resting orders before computing `adjusted_mid`.
- **SQLite WAL Pruning, Webhook Concurrency & Fail-Closed Dashboard**: `Ledger` caches schema columns at init and prunes snapshots older than 7 days; `Notifier` bounds concurrency with `BoundedSemaphore(4)`, redacts webhook URLs/tokens in error logs, and reports `engine_state` and `mtm_equity`; `dashboard.check_password()` fails closed unless `PMUS_DASHBOARD_PASSWORD` or `PMUS_DASHBOARD_ALLOW_UNAUTH_LOCAL=1` is set.

---

## 4. Verification Receipt

```text
& "d:\neststock\scripts\polymarket_bot\venv\Scripts\python.exe" -m pytest pmus/tests -q
533 passed in 5.49s
```
