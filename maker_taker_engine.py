"""
Maker-Taker Asymmetric Execution Engine for Polymarket CLOB Arbitrage.

Solves non-atomic dual-taker leg-out failures by separating execution into:
1. Leg 1 (Maker): Passive GTC limit order inside the spread (0% taker fee, 0 slippage).
2. Timeout & Cancellation: If unfilled within timeout (default 5.0s), cancelled with $0 cost and $0 loss.
3. Leg 2 (Taker): Only after Leg 1 is 100% matched and confirmed, fires an instant FOK taker order.
4. Capital Protection Rollback: If Leg 2 misses, places a break-even limit sell at maker_price (never penny dumps).
"""

import logging
import math
import os
import sys
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("MakerTakerEngine")

def _round_to_tick_size(price: float, tick_size: float = 0.001) -> float:
    if tick_size <= 0:
        return round(price, 4)
    steps = round(price / tick_size)
    return round(steps * tick_size, 4)

try:
    from py_clob_client_v2.clob_types import OrderArgsV2, OrderType, PostOrdersV2Args
except ImportError:
    class OrderType:
        GTC = "GTC"
        FOK = "FOK"
        GTD = "GTD"

    class OrderArgsV2:
        def __init__(self, price: float, size: float, side: str, token_id: str):
            self.price = price
            self.size = size
            self.side = side
            self.token_id = token_id

    class PostOrdersV2Args:
        def __init__(self, order: Any, orderType: Any):
            self.order = order
            self.orderType = orderType


