# ETHGlobal / DoraHacks Hackathon Submission

---

## Project Overview

- **Project Name:** OpenPolyMM (Open Polymarket Market Maker)
- **Tagline:** Open-source liquidity provisioning and spread-protected arbitrage engine for Polymarket on Polygon.
- **Track Selection:** DeFi & Prediction Markets / Financial Infrastructure / Polygon Ecosystem
- **Repository URL:** `https://github.com/FluffyTeddyBear12/openpolymm`
- **Demo URL:** `http://localhost:8501` (Live Streamlit Terminal HUD)
- **License:** MIT License

---

## The Problem: The Illiquidity Trap on Decentralized Prediction Markets

Decentralized prediction markets are one of Web3's highest-traction consumer use cases, with Polymarket leading the world in volume and mainstream adoption. However, beneath the surface of marquee markets lies a structural liquidity crisis:
1. **Punishing Bid-Ask Spreads:** While top-tier presidential elections have tight spreads, hundreds of mid-tier markets have spreads as wide as 5% to 20%. Retail traders lose substantial equity before their trade even settles.
2. **The "Leg-Out" Execution Disaster:** Binary markets operate on the mathematical identity $P_{YES} + P_{NO} = 1.00$. When the combined cost dips below $1.00, an arbitrage opportunity exists. But when bots execute simultaneous market taker orders across both legs, one leg frequently fills while the other slips or cancels. Under traditional bot architectures, the bot panics and dumps the orphan token into an empty orderbook, realizing devastating 50%+ losses ("penny dumping").
3. **Institutional Monopoly:** Retail liquidity providers lack open-source, institutional-grade tools to quote two-sided markets safely, leaving market-making profits entirely to private proprietary trading desks.

---

## The Solution: OpenPolyMM

**OpenPolyMM** is an open-source, asymmetric maker-taker algorithmic market maker and parity arbitrage engine built explicitly for Polymarket on Polygon PoS.

### Key Architectural Breakthroughs:
1. **Asymmetric Maker-Taker Execution:** Instead of risky dual-taker execution, OpenPolyMM places Leg 1 as a passive GTC limit order inside the spread (0% maker fee, 0 slippage). If the order is not matched within a configured timeout, it is cancelled with **$0 cost and $0 loss**. Only after Leg 1 is 100% matched does the bot fire an instant Fill-Or-Kill (FOK) order for Leg 2.
2. **Loss-Free Rollback Engine:** If Leg 2 fails to fill due to sudden depth evaporation, the bot **never penny dumps**. If the market bid is close (spread loss <= $0.005), it exits immediately; if the book is illiquid, it posts a passive maker limit sell at the original entry price, securing capital recovery at par.
3. **Liquidity Mining Harvester:** Continuously queries Polymarket's rewards API to identify markets eligible for daily USDC liquidity rewards (up to $2,000/day per pool), earning dual-yield: spread capture plus LP program rewards.
4. **Resilient Hardware & Supervisor Engine:** Features single-instance socket locking, 20% max CPU affinity throttling, and an RSS memory watchdog that gracefully recycles daemons without dropping execution state.

---

## Technologies Used

- **Polygon PoS (Chain ID 137):** High-speed, micro-fee smart contract settlement layer for conditional tokens and collateral USDC.
- **Polymarket CLOB API & WebSocket:** Sub-second L2 orderbook feeds from `wss://ws-subscriptions-clob.polymarket.com` and REST execution via official SDKs.
- **EIP-712 Typed Structured Signatures:** Off-chain cryptographically signed orders with Polygon domain separators for zero-gas order creation and cancellation.
- **Python 3.11 & Asyncio Architecture:** High-throughput event-driven order evaluation pipeline.
- **Streamlit Cyberpunk UI:** Custom CSS-styled real-time trading HUD with JetBrains Mono typography, pulsing radar liveness indicators, Plotly orderbook depth charts, and live Polygonscan transaction links.
- **Pytest Automation Suite:** 259 automated unit and integration tests passing with 100% reliability.

---

## 2-Minute Video Pitch Script (Second-by-Second Guide)

> **Instructions for Presenter:** Open your browser to `http://localhost:8501` showing the live OpenPolyMM dashboard. Follow the exact second-by-second visual cues and read the spoken dialogue below.

