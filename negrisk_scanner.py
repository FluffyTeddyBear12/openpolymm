"""
Multi-Outcome Neg-Risk Basket Scanner & Parity Arbitrage Adapter for Polymarket.

In multi-outcome Neg-Risk markets (e.g., elections, tournaments, award categories),
outcomes are mutually exclusive and collectively exhaustive. When the sum of all YES asks
across all N outcomes falls below 1.00 - min_edge:
    sum(Ask_i) * (1 + fee_rate) < 1.0000
Buying 1 share of YES for each outcome guarantees a $1.00 settlement payout regardless
of which outcome resolves YES. This yields pure mathematical arbitrage with zero directional risk.
"""

import logging
import math
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("NegRiskScanner")


class NegRiskBasketScanner:
    """
    Discovers, indexes, and tracks multi-outcome baskets grouped by neg_risk_market_id.
    Continuously monitors multi-leg parity and executable liquidity depth.
    """

    def __init__(self):
        # Mapping: neg_risk_market_id -> list of outcome metadata dicts
        self.baskets: Dict[str, List[dict]] = {}
        # Reverse mapping: condition_id -> neg_risk_market_id
        self.market_to_basket: Dict[str, str] = {}
        # Mapping: neg_risk_market_id -> expected total outcome count
        self.basket_expected_counts: Dict[str, int] = {}

    def index_market_universe(self, markets_data: Union[List[dict], Dict[str, dict]]) -> int:
        """
        Extracts all markets where neg_risk == True and groups them by neg_risk_market_id.
        Stores mapping:
            self.baskets[neg_risk_market_id] = [
                {
                    "condition_id": ...,
                    "token_yes": ...,
                    "token_no": ...,
                    "question": ...,
                    "tick_size": ...
                }, ...
            ]
        Returns the number of indexed baskets.
        """
        self.baskets = {}
        self.market_to_basket = {}
        self.basket_expected_counts = {}

        if not markets_data:
            return 0

        # Normalize to iterable of market dictionaries
        market_items = []
        if isinstance(markets_data, dict):
            if "data" in markets_data and isinstance(markets_data["data"], list):
                market_items = [m for m in markets_data["data"] if isinstance(m, dict)]
            else:
                for k, v in markets_data.items():
                    if isinstance(v, dict):
                        m_copy = dict(v)
                        if "condition_id" not in m_copy and "conditionId" not in m_copy:
                            m_copy["condition_id"] = k
                        market_items.append(m_copy)
        elif isinstance(markets_data, list):
            market_items = [m for m in markets_data if isinstance(m, dict)]

        for m in market_items:
            # Check neg_risk flag
            meta = m.get("market_meta") if isinstance(m.get("market_meta"), dict) else {}
            is_neg_risk = bool(
                m.get("neg_risk")
                or m.get("negRisk")
                or meta.get("neg_risk")
                or meta.get("negRisk")
            )

            # Extract neg_risk_market_id
            neg_risk_id = (
                m.get("neg_risk_market_id")
                or m.get("negRiskMarketID")
                or m.get("negRiskMarketId")
                or meta.get("neg_risk_market_id")
                or meta.get("negRiskMarketID")
                or meta.get("negRiskMarketId")
            )

            if not neg_risk_id:
                continue

            if not is_neg_risk and not neg_risk_id:
                continue

            cid = m.get("condition_id") or m.get("conditionId")
            if not cid:
                continue

            # Resolve token IDs
            token_yes = m.get("token_yes")
            token_no = m.get("token_no")
            if not token_yes:
                tids = m.get("token_ids") or m.get("clobTokenIds") or []
                if isinstance(tids, str):
                    try:
                        import json
                        tids = json.loads(tids)
                    except Exception:
                        tids = []
                if isinstance(tids, list) and len(tids) >= 2:
                    token_yes = str(tids[0])
                    token_no = str(tids[1])
                elif isinstance(m.get("tokens"), list) and len(m["tokens"]) >= 2:
                    token_yes = str(m["tokens"][0].get("token_id"))
                    token_no = str(m["tokens"][1].get("token_id"))

            question = (
                m.get("question")
                or m.get("title")
                or meta.get("question")
                or meta.get("title")
                or f"Outcome {cid[:8]}"
            )

            tick_size = float(
                m.get("tick_size")
                or m.get("minimum_tick_size")
                or meta.get("minimum_tick_size")
                or meta.get("tick_size")
                or 0.001
            )

            outcome_entry = {
                "condition_id": cid,
                "token_yes": token_yes,
                "token_no": token_no,
                "question": question,
                "tick_size": tick_size,
            }

            if neg_risk_id not in self.baskets:
                self.baskets[neg_risk_id] = []

            # Deduplicate by condition_id within basket
            existing_cids = {o["condition_id"] for o in self.baskets[neg_risk_id]}
            if cid not in existing_cids:
                self.baskets[neg_risk_id].append(outcome_entry)

            exp_cnt = (
                m.get("outcome_count")
                or m.get("num_outcomes")
                or m.get("total_outcomes")
                or meta.get("outcome_count")
                or meta.get("num_outcomes")
            )
            if exp_cnt:
                try:
                    self.basket_expected_counts[neg_risk_id] = int(exp_cnt)
                except (ValueError, TypeError):
                    pass

            self.market_to_basket[cid] = neg_risk_id

        logger.info(
            f"NegRiskBasketScanner indexed {len(self.baskets)} baskets across {len(self.market_to_basket)} markets."
        )
        return len(self.baskets)

    def check_basket_parity(
        self,
        neg_risk_market_id: str,
        market_books: Dict[str, dict],
        market_depths: Dict[str, dict],
        fee_rate: float = 0.0,
        min_edge: float = 0.015,
        min_depth_shares: float = 5.0,
    ) -> Optional[dict]:
        """
        Evaluates basket parity arbitrage across all N outcomes in neg_risk_market_id.
        Verifies all N outcomes have an active YES ask > 0 and depth >= min_depth_shares.
        Calculates:
            sum_ask = sum(outcome_ask_i)
            total_cost = sum_ask * (1.0 + fee_rate)
            edge = 1.0000 - total_cost
        If edge >= min_edge:
            returns execution opportunity dict.
        Otherwise returns None.
        """
        if neg_risk_market_id not in self.baskets:
            return None

        outcomes = self.baskets[neg_risk_market_id]
        expected_count = self.basket_expected_counts.get(neg_risk_market_id, 0)
        if expected_count > 0 and len(outcomes) < expected_count:
            return None

        # Enforce optimal cardinality filter (2 to 8 outcomes)
        if not outcomes or not (2 <= len(outcomes) <= 8):
            return None

        sum_ask = 0.0
        min_depth = float("inf")
        outcome_details = []

        for o in outcomes:
            cid = o.get("condition_id")
            token_yes = o.get("token_yes")

            # Extract YES ask
            ask_yes = None
            if cid and cid in market_books and isinstance(market_books[cid], dict):
                b_mkt = market_books[cid]
                if token_yes and token_yes in b_mkt:
                    ask_yes = b_mkt[token_yes]
                elif "token_yes" in b_mkt:
                    ask_yes = b_mkt["token_yes"]
                elif "YES" in b_mkt:
                    ask_yes = b_mkt["YES"]
                elif "ask_yes" in b_mkt:
                    ask_yes = b_mkt["ask_yes"]

            if ask_yes is None and token_yes and token_yes in market_books:
                val = market_books[token_yes]
                if isinstance(val, (int, float)):
                    ask_yes = float(val)
                elif isinstance(val, dict):
                    ask_yes = val.get("ask") or val.get("price") or val.get("ask_yes")

            if ask_yes is None:
                return None
            try:
                ask_yes = float(ask_yes)
            except (ValueError, TypeError):
                return None

            if ask_yes <= 0.0:
                return None

            # Extract YES depth
            depth_yes = None
            if cid and cid in market_depths and isinstance(market_depths[cid], dict):
                d_mkt = market_depths[cid]
                if token_yes and token_yes in d_mkt:
                    depth_yes = d_mkt[token_yes]
                elif "token_yes" in d_mkt:
                    depth_yes = d_mkt["token_yes"]
                elif "YES" in d_mkt:
                    depth_yes = d_mkt["YES"]
                elif "depth_yes" in d_mkt:
                    depth_yes = d_mkt["depth_yes"]

            if depth_yes is None and token_yes and token_yes in market_depths:
                val = market_depths[token_yes]
                if isinstance(val, (int, float)):
                    depth_yes = float(val)
                elif isinstance(val, dict):
                    depth_yes = val.get("depth") or val.get("size") or val.get("depth_yes")

            if depth_yes is None:
                return None
            try:
                depth_yes = float(depth_yes)
            except (ValueError, TypeError):
                return None

            if depth_yes <= 0.0 or depth_yes < min_depth_shares:
                return None

            sum_ask += ask_yes
            if depth_yes < min_depth:
                min_depth = depth_yes

            outcome_details.append(
                {
                    "condition_id": cid,
                    "token_yes": token_yes,
                    "token_no": o.get("token_no"),
                    "question": o.get("question"),
                    "ask_yes": round(ask_yes, 4),
                    "depth_shares": round(depth_yes, 2),
                    "tick_size": o.get("tick_size", 0.001),
                }
            )

        total_cost = sum_ask * (1.0 + fee_rate)
        edge = 1.0000 - total_cost

        if edge < min_edge:
            return None

        # Calibrate minimum shares with $4.00 floor and 5.0 CLOB shares floor
        calibrated_min_shares = max(5.0, float(math.ceil(4.00 / max(0.01, sum_ask))))

        # Pre-flight depth bottleneck check
        if min_depth < calibrated_min_shares:
            return None

        max_shares = min_depth
        trade_size = max_shares * sum_ask
        expected_profit = edge * max_shares

        return {
            "execution_type": "negrisk_basket",
            "neg_risk_market_id": neg_risk_market_id,
            "num_outcomes": len(outcomes),
            "sum_ask": round(sum_ask, 4),
            "total_cost": round(total_cost, 4),
            "edge": round(edge, 4),
            "calibrated_min_shares": calibrated_min_shares,
            "max_shares": max_shares,
            "trade_size": round(trade_size, 2),
            "expected_profit": round(expected_profit, 4),
            "outcomes": outcome_details,
        }

    def check_no_basket_parity(
        self,
        neg_risk_market_id: str,
        market_books: Dict[str, dict],
        market_depths: Dict[str, dict],
        fee_rate: float = 0.0,
        min_edge: float = 0.015,
        min_depth_shares: float = 5.0,
    ) -> Optional[dict]:
        """
        Evaluates basket parity arbitrage buying NO across all N outcomes in neg_risk_market_id.
        In an N-outcome mutually exclusive event where exactly 1 outcome resolves YES,
        exactly (N - 1) outcomes resolve NO, each paying $1.00.
        Settlement payout is (N - 1) * $1.00.
        Total cost = sum(Ask_NO_i) * (1.0 + fee_rate).
        Edge = payout - total_cost.
        """
        if neg_risk_market_id not in self.baskets:
            return None

        outcomes = self.baskets[neg_risk_market_id]
        expected_count = self.basket_expected_counts.get(neg_risk_market_id, 0)
        if expected_count > 0 and len(outcomes) < expected_count:
            return None

        if not outcomes or not (2 <= len(outcomes) <= 8):
            return None

        payout = float(len(outcomes) - 1)
        sum_ask = 0.0
        min_depth = float("inf")
        outcome_details = []

        for o in outcomes:
            cid = o.get("condition_id")
            token_no = o.get("token_no")

            ask_no = None
            if cid and cid in market_books and isinstance(market_books[cid], dict):
                b_mkt = market_books[cid]
                if token_no and token_no in b_mkt:
                    ask_no = b_mkt[token_no]
                elif "token_no" in b_mkt:
                    ask_no = b_mkt["token_no"]
                elif "NO" in b_mkt:
                    ask_no = b_mkt["NO"]
                elif "ask_no" in b_mkt:
                    ask_no = b_mkt["ask_no"]

            if ask_no is None and token_no and token_no in market_books:
                val = market_books[token_no]
                if isinstance(val, (int, float)):
                    ask_no = float(val)
                elif isinstance(val, dict):
                    ask_no = val.get("ask") or val.get("price") or val.get("ask_no")

            if ask_no is None:
                return None
            try:
                ask_no = float(ask_no)
            except (ValueError, TypeError):
                return None

            if ask_no <= 0.0:
                return None

            depth_no = None
            if cid and cid in market_depths and isinstance(market_depths[cid], dict):
                d_mkt = market_depths[cid]
                if token_no and token_no in d_mkt:
                    depth_no = d_mkt[token_no]
                elif "token_no" in d_mkt:
                    depth_no = d_mkt["token_no"]
                elif "NO" in d_mkt:
                    depth_no = d_mkt["NO"]
                elif "depth_no" in d_mkt:
                    depth_no = d_mkt["depth_no"]

            if depth_no is None and token_no and token_no in market_depths:
                val = market_depths[token_no]
                if isinstance(val, (int, float)):
                    depth_no = float(val)
                elif isinstance(val, dict):
                    depth_no = val.get("depth") or val.get("size") or val.get("depth_no")

            if depth_no is None:
                return None
            try:
                depth_no = float(depth_no)
            except (ValueError, TypeError):
                return None

            if depth_no <= 0.0 or depth_no < min_depth_shares:
                return None

            sum_ask += ask_no
            if depth_no < min_depth:
                min_depth = depth_no

            outcome_details.append(
                {
                    "condition_id": cid,
                    "token_yes": o.get("token_yes"),
                    "token_no": token_no,
                    "question": o.get("question"),
                    "ask_no": round(ask_no, 4),
                    "depth_shares": round(depth_no, 2),
                    "tick_size": o.get("tick_size", 0.001),
                }
            )

        total_cost = sum_ask * (1.0 + fee_rate)
        edge = payout - total_cost

        if edge < min_edge:
            return None

        calibrated_min_shares = max(5.0, float(math.ceil(4.00 / max(0.01, sum_ask))))
        if min_depth < calibrated_min_shares:
            return None

        max_shares = min_depth
        trade_size = max_shares * sum_ask
        expected_profit = edge * max_shares

        return {
            "execution_type": "negrisk_no_basket",
            "neg_risk_market_id": neg_risk_market_id,
            "num_outcomes": len(outcomes),
            "payout": payout,
            "sum_ask": round(sum_ask, 4),
            "total_cost": round(total_cost, 4),
            "edge": round(edge, 4),
            "calibrated_min_shares": calibrated_min_shares,
            "max_shares": max_shares,
            "trade_size": round(trade_size, 2),
            "expected_profit": round(expected_profit, 4),
            "outcomes": outcome_details,
        }

    def check_over_parity_sell(
        self,
        neg_risk_market_id: str,
        market_books: Dict[str, dict],
        market_depths: Dict[str, dict],
        fee_rate: float = 0.0,
        min_edge: float = 0.015,
        min_depth_shares: float = 5.0,
    ) -> Optional[dict]:
        """
        Evaluates over-parity arbitrage (Mint & Sell).
        When the sum of best BIDS across all outcomes exceeds 1.00:
            sum(Bid_i) * (1 - fee_rate) > 1.0000 + min_edge
        Splitting $1.00 USDC collateral into 1 share of each outcome and selling
        immediately into resting bids locks in risk-free profit.
        """
        target_id = self.market_to_basket.get(neg_risk_market_id, neg_risk_market_id)
        if target_id not in self.baskets:
            return None

        outcomes = self.baskets[target_id]
        expected_count = self.basket_expected_counts.get(target_id, 0)
        if expected_count > 0 and len(outcomes) < expected_count:
            return None

        if not outcomes or not (2 <= len(outcomes) <= 8):
            return None

        sum_bid = 0.0
        min_depth = float("inf")
        outcome_details = []

        for o in outcomes:
            cid = o.get("condition_id")
            token_yes = o.get("token_yes")

            bid_yes = None
            if cid and cid in market_books and isinstance(market_books[cid], dict):
                b_mkt = market_books[cid]
                if token_yes and token_yes in b_mkt:
                    bid_yes = b_mkt[token_yes]
                elif "token_yes" in b_mkt:
                    bid_yes = b_mkt["token_yes"]
                elif "bid_yes" in b_mkt:
                    bid_yes = b_mkt["bid_yes"]
                elif "YES" in b_mkt:
                    bid_yes = b_mkt["YES"]

            if bid_yes is None and token_yes and token_yes in market_books:
                val = market_books[token_yes]
                if isinstance(val, (int, float)):
                    bid_yes = float(val)
                elif isinstance(val, dict):
                    bid_yes = val.get("bid") or val.get("price") or val.get("bid_yes")

            if bid_yes is None:
                return None
            try:
                bid_yes = float(bid_yes)
            except (ValueError, TypeError):
                return None

            if bid_yes <= 0.0:
                return None

            depth_yes = None
            if cid and cid in market_depths and isinstance(market_depths[cid], dict):
                d_mkt = market_depths[cid]
                if token_yes and token_yes in d_mkt:
                    depth_yes = d_mkt[token_yes]
                elif "token_yes" in d_mkt:
                    depth_yes = d_mkt["token_yes"]
                elif "depth_yes" in d_mkt:
                    depth_yes = d_mkt["depth_yes"]
                elif "YES" in d_mkt:
                    depth_yes = d_mkt["YES"]

            if depth_yes is None and token_yes and token_yes in market_depths:
                val = market_depths[token_yes]
                if isinstance(val, (int, float)):
                    depth_yes = float(val)
                elif isinstance(val, dict):
                    depth_yes = val.get("depth") or val.get("size") or val.get("depth_yes") or val.get("bid_depth")

            if depth_yes is None:
                return None
            try:
                depth_yes = float(depth_yes)
            except (ValueError, TypeError):
                return None

            if depth_yes <= 0.0 or depth_yes < min_depth_shares:
                return None

            sum_bid += bid_yes
            if depth_yes < min_depth:
                min_depth = depth_yes

            outcome_details.append(
                {
                    "condition_id": cid,
                    "token_yes": token_yes,
                    "question": o.get("question"),
                    "bid_yes": round(bid_yes, 4),
                    "depth_shares": round(depth_yes, 2),
                }
            )

        net_proceeds = sum_bid * (1.0 - fee_rate)
        mint_cost = 1.0000
        edge = net_proceeds - mint_cost

        if edge < min_edge:
            return None

        calibrated_min_shares = max(5.0, float(math.ceil(4.00 / max(0.01, sum_bid))))
        if min_depth < calibrated_min_shares:
            return None

        max_shares = min_depth
        trade_size = max_shares * mint_cost
        expected_profit = edge * max_shares

        return {
            "execution_type": "negrisk_over_parity_sell",
            "neg_risk_market_id": target_id,
            "num_outcomes": len(outcomes),
            "sum_bid": round(sum_bid, 4),
            "net_proceeds": round(net_proceeds, 4),
            "mint_cost": mint_cost,
            "edge": round(edge, 4),
            "calibrated_min_shares": calibrated_min_shares,
            "max_shares": max_shares,
            "trade_size": round(trade_size, 2),
            "expected_profit": round(expected_profit, 4),
            "outcomes": outcome_details,
        }

    def check_calendar_monotonicity(
        self,
        earlier_market: Union[str, dict, list],
        later_market: Optional[Union[str, dict]] = None,
        market_books: Optional[Dict[str, dict]] = None,
        market_depths: Optional[Dict[str, dict]] = None,
        fee_rate: float = 0.0,
        min_edge: float = 0.01,
        min_depth_shares: float = 5.0,
    ) -> Optional[dict]:
        """
        Evaluates calendar monotonicity arbitrage between two chronological markets
        where later event is superset of earlier event (P(earlier) <= P(later)).
        If Bid(earlier) * (1 - fee) > Ask(later) * (1 + fee) + min_edge:
            Selling earlier YES and buying later YES guarantees risk-free profit.
        Supports passing two market IDs/dicts, or a list of chronologically ordered markets.
        """
        if isinstance(earlier_market, list):
            m_list = earlier_market
            books = market_books or {}
            depths = market_depths or {}
            for i in range(len(m_list) - 1):
                opp = self.check_calendar_monotonicity(
                    earlier_market=m_list[i],
                    later_market=m_list[i + 1],
                    market_books=books,
                    market_depths=depths,
                    fee_rate=fee_rate,
                    min_edge=min_edge,
                    min_depth_shares=min_depth_shares,
                )
                if opp is not None:
                    return opp
            return None

        if later_market is None or market_books is None or market_depths is None:
            return None

        def _get_info(m: Union[str, dict]):
            if isinstance(m, dict):
                cid = m.get("condition_id") or m.get("id") or ""
                tok_y = m.get("token_yes") or m.get("token_id") or cid
                return str(cid), str(tok_y)
            return str(m), str(m)

        cid_early, tok_early = _get_info(earlier_market)
        cid_late, tok_late = _get_info(later_market)

        def _get_price(books, cid, tok, side):
            b_mkt = books.get(cid, {}) if isinstance(books.get(cid), dict) else {}
            if tok in b_mkt:
                val = b_mkt[tok]
            elif side in b_mkt:
                val = b_mkt[side]
            elif f"{side.lower()}_yes" in b_mkt:
                val = b_mkt[f"{side.lower()}_yes"]
            else:
                val = books.get(tok)
            if isinstance(val, dict):
                return val.get(side.lower()) or val.get("price")
            return val

        def _get_depth(depths, cid, tok, side):
            d_mkt = depths.get(cid, {}) if isinstance(depths.get(cid), dict) else {}
            if tok in d_mkt:
                val = d_mkt[tok]
            elif side in d_mkt:
                val = d_mkt[side]
            elif f"depth_{side.lower()}" in d_mkt:
                val = d_mkt[f"depth_{side.lower()}"]
            else:
                val = depths.get(tok)
            if isinstance(val, dict):
                return val.get("depth") or val.get("size")
            return val

        bid_early = _get_price(market_books, cid_early, tok_early, "bid")
        depth_bid_early = _get_depth(market_depths, cid_early, tok_early, "bid")

        ask_late = _get_price(market_books, cid_late, tok_late, "ask")
        depth_ask_late = _get_depth(market_depths, cid_late, tok_late, "ask")

        if bid_early is None or ask_late is None or depth_bid_early is None or depth_ask_late is None:
            return None

        try:
            bid_early = float(bid_early)
            ask_late = float(ask_late)
            depth_bid_early = float(depth_bid_early)
            depth_ask_late = float(depth_ask_late)
        except (ValueError, TypeError):
            return None

        if bid_early <= 0.0 or ask_late <= 0.0:
            return None
        if depth_bid_early < min_depth_shares or depth_ask_late < min_depth_shares:
            return None

        net_bid = bid_early * (1.0 - fee_rate)
        gross_ask = ask_late * (1.0 + fee_rate)
        edge = net_bid - gross_ask

        if edge < min_edge:
            return None

        max_shares = min(depth_bid_early, depth_ask_late)
        expected_profit = edge * max_shares

        return {
            "execution_type": "calendar_monotonicity",
            "earlier_market": cid_early,
            "later_market": cid_late,
            "bid_early": round(bid_early, 4),
            "ask_late": round(ask_late, 4),
            "net_bid": round(net_bid, 4),
            "gross_ask": round(gross_ask, 4),
            "edge": round(edge, 4),
            "max_shares": max_shares,
            "expected_profit": round(expected_profit, 4),
        }


