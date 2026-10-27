# Dynamic Minute-by-Minute Re-Ranking & Capital Concentration Audit
**Date**: October 27, 2026  
**Venue**: Polymarket CLOB (Global / Signature Type 3)  
**Host**: AWS Ireland Lightsail VPS (`52.16.177.132`)  
**Service**: `pmus.service` (Active PID 423299)  
**Wallet**: `0xe7d565d58c61e2b96adb0e4518e7c33978402e7f` (Signer: `0x27D2B1ad0BAc1C53e83a0Bb746657E8fD08C6Acf`)  

---

## 1. Executive Summary

Previously, the bot suffered from two operational constraints:
1. **Reallocation Stagnation**: Market reallocations occurred only once per hour (`PMUS_REALLOCATE_SECONDS=3600`), preventing the bot from dynamically pivoting into high-yield opportunities or adjusting order books as conditions evolved.
2. **Capital Under-Allocation**: Single-market quotes were constrained by a hardcoded concentration ceiling (`cond_cap_limit = $85.00`, `dynamic_leg_col = $68.00`), causing $92.86 (38.4% of funds) to sit completely idle when only one condition was selected.

Through the implementation of the **60-Second Dynamic Universe Scanner**, **Single-Condition Capital Concentration**, and **1-Tick Churn-Resistant Reallocation**, the bot now:
- Evaluates live order books across all eligible incentive markets every 60 seconds in ~1.5 seconds.
- Achieves **98.3% live capital deployment** ($230.40 deployed out of $234.40 budget).
- Concentrates collateral into the highest-yielding incentive pool (*Netanyahu out by end of 2026?*, $200/day pool), increasing expected daily rewards from **$1.02/day to $4.99/day** (a **+389% increase**).

---

## 2. Technical Architecture & Invariants

### A. Candidate Universe Caching (`ClobVenueClient`)
- Full sampling market discovery and safety filtering across 3,500+ markets takes 20–35 seconds. Running this every 60 seconds would peg CPU and risk Gamma API rate limits.
- `ClobVenueClient` now maintains a 15-minute monotonic timestamp cache (`_reward_conditions_cache`, `ttl = 900.0s`).
- Every 60 seconds, `LiveEngine.prepare(is_realloc=True)` instantly reuses the filtered universe and performs an ultra-fast batch book fetch (`fetch_books_batch`) across 27 conditions in **1.04 seconds**.
- Cache is forcibly refreshed on yield collapse, capital under-deployment, or manual invalidation.

### B. Uncapped Single-Condition Concentration (`LiveEngine` & `condition_selector`)
- When bankroll or market qualification yields a single optimal condition (`est_pairs <= 1`):
  $$\text{cond\_cap\_limit} = \text{effective\_budget}$$
  $$\text{dynamic\_leg\_col} = \max(\text{max\_collateral\_per\_market}, 0.80 \times \text{effective\_budget})$$
- In `_waterfill_conditions`, the single selected target sweeps remaining cash up to `total_budget`, sizing the quote to maximum bankroll efficiency while respecting `max_contracts` (600) and `max_leg_collateral` ($250.00).

### C. 1-Tick Churn Prevention & Unhedged Leg Retention
- In `prepare(is_realloc=True)`, resting orders within $\le 1$ tick of target price are preserved without cancellation, protecting exchange queue priority.
- If a market has an unhedged filled leg (`net_qty >= 5`), the partner leg is strictly protected from cancellation and retained in `target_slugs` until hedged for auto-merge.

---

## 3. Adversarial Review Consensus Matrix

| Reviewer | Tier | Focus | Key Findings & Applied Remediations |
| :--- | :--- | :--- | :--- |
| **Reviewer A** | `pro` | Correctness & Bounds | Identified `$68.00` dynamic leg collateral throttle in `engine.py:773`. Fixed by scaling `est_cond_cap` and `dynamic_leg_col` to `effective_budget` when `est_pairs <= 1`. |
| **Reviewer B** | `inherit` | Invariants & Cache | Identified cache invalidation leak where `invalidate_sampling_cache()` left `_reward_conditions_cache` stale. Cleared both caches in unified handler. |
| **Reviewer C** | `flash` | Risk & Unhedged Legs | Discovered unhedged leg drop risk during reallocations. Implemented sticky partner leg retention in `prepare()` to preserve merge paths. |

---

## 4. Live Verification Receipts (AWS Ireland VPS)

```
Oct 27 19:09:39 [INFO] Starting LiveEngine indefinitely
Oct 27 19:09:39 [INFO] Dynamic capital scaling: bp=$241.66 buffer=$7.25 budget=$234.40 leg_col=$250.0
Oct 27 19:11:13 [INFO] Book fetch cycle: 27 conditions after filters, 27 conditions with books, elapsed=1.04s
Oct 27 19:11:14 [INFO] Condition target question='Netanyahu out by end of 2026?', mode=two_sided, size=240, yes_price=0.42, no_price=0.54, cost=$230.40, $/day=$4.99
Oct 27 19:11:14 [INFO] Selected candidate YES: 240 contracts @ 0.42 = $100.80
Oct 27 19:11:14 [INFO] Selected candidate NO: 240 contracts @ 0.54 = $129.60
Oct 27 19:12:15 [INFO] Reallocation tick (60s): Scored 27 conditions in 1.52s, retained resting orders without cancel churn
Oct 27 19:13:21 [INFO] Reallocation tick (60s): Active monitoring clean, zero errors
```

- **Total Open Resting Collateral**: **$230.40**
- **Bankroll Utilization**: **98.29%**
- **Expected Daily Rewards**: **$4.99 / day** ($149.70 / month run-rate on $240 capital)
- **Unit Test Verification**: **554 passed, 0 failures** on both local and VPS test harnesses.
