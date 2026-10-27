# Polymarket Bot Yield Optimization, Volume Filtering & Merge Arbitrage Proposal
**Date:** October 27, 2026  
**Target System:** `pmus` (Polymarket US & Global Quoter)  
**Target Host:** AWS VPS `52.16.177.132`  
**Wallet Proxy:** `0xe7d565d58c61e2b96adb0e4518e7c33978402e7f`  

---

## 1. Executive Summary & Root Cause Forensics

### The Problem
The bot currently targets small, illiquid prediction markets (e.g. Spanish provincial elections, Mexican legislative seats) that generate theoretical liquidity rewards on paper, but suffer from **adverse selection fills** and **capital traps**.

A live audit of proxy wallet `0xe7d565d58c61e2b96adb0e4518e7c33978402e7f` reveals:
- **Total Portfolio:** $210.58 cash + $34.79 live shares ($245.37 total value).
- **Trapped Inventory:** ~$55.12 locked in 65 shares of `Amcha Yisrael` (unrealized -$7.80) and 65 shares of `Markus Ruck` (unrealized -$13.52).
- **Economic Asymmetry:** While gross liquidity rewards accrue at ~$10–$20/day, a single informed taker fill on an illiquid market wipes out 24 to 48 hours of gross earnings.

### Three Root Causes Identified Across the Swarm

```
                  ┌────────────────────────────────────────────────────────┐
                  │ 1. Volume Blindness (CLOB API lacks volume24h data)    │
                  │    - Obscure $60/day markets look identical to $500k   │
                  │    - Theoretical optimizer prioritizes zero-comp pools │
                  └───────────────────────────┬────────────────────────────┘
                                              │
                  ┌───────────────────────────▼────────────────────────────┐
                  │ 2. Budget Fragmentation (Small per-condition caps)     │
                  │    - $250 split across 4-5 pairs = $50/condition       │
                  │    - Strictly excludes liquid markets (target_size>=200)│
                  └───────────────────────────┬────────────────────────────┘
                                              │
                  ┌───────────────────────────▼────────────────────────────┐
                  │ 3. 0-Cushion Front Shield Bug + Passive Unwind Lock    │
                  │    - Quoter matched top_bid with 0 cushion             │
                  │    - On fills, COLLAPSED_STUB_HOLD refuses to sell     │
                  │      below entry in dead books, freezing capital       │
                  └────────────────────────────────────────────────────────┘
```

1. **Volume Blindness**: The Polymarket CLOB `/sampling-markets` endpoint provides **zero volume data**. Because competitor score $S_{\text{comp}}$ is near zero ($40–80$) in obscure markets, our orders capture $50\%–70\%$ of the reward pool on paper. In liquid markets, competitor score is deep ($S_{\text{comp}} \approx 3,000–10,000$), so nominal reward share looks like $1\%–2\%$.
2. **Budget Fragmentation Barrier**: Major liquid markets have `target_size` of 200 to 500 contracts ($190 to $450 per pair). When the bot fragments $250 across 4–5 pairs (~$50 each), `max_affordable_contracts` drops to ~55, strictly disqualifying all major liquid markets and leaving only micro-cap markets.
3. **Front-Shield & Unwind Lock**:
   - `_has_front_shield` evaluated `lvl.price >= price` (including our own price), tricking the quoter into matching `top_bid` with zero cushion.
   - Once filled, `classify_unwind_regime` requires 150 shares on the bid and spread $\le 0.03$ to allow liquid stops; in illiquid markets, it defaults to `COLLAPSED_STUB_HOLD`, posting sell orders at $\ge \text{avg\_price} + 1\text{ tick}$ that never execute as the market falls.

---

## 2. The Solution: Four Strategic Pillars

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       FOUR-PILLAR OPTIMIZATION BLUEPRINT                    │
├──────────────────────┬──────────────────────┬───────────────────────────────┤
│ Pillar 1:            │ Pillar 2:            │ Pillar 3:                     │
│ Volume Enrichment    │ Spread Cushioning    │ Active Merge Arbitrage        │
│ • Gamma API 24h vol  │ • Step back 2-4 ticks│ • Instant complement bidding  │
│ • Min $5,000/day vol │ • Cushion ratio 0.85 │ • Relayer v2 on-chain merge   │
│ • Capital concentrate│ • 85% rewards kept   │ • Lock risk-free +$0.02 profit│
└──────────────────────┴──────────────────────┴───────────────────────────────┘
```

### Pillar 1: Gamma API 24-Hour Volume Ingestion & Capital Concentration
- **Batch Enrichment**: Query Polymarket Gamma API (`https://gamma-api.polymarket.com/markets?condition_ids=...`) during candidate scans to ingest `volume24hr` and `liquidityNum`.
- **Hard Volume Floor**: Reject any market with $< \$5,000$ 24-hour volume.
- **Capital Concentration**: Rather than fragmenting $250 across 5 micro-markets, deploy into **1 or 2 high-volume markets** ($125–$250 each). This clears the 200-contract `target_size` barrier, unlocking real liquid markets where two-way organic retail flow allows clean round-trip spread capture.

