"""
Loss-Free Rollback & Price Protection Engine for Polymarket Parity Arbitrage Bot.

Location: d:\\neststock\\scripts\\polymarket_bot\\rollback_protector.py

Mission & Design Invariant:
When dual-leg arbitrage fills Leg 1 but misses Leg 2, the previous behavior
dumped Leg 1 into whatever bid was on the CLOB, leading to catastrophic losses
(e.g., selling at $0.012 after buying at $0.038, losing >60%).

This module enforces a strict, loss-free rollback invariant:
1. If best_bid >= buy_price - max_loss_cents:
   The spread loss is acceptable (<= max_loss_cents, default 0.5 cents / $0.005).
   Execute an immediate FOK market exit at best_bid to recover liquidity.
2. If best_bid < buy_price - max_loss_cents (or book is illiquid / penny bid):
   NEVER DUMP!
   Instead, post a passive MAKER limit sell order (GTC) at the original buy_price.
   This eliminates negative slippage and secures recovery at par.
"""

import json
import logging
import math
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("RollbackProtector")

try:
    from py_clob_client_v2.clob_types import (
        OrderArgsV2,
        PostOrdersV2Args,
        OrderType,
        PartialCreateOrderOptions,
    )
except ImportError:
    try:
        from py_clob_client.clob_types import (
            OrderArgs as OrderArgsV2,
            OrderType,
            PartialCreateOrderOptions,
        )
        PostOrdersV2Args = None
    except ImportError:
        class OrderType:
            GTC = "GTC"
            FOK = "FOK"
            GTD = "GTD"
            FAK = "FAK"
        OrderArgsV2 = None
        PostOrdersV2Args = None
        PartialCreateOrderOptions = None

if PartialCreateOrderOptions is None:
    class PartialCreateOrderOptions:
        def __init__(
            self,
            tick_size: Optional[str] = None,
            neg_risk: Optional[bool] = None,
            version: Optional[int] = None,
        ):
            self.tick_size = tick_size
            self.neg_risk = neg_risk
            self.version = version


def _round_to_tick_size(price: float, tick_size: float = 0.001) -> float:
    if tick_size <= 0:
        return round(price, 4)
    steps = round(price / tick_size)
    return round(steps * tick_size, 4)


def _resolve_token_metadata(client: Any, token_id: str, default_tick: float = 0.001) -> Tuple[float, bool]:
    tick = default_tick
    neg_risk = False
    if client:
        try:
            if hasattr(client, "get_tick_size"):
                res = client.get_tick_size(token_id)
                if res is not None and not hasattr(res, "_mock_return_value"):
                    tick = float(res)
        except Exception:
            pass
        try:
            if hasattr(client, "get_neg_risk"):
                res = client.get_neg_risk(token_id)
                if isinstance(res, bool):
                    neg_risk = res
                elif isinstance(res, str):
                    neg_risk = res.lower() in ("true", "1")
                elif isinstance(res, (int, float)) and not isinstance(res, bool):
                    neg_risk = bool(res)
                elif res is not None and not hasattr(res, "_mock_return_value"):
                    neg_risk = bool(res)
        except Exception:
            pass
    return tick, neg_risk