class MakerTakerExecutor:
    """
    Asymmetric Maker-Taker Execution Engine for Polymarket binary parity arbitrage.
    """

    def __init__(self, client, dash_state=None):
        self.client = client
        self.dash_state = dash_state

    def _log_activity(self, message: str):
        logger.info(message)
        if self.dash_state and hasattr(self.dash_state, "add_activity_log"):
            try:
                self.dash_state.add_activity_log(message)
            except Exception as e:
                logger.debug(f"Failed to push activity log to dash_state: {e}")

    def _extract_order_id(self, post_resp: Any) -> Optional[str]:
        if not post_resp:
            return None
        if isinstance(post_resp, list) and len(post_resp) > 0:
            first = post_resp[0]
            if isinstance(first, dict):
                if first.get("errorMsg"):
                    return None
                return first.get("orderID") or first.get("order_id")
            return getattr(first, "orderID", None) or getattr(first, "order_id", None)
        elif isinstance(post_resp, dict):
            if post_resp.get("errorMsg"):
                return None
            return post_resp.get("orderID") or post_resp.get("order_id")
        return getattr(post_resp, "orderID", None) or getattr(post_resp, "order_id", None)

    def _extract_status_and_matched(self, order_info: Any) -> Tuple[str, float]:
        if not order_info:
            return "UNKNOWN", 0.0

        if isinstance(order_info, dict):
            status = str(order_info.get("status", "UNKNOWN")).upper()
            try:
                matched = float(
                    order_info.get("size_matched")
                    or order_info.get("matched_size")
                    or order_info.get("takingAmount")
                    or 0.0
                )
            except (ValueError, TypeError):
                matched = 0.0
            return status, matched

        status = str(getattr(order_info, "status", "UNKNOWN")).upper()
        matched_attr = (
            getattr(order_info, "size_matched", None)
            or getattr(order_info, "matched_size", None)
            or getattr(order_info, "takingAmount", None)
            or 0.0
        )
        try:
            matched = float(matched_attr)
        except (ValueError, TypeError):
            matched = 0.0
        return status, matched

    def cancel_order(self, order_id: str) -> bool:
        if not self.client or not order_id:
            return False
        try:
            if hasattr(self.client, "cancel_orders"):
                self.client.cancel_orders([order_id])
                return True
            elif hasattr(self.client, "cancel"):
                self.client.cancel(order_id)
                return True
            elif hasattr(self.client, "cancel_order"):
                self.client.cancel_order(order_id)
                return True
            return False
        except Exception as e:
            logger.warning(f"Error cancelling order {order_id}: {e}")
            return False

    def execute_maker_taker_arbitrage(
        self,
        token_maker: str,
        maker_price: float,
        token_taker: str,
        taker_price: float,
        size: float,
        timeout_seconds: float = 5.0,
        rollback_mode: str = "LIMIT_SELL",
        dash_state: Optional[Any] = None,
        min_edge: float = 0.0080,
        tick_size: float = 0.001,
    ) -> Tuple[bool, str, dict]:
        if dash_state is not None:
            self.dash_state = dash_state

        if not self.client:
            return False, "CLIENT_NOT_INITIALIZED", {}

        maker_price = _round_to_tick_size(maker_price, tick_size)
        taker_price = round(taker_price, 4)
        size = float(size)

        # -------------------------------------------------------------
        # STEP 1: Maker Leg (Passive Limit Order with 0% Taker Fee)
        # -------------------------------------------------------------
        self._log_activity(
            f"🎯 [MAKER LEG] Posting passive limit order: {size} shares @ ${maker_price:.4f} (GTC) on token {token_maker[-6:]}..."
        )
        try:
            order_maker = self.client.create_order(
                OrderArgsV2(
                    price=maker_price,
                    size=size,
                    side="BUY",
                    token_id=token_maker,
                )
            )
            resp_maker = self.client.post_orders(
                [PostOrdersV2Args(order=order_maker, orderType=OrderType.GTC)]
            )
        except Exception as e:
            err_msg = f"Failed to post Maker limit order: {e}"
            logger.error(err_msg)
            return False, "MAKER_POST_FAILED", {"error": str(e)}

        order_id_maker = self._extract_order_id(resp_maker)
        if not order_id_maker:
            err_msg = f"Maker order submission failed or rejected by sequencer: {resp_maker}"
            logger.warning(err_msg)
            return False, "MAKER_POST_FAILED", {"response": resp_maker}

        self._log_activity(f"⏳ [MAKER PENDING] Order {order_id_maker} active. Awaiting fill (timeout={timeout_seconds}s)...")

        # -------------------------------------------------------------
        # STEP 2: Wait for Fill or Timeout
        # -------------------------------------------------------------
        start_time = time.time()
        poll_interval = 0.5
        leg_1_filled = False
        matched_size = 0.0

        while time.time() - start_time < timeout_seconds:
            time.sleep(poll_interval)
            try:
                order_info = self.client.get_order(order_id_maker)
            except Exception as e:
                logger.debug(f"Polling get_order({order_id_maker}) encountered error: {e}")
                continue

            status, matched = self._extract_status_and_matched(order_info)

            if status in ("MATCHED", "FILLED") or matched >= (size - 1e-6):
                leg_1_filled = True
                matched_size = max(matched, size)
                self._log_activity(
                    f"✅ [MAKER FILLED] Leg 1 secured! {matched_size:.2f} shares matched @ ${maker_price:.4f}."
                )
                break

            if status in ("CANCELED", "KILLED", "EXPIRED"):
                self._log_activity(f"⚠️ [MAKER CANCELLED] Order {order_id_maker} was terminated externally with status '{status}'.")
                return False, "MAKER_CANCELLED_EXTERNALLY", {"status": status, "order_id": order_id_maker}

        # Handle Maker Leg Timeout
        if not leg_1_filled:
            self.cancel_order(order_id_maker)
            try:
                final_info = self.client.get_order(order_id_maker)
                st, m_sz = self._extract_status_and_matched(final_info)
                if st in ("MATCHED", "FILLED") or m_sz >= (size - 1e-6):
                    leg_1_filled = True
                    matched_size = max(m_sz, size)
                    self._log_activity(
                        f"✅ [MAKER RACED] Leg 1 filled immediately prior to cancellation: {matched_size:.2f} shares."
                    )
            except Exception:
                pass

        if not leg_1_filled:
            timeout_log = f"⏱️ [MAKER TIMEOUT] Leg 1 unfilled after {timeout_seconds}s. Cancelled limit order with $0 loss."
            self._log_activity(timeout_log)
            return False, "MAKER_TIMEOUT_ZERO_LOSS", {
                "order_id_maker": order_id_maker,
                "maker_price": maker_price,
                "timeout_seconds": timeout_seconds,
            }

        # -------------------------------------------------------------
        # STEP 3: Taker Leg (Instant FOK on Leg 2 with Secured Leg 1)
        # -------------------------------------------------------------
        # Factor in taker fees and minimum required margin buffer
        max_viable_taker_price = round(1.0 - maker_price - min_edge, 4)
        fresh_taker_price = taker_price
        try:
            import urllib.request, json
            book_url = f"https://clob.polymarket.com/book?token_id={token_taker}"
            req = urllib.request.Request(
                book_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PolymarketBot/2.0"}
            )
            with urllib.request.urlopen(req, timeout=3) as resp_b:
                b_data = json.loads(resp_b.read().decode('utf-8'))
            asks_t = b_data.get("asks", [])
            if asks_t:
                live_ask = float(asks_t[0].get("price") if isinstance(asks_t[0], dict) else asks_t[0][0])
                if live_ask <= max_viable_taker_price:
                    fresh_taker_price = live_ask
                    logger.info(f"Updated taker leg to live best ask: ${fresh_taker_price:.4f} (max viable: ${max_viable_taker_price:.4f})")
        except Exception as e:
            logger.debug(f"Could not refresh taker book: {e}")

        # Ensure taker price does not exceed max viable price
        target_taker_price = min(fresh_taker_price, max_viable_taker_price)
        target_taker_price = _round_to_tick_size(target_taker_price, tick_size)

        self._log_activity(
            f"⚡ [TAKER LEG] Leg 1 in hand. Firing instant FOK taker order: {matched_size:.2f} shares @ ${target_taker_price:.4f} on token {token_taker[-6:]}..."
        )
        try:
            order_taker = self.client.create_order(
                OrderArgsV2(
                    price=target_taker_price,
                    size=matched_size,
                    side="BUY",
                    token_id=token_taker,
                )
            )
            resp_taker = self.client.post_orders(
                [PostOrdersV2Args(order=order_taker, orderType=OrderType.FOK)]
            )
        except Exception as e:
            logger.error(f"Failed to post Taker FOK order: {e}")
            resp_taker = {"errorMsg": str(e)}

        leg_taker = resp_taker[0] if isinstance(resp_taker, list) and len(resp_taker) > 0 and isinstance(resp_taker[0], dict) else (resp_taker if isinstance(resp_taker, dict) else {})
        taker_err = leg_taker.get("errorMsg")
        order_id_taker = leg_taker.get("orderID") or leg_taker.get("order_id")

        try:
            taker_taking = float(leg_taker.get("takingAmount") or 0.0) if not taker_err else 0.0
        except (ValueError, TypeError):
            taker_taking = 0.0

        if order_id_taker and not taker_err and (leg_taker.get("status") == "delayed" or taker_taking <= 0):
            poll_start = time.time()
            while time.time() - poll_start < 2.0:
                time.sleep(0.3)
                try:
                    info_t = self.client.get_order(order_id_taker)
                    st_t, m_t = self._extract_status_and_matched(info_t)
                    if st_t in ("MATCHED", "FILLED") or m_t >= (matched_size - 1e-6):
                        taker_taking = max(m_t, matched_size)
                        break
                    elif st_t in ("CANCELED", "KILLED"):
                        break
                except Exception:
                    pass

        taker_filled = (taker_taking > 0) or (leg_taker.get("status") == "matched" and not taker_err)

        if taker_filled:
            success_msg = (
                f"🎉 [ARBITRAGE SECURED] Locked dual-leg pair! Leg 1 Maker @ ${maker_price:.4f}, "
                f"Leg 2 Taker @ ${target_taker_price:.4f}. Total Cost: ${maker_price + target_taker_price:.4f} -> Guaranteed Resolution Value: $1.00."
            )
            self._log_activity(success_msg)
            return True, "SUCCESS", {
                "hedged_size": matched_size,
                "maker_price": maker_price,
                "taker_price": target_taker_price,
                "total_cost": round(maker_price + target_taker_price, 4),
                "profit_per_share": round(1.0 - (maker_price + target_taker_price), 4),
            }

        # -------------------------------------------------------------
        # STEP 4: IMMEDIATE EMERGENCY UNWIND (Eradicate Passive Limit Sell)
        # -------------------------------------------------------------
        rollback_meta = {
            "maker_price": maker_price,
            "size": matched_size,
            "token_maker": token_maker,
            "taker_error": taker_err,
        }

        if rollback_mode == "LIMIT_SELL":
            self._log_activity(
                f"🛡️ [SAFE ROLLBACK] Leg 2 missed. Deploying break-even limit sell for {matched_size:.2f} shares at ${maker_price:.4f}."
            )
            try:
                order_rollback = self.client.create_order(
                    OrderArgsV2(
                        price=maker_price,
                        size=matched_size,
                        side="SELL",
                        token_id=token_maker,
                    )
                )
                resp_rollback = self.client.post_orders(
                    [PostOrdersV2Args(order=order_rollback, orderType=OrderType.GTC)]
                )
                rollback_id = self._extract_order_id(resp_rollback)
                rollback_meta["rollback_order_id"] = rollback_id
                self._log_activity(f"🛡️ [ROLLBACK POSTED] Resting GTC limit sell placed @ ${maker_price:.4f} (Order ID: {rollback_id}).")
            except Exception as e:
                logger.warning(f"Failed to post safe rollback limit sell: {e}. Holding asset at full value.")
                rollback_meta["rollback_error"] = str(e)
            return False, "TAKER_FAILED_ROLLBACK_LIMIT_PLACED", rollback_meta
        else:
            from rollback_protector import RollbackProtector
            self._log_activity(
                f"🚨 [UNWIND TRIGGERED] Leg 2 missed ({taker_err or 'unmatched'}). "
                f"Invoking RollbackProtector for immediate market liquidation of {matched_size:.2f} Leg 1 shares..."
            )
            unwind_ok, unwind_action, unwind_details = RollbackProtector.safe_unwind_or_limit_exit(
                client=self.client,
                token_id=token_maker,
                shares=matched_size,
                buy_price=maker_price,
                label="LEG1_MAKER",
                target_state=self.dash_state,
                force_market_exit=True
            )
            realized_loss = unwind_details.get("realized_loss", 0.0) if isinstance(unwind_details, dict) else 0.0
            rollback_meta.update({
                "unwind_ok": unwind_ok,
                "unwind_action": unwind_action,
                "unwind_details": unwind_details,
                "realized_loss": realized_loss
            })
            self._log_activity(
                f"🛡️ [UNWIND COMPLETE] Action: {unwind_action} | Realized Loss: ${realized_loss:.4f}."
            )
            return False, "TAKER_FAILED_UNWOUND", rollback_meta


