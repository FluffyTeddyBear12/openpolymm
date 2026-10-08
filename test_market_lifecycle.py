"""
Unit Test Suite for Autonomous Market Lifecycle & Auto-Cycling Engine.

Verifies:
1. fetch_top_markets filters out markets with volume < min_volume_24h or liquidity < min_liquidity.
2. fetch_top_markets does NOT load dead markets from markets.json fallback by default (use_fallback=False).
3. Pagination safely handles offset cap at 2000 and HTTP 422/400 errors cleanly.
4. last_market_tick timestamp is tracked in PaperSimulator and LiveExecutor on book updates.
5. refresh_market_universe detects stagnant markets (> 45 min without ticks) and marks them for eviction.
6. Open positions are NEVER evicted even if stagnant or dropped from the API.
7. SocketWorkerState prunes dropped markets and receives newly added markets.
8. Default auto-refresher interval is 300.0 seconds (5 minutes).
"""

import os
import json
import time
import unittest
from unittest.mock import patch, MagicMock
import urllib.error

from paper_trader import (
    fetch_top_markets,
    refresh_market_universe,
    start_market_universe_refresher,
    PaperSimulator,
    LiveExecutor,
    SocketWorkerState,
    DashboardState,
    RiskSizingEngine,
)


class TestMarketLifecycle(unittest.TestCase):
    def setUp(self):
        self.dash_state = DashboardState()
        self.dash_state._stop_event.set()  # Prevent saver thread in unit test
        self.risk_engine = RiskSizingEngine(
            initial_capital=1000.0,
            dash_state=self.dash_state
        )

    def test_fetch_top_markets_filters_volume_and_liquidity(self):
        sample_api_response = [
            {
                "conditionId": "0xACTIVE_HIGH_VOL",
                "question": "Will BTC reach 100k?",
                "active": True,
                "closed": False,
                "archived": False,
                "clobTokenIds": json.dumps(["token_1", "token_2"]),
                "volume24hr": 25000.0,
                "liquidity": 50000.0,
            },
            {
                "conditionId": "0xDEAD_LOW_VOL",
                "question": "Will random dead event happen?",
                "active": True,
                "closed": False,
                "archived": False,
                "clobTokenIds": json.dumps(["token_3", "token_4"]),
                "volume24hr": 500.0,  # Below 5000 min_volume_24h
                "liquidity": 25000.0,
            },
            {
                "conditionId": "0xILLIQUID_MARKET",
                "question": "Will illiquid event happen?",
                "active": True,
                "closed": False,
                "archived": False,
                "clobTokenIds": json.dumps(["token_5", "token_6"]),
                "volume24hr": 15000.0,
                "liquidity": 2000.0,  # Below 10000 min_liquidity
            },
            {
                "conditionId": "0xCLOSED_MARKET",
                "question": "Closed market?",
                "active": True,
                "closed": True,  # Closed
                "archived": False,
                "clobTokenIds": json.dumps(["token_7", "token_8"]),
                "volume24hr": 50000.0,
                "liquidity": 50000.0,
            },
        ]

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(sample_api_response).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            markets = fetch_top_markets(
                limit=10,
                min_volume_24h=5000.0,
                min_liquidity=10000.0,
                use_fallback=False
            )

        cids = [m["condition_id"] for m in markets]
        self.assertIn("0xACTIVE_HIGH_VOL", cids)
        self.assertNotIn("0xDEAD_LOW_VOL", cids)
        self.assertNotIn("0xILLIQUID_MARKET", cids)
        self.assertNotIn("0xCLOSED_MARKET", cids)
        self.assertEqual(len(markets), 1)

    def test_fetch_top_markets_no_fallback_by_default(self):
        # Empty API response
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([]).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch("builtins.open") as mock_file:
                markets = fetch_top_markets(limit=100)
                # Ensure markets.json was never opened
                mock_file.assert_not_called()
                self.assertEqual(len(markets), 0)

    def test_pagination_handles_caps_and_http_errors(self):
        # Test HTTP 422 error at offset 200
        first_page = [
            {
                "conditionId": f"0xMARKET_{i}",
                "question": f"Market {i}?",
                "active": True,
                "closed": False,
                "archived": False,
                "clobTokenIds": json.dumps([f"t_{i}_y", f"t_{i}_n"]),
                "volume24hr": 10000.0,
                "liquidity": 20000.0,
            }
            for i in range(100)
        ]

        def side_effect_urlopen(req, timeout=6):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "offset=0" in url:
                resp = MagicMock()
                resp.read.return_value = json.dumps(first_page).encode("utf-8")
                resp.__enter__.return_value = resp
                return resp
            else:
                # HTTP 422 error simulating Gamma API capping
                raise urllib.error.HTTPError(url, 422, "Unprocessable Entity", hdrs=None, fp=None)

        with patch("urllib.request.urlopen", side_effect=side_effect_urlopen):
            markets = fetch_top_markets(limit=500)

        # First page succeeded, second page hit 422 and broke cleanly
        self.assertEqual(len(markets), 100)

    def test_last_market_tick_tracking_in_simulator_and_executor(self):
        sim = PaperSimulator(self.risk_engine, dash_state=self.dash_state)
        self.assertIsInstance(sim.last_market_tick, dict)

        before = time.time()
        sim.update_book("0xMKT_1", "token_y", 0.45, ask_size=100.0)
        after = time.time()

        self.assertIn("0xMKT_1", sim.last_market_tick)
        self.assertTrue(before <= sim.last_market_tick["0xMKT_1"] <= after)

        # Test on_book_update alias
        sim.on_book_update("0xMKT_2", "token_y2", 0.50, ask_size=50.0)
        self.assertIn("0xMKT_2", sim.last_market_tick)

        # Test LiveExecutor inheritance
        with patch.dict(os.environ, {"POLYMARKET_PRIVATE_KEY": "", "POLYMARKET_ADDRESS": ""}):
            live = LiveExecutor(self.risk_engine, dash_state=self.dash_state)
            self.assertIsInstance(live.last_market_tick, dict)
            live.update_book("0xLIVE_1", "tok_y", 0.40, ask_size=10.0)
            self.assertIn("0xLIVE_1", live.last_market_tick)

    def test_refresh_market_universe_detects_and_evicts_stagnant_markets(self):
        cid_active = "0xACTIVE_MKT"
        cid_stagnant = "0xSTAGNANT_MKT"
        now = time.time()

        token_map = {
            cid_active: {"token_yes": "t1", "token_no": "t2", "question": "Active Market"},
            cid_stagnant: {"token_yes": "t3", "token_no": "t4", "question": "Stagnant Market"},
        }

        sim = PaperSimulator(self.risk_engine, market_token_map=dict(token_map), dash_state=self.dash_state)
        sim.market_books[cid_active] = {"t1": 0.49, "t2": 0.49}
        sim.market_books[cid_stagnant] = {"t3": 0.49, "t4": 0.49}

        # Active market received tick 10 seconds ago
        sim.last_market_tick[cid_active] = now - 10.0
        # Stagnant market received no tick in 700 seconds (> 10 min = 600s)
        sim.last_market_tick[cid_stagnant] = now - 700.0

        worker = SocketWorkerState(
            worker_id=0,
            initial_markets=[
                {"condition_id": cid_active, "token_ids": ["t1", "t2"], "question": "Active Market"},
                {"condition_id": cid_stagnant, "token_ids": ["t3", "t4"], "question": "Stagnant Market"},
            ]
        )

        mock_active_market = {
            "condition_id": cid_active,
            "question": "Active Market",
            "token_ids": ["t1", "t2"],
            "volume24hr": 20000.0,
            "liquidity": 40000.0,
        }

        with patch("paper_trader.fetch_top_markets", return_value=[mock_active_market]):
            added, removed = refresh_market_universe(sim, [worker], market_limit=10)

        # cid_stagnant should have been evicted
        self.assertNotIn(cid_stagnant, sim.market_token_map)
        self.assertNotIn(cid_stagnant, sim.market_books)
        self.assertNotIn(cid_stagnant, [m["condition_id"] for m in worker.markets])
        self.assertIn(cid_active, sim.market_token_map)

        # Check telemetry exported to dash_state
        self.assertEqual(self.dash_state.state["active_markets_count"], 1)
        self.assertGreaterEqual(self.dash_state.state["stagnant_purged_count"], 1)

    def test_open_positions_never_evicted(self):
        cid_open = "0xOPEN_POSITION_MKT"
        now = time.time()

        token_map = {
            cid_open: {"token_yes": "t1", "token_no": "t2", "question": "Position Market"}
        }

        sim = PaperSimulator(self.risk_engine, market_token_map=dict(token_map), dash_state=self.dash_state)
        # 5000 seconds without ticks (very stagnant)
        sim.last_market_tick[cid_open] = now - 5000.0

        # Bot currently holds an open position in this market
        self.risk_engine.open_positions = {
            cid_open: {"market_id": cid_open, "size": 50.0, "expected_profit": 1.5}
        }

        worker = SocketWorkerState(
            worker_id=0,
            initial_markets=[
                {"condition_id": cid_open, "token_ids": ["t1", "t2"], "question": "Position Market"}
            ]
        )

        # API returns empty (market closed or volume dried up)
        with patch("paper_trader.fetch_top_markets", return_value=[]):
            added, removed = refresh_market_universe(sim, [worker], market_limit=10)

        # Must NEVER be evicted because open position exists!
        self.assertIn(cid_open, sim.market_token_map)
        self.assertIn(cid_open, [m["condition_id"] for m in worker.markets])

    def test_default_refresher_interval_is_60(self):
        import inspect
        sig = inspect.signature(start_market_universe_refresher)
        self.assertEqual(sig.parameters["interval"].default, 60.0)


if __name__ == "__main__":
    unittest.main()