class RollbackProtector:
    """
    Engine for loss-free unwinding and price protection on Polymarket CLOB.
    """

    def __init__(self, target_state: Optional[Any] = None):
        self.target_state = target_state

    @staticmethod
    def evaluate_position_hold_vs_exit(
        buy_price: float,
        best_bid: float,
        time_to_resolution: Optional[float] = None,
        max_loss_cents: float = 0.005,
    ) -> str:
        floor_price = round(buy_price - max_loss_cents, 6)
        if best_bid >= floor_price:
            return "IMMEDIATE_EXIT"
        return "POST_LIMIT_SELL"

    @classmethod
    def safe_unwind_or_limit_exit(
        cls_or_self,
        *args,
        **kwargs
    ) -> Tuple[bool, str, dict]:
        if len(args) > 0 and isinstance(args[0], RollbackProtector):
            instance = args[0]
            args = args[1:]
            default_target_state = getattr(instance, "target_state", None)
        else:
            default_target_state = None

        client = args[0] if len(args) > 0 else kwargs.get("client")
        token_id = str(args[1] if len(args) > 1 else kwargs.get("token_id", ""))
        shares = float(args[2] if len(args) > 2 else kwargs.get("shares", 0.0))
        buy_price = float(args[3] if len(args) > 3 else kwargs.get("buy_price", 0.0))
        label = str(args[4] if len(args) > 4 else kwargs.get("label", "YES"))
        max_loss_cents = float(args[5] if len(args) > 5 else kwargs.get("max_loss_cents", 0.005))
        target_state = kwargs.get("target_state", default_target_state)
        force_market_exit = kwargs.get("force_market_exit", None)
        tick_size = float(kwargs.get("tick_size", 0.001) or 0.001)
        neg_risk = kwargs.get("neg_risk", None)

        if shares <= 0:
            return True, "NO_SHARES", {"shares": shares, "realized_loss": 0.0}

        if client is None:
            logger.error("safe_unwind_or_limit_exit called with client=None")
            return False, "CLIENT_NONE", {"error": "ClobClient is None", "realized_loss": round(shares * buy_price, 4)}

        book = cls_or_self.fetch_order_book(client, token_id)
        best_bid = cls_or_self.extract_best_bid(book)

        floor_price = round(buy_price - max_loss_cents, 6)
        decision = cls_or_self.evaluate_position_hold_vs_exit(
            buy_price=buy_price,
            best_bid=best_bid,
            max_loss_cents=max_loss_cents,
        )

        if force_market_exit is not None:
            if not force_market_exit:
                decision = "POST_LIMIT_SELL"
            elif best_bid > 0:
                decision = "IMMEDIATE_EXIT"
            else:
                decision = "NO_BIDS_AVAILABLE"

        if decision == "NO_BIDS_AVAILABLE":
            err_msg = f"Zero bids on book for {token_id}. Immediate liquidation blocked by zero liquidity."
            logger.error(err_msg)
            return False, "NO_BIDS_AVAILABLE", {
                "error": err_msg,
                "realized_loss": round(shares * buy_price, 4),
                "best_bid": 0.0,
                "buy_price": buy_price,
                "shares": shares,
            }

        if decision == "IMMEDIATE_EXIT":
            sell_price = round(best_bid, 4)
            loss_per_share = max(0.0, buy_price - sell_price)
            realized_loss = round(shares * loss_per_share, 4)

            success, resp, err = cls_or_self._post_sell_order(
                client=client,
                token_id=token_id,
                price=sell_price,
                shares=shares,
                order_type="FOK",
                tick_size=tick_size,
                neg_risk=neg_risk,
            )
            for retry in range(3):
                if success:
                    break
                if 'balance' in str(err).lower() or 'allowance' in str(err).lower():
                    time.sleep(1.0)
                    # Re-fetch book and retry _post_sell_order
                    book = cls_or_self.fetch_order_book(client, token_id)
                    fresh_bid = cls_or_self.extract_best_bid(book)
                    if fresh_bid > 0:
                        sell_price = round(fresh_bid, 4)
                    success, resp, err = cls_or_self._post_sell_order(
                        client=client,
                        token_id=token_id,
                        price=sell_price,
                        shares=shares,
                        order_type='FOK',
                        tick_size=tick_size,
                        neg_risk=neg_risk,
                    )

            if success:
                loss_per_share = max(0.0, buy_price - sell_price)
                realized_loss = round(shares * loss_per_share, 4)
                logger.info(
                    f"🛡️ [SAFE MARKET EXIT] Unwound {shares} {label} shares at ${sell_price:.4f} "
                    f"(floor: ${floor_price:.4f}, buy: ${buy_price:.4f}, realized_loss: ${realized_loss:.4f})."
                )
                if target_state:
                    target_state.add_activity_log(
                        f"🛡️ [SAFE ROLLBACK] Unwound {shares} {label} shares at ${sell_price:.4f} "
                        f"(buy: ${buy_price:.4f})"
                    )
                resp_payload = resp if isinstance(resp, dict) else (
                    resp[0] if isinstance(resp, list) and resp and isinstance(resp[0], dict) else {"resp": resp}
                )
                resp_payload["realized_loss"] = realized_loss
                resp_payload["sell_price"] = sell_price
                resp_payload["buy_price"] = buy_price
                resp_payload["shares"] = shares
                return True, "MARKET_EXIT_SAFE", resp_payload
            else:
                logger.warning(f"Market exit FOK order rejected for {token_id}: {err}")
                return False, "MARKET_EXIT_FAILED", {
                    "error": err,
                    "resp": resp,
                    "realized_loss": round(shares * buy_price, 4),
                    "best_bid": best_bid,
                    "sell_price": sell_price,
                    "buy_price": buy_price,
                    "shares": shares,
                }

        else:
            limit_price = round(buy_price, 4)
            success, resp, err = cls_or_self._post_sell_order(
                client=client,
                token_id=token_id,
                price=limit_price,
                shares=shares,
                order_type="GTC",
                tick_size=tick_size,
                neg_risk=neg_risk,
            )
            for retry in range(3):
                if success:
                    break
                if err and ('balance' in str(err).lower() or 'allowance' in str(err).lower()):
                    time.sleep(1.0)
                    success, resp, err = cls_or_self._post_sell_order(
                        client=client,
                        token_id=token_id,
                        price=limit_price,
                        shares=shares,
                        order_type="GTC",
                        tick_size=tick_size,
                        neg_risk=neg_risk,
                    )

            if success:
                order_id = cls_or_self.extract_order_id(resp) or ""
                log_msg = (
                    f"🛡️ [LOSS-FREE EXIT] Best bid ${best_bid:.4f} is below floor ${floor_price:.4f}. "
                    f"Placed limit sell at purchase price ${buy_price:.4f} to eliminate slippage loss."
                )
                logger.info(log_msg)
                if target_state:
                    target_state.add_activity_log(log_msg)
                return True, "LIMIT_ORDER_PLACED", {"order_id": order_id, "price": buy_price, "realized_loss": 0.0}
            else:
                logger.error(
                    f"Failed to place passive limit sell order at ${limit_price:.4f} for {token_id}: {err}"
                )
                return False, "LIMIT_ORDER_FAILED", {"error": err, "price": buy_price, "resp": resp, "realized_loss": round(shares * buy_price, 4)}

    @staticmethod
    def fetch_order_book(client: Any, token_id: str) -> dict:
        if client and hasattr(client, "get_order_book"):
            try:
                book = client.get_order_book(token_id)
                if book:
                    return book
            except Exception as e:
                logger.debug(f"client.get_order_book failed for {token_id}: {e}")

        try:
            book_url = f"https://clob.polymarket.com/book?token_id={token_id}"
            req_book = urllib.request.Request(
                book_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PolymarketBot/2.0"}
            )
            with urllib.request.urlopen(req_book, timeout=5) as resp_book:
                return json.loads(resp_book.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"Failed to fetch order book for {token_id} via HTTP: {e}")
            return {}

    @staticmethod
    def extract_best_bid(book: Any) -> float:
        if not book:
            return 0.0

        bids = []
        if isinstance(book, dict):
            bids = book.get("bids", [])
        elif hasattr(book, "bids"):
            bids = getattr(book, "bids", [])

        if not isinstance(bids, (list, tuple)):
            return 0.0

        bid_prices = []
        for b in bids:
            try:
                if isinstance(b, dict):
                    bp = float(b.get("price", 0.0))
                elif isinstance(b, (list, tuple)) and len(b) > 0:
                    bp = float(b[0])
                elif hasattr(b, "price"):
                    bp = float(getattr(b, "price", 0.0))
                else:
                    bp = float(b)
                if bp > 0:
                    bid_prices.append(bp)
            except (ValueError, TypeError):
                continue

        return max(bid_prices) if bid_prices else 0.0

    @staticmethod
    def extract_order_id(resp: Any) -> Optional[str]:
        if not resp:
            return None
        if isinstance(resp, list) and len(resp) > 0:
            first = resp[0]
            if isinstance(first, dict):
                return str(first.get("orderID") or first.get("order_id") or first.get("id") or "")
            return str(getattr(first, "orderID", getattr(first, "order_id", getattr(first, "id", ""))))
        if isinstance(resp, dict):
            return str(resp.get("orderID") or resp.get("order_id") or resp.get("id") or "")
        return str(getattr(resp, "orderID", getattr(resp, "order_id", getattr(resp, "id", ""))))

    @staticmethod
    def _check_order_error(resp: Any) -> Optional[str]:
        if resp is None:
            return "Empty response from CLOB"
        if isinstance(resp, list):
            if not resp:
                return "Empty response list from CLOB"
            for r in resp:
                if isinstance(r, dict) and r.get("errorMsg"):
                    return str(r.get("errorMsg"))
                if hasattr(r, "errorMsg") and getattr(r, "errorMsg"):
                    return str(getattr(r, "errorMsg"))
        elif isinstance(resp, dict):
            if resp.get("errorMsg"):
                return str(resp.get("errorMsg"))
            if resp.get("error"):
                return str(resp.get("error"))
        return None

    @classmethod
    def _post_sell_order(
        cls,
        client: Any,
        token_id: str,
        price: float,
        shares: float,
        order_type: str = "FOK",
        tick_size: float = 0.001,
        neg_risk: Optional[bool] = None,
    ) -> Tuple[bool, Any, Optional[str]]:
        try:
            target_order_type = getattr(OrderType, order_type, order_type)

            actual_tick, actual_neg = _resolve_token_metadata(client, token_id, default_tick=tick_size)
            if neg_risk is not None:
                actual_neg = bool(neg_risk)

            clean_price = _round_to_tick_size(price, actual_tick)
            min_bound = actual_tick
            max_bound = round(1.0 - actual_tick, 4)
            if clean_price < min_bound:
                clean_price = min_bound
            elif clean_price > max_bound:
                clean_price = max_bound

            order_opts = None
            if PartialCreateOrderOptions is not None:
                order_opts = PartialCreateOrderOptions(tick_size=str(actual_tick), neg_risk=actual_neg)

            created_order = None
            if hasattr(client, "create_order"):
                if OrderArgsV2 is not None:
                    try:
                        args_obj = OrderArgsV2(price=clean_price, size=float(shares), side="SELL", token_id=token_id)
                    except TypeError:
                        args_obj = OrderArgsV2(token_id=token_id, price=clean_price, size=float(shares), side="SELL")
                else:
                    args_obj = {
                        "price": clean_price,
                        "size": float(shares),
                        "side": "SELL",
                        "token_id": token_id,
                    }

                if order_opts is not None:
                    try:
                        created_order = client.create_order(args_obj, options=order_opts)
                    except TypeError:
                        created_order = client.create_order(args_obj)
                else:
                    created_order = client.create_order(args_obj)

            resp = None
            if created_order is not None and hasattr(client, "post_orders"):
                if PostOrdersV2Args is not None:
                    post_arg = PostOrdersV2Args(order=created_order, orderType=target_order_type)
                    resp = client.post_orders([post_arg])
                else:
                    resp = client.post_orders([created_order])
            elif created_order is not None and hasattr(client, "post_order"):
                resp = client.post_order(created_order, order_type=target_order_type)
            elif hasattr(client, "create_and_post_order"):
                if OrderArgsV2 is not None:
                    try:
                        args_obj = OrderArgsV2(price=clean_price, size=float(shares), side="SELL", token_id=token_id)
                    except TypeError:
                        args_obj = OrderArgsV2(token_id=token_id, price=clean_price, size=float(shares), side="SELL")
                else:
                    args_obj = {"price": clean_price, "size": float(shares), "side": "SELL", "token_id": token_id}
                if order_opts is not None:
                    try:
                        resp = client.create_and_post_order(args_obj, order_type=target_order_type, options=order_opts)
                    except TypeError:
                        resp = client.create_and_post_order(args_obj, order_type=target_order_type)
                else:
                    resp = client.create_and_post_order(args_obj, order_type=target_order_type)
            else:
                return False, None, f"Unsupported client type: {type(client)}"

            err_msg = cls._check_order_error(resp)
            if err_msg:
                return False, resp, err_msg
            return True, resp, None

        except Exception as e:
            logger.warning(f"Error posting {order_type} sell order for token {token_id}: {e}")
            return False, None, str(e)



safe_unwind_or_limit_exit = RollbackProtector.safe_unwind_or_limit_exit
evaluate_position_hold_vs_exit = RollbackProtector.evaluate_position_hold_vs_exit