def check_maker_taker_parity(
    market: Any = None,
    bid_yes: Optional[float] = None,
    ask_yes: Optional[float] = None,
    bid_no: Optional[float] = None,
    ask_no: Optional[float] = None,
    tick_size: float = 0.001,
    min_edge: float = 0.0020,
    taker_fee_bps: int = 35,
    market_id: str = "test_market"
) -> Optional[dict]:
    """
    Evaluates asymmetric Maker-Taker parity opportunities.
    Finds opportunities where pure taker fails (Ask_YES + Ask_NO >= 1.00),
    but a passive maker bid on one leg + taker on the other yields positive net edge:
    e.g. Bid_YES + Ask_NO < 1.00 or Bid_NO + Ask_YES < 1.00.
    """
    if isinstance(market, dict):
        d = market
        b_yes = float(d.get("bid_yes") or d.get("yes_bid") or (bid_yes if bid_yes is not None else 0.0))
        a_yes = float(d.get("ask_yes") or d.get("yes_ask") or (ask_yes if ask_yes is not None else 0.0))
        b_no = float(d.get("bid_no") or d.get("no_bid") or (bid_no if bid_no is not None else 0.0))
        a_no = float(d.get("ask_no") or d.get("no_ask") or (ask_no if ask_no is not None else 0.0))
        m_id = str(d.get("market_id") or d.get("condition_id") or market_id)
        tick_size = float(d.get("tick_size") or tick_size)
        min_edge = float(d.get("min_edge") or min_edge)
        taker_fee_bps = int(d.get("taker_fee_bps") or taker_fee_bps)
    else:
        b_yes = float(bid_yes if bid_yes is not None else (market if isinstance(market, (int, float)) else 0.0))
        a_yes = float(ask_yes if ask_yes is not None else 0.0)
        b_no = float(bid_no if bid_no is not None else 0.0)
        a_no = float(ask_no if ask_no is not None else 0.0)
        m_id = str(market_id)


    if a_yes <= 0.0 or a_no <= 0.0:
        return None

    taker_fee_rate = taker_fee_bps / 10000.0
    pure_taker_cost = round(a_yes + a_no, 4)

    # Direction 1: Leg 1 Maker on YES, Leg 2 Taker on NO
    maker_price_yes = min(round(b_yes + tick_size, 4), round(a_yes - tick_size, 4)) if (b_yes > 0 and a_yes > b_yes + tick_size) else round(b_yes if b_yes > 0 else a_yes - tick_size, 4)
    maker_price_yes = max(tick_size, maker_price_yes)
    taker_fee_1 = a_no * taker_fee_rate
    cost_dir1 = round(maker_price_yes + a_no + taker_fee_1, 4)
    edge_dir1 = round(1.00 - cost_dir1, 6)

    # Direction 2: Leg 1 Maker on NO, Leg 2 Taker on YES
    maker_price_no = min(round(b_no + tick_size, 4), round(a_no - tick_size, 4)) if (b_no > 0 and a_no > b_no + tick_size) else round(b_no if b_no > 0 else a_no - tick_size, 4)
    maker_price_no = max(tick_size, maker_price_no)
    taker_fee_2 = a_yes * taker_fee_rate
    cost_dir2 = round(maker_price_no + a_yes + taker_fee_2, 4)
    edge_dir2 = round(1.00 - cost_dir2, 6)

    best_edge = max(edge_dir1, edge_dir2)
    if best_edge < min_edge:
        return None

    use_dir1 = (edge_dir1 >= edge_dir2)
    return {
        "market_id": m_id,
        "opportunity_type": "MAKER_TAKER",
        "maker_side": "YES" if use_dir1 else "NO",
        "maker_price": maker_price_yes if use_dir1 else maker_price_no,
        "taker_side": "NO" if use_dir1 else "YES",
        "taker_price": a_no if use_dir1 else a_yes,
        "bid_yes": b_yes,
        "ask_yes": a_yes,
        "bid_no": b_no,
        "ask_no": a_no,
        "cost": cost_dir1 if use_dir1 else cost_dir2,
        "effective_cost": cost_dir1 if use_dir1 else cost_dir2,
        "edge": best_edge,
        "edge_pct": round(best_edge * 100.0, 4),
        "pure_taker_cost": pure_taker_cost,
        "is_maker_taker": True
    }