### Pillar 2: Deep Spread Quoting & Front-Shield Fix
- **Fix Front-Shield Relational Operator**: Evaluate `lvl.price > price` (strictly higher) so only real competitor liquidity in front provides cushion.
- **Raise Cushion Ratio**: Increase `MAX_CUSHION_SPREAD_RATIO` from `0.625` to `0.85`, allowing the quoter to step back 2 to 4 ticks from the mid-price.
- **Yield vs. Risk Trade-Off**: At 2–3 ticks back, the quadratic reward formula preserves **$80\%–85\%$ of gross LIP rewards** while slashing fill probability by $>95\%$.

### Pillar 3: Active Complement Hedging & Relayer v2 Merge Arbitrage
- **The Merge Mechanism**: On Polymarket, $1.00\text{ Yes} + 1.00\text{ No} = \$1.00\text{ USDC}$ guaranteed collateral redemption.
- **Immediate Post-Fill Bid**: When one leg fills (e.g., Yes @ $0.51$), the quoter should not sit and pray for someone to buy Yes back at $0.52$. Instead, it aggressively bids for No at anything $\le \$0.47$ (so $\text{Yes} + \text{No} \le \$0.98$).
- **Gasless Instant Cashout**: Once both legs match, call `mergePositions` via Polymarket Builder Relayer v2 on Polygon. The position instantly redeems for $\$1.00$ USDC cash, locking in a guaranteed **$+\$0.02/\text{share}$ arbitrage profit** with zero market exposure.

### Pillar 4: Legacy Bag Resolution
- **Immediate Action**: Liquidate or manually exit the stranded legacy positions (`Amcha Yisrael` and `Markus Ruck`).
- Freeing these shares returns **~$\mathbf{\$35.00}$ USDC** of working cash directly to the trading wallet, increasing deployable capital from $\$210$ to $\$245$.

---

## 3. Financial Projections: High-Volume vs. Micro-Cap

| Metric | Micro-Cap Strategy (Current) | High-Volume + Merge Arbitrage (Proposed) |
| :--- | :---: | :---: |
| **Eligible Market Universe** | Spanish provincial, Mexican local elections | Major politics, Fed rates, tech, institutional |
| **24-Hour Market Volume** | $\$50 – \$1,000$ | $\$20,000 – \$1,000,000+$ |
| **Number of Active Pairs** | 4–5 pairs (thinly spread) | 1–2 pairs (concentrated depth) |
| **Target Size Cleared** | 50–70 contracts | 200–300 contracts |
| **LIP Reward Yield** | $\$15 – \$21 / \text{day}$ | $\$12 – \$18 / \text{day}$ |
| **Organic Maker Spread PnL** | $\$0.00$ (never unloads) | $+\$3 – \$8 / \text{day}$ (two-way roundtrips) |
| **Merge Arbitrage PnL** | $\$0.00$ | $+\$2 – \$5 / \text{day}$ (risk-free redemptions) |
| **Fill Risk Profile** | **High** (toxic informed taker dumps) | **Near-Zero** (cushioned behind deep walls) |
| **Net Realized Daily Profit** | **$-\$5\text{ to }+\$5 / \text{day}$** (erratic) | **$+\$15\text{ to }+\$25 / \text{day}$** (steady compound) |

---

## 4. Implementation & Rollout Roadmap

1. **Step 1 (Immediate - 0 Code Risk)**:
   - Liquidate legacy `Amcha Yisrael` ($33.15 value) and `Markus Ruck` ($0.65 value) via Polymarket UI to restore full cash buying power to ~$245.
2. **Step 2 (Quoter Hardening)**:
   - Patch `_has_front_shield` to use strictly `> price`.
   - Update `MAX_CUSHION_SPREAD_RATIO = 0.85` in `global_rewards.py` so orders sit 2–3 ticks deep.
3. **Step 3 (Gamma API Volume Filter & Concentration)**:
   - Add `volume_24h` and `liquidity_usd` fields to condition models.
   - Ingest Gamma API stats during candidate discovery. Filter out markets with $< \$5,000$ 24h volume.
   - Adjust `calculate_num_pairs` to allow 1–2 concentrated pairs on $250 budget to meet liquid 200-contract `target_size`.
4. **Step 4 (Relayer v2 Auto-Merge)**:
   - Hook on-chain `mergePositions` to trigger whenever `min(balance_yes, balance_no) >= 5.0`.