class NegRiskAdapter:
    """
    Simulates and prepares on-chain CTF Exchange mergePositions calldata
    and execution verification for Polygon PoS.
    """

    CTF_EXCHANGE_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
    COLLATERAL_TOKEN = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

    def __init__(self, ctf_address: Optional[str] = None, neg_risk_adapter_address: Optional[str] = None):
        self.ctf_address = ctf_address or self.CTF_EXCHANGE_ADDRESS
        self.neg_risk_adapter_address = neg_risk_adapter_address or self.NEG_RISK_ADAPTER_ADDRESS

    def format_convert_yes_transaction(
        self,
        market_id: str,
        num_outcomes: int,
        amount_shares: float,
        index_set: Optional[int] = None,
    ) -> dict:
        """
        Prepares contract call dictionary for Polymarket NegRiskAdapter:
            convertYESPositions(bytes32 marketId, uint256 indexSet, uint256 amount)
        Target contract: 0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
        indexSet = (1 << num_outcomes) - 1 (bitmask for all N outcomes)
        amount in 6-decimal raw units (USDC standard)
        """
        mid_hex = market_id.lower()
        if mid_hex.startswith("0x"):
            mid_hex = mid_hex[2:]
        mid_bytes32 = "0x" + mid_hex.rjust(64, "0")

        if index_set is None:
            index_set = (1 << num_outcomes) - 1

        amount_raw = int(round(amount_shares * 1_000_000))

        # Selector for convertYESPositions(bytes32,uint256,uint256) is 0x327ddd2b
        selector = "0x327ddd2b"
        calldata = None
        try:
            from web3 import Web3
            comp_sel = Web3.keccak(text="convertYESPositions(bytes32,uint256,uint256)")[:4].hex()
            if not comp_sel.startswith("0x"):
                comp_sel = "0x" + comp_sel
            selector = comp_sel
            from eth_abi import encode
            mid_b = bytes.fromhex(mid_hex.rjust(64, "0"))
            encoded = encode(["bytes32", "uint256", "uint256"], [mid_b, index_set, amount_raw]).hex()
            calldata = selector + encoded
        except Exception:
            arg1 = mid_hex.rjust(64, "0")
            arg2 = hex(index_set)[2:].rjust(64, "0")
            arg3 = hex(amount_raw)[2:].rjust(64, "0")
            calldata = selector + arg1 + arg2 + arg3

        return {
            "to": self.neg_risk_adapter_address,
            "function": "convertYESPositions",
            "market_id": mid_bytes32,
            "index_set": index_set,
            "amount_shares": amount_shares,
            "amount_raw": amount_raw,
            "calldata": calldata,
            "data": calldata,
            "value": 0,
            "gas_limit": 300000,
        }

    def format_merge_transaction(self, condition_id: str, amount_shares: float) -> dict:
        """
        Prepares contract call dictionary for Polymarket CTF Exchange:
            mergePositions(bytes32 conditionId, bytes32 partition, uint256 amount)
        """
        # Normalize conditionId to 32 bytes hex
        cid_hex = condition_id.lower()
        if cid_hex.startswith("0x"):
            cid_hex = cid_hex[2:]
        cid_bytes32 = "0x" + cid_hex.rjust(64, "0")

        # Partition for NegRisk merge: 0x0 (bytes32 zero partition or full outcome set)
        partition = "0x" + "0" * 64

        # Convert shares to 6-decimal atomic units (USDC standard on Polygon)
        amount_raw = int(round(amount_shares * 1_000_000))

        # Build calldata
        calldata = None
        try:
            from web3 import Web3
            selector = Web3.keccak(text="mergePositions(bytes32,bytes32,uint256)")[:4].hex()
            if not selector.startswith("0x"):
                selector = "0x" + selector
            from eth_abi import encode
            cid_b = bytes.fromhex(cid_hex.rjust(64, "0"))
            part_b = bytes.fromhex("0" * 64)
            encoded = encode(["bytes32", "bytes32", "uint256"], [cid_b, part_b, amount_raw]).hex()
            calldata = selector + encoded
        except Exception:
            # Fallback manual standard ABI encoding
            selector = "0x76b2c6e6"
            arg1 = cid_hex.rjust(64, "0")
            arg2 = "0" * 64
            arg3 = hex(amount_raw)[2:].rjust(64, "0")
            calldata = selector + arg1 + arg2 + arg3

        return {
            "to": self.ctf_address,
            "function": "mergePositions",
            "condition_id": cid_bytes32,
            "partition": partition,
            "amount_shares": amount_shares,
            "amount_raw": amount_raw,
            "calldata": calldata,
            "data": calldata,
            "value": 0,
            "gas_limit": 300000,
        }

    def encode_convert_positions(
        self,
        market_id: str,
        index_set: int,
        amount: Union[int, float],
        function_name: str = "convertPositions",
    ) -> str:
        """
        Encodes call to convertPositions or convertNOPositions:
            function(bytes32 marketId, uint256 indexSet, uint256 amount)
        """
        mid_hex = market_id.lower()
        if mid_hex.startswith("0x"):
            mid_hex = mid_hex[2:]

        amount_raw = int(round(amount * 1_000_000)) if isinstance(amount, float) else int(amount)

        try:
            from web3 import Web3
            from eth_abi import encode
            selector = Web3.keccak(text=f"{function_name}(bytes32,uint256,uint256)")[:4].hex()
            if not selector.startswith("0x"):
                selector = "0x" + selector
            mid_b = bytes.fromhex(mid_hex.rjust(64, "0"))
            encoded = encode(["bytes32", "uint256", "uint256"], [mid_b, int(index_set), amount_raw]).hex()
            return selector + encoded
        except Exception:
            if function_name == "convertYESPositions":
                selector = "0x327ddd2b"
            elif function_name == "convertNOPositions":
                selector = "0xead9203c"
            else:
                selector = "0xc64748c4"
            arg1 = mid_hex.rjust(64, "0")
            arg2 = hex(int(index_set))[2:].rjust(64, "0")
            arg3 = hex(amount_raw)[2:].rjust(64, "0")
            return selector + arg1 + arg2 + arg3

    def format_convert_no_transaction(
        self,
        market_id: str,
        num_outcomes: int,
        amount_shares: float,
        index_set: Optional[int] = None,
    ) -> dict:
        """
        Prepares contract call dictionary for Polymarket NegRiskAdapter converting NO positions:
            convertPositions(bytes32 marketId, uint256 indexSet, uint256 amount)
        """
        mid_hex = market_id.lower()
        if mid_hex.startswith("0x"):
            mid_hex = mid_hex[2:]
        mid_bytes32 = "0x" + mid_hex.rjust(64, "0")

        if index_set is None:
            index_set = (1 << num_outcomes) - 1

        amount_raw = int(round(amount_shares * 1_000_000))
        calldata = self.encode_convert_positions(
            market_id=market_id,
            index_set=index_set,
            amount=amount_raw,
            function_name="convertPositions",
        )

        return {
            "to": self.neg_risk_adapter_address,
            "function": "convertPositions",
            "market_id": mid_bytes32,
            "index_set": index_set,
            "amount_shares": amount_shares,
            "amount_raw": amount_raw,
            "calldata": calldata,
            "data": calldata,
            "value": 0,
            "gas_limit": 300000,
        }

    def encode_ctf_5arg_calldata(
        self,
        function_signature: str,
        collateral_token: str,
        parent_collection_id: str,
        condition_id: str,
        partition: List[int],
        amount_raw: int,
    ) -> str:
        """
        Encodes standard 5-argument CTF call:
            function(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] partition, uint256 amount)
        """
        col_addr = collateral_token.lower()
        if not col_addr.startswith("0x"):
            col_addr = "0x" + col_addr

        parent_hex = parent_collection_id.lower()
        if parent_hex.startswith("0x"):
            parent_hex = parent_hex[2:]
        parent_b = bytes.fromhex(parent_hex.rjust(64, "0"))

        cid_hex = condition_id.lower()
        if cid_hex.startswith("0x"):
            cid_hex = cid_hex[2:]
        cid_b = bytes.fromhex(cid_hex.rjust(64, "0"))

        try:
            from web3 import Web3
            from eth_abi import encode
            selector = Web3.keccak(text=function_signature)[:4].hex()
            if not selector.startswith("0x"):
                selector = "0x" + selector
            encoded = encode(
                ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
                [col_addr, parent_b, cid_b, [int(p) for p in partition], int(amount_raw)],
            ).hex()
            return selector + encoded
        except Exception:
            from web3 import Web3
            selector = Web3.keccak(text=function_signature)[:4].hex()
            if not selector.startswith("0x"):
                selector = "0x" + selector
            return selector

    def encode_ctf_merge_positions(
        self,
        condition_id: str,
        amount_shares: float,
        partition: Optional[List[int]] = None,
        collateral_token: Optional[str] = None,
        parent_collection_id: Optional[str] = None,
    ) -> str:
        """
        Encodes CTF mergePositions(address,bytes32,bytes32,uint256[],uint256).
        """
        col = collateral_token or self.COLLATERAL_TOKEN
        parent = parent_collection_id or ("0x" + "0" * 64)
        part = partition if partition is not None else [1, 2]
        amount_raw = int(round(amount_shares * 1_000_000))
        return self.encode_ctf_5arg_calldata(
            function_signature="mergePositions(address,bytes32,bytes32,uint256[],uint256)",
            collateral_token=col,
            parent_collection_id=parent,
            condition_id=condition_id,
            partition=part,
            amount_raw=amount_raw,
        )

    def encode_ctf_split_position(
        self,
        condition_id: str,
        amount_shares: float,
        partition: Optional[List[int]] = None,
        collateral_token: Optional[str] = None,
        parent_collection_id: Optional[str] = None,
    ) -> str:
        """
        Encodes CTF splitPosition(address,bytes32,bytes32,uint256[],uint256).
        """
        col = collateral_token or self.COLLATERAL_TOKEN
        parent = parent_collection_id or ("0x" + "0" * 64)
        part = partition if partition is not None else [1, 2]
        amount_raw = int(round(amount_shares * 1_000_000))
        return self.encode_ctf_5arg_calldata(
            function_signature="splitPosition(address,bytes32,bytes32,uint256[],uint256)",
            collateral_token=col,
            parent_collection_id=parent,
            condition_id=condition_id,
            partition=part,
            amount_raw=amount_raw,
        )

    def format_ctf_merge_v2_transaction(
        self,
        condition_id: str,
        amount_shares: float,
        partition: Optional[List[int]] = None,
    ) -> dict:
        """
        Prepares full 5-argument CTF mergePositions contract call dictionary.
        """
        cid_hex = condition_id.lower()
        if cid_hex.startswith("0x"):
            cid_hex = cid_hex[2:]
        cid_bytes32 = "0x" + cid_hex.rjust(64, "0")
        part = partition if partition is not None else [1, 2]
        amount_raw = int(round(amount_shares * 1_000_000))
        calldata = self.encode_ctf_merge_positions(condition_id, amount_shares, partition=part)

        return {
            "to": self.ctf_address,
            "function": "mergePositions",
            "condition_id": cid_bytes32,
            "partition": part,
            "amount_shares": amount_shares,
            "amount_raw": amount_raw,
            "calldata": calldata,
            "data": calldata,
            "value": 0,
            "gas_limit": 300000,
        }

    def format_ctf_split_transaction(
        self,
        condition_id: str,
        amount_shares: float,
        partition: Optional[List[int]] = None,
    ) -> dict:
        """
        Prepares full 5-argument CTF splitPosition contract call dictionary.
        """
        cid_hex = condition_id.lower()
        if cid_hex.startswith("0x"):
            cid_hex = cid_hex[2:]
        cid_bytes32 = "0x" + cid_hex.rjust(64, "0")
        part = partition if partition is not None else [1, 2]
        amount_raw = int(round(amount_shares * 1_000_000))
        calldata = self.encode_ctf_split_position(condition_id, amount_shares, partition=part)

        return {
            "to": self.ctf_address,
            "function": "splitPosition",
            "condition_id": cid_bytes32,
            "partition": part,
            "amount_shares": amount_shares,
            "amount_raw": amount_raw,
            "calldata": calldata,
            "data": calldata,
            "value": 0,
            "gas_limit": 300000,
        }
