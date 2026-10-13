"""High-Certainty Satellite Engine for Polymarket.

Scans for Parity Arbitrage (sum(asks) < 1.00) and Heavy Favorites (0.94-0.985).
Enforces microstructure defense gates and runs in read-only shadow mode by default.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import time
from typing import Any
import requests

from pmus.certainty_ledger import CertaintyLedger
from pmus.config import Settings
from pmus.models import Book, Level

logger = logging.getLogger("pmus.satellite")


class SatelliteEngine:
    def __init__(
        self,
        settings: Settings | None = None,
        ledger: CertaintyLedger | None = None,
        trade_mode: str = "shadow",
        logger_instance: logging.Logger | None = None,
        gamma_url: str = "https://gamma-api.polymarket.com",
        clob_url: str = "https://clob.polymarket.com",
        bankroll_usd: float = 1000.0,
        order_size_usd: float = 30.0,
        max_open_positions: int = 30,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.ledger = ledger or CertaintyLedger(self.settings.data_dir / "certainty_ledger.db")
        self.trade_mode = trade_mode
        self.logger = logger_instance or logger
        self.gamma_url = gamma_url
        self.clob_url = clob_url
        self.bankroll_usd = bankroll_usd
        self.order_size_usd = order_size_usd
        self.max_open_positions = max_open_positions
        self.price_history_cache: dict[str, list[tuple[float, float]]] = {}

    @staticmethod
    def check_gates(
        market: dict[str, Any],
        book: Book | dict[str, Any],
        price_history: list[tuple[float, float]] | None = None,
        current_time: datetime | None = None,
        require_heavy_favorite: bool = True,
    ) -> tuple[bool, str]:
        """Microstructure Defense Filter:
        1. Spread gate: best_ask - best_bid <= 0.025.
        2. Depth floor: bid_depth_usd >= 250.0 and ask_depth_usd >= 15.0.
        3. Price velocity: Price change in last 60s <= 0.02.
        4. Expiry / APY gate & Horizon:
           - If days to resolution <= 3: accept 0.940 <= best_ask <= 0.985
           - If days to resolution <= 14: accept 0.960 <= best_ask <= 0.985 (with APY >= 20% if > 7d)
           - If days to resolution > 14: reject unconditionally ("Resolution > 14 days exceeds horizon")
        5. Price ceiling: Never buy above 0.985 (paying 0.99+ requires 100:1 win ratio).
        """
        if isinstance(book, Book):
            bids = [(float(b.price), float(b.qty)) for b in book.bids]
            asks = [(float(a.price), float(a.qty)) for a in book.asks]
        elif isinstance(book, dict):
            bids = [
                (float(b.get("price", 0.0)), float(b.get("size", b.get("qty", 0.0))))
                for b in book.get("bids", [])
            ]
            asks = [
                (float(a.get("price", 0.0)), float(a.get("size", a.get("qty", 0.0))))
                for a in book.get("asks", [])
            ]
        else:
            return False, "Invalid book format"

        if not bids or not asks:
            return False, "Book missing bids or asks"

        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])

        best_bid = bids[0][0]
        best_ask = asks[0][0]

        # Gate 5: Price ceiling
        if best_ask > 0.985:
            return False, f"Price {best_ask:.4f} > 0.985 ceiling"

        # Gate 1: Spread gate (calibrated <= 0.025)
        spread = best_ask - best_bid
        if round(spread, 6) > 0.025:
            return False, f"Spread {spread:.4f} > 0.025"

        # Gate 2: Depth floor (bid >= $250.0, ask >= $15.0)
        bid_depth_usd = sum(p * q for p, q in bids)
        ask_depth_usd = sum(p * q for p, q in asks)
        if round(bid_depth_usd, 2) < 250.0:
            return False, f"Bid depth ${bid_depth_usd:.2f} < $250.00"
        if round(ask_depth_usd, 2) < 15.0:
            return False, f"Ask depth ${ask_depth_usd:.2f} < $15.00"

        # Gate 3: Price velocity
        now_dt = current_time or datetime.now(timezone.utc)
        now_ts = now_dt.timestamp()
        if price_history:
            recent_prices = [p for ts, p in price_history if now_ts - ts <= 60.0]
            if recent_prices:
                max_diff = max(abs(best_ask - p) for p in recent_prices)
                if round(max_diff, 6) > 0.02:
                    return False, f"Price velocity {max_diff:.4f} > 0.02 in last 60s"

        # Gate 4: Expiry, Horizon & APY gate
        raw_res = (
            market.get("endDate")
            or market.get("end_date_iso")
            or market.get("expected_resolution")
            or market.get("resolution_date")
        )
        if raw_res:
            res_dt = None
            if isinstance(raw_res, datetime):
                res_dt = raw_res
            elif isinstance(raw_res, str):
                try:
                    iso_str = raw_res.replace("Z", "+00:00")
                    res_dt = datetime.fromisoformat(iso_str)
                except Exception:
                    pass

            if res_dt:
                if res_dt.tzinfo is None:
                    res_dt = res_dt.replace(tzinfo=timezone.utc)
                days_to_res = (res_dt - now_dt).total_seconds() / 86400.0

                if days_to_res <= 0.0:
                    return False, f"Resolution timestamp in the past ({days_to_res:.2f}d)"

                # Unconditional rejection if > 14 days
                if days_to_res > 14.0:
                    return False, f"Resolution > 14 days ({days_to_res:.1f}d) exceeds horizon"

                if require_heavy_favorite:
                    if days_to_res <= 3.0:
                        if not (0.940 <= best_ask <= 0.985):
                            return False, f"Price {best_ask:.4f} outside short-dated band [0.940, 0.985]"
                    elif days_to_res <= 14.0:
                        if not (0.960 <= best_ask <= 0.985):
                            return False, f"Price {best_ask:.4f} outside medium-dated band [0.960, 0.985]"
                        if days_to_res > 7.0:
                            if best_ask > 0:
                                simple_yield = (1.0 - best_ask) / best_ask
                                annualized_yield = simple_yield * (365.0 / days_to_res)
                            else:
                                annualized_yield = 0.0
                            if round(annualized_yield, 4) < 0.20:
                                return False, f"Resolution > 7 days ({days_to_res:.1f}d) and APY {annualized_yield*100:.1f}% < 20%"
                else:
                    if days_to_res > 7.0:
                        if best_ask > 0:
                            simple_yield = (1.0 - best_ask) / best_ask
                            annualized_yield = simple_yield * (365.0 / days_to_res)
                        else:
                            annualized_yield = 0.0
                            if round(annualized_yield, 4) < 0.20:
                                return False, f"Resolution > 7 days ({days_to_res:.1f}d) and APY {annualized_yield*100:.1f}% < 20%"
        elif require_heavy_favorite:
            if not (0.960 <= best_ask <= 0.985):
                return False, f"Price {best_ask:.4f} outside medium-dated band [0.960, 0.985]"

        return True, "All microstructure gates passed"

    def fetch_public_markets(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            url = f"{self.gamma_url}/markets"
            now = datetime.now(timezone.utc)
            now_iso = now.isoformat()
            short_max_iso = (now + timedelta(days=3)).isoformat()

            slices = [
                # Volume slices (offsets: 0, 100, 200)
                {"order": "volume24hr", "descending": "true", "limit": limit, "offset": 0, "active": "true", "closed": "false"},
                {"order": "volume24hr", "descending": "true", "limit": limit, "offset": 100, "active": "true", "closed": "false"},
                {"order": "volume24hr", "descending": "true", "limit": limit, "offset": 200, "active": "true", "closed": "false"},
                # Liquidity slices (offsets: 0, 100)
                {"order": "liquidity", "descending": "true", "limit": limit, "offset": 0, "active": "true", "closed": "false"},
                {"order": "liquidity", "descending": "true", "limit": limit, "offset": 100, "active": "true", "closed": "false"},
                # Short expiring slice (<= 3 days)
                {"order": "volume24hr", "descending": "true", "limit": limit, "active": "true", "closed": "false", "end_date_min": now_iso, "end_date_max": short_max_iso},
            ]

            def _fetch_slice(params: dict[str, Any]) -> list[dict[str, Any]]:
                try:
                    resp = requests.get(url, params=params, timeout=10.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, list):
                            return data
                except Exception as e:
                    self.logger.debug(f"Slice fetch error: {e}")
                return []

            with ThreadPoolExecutor(max_workers=5) as executor:
                slice_results = list(executor.map(_fetch_slice, slices))

            seen: set[str] = set()
            combined: list[dict[str, Any]] = []
            for batch in slice_results:
                for m in batch:
                    cond_id = str(m.get("conditionId") or m.get("condition_id") or "")
                    slug = str(m.get("slug") or "")
                    dedup_key = cond_id or slug
                    if not dedup_key:
                        continue
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    if cond_id:
                        seen.add(cond_id)
                    if slug:
                        seen.add(slug)
                    combined.append(m)
            return combined
        except Exception as e:
            self.logger.error(f"Error fetching markets from Gamma API: {e}")
            return []

    def fetch_book(self, token_id: str) -> Book:
        url = f"{self.clob_url}/book?token_id={token_id}"
        resp = requests.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()

        def parse_level(item: Any) -> Level:
            if isinstance(item, dict):
                return Level(
                    price=Decimal(str(item.get("price", "0"))),
                    qty=Decimal(str(item.get("size", item.get("qty", "0")))),
                )
            if hasattr(item, "price") and hasattr(item, "size"):
                return Level(price=Decimal(str(item.price)), qty=Decimal(str(item.size)))
            return Level(price=Decimal(str(item[0])), qty=Decimal(str(item[1])))

        raw_bids = data.get("bids", []) if isinstance(data, dict) else []
        raw_asks = data.get("asks", []) if isinstance(data, dict) else []

        bids = sorted([parse_level(b) for b in raw_bids], key=lambda x: x.price, reverse=True)
        asks = sorted([parse_level(a) for a in raw_asks], key=lambda x: x.price)

        return Book(
            slug=token_id,
            bids=tuple(bids),
            asks=tuple(asks),
            ts_ms=int(time.time() * 1000),
        )

    def _record_price_history(self, token_id: str, price: float) -> None:
        now_ts = time.time()
        history = self.price_history_cache.setdefault(token_id, [])
        history.append((now_ts, price))
        # Keep only the last 300 seconds
        self.price_history_cache[token_id] = [h for h in history if now_ts - h[0] <= 300.0]

    def scan_heavy_favorites(self, markets: list[dict[str, Any]], order_size_usd: float | None = None) -> list[int]:
        open_trades = self.ledger.get_open_trades()
        if len(open_trades) >= self.max_open_positions:
            self.logger.info(f"Max open positions reached ({len(open_trades)}/{self.max_open_positions}). Skipping scan.")
            return []

        allocated_capital = sum(float(t["size_usd"]) for t in open_trades)
        available_cash = max(0.0, self.bankroll_usd - allocated_capital)
        order_size = order_size_usd if order_size_usd is not None else self.order_size_usd

        if available_cash < order_size:
            self.logger.info(f"Insufficient cash for trade (${available_cash:.2f} < ${order_size:.2f}). Skipping scan.")
            return []

        open_tokens = {str(t["token_id"]) for t in open_trades}
        slug_counts: dict[str, int] = {}
        for t in open_trades:
            s = str(t.get("market_slug", ""))
            slug_counts[s] = slug_counts.get(s, 0) + 1

        # 1. Pre-filter candidate tokens across markets using indicative Gamma price
        # Candidate tokens must satisfy: 0.935 <= gamma_px <= 0.985
        candidates: list[tuple[dict[str, Any], str, str, float]] = []

        for m in markets:
            cond_id = str(m.get("conditionId") or m.get("condition_id") or "")
            market_slug = str(m.get("slug") or "")
            question = str(m.get("question") or "")

            if slug_counts.get(market_slug, 0) >= 4:
                continue

            # Parse clobTokenIds and outcomePrices
            clob_ids = m.get("clobTokenIds")
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except Exception:
                    clob_ids = []
            if not isinstance(clob_ids, list):
                clob_ids = []

            outcome_prices = m.get("outcomePrices")
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except Exception:
                    outcome_prices = []
            if not isinstance(outcome_prices, list):
                outcome_prices = []

            outcomes = m.get("outcomes")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = []
            if not isinstance(outcomes, list):
                outcomes = []

            tokens_to_eval: list[tuple[str, str, float]] = []
            if clob_ids:
                for idx, c_id in enumerate(clob_ids):
                    tok_id = str(c_id)
                    out_name = str(outcomes[idx]).upper() if idx < len(outcomes) else f"OUT_{idx}"
                    try:
                        gamma_px = float(outcome_prices[idx]) if idx < len(outcome_prices) else 0.0
                    except (ValueError, TypeError):
                        gamma_px = 0.0
                    tokens_to_eval.append((tok_id, out_name, gamma_px))
            else:
                raw_tokens = m.get("tokens") or []
                if isinstance(raw_tokens, list):
                    for tok in raw_tokens:
                        if isinstance(tok, dict):
                            tok_id = str(tok.get("token_id") or "")
                            outcome = str(tok.get("outcome") or "YES").upper()
                            try:
                                price = float(tok.get("price") or 0.0)
                            except (ValueError, TypeError):
                                price = 0.0
                            tokens_to_eval.append((tok_id, outcome, price))

            for tok_id, outcome, gamma_px in tokens_to_eval:
                if not tok_id or tok_id in open_tokens:
                    continue
                # Pre-filter band: 0.935 <= gamma_px <= 0.985
                if 0.935 <= gamma_px <= 0.985:
                    candidates.append((m, tok_id, outcome, gamma_px))

        if not candidates:
            return []

        # 2. Concurrently fetch live CLOB books for candidate tokens
        def _fetch_candidate_book(item: tuple[dict[str, Any], str, str, float]) -> tuple[tuple[dict[str, Any], str, str, float], Book | None]:
            tok_id = item[1]
            try:
                book = self.fetch_book(tok_id)
                return (item, book)
            except Exception as e:
                self.logger.debug(f"Failed to fetch CLOB book for candidate {tok_id}: {e}")
                return (item, None)

        with ThreadPoolExecutor(max_workers=8) as executor:
            fetched = list(executor.map(_fetch_candidate_book, candidates))

        # 3. Gate verification and trade recording
        entered_ids: list[int] = []
        current_open_count = len(open_trades)

        for (m, tok_id, outcome, gamma_px), book in fetched:
            if current_open_count >= self.max_open_positions:
                break
            if available_cash < order_size:
                break
            if not book or not book.asks or not book.bids:
                continue

            market_slug = str(m.get("slug") or "")
            if slug_counts.get(market_slug, 0) >= 4:
                continue

            best_ask = float(book.asks[0].price)
            self._record_price_history(tok_id, best_ask)

            history = self.price_history_cache.get(tok_id)
            ok, reason = self.check_gates(m, book, price_history=history, require_heavy_favorite=True)
            if ok:
                cond_id = str(m.get("conditionId") or m.get("condition_id") or "")
                question = str(m.get("question") or "")
                shares = round(order_size / best_ask, 4)
                exp_res = str(m.get("endDate") or m.get("end_date_iso") or "")
                trade_id = self.ledger.record_entry(
                    trade_mode=self.trade_mode,
                    strategy_type="heavy_favorite",
                    condition_id=cond_id,
                    token_id=tok_id,
                    market_slug=market_slug,
                    question=question,
                    outcome=outcome,
                    side="BUY",
                    entry_price=best_ask,
                    size_usd=order_size,
                    shares=shares,
                    expected_resolution=exp_res,
                    status="FILLED",
                    notes=f"Heavy Favorite fill @ {best_ask:.4f}",
                )
                open_tokens.add(tok_id)
                entered_ids.append(trade_id)
                available_cash -= order_size
                current_open_count += 1
                slug_counts[market_slug] = slug_counts.get(market_slug, 0) + 1
                self.logger.info(
                    f"Entered heavy favorite trade {trade_id} on {market_slug} ({outcome}) @ {best_ask:.4f} "
                    f"| Remaining Cash: ${available_cash:.2f}"
                )
            else:
                self.logger.debug(f"Rejected {market_slug} ({outcome}): {reason}")

        return entered_ids

    def scan_parity_arbitrage(self, markets: list[dict[str, Any]], order_size_usd: float = 10.0) -> list[int]:
        open_trades = self.ledger.get_open_trades()
        if len(open_trades) >= self.max_open_positions:
            return []
        allocated_capital = sum(float(t["size_usd"]) for t in open_trades)
        available_cash = max(0.0, self.bankroll_usd - allocated_capital)
        if available_cash < order_size_usd * 2:
            return []

        open_tokens = {str(t["token_id"]) for t in open_trades}
        entered_ids = []

        for m in markets:
            cond_id = str(m.get("conditionId") or m.get("condition_id") or "")
            market_slug = str(m.get("slug") or "")
            question = str(m.get("question") or "")
            raw_tokens = m.get("tokens") or []

            tokens_list = []
            if isinstance(raw_tokens, list) and raw_tokens and isinstance(raw_tokens[0], dict):
                for tok in raw_tokens:
                    tok_id = str(tok.get("token_id") or "")
                    outcome = str(tok.get("outcome") or "").upper()
                    if tok_id:
                        tokens_list.append((tok_id, outcome))
            else:
                clob_ids = m.get("clobTokenIds")
                if isinstance(clob_ids, str):
                    try:
                        clob_ids = json.loads(clob_ids)
                    except Exception:
                        clob_ids = []
                outcomes = m.get("outcomes") or ["Yes", "No"]
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except Exception:
                        outcomes = ["Yes", "No"]
                if isinstance(clob_ids, list):
                    for idx, c_id in enumerate(clob_ids):
                        out_name = outcomes[idx] if idx < len(outcomes) else f"OUT_{idx}"
                        tokens_list.append((str(c_id), str(out_name).upper()))

            if len(tokens_list) < 2:
                continue

            raw_op = m.get("outcomePrices")
            if raw_op is not None:
                if isinstance(raw_op, str):
                    try:
                        raw_op = json.loads(raw_op)
                    except Exception:
                        raw_op = []
                if isinstance(raw_op, list):
                    try:
                        parsed_prices = [float(x) for x in raw_op]
                    except (ValueError, TypeError):
                        parsed_prices = []
                    if len(parsed_prices) >= 2:
                        sum_op = sum(parsed_prices)
                        if sum_op >= 1.01 or sum_op <= 0.0:
                            continue

            # Fetch books for all outcomes
            books = {}
            for tok_id, _ in tokens_list:
                try:
                    books[tok_id] = self.fetch_book(tok_id)
                except Exception:
                    books[tok_id] = None

            if any(b is None or not b.asks or not b.bids for b in books.values()):
                continue

            sum_asks = sum(float(books[tok_id].asks[0].price) for tok_id, _ in tokens_list)
            if round(sum_asks, 6) < 1.00:
                # Check gates for each leg (ignoring the 0.985 ceiling on low-price legs)
                all_passed = True
                for tok_id, _ in tokens_list:
                    b = books[tok_id]
                    self._record_price_history(tok_id, float(b.asks[0].price))
                    ok, _ = self.check_gates(
                        m, b, price_history=self.price_history_cache.get(tok_id), require_heavy_favorite=False
                    )
                    if not ok:
                        all_passed = False
                        break

                if all_passed:
                    exp_res = str(m.get("endDate") or m.get("end_date_iso") or "")
                    for tok_id, out_name in tokens_list:
                        if tok_id in open_tokens:
                            continue
                        ask_px = float(books[tok_id].asks[0].price)
                        shares = round(order_size_usd / ask_px, 4)
                        tid = self.ledger.record_entry(
                            trade_mode=self.trade_mode,
                            strategy_type="parity_arb",
                            condition_id=cond_id,
                            token_id=tok_id,
                            market_slug=market_slug,
                            question=question,
                            outcome=out_name,
                            side="BUY",
                            entry_price=ask_px,
                            size_usd=order_size_usd,
                            shares=shares,
                            expected_resolution=exp_res,
                            status="FILLED",
                            notes=f"Parity Arb sum_asks={sum_asks:.4f}",
                        )
                        open_tokens.add(tok_id)
                        entered_ids.append(tid)
                    self.logger.info(f"Entered Parity Arb set on {market_slug}: sum_asks={sum_asks:.4f}")

        return entered_ids

    def monitor_positions(self) -> None:
        open_trades = self.ledger.get_open_trades()
        if not open_trades:
            return

        for t in open_trades:
            trade_id = t["id"]
            tok_id = str(t["token_id"])
            market_slug = t["market_slug"]
            outcome = t["outcome"]

            # 1. Secondary CLOB Exit check: Lowered to 0.990
            try:
                book = self.fetch_book(tok_id)
                if book.bids:
                    best_bid = float(book.bids[0].price)
                    if best_bid >= 0.990:
                        self.ledger.record_clob_exit(
                            trade_id=trade_id,
                            exit_price=best_bid,
                            notes=f"Secondary CLOB exit @ {best_bid:.4f} >= 0.990",
                        )
                        self.logger.info(f"Secondary CLOB exit executed for trade {trade_id} @ {best_bid:.4f}")
                        continue
            except Exception as e:
                self.logger.debug(f"Could not fetch book for trade {trade_id}: {e}")

            # 2. Resolution check via Gamma API: Map clobTokenIds to outcomePrices
            try:
                settled_px = None
                url = f"{self.gamma_url}/markets"
                resp = requests.get(url, params={"slug": market_slug}, timeout=10.0)
                if resp.status_code == 200:
                    data = resp.json()
                    m_data = data[0] if (isinstance(data, list) and data) else (data if isinstance(data, dict) else None)
                    if m_data and (m_data.get("closed") or m_data.get("resolved")):
                        clob_ids = m_data.get("clobTokenIds")
                        if isinstance(clob_ids, str):
                            try:
                                clob_ids = json.loads(clob_ids)
                            except Exception:
                                clob_ids = []
                        if not isinstance(clob_ids, list):
                            clob_ids = []

                        outcome_prices = m_data.get("outcomePrices")
                        if isinstance(outcome_prices, str):
                            try:
                                outcome_prices = json.loads(outcome_prices)
                            except Exception:
                                outcome_prices = []
                        if not isinstance(outcome_prices, list):
                            outcome_prices = []

                        for idx, cid in enumerate(clob_ids):
                            if str(cid) == tok_id:
                                if idx < len(outcome_prices):
                                    try:
                                        settled_px = float(outcome_prices[idx])
                                    except (ValueError, TypeError):
                                        settled_px = None
                                break

                        # Fallback to tokens array if clobTokenIds mapping did not find settled_px
                        if settled_px is None:
                            raw_tokens = m_data.get("tokens") or []
                            if isinstance(raw_tokens, list):
                                for tok in raw_tokens:
                                    if isinstance(tok, dict) and str(tok.get("token_id")) == tok_id:
                                        if tok.get("winner") is True:
                                            settled_px = 1.0
                                        elif tok.get("winner") is False:
                                            settled_px = 0.0
                                        else:
                                            try:
                                                settled_px = float(tok.get("price") or 0.0)
                                            except (ValueError, TypeError):
                                                settled_px = None
                                        break

                if settled_px is None and t.get("condition_id"):
                    clob_m_url = f"{self.clob_url}/markets/{t['condition_id']}"
                    c_resp = requests.get(clob_m_url, timeout=10.0)
                    if c_resp.status_code == 200:
                        c_data = c_resp.json()
                        if isinstance(c_data, dict) and (
                            c_data.get("closed")
                            or c_data.get("resolved")
                            or any(isinstance(tok, dict) and tok.get("winner") is True for tok in (c_data.get("tokens") or []))
                        ):
                            for tok in (c_data.get("tokens") or []):
                                if isinstance(tok, dict) and str(tok.get("token_id")) == tok_id:
                                    if tok.get("winner") is True:
                                        settled_px = 1.0
                                    elif tok.get("winner") is False:
                                        settled_px = 0.0
                                    elif tok.get("price") is not None:
                                        settled_px = float(tok.get("price"))
                                    break

                if settled_px is not None:
                    if settled_px >= 0.99:
                        self.ledger.record_resolution(trade_id, outcome_won=True, payout_per_share=1.0, notes="Gamma resolved WIN")
                        self.logger.info(f"Trade {trade_id} ({outcome}) resolved WIN @ $1.00/share")
                    elif settled_px <= 0.01:
                        self.ledger.record_resolution(trade_id, outcome_won=False, payout_per_share=0.0, notes="Gamma resolved LOSS")
                        self.logger.error(f"CIRCUIT BREAKER TRIGGERED: Trade {trade_id} resolved LOSS! Halting scans for 48h.")
            except Exception as e:
                self.logger.debug(f"Could not check resolution for trade {trade_id}: {e}")

    def run_once(self) -> dict[str, Any]:
        is_active, rem_hours = self.ledger.is_circuit_breaker_active(48.0)
        if is_active:
            self.logger.warning(f"Circuit breaker ACTIVE ({rem_hours:.1f} hours remaining). Halting scans.")
            return {"status": "HALTED", "reason": f"Circuit breaker active: {rem_hours:.1f}h remaining"}

        self.monitor_positions()

        # Re-check circuit breaker after position resolution
        is_active, rem_hours = self.ledger.is_circuit_breaker_active(48.0)
        if is_active:
            self.logger.warning(f"Circuit breaker tripped after monitoring ({rem_hours:.1f}h remaining). Halting.")
            return {"status": "HALTED", "reason": "Circuit breaker triggered"}

        markets = self.fetch_public_markets(limit=100)
        hf_trades = self.scan_heavy_favorites(markets)
        parity_trades = self.scan_parity_arbitrage(markets)

        return {
            "status": "OK",
            "heavy_favorites_entered": len(hf_trades),
            "parity_arb_entered": len(parity_trades),
            "open_trades": len(self.ledger.get_open_trades()),
        }

    def run(self, poll_interval_sec: float = 30.0) -> None:
        self.logger.info(f"Starting Satellite Harvester Engine in [{self.trade_mode.upper()}] mode...")
        try:
            while True:
                self.run_once()
                time.sleep(poll_interval_sec)
        except KeyboardInterrupt:
            self.logger.info("Satellite Engine stopped by operator.")

    @staticmethod
    def format_status(ledger: CertaintyLedger, bankroll_usd: float = 1000.0) -> str:
        summary = ledger.get_performance_summary()
        cb_active, rem_hours = ledger.is_circuit_breaker_active(48.0)
        cb_status = f"ACTIVE ({rem_hours:.1f} hours remaining)" if cb_active else "Inactive (Normal)"

        open_trades = ledger.get_open_trades()
        allocated_capital = sum(float(t["size_usd"]) for t in open_trades)
        available_cash = max(0.0, bankroll_usd - allocated_capital)
        utilization_pct = (allocated_capital / bankroll_usd * 100.0) if bankroll_usd > 0 else 0.0

        lines = [
            "=" * 55,
            "     HIGH-CERTAINTY SATELLITE ENGINE STATUS",
            "=" * 55,
            f"DB Path:            {ledger.db_path}",
            f"Circuit Breaker:    {cb_status}",
            f"Virtual Bankroll:   ${bankroll_usd:,.2f}",
            f"Allocated Capital:  ${allocated_capital:,.2f}",
            f"Available Cash:     ${available_cash:,.2f}",
            f"Utilization:        {utilization_pct:.1f}%",
            "-" * 55,
            f"Total Trades:       {summary['total_trades']}",
            f"Open Positions:     {summary['open_trades']}",
            f"Closed Trades:      {summary['closed_trades']}",
            f"Wins:               {summary['wins']}",
            f"Losses:             {summary['losses']}",
            f"Win Rate:           {summary['win_rate'] * 100:.1f}%",
            f"Total PnL:          ${summary['total_pnl']:.4f}",
            f"Sharpe Ratio:       {summary['sharpe_ratio']:.4f}",
            "-" * 55,
            "OPEN PAPER POSITIONS:",
        ]

        if not open_trades:
            lines.append("  (None)")
        else:
            for t in open_trades:
                lines.append(
                    f"  [#{t['id']}] {t['strategy_type']} | {t['outcome']} @ {t['entry_price']:.4f} | "
                    f"Size: ${t['size_usd']:.2f} | Shares: {t['shares']:.2f} | Slug: {t['market_slug'][:30]}"
                )

        lines.append("=" * 55)
        return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Polymarket Satellite Engine Standalone")
    parser.add_argument("--shadow", action="store_true", default=True, help="Shadow mode")
    parser.add_argument("--live", action="store_true", help="Live mode")
    parser.add_argument("--status", action="store_true", help="Show performance status")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    ledger = CertaintyLedger()
    if args.status:
        print(SatelliteEngine.format_status(ledger))
    else:
        mode = "live" if args.live else "shadow"
        engine = SatelliteEngine(ledger=ledger, trade_mode=mode)
        if args.once:
            res = engine.run_once()
            print("Run result:", res)
        else:
            engine.run()
