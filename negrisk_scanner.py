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