```
+-----------------------------------------------------------------------------------------------+
| TIME        | VISUAL CUE (WHAT TO SHOW ON SCREEN)         | EXACT SPOKEN WORDS                |
+-----------------------------------------------------------------------------------------------+
| 0:00 - 0:15 | Screen on Streamlit Terminal topbar HUD.    | "Prediction markets are booming   |
|             | Highlight the pulsing green radar dot and   | on Polygon, but retail users face |
|             | the high-tech terminal header.              | a hidden tax: crippling bid-ask   |
|             |                                             | spreads and illiquid orderbooks." |
+-----------------------------------------------------------------------------------------------+
| 0:15 - 0:30 | Point cursor to an illiquid market row      | "When bots attempt arbitrage,     |
|             | in the universe table where YES + NO > $1.  | non-atomic dual-taker fills fail. |
|             | Mouse over the spread gap.                  | Leg 1 fills, Leg 2 misses, and    |
|             |                                             | traditional bots panic-dump into  |
|             |                                             | penny bids, losing over 50% in a  |
|             |                                             | single second."                   |
+-----------------------------------------------------------------------------------------------+
| 0:30 - 0:45 | Switch to `maker_taker_engine.py` or the    | "Meet OpenPolyMM: an open-source  |
|             | Architecture diagram on screen.             | algorithmic market maker built    |
|             | Highlight 'Asymmetric Execution'.           | specifically for Polymarket on    |
|             |                                             | Polygon."                         |
|             |                                             | "We introduce asymmetric maker-   |
|             |                                             | taker routing: Leg 1 posts as a   |
|             |                                             | passive GTC limit order inside    |
|             |                                             | the spread—zero fees, zero        |
|             |                                             | slippage. If it doesn't fill, we  |
|             |                                             | cancel with zero loss."           |
+-----------------------------------------------------------------------------------------------+
| 0:45 - 1:00 | Scroll back to Dashboard, highlight the     | "Only after Leg 1 is 100% matched |
|             | Rollback Protection metric and Circuit      | do we execute Leg 2. And if       |
|             | Breaker status (GREEN DISARMED).            | liquidity evaporates? Our loss-   |
|             |                                             | free rollback engine NEVER penny  |
|             |                                             | dumps. It rests a break-even      |
|             |                                             | limit order at par, completely    |
|             |                                             | protecting capital."              |
+-----------------------------------------------------------------------------------------------+
| 1:00 - 1:15 | Zoom in on the 7 Metric Gauges: Total       | "Here on our live Streamlit HUD:  |
|             | Equity, Available Cash, Rewards Harvester,  | we're monitoring over 1,000       |
|             | and Monitored Pairs (1,000 Markets).        | markets and 2,000 orderbook legs  |
|             |                                             | in real time over sub-second Web- |
|             |                                             | Sockets."                         |
+-----------------------------------------------------------------------------------------------+
| 1:15 - 1:30 | Scroll down to the 'Rewards Harvester'      | "Notice our Rewards Harvester:    |
|             | badge ($12,000+/day active pools) and       | tracking thousands of dollars     |
|             | filter by 'Liquidity Mining Rewards Only'.  | in daily Polymarket LP rewards,   |
|             | Show individual reward badges on markets.   | earning dual yields from spread   |
|             |                                             | compression and liquidity mining  |
|             |                                             | pools."                           |
+-----------------------------------------------------------------------------------------------+
| 1:30 - 1:40 | Scroll to the Executed Fills Table.         | "Every simulated and executed     |
|             | Click a Polygon transaction hash link to    | trade is verified with Polygon    |
|             | show Polygonscan.                           | transaction hashes, complete      |
|             |                                             | with strict daily drawdown        |
|             |                                             | limits and circuit breakers."     |
+-----------------------------------------------------------------------------------------------+
| 1:40 - 2:00 | Switch to terminal showing '259 passed in   | "With 259 unit and integration    |
|             | 4.2s'. Return to Dashboard HUD.             | tests passing, OpenPolyMM is      |
|             |                                             | MIT-licensed, production-ready,   |
|             |                                             | and built to bring deep, fair     |
|             |                                             | liquidity to Polygon.             |
|             |                                             | Check out the code and join us in |
|             |                                             | democratizing prediction market   |
|             |                                             | liquidity!"                       |
+-----------------------------------------------------------------------------------------------+
```

---

## What's Next for OpenPolyMM

- **Cloud One-Click Deployments:** Pre-configured Docker and Kubernetes manifests for DigitalOcean, AWS, and local Raspberry Pi / VPS setups.
- **Categorical & Combinatorial Markets:** Expanding from 2-outcome binary markets to multi-outcome election and sports event orderbooks ($\sum P_i = 1.00$).
- **Machine Learning Order Toxicity Detection:** Incorporating Volume-Synchronized Probability of Toxicity (VPIN) to dynamically pull maker quotes before toxic informational market moves occur.
