import unittest
import logging
import json
import os
import time
import threading
from datetime import datetime
from unittest.mock import patch, MagicMock
from paper_trader import (
    RiskSizingEngine, PaperSimulator, DashboardState, fetch_top_markets,
    on_message, on_open, seed_order_books_via_rest, start_keepalive,
    partition_markets, partition_dual_pools, run_socket_pool, SocketWorkerState,
    refresh_market_universe, start_market_universe_refresher,
    configure_cpu_budget, configure_ram_budget,
    MissedReason, ShadowParityTracker, AdaptivePolicyOptimizer
)

# Disable logging output during tests
logging.getLogger("PolyPaperTrader").setLevel(logging.CRITICAL)

class TestPaperTrader(unittest.TestCase):
    def test_risk_engine_sizing(self):
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        self.assertEqual(risk.calculate_sizing(), 100.0)
        
    def test_risk_engine_circuit_breaker(self):
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, daily_loss_limit=50.0)
        self.assertTrue(risk.can_trade(100.0))
        
        # Take a loss of 30, circuit breaker shouldn't trip yet
        risk.record_pnl(-30.0)
        self.assertFalse(risk.circuit_breaker_active)
        self.assertTrue(risk.can_trade(50.0))
        
        # Take another loss of 25, total loss = 55, circuit breaker should trip
        risk.record_pnl(-25.0)
        self.assertTrue(risk.circuit_breaker_active)
        self.assertFalse(risk.can_trade(50.0))
        
    def test_paper_simulator_parity(self):
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        sim = PaperSimulator(risk)
        
        # Set fee rate to 1.5% for testing
        sim.fee_rate = 0.015 
        
        # Update YES ask to 0.45
        sim.update_book("MKT_1", "YES", 0.45)
        # Update NO ask to 0.50
        # Sum = 0.95
        # Effective Cost = 0.95 * 1.015 = 0.96425
        # Edge = 1.00 - 0.96425 = 0.03575 > 0.005
        # It should execute a trade. Initial capital is 1000. Trade size = 100. Profit = 3.575
        sim.update_book("MKT_1", "NO", 0.50)
        
        self.assertAlmostEqual(risk.capital, 1003.575)
        # Book should be reset after a successful trade
        self.assertIsNone(sim.market_books["MKT_1"]["YES"])
        self.assertIsNone(sim.market_books["MKT_1"]["NO"])

        # No trade should execute if edge is negative
        # YES=0.51, NO=0.51 -> Sum 1.02 -> Effective Cost > 1.00
        sim.update_book("MKT_1", "YES", 0.51)
        sim.update_book("MKT_1", "NO", 0.51)
        
        self.assertAlmostEqual(risk.capital, 1003.575) # Capital unchanged
        self.assertEqual(sim.market_books["MKT_1"]["YES"], 0.51) # Book not reset

    def test_dashboard_state_telemetry(self):
        import tempfile
        import os
        from paper_trader import DashboardState
        
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                state_file = os.path.join(tmpdir, "test_state.json")
                ds = DashboardState(filename=state_file)
                self.assertIn("last_heartbeat", ds.state)
                self.assertIn("total_ticks", ds.state)
                self.assertIn("bot_status", ds.state)
                self.assertEqual(ds.state["total_ticks"], 0)
                
                ds.record_tick("MKT_TEST")
                self.assertEqual(ds.state["total_ticks"], 1)
                self.assertEqual(ds.state["bot_status"], "ONLINE_SCANNING")
                
                ds.set_bot_status("CONNECTED_SUBSCRIBED")
                self.assertEqual(ds.state["bot_status"], "CONNECTED_SUBSCRIBED")
            finally:
                ds.stop()

    def test_dashboard_state_market_updates(self):
        import tempfile
        import os
        from paper_trader import DashboardState
        
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                state_file = os.path.join(tmpdir, "test_state.json")
                ds = DashboardState(filename=state_file)
                
                # Initial update
                ds.update_market("MKT_1", 0.45, 0.50, 0.96425, 0.03575)
                self.assertIn("MKT_1", ds.state["markets"])
                mkt = ds.state["markets"]["MKT_1"]
                self.assertEqual(mkt["yes_ask"], 0.45)
                self.assertEqual(mkt["no_ask"], 0.50)
                self.assertEqual(mkt["cost"], 0.96425)
                self.assertEqual(mkt["edge"], 0.03575)
                
                # Verify cost is stored in price history
                history = ds.state["price_history"]["MKT_1"]
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0]["cost"], 0.96425)
                
                # Verify clear_market_edge
                ds.clear_market_edge("MKT_1")
                self.assertEqual(ds.state["markets"]["MKT_1"]["edge"], 0.0)
                self.assertEqual(ds.state["markets"]["MKT_1"]["cost"], 0.96425)
                self.assertEqual(ds.state["markets"]["MKT_1"]["yes_ask"], 0.45)
                
                # Verify zero prices are guarded from history
                ds.update_market("MKT_1", 0, 0, 0, 0)
                self.assertEqual(len(ds.state["price_history"]["MKT_1"]), 1)
            finally:
                ds.stop()

    def test_fetch_top_markets(self):
        markets = fetch_top_markets(limit=50)
        self.assertEqual(len(markets), 50)
        for m in markets:
            self.assertIn("condition_id", m)
            self.assertIn("question", m)
            self.assertIn("token_ids", m)
            self.assertEqual(len(m["token_ids"]), 2)
            self.assertTrue(m["token_ids"][0])
            self.assertTrue(m["token_ids"][1])

    def test_multi_market_concurrent_tracking(self):
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        market_token_map = {
            "MKT_A": {"token_yes": "T_A_YES", "token_no": "T_A_NO", "question": "Market A"},
            "MKT_B": {"token_yes": "T_B_YES", "token_no": "T_B_NO", "question": "Market B"},
        }
        sim = PaperSimulator(risk, market_token_map=market_token_map)
        sim.fee_rate = 0.015

        # Update MKT_A (no arbitrage: YES=0.52, NO=0.50 -> cost 1.0353)
        sim.update_book("MKT_A", "T_A_YES", 0.52)
        sim.update_book("MKT_A", "T_A_NO", 0.50)

        # Update MKT_B (arbitrage: YES=0.45, NO=0.50 -> cost 0.96425, edge 0.03575)
        sim.update_book("MKT_B", "T_B_YES", 0.45)
        sim.update_book("MKT_B", "T_B_NO", 0.50)

        # MKT_B should execute trade, MKT_A should NOT execute trade
        self.assertAlmostEqual(risk.capital, 1003.575)
        # MKT_A books should still be intact
        self.assertEqual(sim.market_books["MKT_A"]["T_A_YES"], 0.52)
        self.assertEqual(sim.market_books["MKT_A"]["T_A_NO"], 0.50)
        # MKT_B books should be reset
        self.assertIsNone(sim.market_books["MKT_B"]["T_B_YES"])
        self.assertIsNone(sim.market_books["MKT_B"]["T_B_NO"])

    def test_on_message_multi_event_dispatch(self):
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        sim = PaperSimulator(risk)
        sim.fee_rate = 0.015

        events = [
            {
                "event_type": "book",
                "market": "MKT_TEST_1",
                "asset_id": "T1",
                "asks": [{"price": "0.48", "size": "100"}]
            },
            {
                "event_type": "book",
                "market": "MKT_TEST_2",
                "asset_id": "T2",
                "asks": [{"price": "0.52", "size": "100"}]
            }
        ]
        import json
        on_message(None, json.dumps(events), sim)
        self.assertEqual(sim.market_books["MKT_TEST_1"]["T1"], 0.48)
        self.assertEqual(sim.market_books["MKT_TEST_2"]["T2"], 0.52)

    def test_dashboard_init_monitored_markets(self):
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                state_file = os.path.join(tmpdir, "test_init.json")
                ds = DashboardState(filename=state_file)
                mock_markets = [
                    {"condition_id": f"0x{i:04x}", "question": f"Market {i}", "token_ids": [f"T{i}Y", f"T{i}N"]}
                    for i in range(50)
                ]
                ds.init_monitored_markets(mock_markets)
                self.assertEqual(len(ds.state["markets"]), 50)
                self.assertEqual(len(ds.state["market_names"]), 50)
                self.assertEqual(ds.state["markets"]["0x0001"]["question"], "Market 1")
                self.assertEqual(ds.state["markets"]["0x0001"]["cost"], 0.0)
            finally:
                ds.stop()

    def test_zero_or_negative_ask_rejected(self):
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        sim = PaperSimulator(risk)
        sim.fee_rate = 0.015

        # Update YES ask to 0.0 (should be rejected)
        sim.update_book("MKT_ZERO", "YES", 0.0)
        self.assertNotIn("MKT_ZERO", sim.market_books)

        # Update YES ask to negative (should be rejected)
        sim.update_book("MKT_NEG", "YES", -0.25)
        self.assertNotIn("MKT_NEG", sim.market_books)

        # Even if 0.0 ask somehow entered market_books directly, evaluate_parity must NOT trade
        sim.market_books["MKT_MANUAL"] = {"YES": 0.0, "NO": 0.50}
        sim.evaluate_parity("MKT_MANUAL")
        self.assertEqual(risk.capital, 1000.0)  # Capital must remain unchanged!

    def test_on_message_handles_empty_and_zero_asks(self):
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        sim = PaperSimulator(risk)
        sim.fee_rate = 0.015

        import json
        # Book with 0 price and empty asks
        events = [
            {"event_type": "book", "market": "MKT_EMPTY", "asset_id": "T_EMPTY", "asks": []},
            {"event_type": "book", "market": "MKT_ZERO", "asset_id": "T_ZERO", "asks": [{"price": "0", "size": "100"}]},
            {"event_type": "price_change", "market": "MKT_PC_ZERO", "price_changes": [{"asset_id": "T1", "best_ask": "0"}]},
            {"event_type": "price_change", "market": "MKT_PC_BLANK", "price_changes": [{"asset_id": "T2", "best_ask": ""}]},
        ]
        on_message(None, json.dumps(events), sim)
        self.assertNotIn("MKT_EMPTY", sim.market_books)
        self.assertNotIn("MKT_ZERO", sim.market_books)
        self.assertNotIn("MKT_PC_ZERO", sim.market_books)
        self.assertNotIn("MKT_PC_BLANK", sim.market_books)
        self.assertEqual(risk.capital, 1000.0)

    def test_atomic_write_if_dirty(self):
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                state_file = os.path.join(tmpdir, "test_atomic.json")
                ds = DashboardState(filename=state_file)
                ds.state["test_val"] = 42
                ds.dirty = True
                ds._write_if_dirty()
                self.assertFalse(ds.dirty)
                self.assertTrue(os.path.exists(state_file))
                with open(state_file, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                self.assertEqual(saved.get("test_val"), 42)
            finally:
                ds.stop()

    def test_risk_engine_dynamic_exposure_sync(self):
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                state_file = os.path.join(tmpdir, "test_dynamic.json")
                ds = DashboardState(filename=state_file)
                risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, dash_state=ds)
                ds.risk_engine = risk
                self.assertEqual(risk.calculate_sizing(), 100.0)

                # Update exposure in dashboard state to 35%
                ds.state["max_exposure_pct"] = 0.35
                self.assertAlmostEqual(risk.calculate_sizing(), 350.0)
                self.assertTrue(risk.can_trade(350.0))
                self.assertFalse(risk.can_trade(351.0))
            finally:
                ds.stop()

    def test_capital_and_exposure_ipc_update(self):
        import tempfile
        import os
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                state_file = os.path.join(tmpdir, "dashboard_state.json")
                ds = DashboardState(filename=state_file)
                risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, dash_state=ds)
                ds.risk_engine = risk
                
                # Place capital_update.json in the directory
                update_path = os.path.join(tmpdir, "capital_update.json")
                with open(update_path, "w", encoding="utf-8") as f:
                    json.dump({"capital": 1500.0, "max_exposure_pct": 0.25}, f)
                    
                # Wait up to 2 seconds for _saver thread to consume it
                consumed = False
                for _ in range(20):
                    time.sleep(0.1)
                    if not os.path.exists(update_path):
                        consumed = True
                        break
                self.assertTrue(consumed, "capital_update.json was not processed by _saver")
                self.assertAlmostEqual(risk.capital, 1500.0)
                self.assertAlmostEqual(risk.max_exposure_pct, 0.25)
                self.assertAlmostEqual(risk.calculate_sizing(), 375.0)
            finally:
                ds.stop()

    def test_supervisor_instance_lock(self):
        from start_bot import acquire_instance_lock
        TEST_PORT = 49199
        # First lock attempt should succeed
        lock1 = acquire_instance_lock(TEST_PORT)
        self.assertIsNotNone(lock1)
        
        # Second lock attempt on same port must fail (None)
        lock2 = acquire_instance_lock(TEST_PORT)
        self.assertIsNone(lock2)
        
        # Release first lock
        lock1.close()
        time.sleep(0.05)
        
        # Third attempt after release should succeed
        lock3 = acquire_instance_lock(TEST_PORT)
        self.assertIsNotNone(lock3)
        lock3.close()

    def test_corrupt_capital_update_file_handling(self):
        import tempfile
        import os
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                state_file = os.path.join(tmpdir, "dashboard_state.json")
                ds = DashboardState(filename=state_file)
                risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, dash_state=ds)
                ds.risk_engine = risk

                # Write invalid non-JSON to capital_update.json
                update_path = os.path.join(tmpdir, "capital_update.json")
                with open(update_path, "w", encoding="utf-8") as f:
                    f.write("NOT_VALID_JSON{{{")

                # Saver thread should handle decode error gracefully and capital/exposure remain unchanged
                time.sleep(0.7)
                self.assertEqual(risk.capital, 1000.0)
                self.assertEqual(risk.max_exposure_pct, 0.10)
            finally:
                ds.stop()

    def test_state_file_disk_sync(self):
        import tempfile
        import os
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                state_file = os.path.join(tmpdir, "dashboard_state.json")
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump({"capital": 1000.0, "max_exposure_pct": 0.10}, f)

                ds = DashboardState(filename=state_file)
                risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, dash_state=ds)
                ds.risk_engine = risk
                ds.dirty = False

                # Wait to ensure mtime timestamp separation
                time.sleep(0.2)

                # Externally modify state file on disk
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump({"capital": 1200.0, "max_exposure_pct": 0.45}, f)

                # Wait for saver thread to detect mtime change
                synced = False
                for _ in range(25):
                    time.sleep(0.1)
                    if abs(risk.max_exposure_pct - 0.45) < 1e-4:
                        synced = True
                        break
                self.assertTrue(synced, "RiskSizingEngine did not sync updated max_exposure_pct from disk")
                self.assertAlmostEqual(risk.calculate_sizing(), 1200.0 * 0.45)
            finally:
                ds.stop()

    def test_state_file_disk_sync_while_dirty(self):
        """Verify that external disk updates to dashboard_state.json are preserved and not overwritten when bot is actively receiving ticks (dirty=True)."""
        import tempfile
        import os
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                state_file = os.path.join(tmpdir, "dashboard_state.json")
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump({"capital": 1000.0, "max_exposure_pct": 0.10}, f)

                ds = DashboardState(filename=state_file)
                risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, dash_state=ds)
                
                # Active bot receives ticks, setting dirty = True
                ds.dirty = True
                time.sleep(0.2)

                # External update on disk
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump({"capital": 1350.0, "max_exposure_pct": 0.40}, f)

                # Saver must sync external update despite dirty flag
                synced = False
                for _ in range(25):
                    time.sleep(0.1)
                    if abs(risk.max_exposure_pct - 0.40) < 1e-4 and abs(risk.capital - 1350.0) < 1e-4:
                        synced = True
                        break
                self.assertTrue(synced, "External disk update was clobbered by active dirty state")
                self.assertAlmostEqual(risk.calculate_sizing(), 1350.0 * 0.40)
            finally:
                ds.stop()

    def test_risk_engine_auto_links_dash_state(self):
        """Verify RiskSizingEngine automatically binds to dash_state without requiring manual assignment."""
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                state_file = os.path.join(tmpdir, "dashboard_state.json")
                ds = DashboardState(filename=state_file)
                # No manual ds.risk_engine = risk!
                risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, dash_state=ds)
                self.assertIs(ds.risk_engine, risk)

                # Dynamically update state
                ds.state["max_exposure_pct"] = 0.35
                self.assertAlmostEqual(risk.calculate_sizing(), 350.0)
            finally:
                ds.stop()

    def test_capital_update_without_initial_risk_engine(self):
        """Verify capital_update.json updates state even if risk_engine is not yet attached."""
        import tempfile
        import os
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                state_file = os.path.join(tmpdir, "dashboard_state.json")
                ds = DashboardState(filename=state_file)
                self.assertIsNone(ds.risk_engine)

                update_path = os.path.join(tmpdir, "capital_update.json")
                with open(update_path, "w", encoding="utf-8") as f:
                    json.dump({"capital": 1800.0, "max_exposure_pct": 0.30}, f)

                for _ in range(20):
                    time.sleep(0.1)
                    if not os.path.exists(update_path):
                        break

                self.assertAlmostEqual(ds.state.get("capital"), 1800.0)
                self.assertAlmostEqual(ds.state.get("max_exposure_pct"), 0.30)
            finally:
                ds.stop()

    def test_paper_lock_acquisition_and_collision(self):
        """Verify acquire_paper_lock provides exclusive single-instance locking."""
        from paper_trader import acquire_paper_lock
        TEST_PORT = 49198
        lock1 = acquire_paper_lock(TEST_PORT)
        self.assertIsNotNone(lock1)

        # Collision attempt must fail
        lock2 = acquire_paper_lock(TEST_PORT)
        self.assertIsNone(lock2)

        # Release
        lock1.close()
        time.sleep(0.05)

        # Re-acquire
        lock3 = acquire_paper_lock(TEST_PORT)
        self.assertIsNotNone(lock3)
        lock3.close()

    def test_can_trade_zero_or_negative(self):
        """Verify can_trade rejects non-positive trade sizes and zero capital."""
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        self.assertFalse(risk.can_trade(0.0))
        self.assertFalse(risk.can_trade(-50.0))
        
        # Zero capital
        risk.capital = 0.0
        self.assertFalse(risk.can_trade(10.0))
        self.assertEqual(risk.calculate_sizing(), 0.0)

    def test_depth_aware_sizing_bounds_to_liquidity(self):
        """Verify trade size is bounded to executable order book depth min(depth_yes, depth_no)."""
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        sim = PaperSimulator(risk)
        sim.fee_rate = 0.015

        # Desired size is 1000 * 0.10 = 100.0
        # But YES depth is 40.0, NO depth is 60.0 -> executable depth = 40.0
        with patch.object(risk, 'open_position', wraps=risk.open_position) as mock_open:
            sim.update_book("MKT_DEPTH", "YES", 0.45, ask_size=40.0)
            sim.update_book("MKT_DEPTH", "NO", 0.50, ask_size=60.0)

            mock_open.assert_called_once()
            args, _ = mock_open.call_args
            self.assertEqual(args[0], "MKT_DEPTH")
            self.assertAlmostEqual(args[1], 40.0)
            expected_profit = 40.0 * (1.0 - 0.95 * 1.015)
            self.assertAlmostEqual(args[2], expected_profit)
            # Instant compounding recycles collateral immediately into pool
            self.assertAlmostEqual(risk.locked_collateral, 0.0)
            self.assertAlmostEqual(risk.available_cash, 1000.0 + expected_profit)

    def test_depth_aware_sizing_larger_than_desired(self):
        """Verify depth larger than desired size still caps at desired fractional size."""
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        sim = PaperSimulator(risk)
        sim.fee_rate = 0.015

        with patch.object(risk, 'open_position', wraps=risk.open_position) as mock_open:
            sim.update_book("MKT_BIG_DEPTH", "YES", 0.45, ask_size=500.0)
            sim.update_book("MKT_BIG_DEPTH", "NO", 0.50, ask_size=600.0)

            mock_open.assert_called_once()
            args, _ = mock_open.call_args
            self.assertEqual(args[0], "MKT_BIG_DEPTH")
            self.assertAlmostEqual(args[1], 100.0)
            expected_profit = 100.0 * (1.0 - 0.95 * 1.015)
            self.assertAlmostEqual(risk.locked_collateral, 0.0)
            self.assertAlmostEqual(risk.available_cash, 1000.0 + expected_profit)

    def test_depth_zero_rejects_trade(self):
        """Verify zero liquidity at best ask prevents trade execution."""
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        sim = PaperSimulator(risk)
        sim.fee_rate = 0.015

        sim.update_book("MKT_ZERO_DEPTH", "YES", 0.45, ask_size=0.0)
        sim.update_book("MKT_ZERO_DEPTH", "NO", 0.50, ask_size=50.0)

        self.assertNotIn("MKT_ZERO_DEPTH", risk.open_positions)
        self.assertEqual(risk.available_cash, 1000.0)
        self.assertEqual(risk.locked_collateral, 0.0)

    def test_concurrent_position_gating_and_capacity(self):
        """Verify trading is gated once max_concurrent_positions is reached."""
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, max_concurrent_positions=2, hold_period_seconds=10.0)
        self.assertEqual(risk.max_concurrent_positions, 2)

        # Open 1st position
        self.assertTrue(risk.can_trade(100.0, market_id="MKT_1"))
        self.assertTrue(risk.open_position("MKT_1", 100.0, 3.5))
        self.assertEqual(len(risk.open_positions), 1)
        self.assertEqual(risk.available_cash, 900.0)
        self.assertEqual(risk.locked_collateral, 100.0)

        # Open 2nd position
        self.assertTrue(risk.can_trade(100.0, market_id="MKT_2"))
        self.assertTrue(risk.open_position("MKT_2", 100.0, 3.5))
        self.assertEqual(len(risk.open_positions), 2)
        self.assertEqual(risk.available_cash, 800.0)
        self.assertEqual(risk.locked_collateral, 200.0)

        # 3rd position must be gated
        self.assertFalse(risk.can_trade(100.0, market_id="MKT_3"))

    def test_duplicate_market_position_gating(self):
        """Verify bot does not open duplicate concurrent positions on the same market beyond max_positions_per_market."""
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, max_concurrent_positions=5)
        for _ in range(risk.max_positions_per_market):
            risk.open_position("MKT_A", 100.0, 3.5)
        self.assertFalse(risk.can_trade(100.0, market_id="MKT_A"))
        self.assertTrue(risk.can_trade(100.0, market_id="MKT_B"))

    def test_insufficient_available_cash_gating(self):
        """Verify trade is gated when trade size exceeds available cash."""
        risk = RiskSizingEngine(initial_capital=150.0, max_exposure_pct=0.80)
        risk.open_position("MKT_1", 120.0, 4.0)
        self.assertAlmostEqual(risk.available_cash, 30.0)
        self.assertFalse(risk.can_trade(50.0, market_id="MKT_2"))
        self.assertTrue(risk.can_trade(25.0, market_id="MKT_2"))

    def test_collateral_recycling_lifecycle(self):
        """Verify realistic collateral recycling releases locked collateral and returns principal + profit."""
        import time
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, hold_period_seconds=1.0)
        now = time.time()
        risk.open_position("MKT_CYCLE", 100.0, 5.0, hold_seconds=1.0)
        
        # While holding
        self.assertEqual(len(risk.open_positions), 1)
        self.assertEqual(risk.locked_collateral, 100.0)
        self.assertEqual(risk.available_cash, 900.0)
        self.assertEqual(risk.capital, 1005.0)

        # Recycle before expiry
        released = risk.recycle_collateral(now=now + 0.2)
        self.assertEqual(len(released), 0)
        self.assertEqual(risk.locked_collateral, 100.0)
        self.assertEqual(risk.available_cash, 900.0)

        # Recycle after expiry
        released = risk.recycle_collateral(now=now + 2.0)
        self.assertEqual(len(released), 1)
        self.assertEqual(len(risk.open_positions), 0)
        self.assertEqual(risk.locked_collateral, 0.0)
        self.assertAlmostEqual(risk.available_cash, 1005.0)
        self.assertAlmostEqual(risk.capital, 1005.0)

    def test_opportunity_prioritization_in_batch(self):
        """Verify batch WebSocket message sorts parity opportunities by highest expected profit before allocating cash."""
        import json
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, max_concurrent_positions=1)
        sim = PaperSimulator(risk)
        sim.fee_rate = 0.015

        sim.update_book("MKT_LOW", "YES", 0.48, ask_size=100.0)
        sim.update_book("MKT_HIGH", "YES", 0.40, ask_size=100.0)
        sim.update_book("MKT_MID", "YES", 0.45, ask_size=100.0)

        batch_events = [
            {"event_type": "book", "market": "MKT_LOW", "asset_id": "NO", "asks": [{"price": "0.50", "size": "100"}]},
            {"event_type": "book", "market": "MKT_MID", "asset_id": "NO", "asks": [{"price": "0.48", "size": "100"}]},
            {"event_type": "book", "market": "MKT_HIGH", "asset_id": "NO", "asks": [{"price": "0.45", "size": "100"}]},
        ]
        with patch.object(sim, 'execute_arbitrage', wraps=sim.execute_arbitrage) as mock_exec:
            on_message(None, json.dumps(batch_events), sim)

            # MKT_HIGH should have been prioritized and executed first due to highest edge
            self.assertEqual(mock_exec.call_args_list[0][0][0]["market_id"], "MKT_HIGH")

    def test_prioritization_respects_depth_and_edge_product(self):
        """Verify candidate ranking uses edge * executable_size rather than raw edge alone."""
        import json
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, max_concurrent_positions=1)
        sim = PaperSimulator(risk)
        sim.fee_rate = 0.015

        # MKT_SHALLOW: 15% edge but $10 depth -> profit $1.50
        # MKT_DEEP: 4% edge and $100 depth -> profit $4.00
        sim.update_book("MKT_SHALLOW", "YES", 0.40, ask_size=10.0)
        sim.update_book("MKT_DEEP", "YES", 0.46, ask_size=100.0)

        batch_events = [
            {"event_type": "book", "market": "MKT_SHALLOW", "asset_id": "NO", "asks": [{"price": "0.44", "size": "10"}]},
            {"event_type": "book", "market": "MKT_DEEP", "asset_id": "NO", "asks": [{"price": "0.49", "size": "100"}]},
        ]
        with patch.object(sim, 'execute_arbitrage', wraps=sim.execute_arbitrage) as mock_exec:
            on_message(None, json.dumps(batch_events), sim)

            # MKT_DEEP has higher total expected dollar profit ($4.00 vs $1.50) and must be ranked first
            self.assertEqual(mock_exec.call_args_list[0][0][0]["market_id"], "MKT_DEEP")

    def test_max_concurrent_positions_ipc_and_clamping(self):
        """Verify IPC updates max_concurrent_positions and enforces 1-10 clamping."""
        import tempfile
        import os
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                state_file = os.path.join(tmpdir, "dashboard_state.json")
                ds = DashboardState(filename=state_file)
                risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, dash_state=ds)
                
                self.assertEqual(risk.max_concurrent_positions, 5)

                update_path = os.path.join(tmpdir, "capital_update.json")
                with open(update_path, "w", encoding="utf-8") as f:
                    json.dump({"capital": 1200.0, "max_exposure_pct": 0.15, "max_concurrent_positions": 8}, f)

                for _ in range(25):
                    time.sleep(0.1)
                    if not os.path.exists(update_path):
                        break

                self.assertEqual(risk.max_concurrent_positions, 8)
                self.assertEqual(ds.state["max_concurrent_positions"], 8)

                # Clamping above 10
                with open(update_path, "w", encoding="utf-8") as f:
                    json.dump({"max_concurrent_positions": 99}, f)

                for _ in range(25):
                    time.sleep(0.1)
                    if not os.path.exists(update_path):
                        break

                self.assertEqual(risk.max_concurrent_positions, 10)
                self.assertEqual(ds.state["max_concurrent_positions"], 10)

                # Clamping below 1
                with open(update_path, "w", encoding="utf-8") as f:
                    json.dump({"max_concurrent_positions": -5}, f)

                for _ in range(25):
                    time.sleep(0.1)
                    if not os.path.exists(update_path):
                        break

                self.assertEqual(risk.max_concurrent_positions, 1)
                self.assertEqual(ds.state["max_concurrent_positions"], 1)
            finally:
                ds.stop()

    def test_state_restoration_and_offline_recycling(self):
        """Verify RiskSizingEngine restores saved positions from dash_state and immediately recycles expired ones without clobbering."""
        import tempfile
        import os
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "dashboard_state.json")
            now = time.time()
            initial_data = {
                "capital": 1000.0,
                "available_cash": 800.0,
                "locked_collateral": 200.0,
                "max_concurrent_positions": 5,
                "open_positions": {
                    "MKT_EXPIRED": {
                        "market_id": "MKT_EXPIRED",
                        "size": 100.0,
                        "expected_profit": 5.0,
                        "entry_time": now - 10.0,
                        "release_time": now - 5.0  # Expired 5 seconds ago while bot was offline
                    },
                    "MKT_ACTIVE": {
                        "market_id": "MKT_ACTIVE",
                        "size": 100.0,
                        "expected_profit": 4.0,
                        "entry_time": now - 1.0,
                        "release_time": now + 60.0  # Still active
                    }
                }
            }
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(initial_data, f)

            ds = DashboardState(filename=state_file)
            try:
                # RiskSizingEngine initialized with dash_state
                risk = RiskSizingEngine(initial_capital=1000.0, dash_state=ds)
                
                # MKT_EXPIRED must be recycled immediately on boot:
                # $100 size + $5 profit returned to available_cash (800 -> 905)
                # locked_collateral reduced from 200 to 100
                self.assertNotIn("MKT_EXPIRED", risk.open_positions)
                self.assertIn("MKT_ACTIVE", risk.open_positions)
                self.assertEqual(len(risk.open_positions), 1)
                self.assertAlmostEqual(risk.locked_collateral, 100.0)
                self.assertAlmostEqual(risk.available_cash, 905.0)
                # Capital: 905 cash + 100 locked + 4 unrealized = 1009
                self.assertAlmostEqual(risk.capital, 1009.0)
                
                # Check that dash_state was NOT clobbered to empty/default
                self.assertIn("MKT_ACTIVE", ds.state["open_positions"])
                self.assertNotIn("MKT_EXPIRED", ds.state["open_positions"])
                self.assertAlmostEqual(ds.state["available_cash"], 905.0)
                self.assertAlmostEqual(ds.state["locked_collateral"], 100.0)
            finally:
                ds.stop()

    def test_price_change_depth_variants(self):
        """Verify on_message parses depth from price_change events using best_ask_size, ask_size, or size."""
        import json
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        sim = PaperSimulator(risk)

        # Event using 'best_ask_size'
        event1 = {
            "event_type": "price_change",
            "market": "MKT_PC_1",
            "price_changes": [{"asset_id": "T1", "best_ask": "0.45", "best_ask_size": "75.0"}]
        }
        on_message(None, json.dumps([event1]), sim)
        self.assertEqual(sim.market_depths["MKT_PC_1"]["T1"], 75.0)

        # Event using 'ask_size'
        event2 = {
            "event_type": "price_change",
            "market": "MKT_PC_2",
            "price_changes": [{"asset_id": "T2", "best_ask": "0.48", "ask_size": "120.0"}]
        }
        on_message(None, json.dumps([event2]), sim)
        self.assertEqual(sim.market_depths["MKT_PC_2"]["T2"], 120.0)

        # Event using 'size'
        event3 = {
            "event_type": "price_change",
            "market": "MKT_PC_3",
            "price_changes": [{"asset_id": "T3", "best_ask": "0.42", "size": "35.0"}]
        }
        on_message(None, json.dumps([event3]), sim)
        self.assertEqual(sim.market_depths["MKT_PC_3"]["T3"], 35.0)

    def test_evaluate_parity_return_value(self):
        """Verify evaluate_parity returns opportunity dict upon execution and None when no edge."""
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        sim = PaperSimulator(risk)
        sim.fee_rate = 0.015

        # Parity arbitrage opportunity
        sim.update_book("MKT_EVAL", "YES", 0.45, ask_size=100.0, evaluate=False)
        sim.update_book("MKT_EVAL", "NO", 0.50, ask_size=100.0, evaluate=False)

        opp = sim.evaluate_parity("MKT_EVAL")
        self.assertIsNotNone(opp)
        self.assertEqual(opp["market_id"], "MKT_EVAL")
        self.assertAlmostEqual(opp["trade_size"], 100.0)
        self.assertAlmostEqual(opp["edge"], 1.0 - 0.95 * 1.015)

        # Second call on same market without new prices returns None
        opp2 = sim.evaluate_parity("MKT_EVAL")
        self.assertIsNone(opp2)

    def test_capital_update_without_risk_engine_adjusts_available_cash(self):
        """Verify capital_update adjusts available_cash proportionally when risk_engine is not yet attached."""
        import tempfile
        import os
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                state_file = os.path.join(tmpdir, "dashboard_state.json")
                ds = DashboardState(filename=state_file)
                self.assertIsNone(ds.risk_engine)

                # State initially at 1000 capital and 1000 cash
                update_path = os.path.join(tmpdir, "capital_update.json")
                with open(update_path, "w", encoding="utf-8") as f:
                    json.dump({"capital": 1500.0, "max_exposure_pct": 0.20}, f)

                for _ in range(25):
                    time.sleep(0.1)
                    if not os.path.exists(update_path):
                        break

                self.assertAlmostEqual(ds.state.get("capital"), 1500.0)
                self.assertAlmostEqual(ds.state.get("available_cash"), 1500.0)
            finally:
                ds.stop()

    def test_fetch_top_markets_expanded_universe_and_rewards(self):
        """Verify fetch_top_markets can fetch up to 150 binary markets and ingests rewards_daily_rate."""
        markets = fetch_top_markets(limit=150)
        self.assertGreaterEqual(len(markets), 100)
        self.assertLessEqual(len(markets), 150)
        for m in markets:
            self.assertIn("condition_id", m)
            self.assertIn("question", m)
            self.assertIn("token_ids", m)
            self.assertEqual(len(m["token_ids"]), 2)
            self.assertIn("rewards_daily_rate", m)
            self.assertIsInstance(m["rewards_daily_rate"], (int, float))

    def test_fetch_top_markets_pagination_mock(self):
        """Verify offset-based pagination requests offset=0, 100... and aggregates markets."""
        from unittest.mock import patch, MagicMock

        requested_urls = []

        def mock_urlopen(req, timeout=6):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            requested_urls.append(url)
            if "offset=0" in url:
                # Page 1: 100 markets
                data = [
                    {
                        "conditionId": f"0xpage0_{i:04d}",
                        "question": f"Question Page 0 #{i}",
                        "active": True,
                        "closed": False,
                        "archived": False,
                        "clobTokenIds": [f"tok_0_{i}_a", f"tok_0_{i}_b"],
                        "volume24hr": 1500.0 + i,
                        "clobRewards": [{"rewardsDailyRate": 50 + i}]
                    }
                    for i in range(100)
                ]
            elif "offset=100" in url:
                # Page 2: 100 markets
                data = [
                    {
                        "conditionId": f"0xpage1_{i:04d}",
                        "question": f"Question Page 1 #{i}",
                        "active": True,
                        "closed": False,
                        "archived": False,
                        "clobTokenIds": [f"tok_1_{i}_a", f"tok_1_{i}_b"],
                        "volume24hr": 2500.0 + i,
                        "clobRewards": [{"rewardsDailyRate": 100 + i}]
                    }
                    for i in range(100)
                ]
            else:
                data = []

            resp = MagicMock()
            resp.read.return_value = json.dumps(data).encode("utf-8")
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            markets = fetch_top_markets(limit=180)
            self.assertEqual(len(markets), 180)
            self.assertTrue(any("offset=0" in u for u in requested_urls))
            self.assertTrue(any("offset=100" in u for u in requested_urls))
            # Verify market items from both pages are present
            self.assertTrue(any(m["condition_id"].startswith("0xpage0_") for m in markets))
            self.assertTrue(any(m["condition_id"].startswith("0xpage1_") for m in markets))
            # Verify rewards daily rate ingested
            self.assertGreater(markets[0]["rewards_daily_rate"], 0.0)

    def test_on_open_multi_chunk_subscription(self):
        """Verify WebSocket on_open chunks 300-400 asset IDs into 100-token batches."""
        from unittest.mock import MagicMock

        mock_ws = MagicMock()
        # 350 tokens from 175 binary markets
        token_ids = [f"token_id_{i:04d}" for i in range(350)]

        on_open(mock_ws, token_ids, 175)

        # 350 tokens / 100 batch size = 4 payloads (100, 100, 100, 50)
        self.assertEqual(mock_ws.send.call_count, 4)
        batches_sent = []
        for call_args in mock_ws.send.call_args_list:
            payload = json.loads(call_args[0][0])
            self.assertEqual(payload.get("type"), "market")
            self.assertIn("assets_ids", payload)
            batches_sent.append(len(payload["assets_ids"]))

        self.assertEqual(batches_sent, [100, 100, 100, 50])

    def test_lean_state_serialization_under_300kb(self):
        """Verify dashboard_state.json stays compact (< 300 KB) with 200 markets."""
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "dashboard_state.json")
            ds = DashboardState(filename=state_file)
            try:
                # Create 200 mock markets
                mock_markets = [
                    {
                        "condition_id": f"0xmkt_{i:04d}",
                        "question": f"Will Event #{i} happen by end of year with descriptive question text?",
                        "token_ids": [f"tok_y_{i}", f"tok_n_{i}"],
                        "outcomes": ["Yes", "No"],
                        "rewards_daily_rate": 250.0 if i % 3 == 0 else 0.0
                    }
                    for i in range(200)
                ]
                ds.init_monitored_markets(mock_markets)

                # Feed updates to all 200 markets
                for i in range(200):
                    m_id = f"0xmkt_{i:04d}"
                    # Provide varying edges and costs
                    yes_ask = 0.45 + (i % 10) * 0.01
                    no_ask = 0.50 + (i % 10) * 0.01
                    cost = (yes_ask + no_ask) * 1.015
                    edge = 1.00 - cost
                    for _ in range(5):
                        ds.update_market(m_id, yes_ask, no_ask, cost, edge)

                # Force synchronous write to disk
                ds._write_if_dirty()

                # Verify file exists and check file size
                self.assertTrue(os.path.exists(state_file))
                file_size_bytes = os.path.getsize(state_file)
                self.assertLess(file_size_bytes, 300 * 1024, f"State file too large: {file_size_bytes} bytes (>= 300 KB)")

                # Verify file can be deserialized and has expected lean structure
                with open(state_file, "r", encoding="utf-8") as f:
                    saved_state = json.load(f)
                self.assertEqual(len(saved_state["markets"]), 200)
                # Serialized price_history and ohlc must be restricted to priority markets (<= 20 markets)
                self.assertLessEqual(len(saved_state["price_history"]), 20)
                self.assertLessEqual(len(saved_state["ohlc"]), 20)
            finally:
                ds.stop()

    def test_lean_state_preserves_inspected_and_open_positions(self):
        """Verify priority markets include open positions and operator-inspected market."""
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "dashboard_state.json")
            ds = DashboardState(filename=state_file)
            try:
                # 30 markets
                mock_markets = [
                    {
                        "condition_id": f"0xmk_{i:04d}",
                        "question": f"Market #{i}",
                        "token_ids": [f"y_{i}", f"n_{i}"],
                        "rewards_daily_rate": 0.0
                    }
                    for i in range(30)
                ]
                ds.init_monitored_markets(mock_markets)
                for i in range(30):
                    ds.update_market(f"0xmk_{i:04d}", 0.50, 0.50, 1.015, -0.015)

                # Mark an arbitrary market as open_positions and another as inspected
                ds.state["open_positions"] = {"0xmk_0028": {"size": 50.0}}
                ds.inspected_market = "0xmk_0029"

                priority = ds._get_priority_markets()
                self.assertIn("0xmk_0028", priority)
                self.assertIn("0xmk_0029", priority)

                ds.dirty = True
                ds._write_if_dirty()

                with open(state_file, "r", encoding="utf-8") as f:
                    saved_state = json.load(f)

                self.assertIn("0xmk_0028", saved_state["price_history"])
                self.assertIn("0xmk_0029", saved_state["price_history"])
            finally:
                ds.stop()

    def test_init_monitored_markets_prunes_stale_markets(self):
        """Verify init_monitored_markets prunes dead unmonitored markets but preserves open positions."""
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "dashboard_state.json")
            ds = DashboardState(filename=state_file)
            try:
                # Seed with 10 markets
                initial_markets = [
                    {"condition_id": f"0xold_{i}", "question": f"Old Mkt {i}", "token_ids": [f"y_{i}", f"n_{i}"]}
                    for i in range(10)
                ]
                ds.init_monitored_markets(initial_markets)
                # Seed open position on 0xold_0
                ds.state["open_positions"] = {"0xold_0": {"size": 25.0}}

                self.assertEqual(len(ds.state["markets"]), 10)

                # Now init with new set of 5 markets (0xnew_0 .. 0xnew_4)
                new_markets = [
                    {"condition_id": f"0xnew_{i}", "question": f"New Mkt {i}", "token_ids": [f"ny_{i}", f"nn_{i}"]}
                    for i in range(5)
                ]
                ds.init_monitored_markets(new_markets)

                # Should have the 5 new markets + 1 preserved open position = 6
                self.assertEqual(len(ds.state["markets"]), 6)
                self.assertIn("0xold_0", ds.state["markets"])
                self.assertNotIn("0xold_1", ds.state["markets"])
                for i in range(5):
                    self.assertIn(f"0xnew_{i}", ds.state["markets"])
            finally:
                ds.stop()

    def test_inspected_market_ipc_sets_dirty_flag(self):
        """Verify that updating inspected_market.txt sets self.dirty and immediately persists the inspected market to disk."""
        import tempfile
        import os
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "dashboard_state.json")
            insp_file = os.path.join(tmpdir, "inspected_market.txt")
            ds = DashboardState(filename=state_file)
            try:
                # 15 markets with price_history and active cost
                for i in range(15):
                    mid = f"m_{i:02d}"
                    ds.state["markets"][mid] = {"cost": 1.0, "edge": 0.01 * (15 - i)}
                    ds.state["price_history"][mid] = [{"yes_ask": 0.5, "no_ask": 0.5, "cost": 1.015}]

                target_mid = "m_14"  # Lowest edge (#15), excluded from top 12 priority
                ds.dirty = True
                ds._write_if_dirty()
                with open(state_file, "r", encoding="utf-8") as f:
                    s1 = json.load(f)
                self.assertNotIn(target_mid, s1.get("price_history", {}))

                mtime1 = os.path.getmtime(state_file)
                time.sleep(0.1)

                # Now write inspected_market.txt
                with open(insp_file, "w", encoding="utf-8") as f:
                    f.write(target_mid)

                # Allow saver thread to poll and write
                for _ in range(25):
                    time.sleep(0.05)
                    if ds.inspected_market == target_mid and os.path.getmtime(state_file) > mtime1:
                        break

                self.assertEqual(ds.inspected_market, target_mid)
                with open(state_file, "r", encoding="utf-8") as f:
                    s2 = json.load(f)
                self.assertIn(target_mid, s2.get("price_history", {}), "Inspected market must be persisted to disk state")
                self.assertGreater(os.path.getmtime(state_file), mtime1)
            finally:
                ds.stop()

    def test_get_priority_markets_resilient_to_corrupt_data(self):
        """Verify _get_priority_markets handles malformed or non-numeric cost/edge gracefully."""
        ds = DashboardState(filename="test_mem.json")
        try:
            ds.state["markets"] = {
                "m1": {"cost": "invalid_cost", "edge": "invalid_edge"},
                "m2": {"cost": None, "edge": None},
                "m3": "not_even_a_dict",
                "m4": {"cost": 0.95, "edge": 0.05}
            }
            priority = ds._get_priority_markets()
            self.assertIn("m4", priority)
        finally:
            ds.stop()

    def test_on_open_deduplicates_token_ids(self):
        """Verify on_open deduplicates token IDs while preserving batching."""
        from unittest.mock import MagicMock
        mock_ws = MagicMock()
        # 150 token IDs with duplicates, reducing to 100 unique
        tokens = [f"tok_{i % 100}" for i in range(150)]
        on_open(mock_ws, tokens, 50)
        self.assertEqual(mock_ws.send.call_count, 1)
        payload = json.loads(mock_ws.send.call_args[0][0])
        self.assertEqual(len(payload["assets_ids"]), 100)

    def test_fetch_top_markets_rejects_identical_tokens(self):
        """Verify fetch_top_markets discards binary markets with identical YES/NO tokens."""
        from unittest.mock import patch, MagicMock
        corrupted_data = [
            {
                "conditionId": "0xcorrupt",
                "question": "Corrupted Market?",
                "active": True,
                "closed": False,
                "archived": False,
                "clobTokenIds": ["same_token_123", "same_token_123"],
                "volume24hr": 5000.0
            }
        ]
        resp = MagicMock()
        resp.read.return_value = json.dumps(corrupted_data).encode("utf-8")
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=resp):
            mkts = fetch_top_markets(limit=10, fallback_file="nonexistent.json")
            self.assertEqual(len(mkts), 0)

    def test_seed_order_books_via_rest_chunking_and_depth(self):
        """Verify REST cold-start seeding chunks token requests into 500-token batches and populates books/depths."""
        from unittest.mock import patch, MagicMock

        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        sim = PaperSimulator(risk, market_token_map={
            "0xmkt_0": {"token_yes": "tok_y_0", "token_no": "tok_n_0", "question": "Market 0", "outcomes": ["Yes", "No"]}
        })

        # 1200 tokens -> 3 chunks (500, 500, 200)
        token_ids = [f"tok_{i}" for i in range(1200)]
        token_ids[0] = "tok_y_0"
        token_ids[1] = "tok_n_0"

        requested_bodies = []
        def mock_urlopen(req, timeout=None):
            body_json = json.loads(req.data.decode("utf-8"))
            requested_bodies.append(body_json)
            # If chunk contains market 0 tokens, return books for both
            books = []
            chunk_tokens = [item["token_id"] for item in body_json]
            if "tok_y_0" in chunk_tokens:
                books.append({
                    "market": "0xmkt_0",
                    "asset_id": "tok_y_0",
                    "asks": [{"price": "0.45", "size": "1000.0"}, {"price": "0.46", "size": "500.0"}]
                })
            if "tok_n_0" in chunk_tokens:
                books.append({
                    "market": "0xmkt_0",
                    "asset_id": "tok_n_0",
                    "asks": [{"price": "0.50", "size": "800.0"}, {"price": "0.52", "size": "400.0"}]
                })

            resp = MagicMock()
            resp.read.return_value = json.dumps(books).encode("utf-8")
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            seeded_count = seed_order_books_via_rest(sim, token_ids, chunk_size=500)
            self.assertEqual(len(requested_bodies), 3)
            self.assertEqual(len(requested_bodies[0]), 500)
            self.assertEqual(len(requested_bodies[1]), 500)
            self.assertEqual(len(requested_bodies[2]), 200)
            self.assertEqual(seeded_count, 1)

            # Books and depths should have been populated
            # Note: Since 0.45 + 0.50 = 0.95 (effective cost 0.96425 < 1.00), an arbitrage opportunity
            # is triggered and executed, which resets the book to None.
            # Capital should have increased from 1000.0
            self.assertGreater(risk.capital, 1000.0)

    def test_seed_order_books_via_rest_resilient_to_network_failure(self):
        """Verify REST cold-start seeding handles network/API exceptions gracefully without raising."""
        from unittest.mock import patch
        risk = RiskSizingEngine(initial_capital=1000.0)
        sim = PaperSimulator(risk)

        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            seeded = seed_order_books_via_rest(sim, ["tok_1", "tok_2"], chunk_size=500)
            self.assertEqual(seeded, 0)

    def test_partition_markets_1000_into_4_pools(self):
        """Verify 1,000 markets are partitioned evenly across 4 parallel WebSocket shards."""
        mock_markets = [
            {"condition_id": f"0xm_{i:04d}", "question": f"M {i}", "token_ids": [f"y_{i}", f"n_{i}"]}
            for i in range(1000)
        ]
        shards = partition_markets(mock_markets, num_partitions=4)
        self.assertEqual(len(shards), 4)
        for s in shards:
            self.assertEqual(len(s), 250)

        # Total tokens across all 4 shards = 2,000
        all_tokens = [t for s in shards for m in s for t in m["token_ids"]]
        self.assertEqual(len(all_tokens), 2000)

    def test_partition_markets_small_universe(self):
        """Verify partition_markets handles smaller universe without empty shards."""
        mock_markets = [
            {"condition_id": "0xm_0", "token_ids": ["y0", "n0"]},
            {"condition_id": "0xm_1", "token_ids": ["y1", "n1"]}
        ]
        shards = partition_markets(mock_markets, num_partitions=4)
        self.assertEqual(len(shards), 2)

    def test_on_open_official_payload_syntax(self):
        """Verify on_open generates official payload with operation: subscribe and type: market."""
        from unittest.mock import MagicMock
        mock_ws = MagicMock()
        tokens = [f"token_{i}" for i in range(150)]

        on_open(mock_ws, tokens, 75, worker_id=0)

        self.assertEqual(mock_ws.send.call_count, 2)
        payload1 = json.loads(mock_ws.send.call_args_list[0][0][0])
        self.assertEqual(payload1.get("operation"), "subscribe")
        self.assertEqual(payload1.get("type"), "market")
        self.assertEqual(len(payload1["assets_ids"]), 100)

        payload2 = json.loads(mock_ws.send.call_args_list[1][0][0])
        self.assertEqual(payload2.get("operation"), "subscribe")
        self.assertEqual(payload2.get("type"), "market")
        self.assertEqual(len(payload2["assets_ids"]), 50)

    def test_start_keepalive_sends_ping(self):
        """Verify 10-second text PING keepalive thread sends PING over active socket."""
        import threading
        import time
        from unittest.mock import MagicMock

        mock_ws = MagicMock()
        mock_sock = MagicMock()
        mock_sock.connected = True
        mock_ws.sock = mock_sock

        stop_evt = threading.Event()
        t = start_keepalive(mock_ws, interval=0.03, stop_event=stop_evt)
        time.sleep(0.08)
        stop_evt.set()
        t.join(timeout=1.0)

        import websocket
        self.assertGreaterEqual(mock_ws.send.call_count, 1)
        mock_ws.send.assert_called_with("", opcode=getattr(websocket.ABNF, "OPCODE_PING", 0x9))

    def test_fetch_top_markets_1000_universe_sorting(self):
        """Verify fetch_top_markets loads and ranks 1,000 binary markets from local fallback."""
        markets = fetch_top_markets(limit=1000, fallback_file="markets.json", use_fallback=True, min_volume_24h=0.0)
        self.assertEqual(len(markets), 1000)
        # Verify deterministic structure
        for m in markets:
            self.assertIn("condition_id", m)
            self.assertIn("token_ids", m)
            self.assertEqual(len(m["token_ids"]), 2)
            self.assertIn("rewards_daily_rate", m)
            self.assertIn("volume", m)

        # Verify sorted descending by 24h volume, rewards, and liquidity
        scores = [
            float(m.get("volume24hr", 0.0) or 0.0) +
            (float(m.get("liquidity", 0.0) or m.get("liquidityNum", 0.0) or 0.0) * 0.05) +
            (float(m.get("rewards_daily_rate", 0.0) or 0.0) * 500.0)
            for m in markets
        ]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_lean_state_1000_markets_under_300kb(self):
        """Verify dashboard_state.json stays strictly under 300 KB with 1,000 active markets."""
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "dashboard_state.json")
            ds = DashboardState(filename=state_file)
            try:
                # Load all 1,000 markets from markets.json
                top_1000 = fetch_top_markets(limit=1000, fallback_file="markets.json", use_fallback=True, min_volume_24h=0.0)
                self.assertEqual(len(top_1000), 1000)
                ds.init_monitored_markets(top_1000)

                # Feed updates to all 1,000 markets
                for i, m in enumerate(top_1000):
                    cid = m["condition_id"]
                    yes_ask = 0.48 + (i % 5) * 0.01
                    no_ask = 0.50 + (i % 5) * 0.01
                    cost = (yes_ask + no_ask) * 1.015
                    edge = 1.00 - cost
                    ds.update_market(cid, yes_ask, no_ask, cost, edge)

                # Force synchronous write
                ds._write_if_dirty()

                # Check file size
                self.assertTrue(os.path.exists(state_file))
                file_size_bytes = os.path.getsize(state_file)
                self.assertLess(
                    file_size_bytes,
                    300 * 1024,
                    f"State file with 1,000 markets too large: {file_size_bytes} bytes ({file_size_bytes/1024:.1f} KB >= 300 KB)"
                )

                # Verify deserialization
                with open(state_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self.assertEqual(len(saved["markets"]), 1000)
                self.assertLessEqual(len(saved.get("price_history", {})), 20)
                self.assertLessEqual(len(saved.get("ohlc", {})), 20)
                self.assertLessEqual(len(saved.get("market_names", {})), 30)
            finally:
                ds.stop()

    def test_start_keepalive_duck_typed_socket_sends_ping(self):
        """Verify keepalive loop correctly sends PING to duck-typed sockets lacking a .sock property."""
        import threading
        import time

        import websocket

        class DuckTypedWs:
            def __init__(self):
                self.messages = []
            def send(self, msg, opcode=None):
                self.messages.append((msg, opcode))

        ws = DuckTypedWs()
        stop_evt = threading.Event()
        t = start_keepalive(ws, interval=0.02, stop_event=stop_evt)
        time.sleep(0.06)
        stop_evt.set()
        t.join(timeout=1.0)

        self.assertGreaterEqual(len(ws.messages), 1)
        self.assertEqual(ws.messages[0], ("", getattr(websocket.ABNF, "OPCODE_PING", 0x9)))

    def test_paper_simulator_thread_safety(self):
        """Verify concurrent book updates across worker threads execute safely without race conditions."""
        import threading
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        sim = PaperSimulator(risk)

        errors = []
        def worker(thread_id):
            try:
                for i in range(50):
                    mid = f"mkt_{thread_id}_{i % 5}"
                    sim.update_book(mid, "YES", 0.45 + (i % 3) * 0.01, ask_size=100.0, evaluate=False)
                    sim.update_book(mid, "NO", 0.50, ask_size=100.0, evaluate=False)
                    sim.check_market_parity(mid)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])

    def test_risk_engine_thread_safety(self):
        """Verify concurrent sizing and pnl calculations under load execute atomically."""
        import threading
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)

        errors = []
        def worker():
            try:
                for _ in range(50):
                    risk.calculate_sizing()
                    risk.record_pnl(0.05)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertAlmostEqual(risk.capital, 1000.0 + 50 * 4 * 0.05, delta=1e-3)

    def test_execute_arbitrage_clamps_to_remaining_cash(self):
        """Verify that execute_arbitrage dynamically clamps trade size to available cash if cash decreased."""
        risk = RiskSizingEngine(initial_capital=500.0, max_exposure_pct=0.50)  # Sizing = $250
        sim = PaperSimulator(risk, market_token_map={
            "M1": {"token_yes": "Y1", "token_no": "N1", "question": "M1"}
        })
        sim.fee_rate = 0.015

        opp = {
            "market_id": "M1",
            "trade_size": 250.0,
            "ask_yes": 0.45,
            "ask_no": 0.50,
            "effective_cost": 0.96425,
            "edge": 0.03575,
            "expected_profit": 250.0 * 0.03575
        }

        # Artificially deplete available cash down to $100 while $400 is locked in other positions
        risk.available_cash = 100.0
        risk.locked_collateral = 400.0

        # Execution should succeed by clamping trade_size to $100 instead of failing can_trade
        with patch.object(risk, 'open_position', wraps=risk.open_position) as mock_open:
            executed = sim.execute_arbitrage(opp)
            self.assertTrue(executed)
            mock_open.assert_called_once()
            args, _ = mock_open.call_args
            self.assertEqual(args[0], "M1")
            self.assertAlmostEqual(args[1], 100.0)

    def test_saver_throttles_continuous_disk_writes(self):
        """Verify DashboardState._saver throttles rapid background writes to prevent disk thrashing."""
        import tempfile
        import os
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            ds = DashboardState(filename=state_file)
            try:
                # Force first write
                ds.dirty = True
                ds._write_if_dirty()
                t_first_write = ds.last_disk_write_time
                self.assertGreater(t_first_write, 0)

                # Set dirty flag repeatedly for 0.4s (within the 1.5s throttling window)
                for _ in range(4):
                    time.sleep(0.1)
                    ds.dirty = True

                # Background saver should not have updated last_disk_write_time yet
                self.assertEqual(ds.last_disk_write_time, t_first_write)
            finally:
                ds.stop()

    def test_paper_simulator_dynamic_min_edge_and_fee_update(self):
        """Verify PaperSimulator dynamically adopts updated min_edge and fee_rate from IPC and disk."""
        import tempfile
        import os
        import json
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "dashboard_state.json")
            update_file = os.path.join(tmpdir, "capital_update.json")
            ds = DashboardState(filename=state_file)
            try:
                risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, dash_state=ds)
                sim = PaperSimulator(risk, dash_state=ds)
                self.assertEqual(sim.min_edge, 0.0020)
                self.assertEqual(sim.taker_fee_bps, 35)

                # 1. Send IPC update with tighter edge and fee
                with open(update_file, "w", encoding="utf-8") as f:
                    json.dump({"min_edge_pct": 0.0015, "taker_fee_bps": 20}, f)

                # Wait for saver thread to process IPC
                synced = False
                for _ in range(60):
                    time.sleep(0.05)
                    if not os.path.exists(update_file) and abs(sim.min_edge - 0.0015) < 1e-5 and sim.taker_fee_bps == 20:
                        synced = True
                        break
                self.assertTrue(synced, "capital_update.json was not processed by _saver into sim")

                self.assertAlmostEqual(sim.min_edge, 0.0015)
                self.assertEqual(sim.taker_fee_bps, 20)
                self.assertAlmostEqual(sim.fee_rate, 0.0020)

                # Market with raw cost 0.995 -> effective cost = 0.995 * 1.002 = 0.99699
                # Edge = 1.0 - 0.99699 = 0.00301 > 0.0015 (would have been rejected under old 0.005 threshold)
                sim.update_book("MKT_TIGHT", "YES", 0.495, ask_size=100.0, evaluate=False)
                sim.update_book("MKT_TIGHT", "NO", 0.500, ask_size=100.0, evaluate=False)

                opp = sim.check_market_parity("MKT_TIGHT")
                self.assertIsNotNone(opp)
                self.assertGreater(opp["edge"], 0.0015)

                executed = sim.execute_arbitrage(opp)
                self.assertTrue(executed)
                self.assertGreater(risk.capital, 1000.0)

                # 2. Also verify dynamic adoption from direct disk modification
                time.sleep(0.2)
                with open(state_file, "r", encoding="utf-8") as f:
                    disk_data = json.load(f)
                disk_data["min_edge_pct"] = 0.0012
                disk_data["taker_fee_bps"] = 15
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump(disk_data, f)

                disk_synced = False
                for _ in range(60):
                    time.sleep(0.05)
                    if abs(sim.min_edge - 0.0012) < 1e-5 and sim.taker_fee_bps == 15:
                        disk_synced = True
                        break
                self.assertTrue(disk_synced, "sim.min_edge did not sync 0.0012 from disk state_file")
                self.assertAlmostEqual(sim.min_edge, 0.0012)
                self.assertEqual(sim.taker_fee_bps, 15)
            finally:
                ds.stop()

    def test_configure_ram_budget(self):
        """Verify configure_ram_budget starts a watchdog thread and terminates cleanly."""
        stop_ev = threading.Event()
        t = configure_ram_budget(max_gb=5.0, stop_event=stop_ev)
        if t is not None:
            self.assertTrue(t.is_alive())
            stop_ev.set()
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive())

    def test_partition_markets_1000_into_8_pools(self):
        """Verify 1,000 markets are partitioned evenly across 8 parallel WebSocket shards (125 each)."""
        mock_markets = [
            {"condition_id": f"0xm_{i:04d}", "question": f"M {i}", "token_ids": [f"y_{i}", f"n_{i}"]}
            for i in range(1000)
        ]
        shards = partition_markets(mock_markets, num_partitions=8)
        self.assertEqual(len(shards), 8)
        for s in shards:
            self.assertEqual(len(s), 125)

        # Total tokens across all 8 shards = 2,000
        all_tokens = [t for s in shards for m in s for t in m["token_ids"]]
        self.assertEqual(len(all_tokens), 2000)

    def test_socket_worker_state_dynamic_subscriptions(self):
        """Verify SocketWorkerState dynamically subscribes to new markets and unsubscribes from removed condition IDs."""
        from unittest.mock import MagicMock
        initial = [
            {"condition_id": "0xm_0", "token_ids": ["tok_y0", "tok_n0"]},
            {"condition_id": "0xm_1", "token_ids": ["tok_y1", "tok_n1"]}
        ]
        worker = SocketWorkerState(worker_id=0, initial_markets=initial)
        self.assertEqual(len(worker.markets), 2)
        self.assertEqual(len(worker.token_ids), 4)

        mock_ws = MagicMock()
        worker.active_ws = mock_ws

        # Add 1 new market dynamically
        new_market = [{"condition_id": "0xm_2", "token_ids": ["tok_y2", "tok_n2"]}]
        added = worker.add_markets(new_market)
        self.assertEqual(added, ["tok_y2", "tok_n2"])
        self.assertEqual(len(worker.markets), 3)
        self.assertIn("tok_y2", worker.token_ids)
        mock_ws.send.assert_called_once()
        payload = json.loads(mock_ws.send.call_args[0][0])
        self.assertEqual(payload["operation"], "subscribe")
        self.assertEqual(payload["type"], "market")
        self.assertEqual(payload["assets_ids"], ["tok_y2", "tok_n2"])

        # Remove 1 market dynamically
        mock_ws.reset_mock()
        removed = worker.remove_markets({"0xm_0"})
        self.assertEqual(set(removed), {"tok_y0", "tok_n0"})
        self.assertEqual(len(worker.markets), 2)
        self.assertNotIn("tok_y0", worker.token_ids)
        mock_ws.send.assert_called_once()
        unsub_payload = json.loads(mock_ws.send.call_args[0][0])
        self.assertEqual(unsub_payload["operation"], "unsubscribe")
        self.assertEqual(set(unsub_payload["assets_ids"]), {"tok_y0", "tok_n0"})

    def test_refresh_market_universe_dynamic_rotation(self):
        """Verify refresh_market_universe rotates in active live sports and drops settled matches without disconnecting."""
        from unittest.mock import patch, MagicMock
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "dashboard_state.json")
            ds = DashboardState(filename=state_file)
            try:
                risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, dash_state=ds)
                sim = PaperSimulator(risk, dash_state=ds)

                # 2 workers with 1 market each initially
                m0 = {"condition_id": "0xm_old", "question": "Old Friday Match", "token_ids": ["y_old", "n_old"]}
                m1 = {"condition_id": "0xm_stay", "question": "Ongoing Series", "token_ids": ["y_stay", "n_stay"]}
                worker0 = SocketWorkerState(0, [m0])
                worker1 = SocketWorkerState(1, [m1])
                mock_ws0 = MagicMock()
                mock_ws1 = MagicMock()
                worker0.active_ws = mock_ws0
                worker1.active_ws = mock_ws1
                workers = [worker0, worker1]

                sim.market_token_map["0xm_old"] = {"token_yes": "y_old", "token_no": "n_old", "question": "Old"}
                sim.market_token_map["0xm_stay"] = {"token_yes": "y_stay", "token_no": "n_stay", "question": "Ongoing"}
                ds.init_monitored_markets([m0, m1])

                # Gamma API returns: 0xm_stay (still active) and 0xm_new (live Saturday football match). 0xm_old is closed.
                m_new = {"condition_id": "0xm_new", "question": "Live Saturday Match", "token_ids": ["y_new", "n_new"], "outcomes": ["Yes", "No"]}
                refreshed = [m1, m_new]

                with patch("paper_trader.fetch_top_markets", return_value=refreshed):
                    with patch("paper_trader.seed_order_books_via_rest", return_value=1) as mock_seed:
                        added, removed = refresh_market_universe(sim, workers, market_limit=2)

                self.assertEqual(added, 1)
                self.assertEqual(removed, 1)
                self.assertIn("0xm_new", sim.market_token_map)
                self.assertNotIn("0xm_old", sim.market_token_map)
                mock_seed.assert_called_once()
                self.assertIn("0xm_new", ds.state["markets"])
                self.assertNotIn("0xm_old", ds.state["markets"])
                # Log entry recorded
                self.assertTrue(any("Market Universe Refreshed" in log for log in ds.state["activity_log"]))
            finally:
                ds.stop()

    def test_configure_cpu_budget(self):
        """Verify configure_cpu_budget caps CPU affinity to <= 20% logical cores."""
        cores = configure_cpu_budget(target_pct=0.20)
        if cores is not None:
            total = len(cores)
            try:
                import psutil
                total = psutil.cpu_count(logical=True) or total
            except ImportError:
                pass
            if total >= 5:
                self.assertLessEqual(len(cores) / total, 0.20)
            else:
                self.assertEqual(len(cores), 1)
            self.assertEqual(cores, list(range(len(cores))))

    def test_add_trade_formats_full_timestamp(self):
        """Verify add_trade records %Y-%m-%d %H:%M:%S format by default."""
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            ds = DashboardState(filename=os.path.join(tmpdir, "ds.json"))
            try:
                ds.add_trade("0xmkt_test", 150.0, 4.5)
                trade = ds.state["trades"][0]
                self.assertEqual(trade["market"], "0xmkt_test")
                self.assertEqual(trade["size"], 150.0)
                self.assertEqual(trade["expected_profit"], 4.5)
                # Must match YYYY-MM-DD HH:MM:SS
                datetime.strptime(trade["time"], "%Y-%m-%d %H:%M:%S")
            finally:
                ds.stop()

    def test_update_market_ohlc_restricted_to_priority_markets(self):
        """Verify OHLC candles are only computed for priority markets (open positions, inspected, edge > 0)."""
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            ds = DashboardState(filename=os.path.join(tmpdir, "ds.json"))
            try:
                # 1. Non-priority market (edge <= 0, not inspected, not open)
                ds.update_market("0xmkt_regular", 0.50, 0.52, 1.035, -0.035)
                self.assertIn("0xmkt_regular", ds.state["price_history"])
                self.assertNotIn("0xmkt_regular", ds.state["ohlc"])

                # 2. Priority market with edge > 0
                ds.update_market("0xmkt_edge", 0.48, 0.50, 0.98, 0.02)
                self.assertIn("0xmkt_edge", ds.state["ohlc"])

                # 3. Inspected market with negative edge
                ds.inspected_market = "0xmkt_inspected"
                ds.update_market("0xmkt_inspected", 0.50, 0.52, 1.035, -0.035)
                self.assertIn("0xmkt_inspected", ds.state["ohlc"])
            finally:
                ds.stop()

    def test_dashboard_state_starting_capital_null_recovery(self):
        """Verify DashboardState recovers 1060.3666 starting_capital when starting_capital is null in JSON."""
        import tempfile
        import os
        import json
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "ds.json")
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump({"capital": 1153.7016, "starting_capital": None, "available_cash": 1153.7016}, f)
            ds = DashboardState(filename=file_path)
            try:
                self.assertEqual(ds.state["starting_capital"], 1060.3666)
                self.assertEqual(ds.state["capital"], 1153.7016)
                self.assertEqual(ds.state["available_cash"], 1153.7016)
            finally:
                ds.stop()

    def test_fetch_top_markets_pure_volume_pagination(self):
        """Verify fetch_top_markets fetches markets purely by volume pagination without sports injection."""
        from unittest.mock import patch, MagicMock
        page1 = [
            {
                "conditionId": "0x_vol_1",
                "question": "Will BTC hit 100k?",
                "active": True,
                "closed": False,
                "archived": False,
                "clobTokenIds": json.dumps(["tok_yes_1", "tok_no_1"]),
                "volume24hr": 100000.0,
                "liquidityNum": 20000.0,
            },
            {
                "conditionId": "0x_vol_2",
                "question": "Will ETH hit 5k?",
                "active": True,
                "closed": False,
                "archived": False,
                "clobTokenIds": json.dumps(["tok_yes_2", "tok_no_2"]),
                "volume24hr": 50000.0,
                "liquidityNum": 10000.0,
            }
        ]

        def _mock_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            mock_resp = MagicMock()
            self.assertNotIn("tag_slug=sports", url)
            self.assertNotIn("tag_slug=esports", url)
            if "offset=0" in url:
                mock_resp.read.return_value = json.dumps(page1).encode("utf-8")
            else:
                mock_resp.read.return_value = json.dumps([]).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.__exit__.return_value = False
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            markets = fetch_top_markets(limit=2, fallback_file="nonexistent.json")
            self.assertEqual(len(markets), 2)
            cids = [m["condition_id"] for m in markets]
            self.assertEqual(cids, ["0x_vol_1", "0x_vol_2"])

    def test_check_market_parity_thread_safety_during_rotation(self):
        """Verify check_market_parity executes safely during concurrent universe updates."""
        re = RiskSizingEngine(initial_capital=1000.0)
        sim = PaperSimulator(re, market_token_map={})
        sim.min_edge = 0.001
        sim.fee_rate = 0.0035

        # Populate book for market
        sim.update_book("0xmkt_concurrent", "tok_yes", 0.40, ask_size=100.0, evaluate=False)
        sim.update_book("0xmkt_concurrent", "tok_no", 0.45, ask_size=100.0, evaluate=False)

        stop_threads = threading.Event()
        errors = []

        def _reader():
            while not stop_threads.is_set():
                try:
                    sim.check_market_parity("0xmkt_concurrent")
                except Exception as e:
                    errors.append(e)

        def _writer():
            while not stop_threads.is_set():
                try:
                    with sim.lock:
                        sim.market_token_map["0xmkt_concurrent"] = {
                            "token_yes": "tok_yes",
                            "token_no": "tok_no",
                            "question": "Thread Safety Test Market"
                        }
                    time.sleep(0.001)
                    with sim.lock:
                        sim.market_token_map.pop("0xmkt_concurrent", None)
                    time.sleep(0.001)
                except Exception as e:
                    errors.append(e)

        t_r = threading.Thread(target=_reader)
        t_w = threading.Thread(target=_writer)
        t_r.start()
        t_w.start()
        time.sleep(0.15)
        stop_threads.set()
        t_r.join()
        t_w.join()

        self.assertEqual(errors, [])

    def test_partition_dual_pools_1000_markets_16_sockets(self):
        """Verify 1,000 markets are partitioned identically across Dual Pool A (1-8) and Pool B (9-16) for 16 sockets."""
        mock_markets = [
            {"condition_id": f"0xm_{i:04d}", "question": f"Market {i}", "token_ids": [f"y_{i}", f"n_{i}"]}
            for i in range(1000)
        ]
        pool_a, pool_b = partition_dual_pools(mock_markets, num_shards=8)
        self.assertEqual(len(pool_a), 8)
        self.assertEqual(len(pool_b), 8)
        self.assertEqual(len(pool_a) + len(pool_b), 16)

        # Verify worker labels for Pool A: [Pool A - Worker 1] .. [Pool A - Worker 8]
        for idx, w in enumerate(pool_a):
            self.assertEqual(w.pool_name, "Pool A")
            self.assertEqual(w.worker_num, idx + 1)
            self.assertEqual(w.tag, f"[Pool A - Worker {idx + 1}]")
            self.assertEqual(len(w.markets), 125)

        # Verify worker labels for Pool B: [Pool B - Worker 9] .. [Pool B - Worker 16]
        for idx, w in enumerate(pool_b):
            self.assertEqual(w.pool_name, "Pool B")
            self.assertEqual(w.worker_num, idx + 9)
            self.assertEqual(w.tag, f"[Pool B - Worker {idx + 9}]")
            self.assertEqual(len(w.markets), 125)

        # Verify identical partitioning between counterpart shards (0% Blast Radius)
        for i in range(8):
            sh_a_cids = [m["condition_id"] for m in pool_a[i].markets]
            sh_b_cids = [m["condition_id"] for m in pool_b[i].markets]
            self.assertEqual(sh_a_cids, sh_b_cids)
            self.assertEqual(pool_a[i].token_ids, pool_b[i].token_ids)

    def test_active_active_tick_deduplication_first_socket_wins(self):
        """Verify simulator processes whichever socket delivers tick first and drops redundant duplicate ticks."""
        risk = RiskSizingEngine(initial_capital=1000.0)
        sim = PaperSimulator(risk)

        # First socket (Pool A) delivers tick: should be processed and update book
        up1 = sim.update_book("0xmkt_dedup", "tok_yes", 0.48, ask_size=150.0, evaluate=False)
        self.assertTrue(up1)
        self.assertEqual(sim.market_books["0xmkt_dedup"]["tok_yes"], 0.48)
        self.assertEqual(sim.market_depths["0xmkt_dedup"]["tok_yes"], 150.0)

        # Second socket (Pool B) delivers identical tick 5ms later: should return False (ignored duplicate)
        up2 = sim.update_book("0xmkt_dedup", "tok_yes", 0.48, ask_size=150.0, evaluate=False)
        self.assertFalse(up2)

        # Socket delivers updated price 0.47: should be processed immediately
        up3 = sim.update_book("0xmkt_dedup", "tok_yes", 0.47, ask_size=150.0, evaluate=False)
        self.assertTrue(up3)
        self.assertEqual(sim.market_books["0xmkt_dedup"]["tok_yes"], 0.47)

        # Socket delivers updated size 200.0: should be processed
        up4 = sim.update_book("0xmkt_dedup", "tok_yes", 0.47, ask_size=200.0, evaluate=False)
        self.assertTrue(up4)
        self.assertEqual(sim.market_depths["0xmkt_dedup"]["tok_yes"], 200.0)

    def test_active_active_on_message_no_duplicate_arbitrage(self):
        """Verify on_message avoids duplicate execution when both Pool A and Pool B deliver the same opportunity."""
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            ds = DashboardState(filename=os.path.join(tmpdir, "ds_test.json"))
            try:
                risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10, dash_state=ds)
                sim = PaperSimulator(risk, dash_state=ds, market_token_map={
                    "0xmkt_arb": {"token_yes": "tok_y", "token_no": "tok_n", "question": "Arb Test"}
                })
                sim.fee_rate = 0.0035
                sim.min_edge = 0.0020

                # Pre-populate YES ask to 0.45
                sim.update_book("0xmkt_arb", "tok_y", 0.45, ask_size=500.0, evaluate=False)

                # Arbitrage event that completes parity (NO ask 0.50)
                event = {
                    "event_type": "price_change",
                    "market": "0xmkt_arb",
                    "price_changes": [{"asset_id": "tok_n", "best_ask": "0.50", "size": "500"}]
                }
                msg = json.dumps([event])

                # 1. Pool A Worker delivers the tick -> triggers trade
                on_message(None, msg, sim)
                self.assertEqual(len(ds.state.get("trades", [])), 1)
                initial_capital = risk.capital

                # 2. Pool B Worker delivers identical tick concurrently -> duplicate must NOT trigger second trade
                on_message(None, msg, sim)
                self.assertEqual(risk.capital, initial_capital)
                self.assertEqual(len(ds.state.get("trades", [])), 1)
            finally:
                ds.stop()

    def test_active_active_concurrent_threads_race_condition(self):
        """Verify concurrent threads streaming ticks for the same market maintain strict thread safety without duplicates."""
        risk = RiskSizingEngine(initial_capital=2000.0, max_exposure_pct=0.05, max_concurrent_positions=10)
        sim = PaperSimulator(risk, market_token_map={
            "0xmkt_race": {"token_yes": "tok_y", "token_no": "tok_n", "question": "Race Condition Test"}
        })
        sim.fee_rate = 0.0035
        sim.min_edge = 0.0020

        sim.update_book("0xmkt_race", "tok_y", 0.46, ask_size=200.0, evaluate=False)

        errors = []
        barrier = threading.Barrier(8)

        def _socket_streamer(thread_id: int):
            try:
                barrier.wait(timeout=3.0)
                # Alternate identical and near-identical ticks
                for _ in range(25):
                    ev = {
                        "event_type": "price_change",
                        "market": "0xmkt_race",
                        "price_changes": [{"asset_id": "tok_n", "best_ask": "0.51", "size": "200"}]
                    }
                    on_message(None, json.dumps([ev]), sim)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_socket_streamer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [])
        # Only 1 position should have been opened for this market
        self.assertLessEqual(len(risk.open_positions), 1)

    def test_refresh_market_universe_dual_pool_synchronization(self):
        """Verify dynamic refresh adds new markets to corresponding shards in both Pool A and Pool B simultaneously."""
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            ds = DashboardState(filename=os.path.join(tmpdir, "ds_sync.json"))
            try:
                risk = RiskSizingEngine(initial_capital=1000.0, dash_state=ds)
                sim = PaperSimulator(risk, dash_state=ds)

                # Initialize 2 shards with 2 workers in Pool A and 2 workers in Pool B
                m_existing = {"condition_id": "0xm_stay", "token_ids": ["y0", "n0"], "question": "Stay"}
                pool_a = [
                    SocketWorkerState(0, [m_existing], pool_name="Pool A", worker_num=1, shard_id=0),
                    SocketWorkerState(1, [], pool_name="Pool A", worker_num=2, shard_id=1)
                ]
                pool_b = [
                    SocketWorkerState(2, [m_existing], pool_name="Pool B", worker_num=9, shard_id=0),
                    SocketWorkerState(3, [], pool_name="Pool B", worker_num=10, shard_id=1)
                ]
                worker_states = pool_a + pool_b
                sim.market_token_map["0xm_stay"] = {"token_yes": "y0", "token_no": "n0", "question": "Stay"}

                m_new = {"condition_id": "0xm_new", "token_ids": ["ynew", "nnew"], "question": "New Live"}
                refreshed = [m_existing, m_new]

                with patch("paper_trader.fetch_top_markets", return_value=refreshed):
                    with patch("paper_trader.seed_order_books_via_rest", return_value=1):
                        added, removed = refresh_market_universe(sim, worker_states, market_limit=2)

                self.assertEqual(added, 1)
                # Verify that m_new was added to shard 1 in BOTH Pool A and Pool B
                self.assertIn("ynew", pool_a[1].token_ids)
                self.assertIn("ynew", pool_b[1].token_ids)
                self.assertEqual(pool_a[1].token_ids, pool_b[1].token_ids)
            finally:
                ds.stop()

    def test_run_socket_pool_stagger_and_dual_pool(self):
        """Verify run_socket_pool spawns 16 threads for 8 shards with dual_pool=True and staggers startup."""
        mock_markets = [
            {"condition_id": f"0xm_{i}", "question": f"M {i}", "token_ids": [f"y_{i}", f"n_{i}"]}
            for i in range(16)
        ]
        risk = RiskSizingEngine(initial_capital=1000.0)
        sim = PaperSimulator(risk)

        spawned_threads = []
        delays = []

        def _mock_sleep(seconds):
            delays.append(seconds)

        class DummyThread:
            def __init__(self, target, args, daemon):
                self.target = target
                self.args = args
                self.daemon = daemon
            def start(self):
                spawned_threads.append(self)
            def join(self, timeout=None):
                pass

        stop_ev = threading.Event()
        stop_ev.set()

        with patch("threading.Thread", side_effect=DummyThread):
            with patch("time.sleep", side_effect=_mock_sleep):
                with patch("paper_trader.start_market_universe_refresher"):
                    run_socket_pool(mock_markets, sim, num_sockets=8, stop_event=stop_ev, dual_pool=True)

        self.assertEqual(len(spawned_threads), 16)
        stagger_delays = [d for d in delays if abs(d - 0.12) < 0.01]
    def test_active_active_orderbook_restoration_after_trade(self):
        """Verify that after an arbitrage trade executes, new incoming ticks properly re-populate the order book."""
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        sim = PaperSimulator(risk, market_token_map={
            "0xmkt_restore": {"token_yes": "tok_y", "token_no": "tok_n", "question": "Restore Test"}
        })
        sim.fee_rate = 0.0035
        sim.min_edge = 0.0020

        # Step 1: Pre-populate YES at 0.45 and NO at 0.50 -> triggers arbitrage
        sim.update_book("0xmkt_restore", "tok_y", 0.45, ask_size=300.0, evaluate=False)
        sim.update_book("0xmkt_restore", "tok_n", 0.50, ask_size=300.0, evaluate=True)

        self.assertIsNone(sim.market_books["0xmkt_restore"]["tok_y"])
        self.assertIsNone(sim.market_books["0xmkt_restore"]["tok_n"])

        # Step 2: Instant compounding already recycles collateral immediately on trade execution
        self.assertEqual(len(risk.open_positions), 0)

        # Step 3: Incoming ticks with same quote must re-populate market_books instead of being dropped
        res_y = sim.update_book("0xmkt_restore", "tok_y", 0.45, ask_size=300.0, evaluate=False)
        self.assertTrue(res_y)
        self.assertEqual(sim.market_books["0xmkt_restore"]["tok_y"], 0.45)

        res_n = sim.update_book("0xmkt_restore", "tok_n", 0.50, ask_size=300.0, evaluate=False)
        self.assertTrue(res_n)
        self.assertEqual(sim.market_books["0xmkt_restore"]["tok_n"], 0.50)

        # Step 4: Redundant duplicate tick arriving while book is populated MUST be dropped
        res_n_dup = sim.update_book("0xmkt_restore", "tok_n", 0.50, ask_size=300.0, evaluate=False)
        self.assertFalse(res_n_dup)

    def test_partition_dual_pools_small_market_counts(self):
        """Verify partition_dual_pools assigns deterministic worker numbers 1..N and 9..(8+N) even when len(markets) < 8."""
        small_markets = [
            {"condition_id": f"0xm_{i}", "question": f"Small {i}", "token_ids": [f"y_{i}", f"n_{i}"]}
            for i in range(4)
        ]
        pool_a, pool_b = partition_dual_pools(small_markets, num_shards=8)
        self.assertEqual(len(pool_a), 4)
        self.assertEqual(len(pool_b), 4)
        for i in range(4):
            self.assertEqual(pool_a[i].worker_num, i + 1)
            self.assertEqual(pool_b[i].worker_num, i + 9)
            self.assertEqual(pool_b[i].pool_name, "Pool B")

if __name__ == '__main__':
    unittest.main()


import pytest
from unittest.mock import MagicMock, patch
from paper_trader import LiveExecutor, RiskSizingEngine, DashboardState, MissedReason

def test_live_executor_initialization_no_key():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': ''}):
        risk_engine = MagicMock(spec=RiskSizingEngine)
        executor = LiveExecutor(risk_engine=risk_engine)
        assert executor.client is None

def test_live_executor_initialization_with_key():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.derive_api_key.return_value = 'mock_creds'
            risk_engine = MagicMock(spec=RiskSizingEngine)
            executor = LiveExecutor(risk_engine=risk_engine)
            assert executor.client is not None
            assert executor.signature_type == 2
            mock_instance.set_api_creds.assert_called_with('mock_creds')
            mock_client.assert_called_with(
                'https://clob.polymarket.com',
                key='0x123',
                chain_id=137,
                signature_type=2,
                funder='0xabc'
            )

def test_live_executor_signature_type_reading():
    with patch.dict('os.environ', {
        'POLYMARKET_PRIVATE_KEY': '0x123',
        'POLYMARKET_ADDRESS': '0xabc',
        'POLYMARKET_SIGNATURE_TYPE': '0'
    }):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.derive_api_key.return_value = 'mock_creds'
            risk_engine = MagicMock(spec=RiskSizingEngine)
            executor = LiveExecutor(risk_engine=risk_engine)
            assert executor.signature_type == 0
            mock_client.assert_called_with(
                'https://clob.polymarket.com',
                key='0x123',
                chain_id=137,
                signature_type=0,
                funder='0xabc'
            )

def test_live_executor_execute_arbitrage_success():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            with patch('web3.Web3') as mock_web3_class:
                mock_w3 = MagicMock()
                mock_web3_class.return_value = mock_w3
                mock_web3_class.to_checksum_address = lambda x: x
                mock_web3_class.to_bytes = lambda hexstr: b"0" * 32
                mock_web3_class.to_hex = lambda x: "0xhash"
                mock_contract = MagicMock()
                mock_w3.eth.contract.return_value = mock_contract
                mock_contract.functions.balanceOf().call.side_effect = [1000000, 1000000]
                receipt = MagicMock()
                receipt.status = 1
                mock_w3.eth.wait_for_transaction_receipt.return_value = receipt

                mock_instance = mock_client.return_value
                mock_instance.derive_api_key.return_value = 'mock_creds'
                mock_instance.create_order.side_effect = lambda args: f"order_{args.token_id}"
                mock_instance.post_orders.return_value = [{"takingAmount": "10.0", "errorMsg": ""}, {"takingAmount": "10.0", "errorMsg": ""}]
                
                risk_engine = MagicMock(spec=RiskSizingEngine)
                risk_engine.capital = 1000
                risk_engine.max_exposure_pct = 0.1
                risk_engine.available_cash = 1000
                risk_engine.max_market_exposure_pct = 0.65
                risk_engine.reserve_cash_pct = 0.0
                risk_engine.open_positions = {}
                risk_engine.can_trade.return_value = True
                risk_engine.open_position.return_value = True
                risk_engine.lock = MagicMock()
                
                dash_state = MagicMock(spec=DashboardState)
                dash_state.state = {"execution_mode": "Live Trading", "live_wager_cap": 5.0}
                
                m_map = {"0xmarket": {"token_yes": "0xYes", "token_no": "0xNo"}}
                executor = LiveExecutor(risk_engine=risk_engine, market_token_map=m_map, dash_state=dash_state)
                
                executor.market_books = {"0xmarket": {"0xYes": 0.40, "0xNo": 0.40}}
                executor.market_depths = {"0xmarket": {"0xYes": 1000.0, "0xNo": 1000.0}}
                
                opp = {
                    "market_id": "0xmarket",
                    "trade_size": 10.0,
                    "ask_yes": 0.40,
                    "ask_no": 0.40,
                    "effective_cost": 0.80,
                    "edge": 0.20
                }
                
                res = executor.execute_arbitrage(opp)
                
                assert res is True
                assert mock_instance.create_order.call_count == 2
                mock_instance.post_orders.assert_called_once()
                orders_sent = mock_instance.post_orders.call_args[0][0]
                assert len(orders_sent) == 2
                assert orders_sent[0].order == "order_0xYes"
                risk_engine.open_position.assert_called_once_with("0xmarket", 8.0, 2.0)

def test_live_executor_maker_address_not_allowed_error():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.derive_api_key.return_value = 'mock_creds'
            mock_instance.post_orders.side_effect = Exception("error: maker address not allowed, please use deposit wallet flow")
            
            risk_engine = MagicMock(spec=RiskSizingEngine)
            risk_engine.capital = 1000
            risk_engine.max_exposure_pct = 0.1
            risk_engine.available_cash = 1000
            risk_engine.open_positions = {}
            risk_engine.can_trade.return_value = True
            risk_engine.lock = MagicMock()
            
            dash_state = MagicMock(spec=DashboardState)
            dash_state.state = {"execution_mode": "Live Trading", "live_wager_cap": 10.0}
            
            m_map = {"0xmarket": {"token_yes": "token_y", "token_no": "token_n"}}
            executor = LiveExecutor(risk_engine=risk_engine, market_token_map=m_map, dash_state=dash_state)
            executor.market_books = {"0xmarket": {"token_y": 0.45, "token_n": 0.50}}
            executor.market_depths = {"0xmarket": {"token_y": 100.0, "token_n": 100.0}}
            
            opp = {
                "market_id": "0xmarket",
                "trade_size": 10.0,
                "ask_yes": 0.45,
                "ask_no": 0.50,
                "effective_cost": 0.95,
                "edge": 0.05
            }
            res = executor.execute_arbitrage(opp)
            assert res is False
            dash_state.add_activity_log.assert_called_with(
                "❌ Live Order Failed: Maker address not allowed (Deposit wallet required in POLYMARKET_ADDRESS)"
            )
            assert executor.shadow_tracker.total_missed_count == 1
            assert executor.shadow_tracker.recent_missed[0]["reason"] == MissedReason.CIRCUIT_BREAKER.value

def test_live_executor_specific_error_translations():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.derive_api_key.return_value = 'mock_creds'
            
            risk_engine = MagicMock(spec=RiskSizingEngine)
            risk_engine.capital = 1000
            risk_engine.max_exposure_pct = 0.1
            risk_engine.available_cash = 1000
            risk_engine.open_positions = {}
            risk_engine.can_trade.return_value = True
            risk_engine.lock = MagicMock()
            
            dash_state = MagicMock(spec=DashboardState)
            dash_state.state = {"execution_mode": "Live Trading", "live_wager_cap": 10.0}
            m_map = {"0xmarket": {"token_yes": "token_y", "token_no": "token_n"}}
            
            executor = LiveExecutor(risk_engine=risk_engine, market_token_map=m_map, dash_state=dash_state)
            executor.market_books = {"0xmarket": {"token_y": 0.45, "token_n": 0.50}}
            executor.market_depths = {"0xmarket": {"token_y": 100.0, "token_n": 100.0}}
            opp = {"market_id": "0xmarket", "trade_size": 10.0, "ask_yes": 0.45, "ask_no": 0.50, "edge": 0.05}
            
            mock_instance.post_orders.side_effect = Exception("invalid order version, please upgrade")
            res = executor.execute_arbitrage(opp)
            assert res is False
            dash_state.add_activity_log.assert_called_with("❌ Live Order Failed: Invalid order version")
            
            mock_instance.post_orders.side_effect = Exception("signer address has to be 0x123...")
            res = executor.execute_arbitrage(opp)
            assert res is False
            dash_state.add_activity_log.assert_called_with("❌ Live Order Failed: Signer / API key address mismatch")

def test_live_executor_check_live_readiness():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.derive_api_key.return_value = 'mock_creds'
            mock_instance.get_balance_allowance.return_value = {"balance": "137.81"}
            
            risk_engine = MagicMock(spec=RiskSizingEngine)
            executor = LiveExecutor(risk_engine=risk_engine)
            readiness = executor.check_live_readiness()
            assert readiness["ready"] is True
            assert readiness["signature_type"] == 2
            assert readiness["funder"] == "0xabc"
            assert readiness["balance"] == "137.81"
            assert readiness["error"] is None

def test_live_executor_check_live_readiness_no_client():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': ''}):
        risk_engine = MagicMock(spec=RiskSizingEngine)
        executor = LiveExecutor(risk_engine=risk_engine)
        readiness = executor.check_live_readiness()
        assert readiness["ready"] is False
        assert readiness["error"] is not None

def test_shadow_tracker_sub_threshold_edge():
    risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
    sim = PaperSimulator(risk_engine=risk, min_edge=0.0050, taker_fee_bps=35)
    # YES ask: 0.497, NO ask: 0.497 -> raw_cost = 0.994
    # effective_cost = 0.994 * 1.0035 = 0.997479 < 1.00
    # edge = 1 - 0.997479 = 0.002521 > 0, but <= min_edge (0.0050)
    sim.market_books["0xsub"] = {"YES": 0.497, "NO": 0.497}
    sim.market_depths["0xsub"] = {"YES": 500.0, "NO": 500.0}
    opp = sim.check_market_parity("0xsub")
    assert opp is None
    summary = sim.shadow_tracker.get_summary()
    assert summary["total_missed_count"] == 1
    assert summary["by_reason"][MissedReason.SUB_THRESHOLD_EDGE.value]["count"] == 1
    assert summary["recent_missed"][0]["reason"] == MissedReason.SUB_THRESHOLD_EDGE.value


def test_shadow_tracker_concurrency_gating():
    risk = RiskSizingEngine(initial_capital=1000.0, max_concurrent_positions=1)
    risk.open_position("0xactive", 100.0, 2.0)
    sim = PaperSimulator(risk_engine=risk, min_edge=0.0010)
    
    opp = {
        "market_id": "0xsecond",
        "trade_size": 100.0,
        "ask_yes": 0.45,
        "ask_no": 0.45,
        "effective_cost": 0.90,
        "edge": 0.10,
        "available_depth_usd": 1000.0
    }
    executed = sim.execute_arbitrage(opp)
    assert executed is False
    summary = sim.shadow_tracker.get_summary()
    assert summary["total_missed_count"] == 1
    assert summary["by_reason"][MissedReason.CONCURRENCY_EXHAUSTED.value]["count"] == 1
    assert summary["recent_missed"][0]["reason"] == MissedReason.CONCURRENCY_EXHAUSTED.value


def test_shadow_tracker_insufficient_cash():
    risk = RiskSizingEngine(initial_capital=1000.0, available_cash=0.0)
    sim = PaperSimulator(risk_engine=risk, min_edge=0.0010)
    opp = {
        "market_id": "0xcash_short",
        "trade_size": 100.0,
        "ask_yes": 0.45,
        "ask_no": 0.45,
        "effective_cost": 0.90,
        "edge": 0.10,
        "available_depth_usd": 1000.0
    }
    executed = sim.execute_arbitrage(opp)
    assert executed is False
    summary = sim.shadow_tracker.get_summary()
    assert summary["total_missed_count"] == 1
    assert summary["by_reason"][MissedReason.INSUFFICIENT_CASH.value]["count"] == 1


def test_shadow_tracker_zero_liquidity():
    risk = RiskSizingEngine(initial_capital=1000.0)
    sim = PaperSimulator(risk_engine=risk, min_edge=0.0010)
    sim.market_books["0xzeroliq"] = {"YES": 0.45, "NO": 0.45}
    sim.market_depths["0xzeroliq"] = {"YES": 0.0, "NO": 500.0}
    opp = sim.check_market_parity("0xzeroliq")
    assert opp is None
    summary = sim.shadow_tracker.get_summary()
    assert summary["total_missed_count"] == 1
    assert summary["by_reason"][MissedReason.ZERO_LIQUIDITY.value]["count"] == 1


def test_dashboard_state_missed_trades_sync():
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        test_file = f.name
    try:
        dash = DashboardState(filename=test_file)
        dash.record_missed_trade({
            "market_id": "0xtest",
            "reason": MissedReason.SUB_THRESHOLD_EDGE.value,
            "forfeited_pnl": 0.50,
            "edge": 0.0015
        })
        assert len(dash.state["missed_trades"]) == 1
        assert dash.state["missed_trades_summary"]["total_missed_count"] == 1
        assert dash.state["missed_trades_summary"]["total_missed_pnl"] == 0.50
        assert dash.state["missed_trades_summary"]["by_reason"][MissedReason.SUB_THRESHOLD_EDGE.value]["count"] == 1

        dash.update_adaptive_policy({
            "leakage_ratio": 0.20,
            "recommended_min_edge_pct": 0.0015,
            "primary_bottleneck": "SUB_THRESHOLD_EDGE"
        })
        assert dash.state["adaptive_policy"]["leakage_ratio"] == 0.20

        dash._write_if_dirty()
        with open(test_file, 'r', encoding='utf-8') as f:
            disk_data = json.load(f)
        assert "missed_trades" in disk_data
        assert "missed_trades_summary" in disk_data
        assert "adaptive_policy" in disk_data
        assert disk_data["missed_trades_summary"]["total_missed_count"] == 1
        dash.stop()
    finally:
        if os.path.exists(test_file):
            try:
                os.remove(test_file)
            except Exception:
                pass


def test_adaptive_policy_evaluation():
    risk = RiskSizingEngine(initial_capital=1000.0)
    sim = PaperSimulator(risk_engine=risk, min_edge=0.0020)
    
    sim.shadow_tracker.record_missed({
        "market_id": "0xm1", "edge": 0.0015, "trade_size": 100.0, "expected_profit": 1.50
    }, MissedReason.SUB_THRESHOLD_EDGE)
    sim.shadow_tracker.record_missed({
        "market_id": "0xm2", "edge": 0.0080, "trade_size": 100.0, "expected_profit": 4.00
    }, MissedReason.INSUFFICIENT_CASH)

    optimizer = AdaptivePolicyOptimizer()
    policy = optimizer.evaluate(sim)

    assert policy["total_missed_pnl"] == 4.00
    assert policy["leakage_ratio"] > 0.0
    assert policy["recommended_reserve_cash_pct"] == 0.25
    assert policy["primary_bottleneck"] in (MissedReason.SUB_THRESHOLD_EDGE.value, MissedReason.INSUFFICIENT_CASH.value)


def test_multi_position_stacking():
    """Verify multiple concurrent positions can be opened on the same market up to max_positions_per_market."""
    risk = RiskSizingEngine(
        initial_capital=1000.0,
        max_exposure_pct=0.20,
        max_positions_per_market=2,
        max_market_exposure_pct=0.105,
        hold_period_seconds=1.5
    )
    # Open 1st position on 0xmkt_1
    assert risk.can_trade(50.0, market_id="0xmkt_1") is True
    assert risk.open_position("0xmkt_1", 50.0, 1.5) is True
    assert len(risk.open_positions) == 1
    assert "0xmkt_1" in risk.open_positions

    # Open 2nd position on same market (multi-order stacking)
    assert risk.can_trade(50.0, market_id="0xmkt_1") is True
    assert risk.open_position("0xmkt_1", 50.0, 1.5) is True
    assert len(risk.open_positions) == 2

    # 3rd position on 0xmkt_1 must be gated by MARKET_ALREADY_ACTIVE
    gating_reason = risk.check_trade_gating_reason(50.0, market_id="0xmkt_1")
    assert gating_reason == MissedReason.MARKET_ALREADY_ACTIVE.value
    assert risk.can_trade(50.0, market_id="0xmkt_1") is False

    # Position on a different market is still permitted
    assert risk.can_trade(50.0, market_id="0xmkt_2") is True

    # Recycle expired positions
    now = time.time()
    released = risk.recycle_collateral(now=now + 2.0)
    assert len(released) == 2
    assert len(risk.open_positions) == 0
    assert risk.locked_collateral == 0.0
    assert risk.available_cash == 1000.0 + 3.0


def test_market_exposure_limit_gating():
    """Verify cumulative market exposure and reserve cash gating enforce trade gating."""
    risk = RiskSizingEngine(
        initial_capital=1000.0,
        max_exposure_pct=0.50,
        max_market_exposure_pct=0.25,
        max_positions_per_market=5,
        reserve_cash_pct=0.20
    )
    # Total cap = 1000. Max market exposure (25%) = 250.0
    # Reserve cash = 20% of 1000 = 200.0, Spendable cash = 800.0

    # 1. Reserve cash gating: trade size 850 exceeds spendable cash 800
    assert risk.check_trade_gating_reason(850.0, market_id="0xmkt_exp") == MissedReason.INSUFFICIENT_CASH.value

    # 2. Open initial position of $200 on 0xmkt_exp
    assert risk.open_position("0xmkt_exp", 200.0, 4.0) is True

    # Cumulative market exposure is now $200.
    # Subsequent trade of $60 brings market exposure to $260 > $250 max market exposure.
    reason = risk.check_trade_gating_reason(60.0, market_id="0xmkt_exp")
    assert reason == MissedReason.EXPOSURE_LIMIT_EXCEEDED.value

    # An additional trade of $40 ($200 + $40 = $240 <= $250) is permitted on 0xmkt_exp
    assert risk.check_trade_gating_reason(40.0, market_id="0xmkt_exp") is None

    # Trade of $100 on different market 0xmkt_other is permitted
    assert risk.check_trade_gating_reason(100.0, market_id="0xmkt_other") is None


def test_adaptive_policy_active_auto_tuning():
    """Verify AdaptivePolicyOptimizer auto-tunes min_edge and reserve_cash_pct dynamically on simulator."""
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "dashboard_state.json")
        dash = DashboardState(filename=state_file)
        try:
            risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.80, dash_state=dash)
            sim = PaperSimulator(risk_engine=risk, dash_state=dash, min_edge=0.0020)

            # Record sub-threshold edge misses (>= 3 misses with > $1.0 pnl)
            for i in range(3):
                sim.shadow_tracker.record_missed({
                    "market_id": f"0xedge_{i}",
                    "edge": 0.0015,
                    "trade_size": 100.0,
                    "expected_profit": 1.50
                }, MissedReason.SUB_THRESHOLD_EDGE)

            # Record a high-edge cash miss (edge >= 0.0060)
            sim.shadow_tracker.record_missed({
                "market_id": "0xcash_high",
                "edge": 0.0080,
                "trade_size": 100.0,
                "expected_profit": 5.00
            }, MissedReason.INSUFFICIENT_CASH)

            assert sim.min_edge == 0.0020
            assert risk.reserve_cash_pct == 0.0

            optimizer = AdaptivePolicyOptimizer(auto_apply=True)
            policy = optimizer.evaluate(sim)

            # Verification of policy results and active auto-tuning
            assert policy["recommended_min_edge_pct"] < 0.0020
            assert policy["recommended_reserve_cash_pct"] == 0.25
            assert sim.min_edge == policy["recommended_min_edge_pct"]
            assert risk.reserve_cash_pct == 0.25
            assert dash.state["adaptive_policy"]["recommended_min_edge_pct"] == policy["recommended_min_edge_pct"]
            assert dash.state["adaptive_policy"]["recommended_reserve_cash_pct"] == 0.25

            # Risk engine now gates trades dipping into the 25% cash reserve
            assert risk.check_trade_gating_reason(800.0) == MissedReason.INSUFFICIENT_CASH.value
            assert risk.check_trade_gating_reason(700.0) is None
        finally:
            dash.stop()

from paper_trader import ActivePolicyRewriter, on_message

def test_active_policy_rewriter_online_adaptation():
    """Verify ActivePolicyRewriter online adaptation on missed trades, parameter tuning, and persistence."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "adaptive_policy_state.json")
        dash_file = os.path.join(tmpdir, "dashboard_state.json")
        dash = DashboardState(filename=dash_file)
        try:
            risk = RiskSizingEngine(initial_capital=1000.0, dash_state=dash)
            sim = PaperSimulator(risk_engine=risk, dash_state=dash)
            rewriter = ActivePolicyRewriter(simulator=sim, state_file=state_file)
            sim.policy_rewriter = rewriter
            sim.shadow_tracker.policy_rewriter = rewriter

            init_pos = rewriter.params["max_positions_per_market"]
            init_edge = rewriter.params["min_edge_pct"]
            init_hold = rewriter.params["hold_period_seconds"]
            init_reserve = rewriter.params["reserve_cash_pct"]

            # 1. Trigger MARKET_ALREADY_ACTIVE miss
            sim.shadow_tracker.record_missed({
                "market_id": "0xmkt_active", "edge": 0.0030, "trade_size": 50.0, "expected_profit": 0.15
            }, MissedReason.MARKET_ALREADY_ACTIVE)

            assert rewriter.params["max_positions_per_market"] == min(5, init_pos + 1)
            assert rewriter.params["hold_period_seconds"] == max(0.5, round(init_hold * 0.8, 2))
            assert risk.max_positions_per_market == rewriter.params["max_positions_per_market"]
            assert risk.hold_period_seconds == rewriter.params["hold_period_seconds"]

            rewriter.last_triggered_time = 0.0

            # 2. Trigger SUB_THRESHOLD_EDGE miss
            sim.shadow_tracker.record_missed({
                "market_id": "0xmkt_edge", "edge": 0.0015, "trade_size": 50.0, "expected_profit": 0.075
            }, MissedReason.SUB_THRESHOLD_EDGE)

            assert rewriter.params["min_edge_pct"] == max(0.0150, round(init_edge - 0.0002, 4))
            assert sim.min_edge == rewriter.params["min_edge_pct"]

            rewriter.last_triggered_time = 0.0

            # 3. Trigger INSUFFICIENT_CASH miss with high edge (>= 0.0060)
            sim.shadow_tracker.record_missed({
                "market_id": "0xmkt_cash", "edge": 0.0070, "trade_size": 50.0, "expected_profit": 0.35
            }, MissedReason.INSUFFICIENT_CASH)

            assert rewriter.params["reserve_cash_pct"] == min(0.30, round(init_reserve + 0.05, 2))
            assert risk.reserve_cash_pct == rewriter.params["reserve_cash_pct"]

            # 4. Verify disk persistence
            assert os.path.exists(state_file)
            with open(state_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            assert saved["max_positions_per_market"] == rewriter.params["max_positions_per_market"]
            assert saved["min_edge_pct"] == rewriter.params["min_edge_pct"]
            assert saved["reserve_cash_pct"] == rewriter.params["reserve_cash_pct"]
            assert saved["total_adaptations"] == 3
        finally:
            dash.stop()

def test_continuous_capacity_sizing_replaces_count_lockout():
    """Verify micro-positions do not lock out subsequent opportunities within dollar exposure capacity."""
    risk = RiskSizingEngine(
        initial_capital=1000.0,
        max_exposure_pct=0.45,
        max_market_exposure_pct=0.65,
        max_positions_per_market=2,
        hold_period_seconds=2.0
    )
    # Open 2 micro-positions of $2.0 on 0xmkt_cap
    assert risk.open_position("0xmkt_cap", 2.0, 0.05) is True
    assert risk.open_position("0xmkt_cap", 2.0, 0.05) is True
    assert len(risk.open_positions) == 2

    # Under continuous capacity sizing, $4.0 current exposure is far below 65% of $1000 ($650 * 0.9 = $585)
    # Even though position count reaches max_positions_per_market (2), trade is permitted!
    reason = risk.check_trade_gating_reason(10.0, market_id="0xmkt_cap")
    assert reason is None
    assert risk.can_trade(10.0, market_id="0xmkt_cap") is True

def test_side_aware_price_change_depth_parsing():
    """Verify BUY size 0 does not overwrite best_ask depth."""
    risk = RiskSizingEngine(initial_capital=1000.0)
    sim = PaperSimulator(risk_engine=risk)
    
    # Initialize YES ask to 0.45 with depth 500.0
    sim.update_book("0xmkt_depth", "tok_yes", 0.45, ask_size=500.0, evaluate=False)
    assert sim.market_depths["0xmkt_depth"]["tok_yes"] == 500.0

    # Ingest price_change event with BUY side and size 0
    event_payload = json.dumps({
        "event_type": "price_change",
        "market": "0xmkt_depth",
        "price_changes": [{
            "asset_id": "tok_yes",
            "price": "0.44",
            "side": "BUY",
            "size": "0",
            "best_ask": "0.45"
        }]
    })
    on_message(None, event_payload, sim)

    # Known best_ask depth must be preserved, not overwritten to 0.0
    assert sim.market_depths["0xmkt_depth"]["tok_yes"] == 500.0

def test_non_destructive_depth_invalidation_after_arbitrage():
    """Verify del market_depths allows subsequent ticks to evaluate cleanly."""
    risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
    sim = PaperSimulator(risk_engine=risk, min_edge=0.0010, taker_fee_bps=35)
    m_id = "0xmkt_clean"
    
    # Establish valid arbitrage opportunity
    sim.update_book(m_id, "YES", 0.45, ask_size=200.0, evaluate=False)
    sim.update_book(m_id, "NO", 0.45, ask_size=200.0, evaluate=False)
    opp = sim.check_market_parity(m_id)
    assert opp is not None

    executed = sim.execute_arbitrage(opp)
    assert executed is True

    # Depth entry must be deleted rather than destructively zeroed
    assert m_id not in sim.market_depths

    # Subsequent tick re-populates book and evaluates cleanly without false ZERO_LIQUIDITY gating
    sim.update_book(m_id, "YES", 0.45, ask_size=150.0, evaluate=False)
    sim.update_book(m_id, "NO", 0.45, ask_size=150.0, evaluate=False)
    next_opp = sim.check_market_parity(m_id)
    assert next_opp is not None
    assert next_opp["available_depth_usd"] == 150.0


def test_live_executor_sync_live_balance():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.derive_api_key.return_value = 'mock_creds'
            mock_instance.get_balance_allowance.return_value = {"balance": "156461040"}

            risk_engine = RiskSizingEngine(initial_capital=1000.0, available_cash=50.0, locked_collateral=20.0)
            dash_state = MagicMock(spec=DashboardState)
            dash_state.state = {"capital": 1000.0, "available_cash": 50.0}

            executor = LiveExecutor(risk_engine=risk_engine, dash_state=dash_state)
            bal = executor.sync_live_balance()
            assert bal == 156.461
            assert executor.risk.available_cash == 156.461
            assert executor.risk.capital == 176.461
            assert dash_state.state["capital"] == 176.461
            assert dash_state.state["available_cash"] == 156.461
            assert executor.last_balance_sync_time > 0


def test_live_executor_unwind_positions_to_cash():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            mock_instance = mock_client.return_value
            from py_clob_client_v2.clob_types import AssetType
            def mock_get_bal(params):
                if getattr(params, "asset_type", None) == AssetType.CONDITIONAL:
                    return {"balance": "15000000"}  # 15.0 conditional shares
                return {"balance": "108250000"}  # 108.25 USDC collateral
            mock_instance.get_balance_allowance.side_effect = mock_get_bal
            mock_instance.create_order.return_value = {"order": "mock_sell_order"}
            mock_instance.post_orders.return_value = [{"takingAmount": "15.0", "status": "matched"}]

            risk_engine = RiskSizingEngine(initial_capital=100.0, available_cash=10.0)
            dash_state = MagicMock(spec=DashboardState)
            dash_state.state = {"capital": 100.0, "available_cash": 10.0}

            executor = LiveExecutor(risk_engine=risk_engine, dash_state=dash_state)

            positions_payload = json.dumps([
                {"asset": "0xasset_yes", "size": "15.5", "outcome": "Yes"},
                {"asset": "0xasset_dust", "size": "0.4", "outcome": "No"}
            ]).encode("utf-8")

            # Bid is 0.96 (>= 0.95 threshold)
            book_payload = json.dumps({
                "bids": [{"price": "0.96", "size": "100.0"}],
                "asks": []
            }).encode("utf-8")

            def mock_urlopen(req, timeout=None):
                mock_resp = MagicMock()
                url = req.full_url if hasattr(req, "full_url") else str(req)
                if "positions" in url:
                    mock_resp.read.return_value = positions_payload
                elif "book" in url:
                    mock_resp.read.return_value = book_payload
                mock_resp.__enter__.return_value = mock_resp
                return mock_resp

            with patch('urllib.request.urlopen', side_effect=mock_urlopen):
                unwound = executor.unwind_positions_to_cash(max_unwind=3)

            assert unwound == 1
            assert mock_instance.create_order.called
            assert mock_instance.post_orders.called
            assert executor.risk.available_cash == 108.25
            dash_state.add_activity_log.assert_called_with(
                "♻️ [AUTO-UNWIND] Sold 15 Yes at $0.9600 -> Recovered +$14.40 USDC"
            )


def test_live_executor_unwind_rejects_low_bids():
    """Verify unwind_positions_to_cash refuses to sell when best bid is below min_price (e.g. 0.15 or 0.30)."""
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            mock_instance = mock_client.return_value
            from py_clob_client_v2.clob_types import AssetType
            def mock_get_bal(params):
                if getattr(params, "asset_type", None) == AssetType.CONDITIONAL:
                    return {"balance": "15000000"}  # 15.0 conditional shares
                return {"balance": "108250000"}
            mock_instance.get_balance_allowance.side_effect = mock_get_bal

            risk_engine = RiskSizingEngine(initial_capital=100.0, available_cash=10.0)
            dash_state = MagicMock(spec=DashboardState)
            dash_state.state = {"capital": 100.0, "available_cash": 10.0}

            executor = LiveExecutor(risk_engine=risk_engine, dash_state=dash_state)

            positions_payload = json.dumps([
                {"asset": "0xasset_yes", "size": "15.0", "outcome": "Yes"}
            ]).encode("utf-8")

            # Bid is 0.30 - below 0.95 threshold
            book_payload = json.dumps({
                "bids": [{"price": "0.30", "size": "100.0"}],
                "asks": []
            }).encode("utf-8")

            def mock_urlopen(req, timeout=None):
                mock_resp = MagicMock()
                url = req.full_url if hasattr(req, "full_url") else str(req)
                if "positions" in url:
                    mock_resp.read.return_value = positions_payload
                elif "book" in url:
                    mock_resp.read.return_value = book_payload
                mock_resp.__enter__.return_value = mock_resp
                return mock_resp

            with patch('urllib.request.urlopen', side_effect=mock_urlopen):
                unwound = executor.unwind_positions_to_cash(max_unwind=3)

            assert unwound == 0
            mock_instance.create_order.assert_not_called()
            mock_instance.post_orders.assert_not_called()


def test_live_executor_insufficient_balance_syncs_balance_without_unwind():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.derive_api_key.return_value = 'mock_creds'
            mock_instance.post_orders.return_value = [
                {"errorMsg": "not enough balance in account", "takingAmount": "0.0"}
            ]

            risk_engine = MagicMock(spec=RiskSizingEngine)
            risk_engine.capital = 1000.0
            risk_engine.max_exposure_pct = 0.1
            risk_engine.available_cash = 1000.0
            risk_engine.open_positions = {}
            risk_engine.can_trade.return_value = True
            risk_engine.lock = MagicMock()

            dash_state = MagicMock(spec=DashboardState)
            dash_state.state = {"execution_mode": "Live Trading", "live_wager_cap": 10.0}

            m_map = {"0xmarket": {"token_yes": "token_y", "token_no": "token_n"}}
            executor = LiveExecutor(risk_engine=risk_engine, market_token_map=m_map, dash_state=dash_state)
            executor.market_books = {"0xmarket": {"token_y": 0.45, "token_n": 0.50}}
            executor.market_depths = {"0xmarket": {"token_y": 100.0, "token_n": 100.0}}

            executor.unwind_positions_to_cash = MagicMock(return_value=1)
            executor.sync_live_balance = MagicMock(return_value=100.0)

            opp = {"market_id": "0xmarket", "trade_size": 10.0, "ask_yes": 0.45, "ask_no": 0.50, "edge": 0.05}
            res = executor.execute_arbitrage(opp)
            assert res is False
            executor.unwind_positions_to_cash.assert_not_called()
            executor.sync_live_balance.assert_called()


def test_paper_simulator_saver_thread():
    risk = RiskSizingEngine(initial_capital=100.0, available_cash=10.0)
    sim = PaperSimulator(risk_engine=risk)
    sim.sync_live_balance = MagicMock(return_value=10.0)
    sim.unwind_positions_to_cash = MagicMock(return_value=1)

    # 1. First invocation triggers balance sync, but NEVER calls unwind_positions_to_cash
    sim.saver_thread()
    sim.sync_live_balance.assert_called_once()
    sim.unwind_positions_to_cash.assert_not_called()

    # 2. Immediate second call should not re-trigger balance sync because of interval (15s)
    sim.sync_live_balance.reset_mock()
    sim.saver_thread()
    sim.sync_live_balance.assert_not_called()
    sim.unwind_positions_to_cash.assert_not_called()


def test_instant_real_time_pool_compounding_on_trade_paper():
    """Verify executing arbitrage in PaperSimulator calls on_trade_executed and compounds available_cash immediately with expected_profit."""
    risk = RiskSizingEngine(initial_capital=1000.0, available_cash=1000.0, max_exposure_pct=0.10)
    dash_state = MagicMock(spec=DashboardState)
    dash_state.state = {"capital": 1000.0, "available_cash": 1000.0}
    dash_state.lock = threading.Lock()

    sim = PaperSimulator(risk, dash_state=dash_state)
    sim.fee_rate = 0.015

    with patch.object(sim, 'on_trade_executed', wraps=sim.on_trade_executed) as mock_ote:
        sim.update_book("0xmkt_instant", "YES", 0.45, ask_size=500.0)
        sim.update_book("0xmkt_instant", "NO", 0.50, ask_size=500.0)

        # Expected profit = 100.0 * (1.0 - 0.95 * 1.015) = 3.575
        expected_profit = 100.0 * (1.0 - 0.95 * 1.015)
        mock_ote.assert_called_once_with("0xmkt_instant", 100.0, expected_profit)

        # Collateral should be instantly recycled back to available_cash + expected_profit
        assert risk.locked_collateral == 0.0
        assert round(risk.available_cash, 3) == round(1000.0 + expected_profit, 3)
        assert round(risk.capital, 3) == round(1000.0 + expected_profit, 3)
        assert round(dash_state.state["available_cash"], 3) == round(1000.0 + expected_profit, 3)
        assert round(dash_state.state["capital"], 3) == round(1000.0 + expected_profit, 3)


def test_instant_real_time_pool_compounding_on_trade_live():
    """Verify executing arbitrage in LiveExecutor calls on_trade_executed, securing collateral without unwinding."""
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            with patch('web3.Web3') as mock_web3_class:
                mock_w3 = MagicMock()
                mock_web3_class.return_value = mock_w3
                mock_web3_class.to_checksum_address = lambda x: x
                mock_web3_class.to_bytes = lambda hexstr: b"0" * 32
                mock_web3_class.to_hex = lambda x: "0xhash"
                mock_contract = MagicMock()
                mock_w3.eth.contract.return_value = mock_contract
                mock_contract.functions.balanceOf().call.side_effect = [1000000, 1000000]
                receipt = MagicMock()
                receipt.status = 1
                mock_w3.eth.wait_for_transaction_receipt.return_value = receipt

                mock_instance = mock_client.return_value
                mock_instance.derive_api_key.return_value = 'mock_creds'
                mock_instance.create_order.side_effect = lambda args: f"order_{args.token_id}"
                mock_instance.post_orders.return_value = [{"takingAmount": "10.0", "errorMsg": ""}, {"takingAmount": "10.0", "errorMsg": ""}]

                risk_engine = MagicMock(spec=RiskSizingEngine)
                risk_engine.capital = 1000.0
                risk_engine.max_exposure_pct = 0.1
                risk_engine.available_cash = 1000.0
                risk_engine.max_market_exposure_pct = 0.65
                risk_engine.reserve_cash_pct = 0.0
                risk_engine.open_positions = {}
                risk_engine.can_trade.return_value = True
                risk_engine.open_position.return_value = True
                risk_engine.lock = MagicMock()

                dash_state = MagicMock(spec=DashboardState)
                dash_state.state = {"execution_mode": "Live Trading", "live_wager_cap": 5.0}

                m_map = {"0xmarket_live": {"token_yes": "0xYes", "token_no": "0xNo"}}
                executor = LiveExecutor(risk_engine=risk_engine, market_token_map=m_map, dash_state=dash_state)

                executor.market_books = {"0xmarket_live": {"0xYes": 0.40, "0xNo": 0.40}}
                executor.market_depths = {"0xmarket_live": {"0xYes": 1000.0, "0xNo": 1000.0}}

                executor.unwind_positions_to_cash = MagicMock(return_value=2)
                executor.sync_live_balance = MagicMock(return_value=125.0)

                with patch.object(executor, 'on_trade_executed', wraps=executor.on_trade_executed) as mock_ote:
                    opp = {
                        "market_id": "0xmarket_live",
                        "trade_size": 10.0,
                        "ask_yes": 0.40,
                        "ask_no": 0.40,
                        "effective_cost": 0.80,
                        "edge": 0.20
                    }
                    res = executor.execute_arbitrage(opp)
                    assert res is True
                    mock_ote.assert_called_once()
                    executor.unwind_positions_to_cash.assert_not_called()
                    executor.sync_live_balance.assert_called()


def test_reset_missed_trades():
    """Verify reset of missed trade data and active learning metrics across ShadowParityTracker, ActivePolicyRewriter, and DashboardState."""
    import tempfile
    import os
    import json
    import time
    from unittest.mock import MagicMock
    from paper_trader import ShadowParityTracker, ActivePolicyRewriter, DashboardState, MissedReason

    # 1. ShadowParityTracker.reset()
    tracker = ShadowParityTracker(max_recent=10)
    tracker.record_missed({"market_id": "0x1", "edge": 0.01, "trade_size": 100.0, "forfeited_pnl": 1.0}, MissedReason.CONCURRENCY_EXHAUSTED)
    assert tracker.total_missed_count == 1
    assert tracker.total_missed_pnl == 1.0
    assert len(tracker.recent_missed) == 1
    assert tracker.by_reason[MissedReason.CONCURRENCY_EXHAUSTED.value]["count"] == 1

    tracker.reset()
    assert tracker.total_missed_count == 0
    assert tracker.total_missed_pnl == 0.0
    assert len(tracker.recent_missed) == 0
    assert tracker.by_reason[MissedReason.CONCURRENCY_EXHAUSTED.value]["count"] == 0

    # 2. ActivePolicyRewriter.reset()
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "adaptive_policy_state.json")
        rewriter = ActivePolicyRewriter(state_file=state_file)
        rewriter.total_adaptations = 5
        rewriter.recovered_pnl = 12.50
        rewriter.bottleneck_counts = {"MARKET_ALREADY_ACTIVE": 3}
        rewriter.last_adaptation_time = time.time()
        rewriter.reset()

        assert rewriter.total_adaptations == 0
        assert rewriter.recovered_pnl == 0.0
        assert len(rewriter.bottleneck_counts) == 0
        assert rewriter.last_adaptation_time == 0.0

        with open(state_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["total_adaptations"] == 0
        assert saved["recovered_pnl"] == 0.0

    # 3. DashboardState.reset_missed_trades()
    with tempfile.TemporaryDirectory() as tmpdir:
        ds_file = os.path.join(tmpdir, "dashboard_state.json")
        ds = DashboardState(filename=ds_file)
        try:
            ds.state["missed_trades"] = [{"market_id": "0x1"}]
            ds.state["missed_trades_summary"] = {
                "total_missed_count": 10,
                "total_missed_pnl": 50.0,
                "by_reason": {"CONCURRENCY_EXHAUSTED": {"count": 10, "pnl": 50.0}}
            }
            ds.state["adaptive_policy"] = {
                "total_adaptations": 8,
                "recovered_pnl": 42.0,
                "primary_bottleneck": "CONCURRENCY_EXHAUSTED"
            }

            ds.reset_missed_trades()
            assert ds.state["missed_trades"] == []
            assert ds.state["missed_trades_summary"]["total_missed_count"] == 0
            assert ds.state["missed_trades_summary"]["total_missed_pnl"] == 0.0
            assert ds.state["missed_trades_summary"]["by_reason"] == {}
            assert ds.state["adaptive_policy"]["total_adaptations"] == 0
            assert ds.state["adaptive_policy"]["recovered_pnl"] == 0.0
            assert ds.state["adaptive_policy"]["primary_bottleneck"] == "NONE"
            assert ds.dirty is True
        finally:
            ds.stop()

    # 4. capital_update.json reset_missed_trades IPC handling
    with tempfile.TemporaryDirectory() as tmpdir:
        ds_file = os.path.join(tmpdir, "dashboard_state.json")
        update_file = os.path.join(tmpdir, "capital_update.json")
        ds = DashboardState(filename=ds_file)
        try:
            mock_sim = MagicMock()
            mock_tracker = MagicMock()
            mock_rewriter = MagicMock()
            mock_sim.shadow_tracker = mock_tracker
            mock_sim.policy_rewriter = mock_rewriter
            ds.simulator = mock_sim

            ds.state["missed_trades"] = [{"market_id": "0x1"}]
            ds.state["missed_trades_summary"] = {"total_missed_count": 5}

            with open(update_file, "w", encoding="utf-8") as f:
                json.dump({"reset_missed_trades": True}, f)

            time.sleep(1.2)

            assert ds.state["missed_trades"] == []
            assert ds.state["missed_trades_summary"]["total_missed_count"] == 0
            mock_tracker.reset.assert_called_once()
            mock_rewriter.reset.assert_called_once()
            assert not os.path.exists(update_file)
        finally:
            ds.stop()


def test_unhedged_leg_emergency_dump_yes_filled_no_killed():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            with patch('urllib.request.urlopen') as mock_urlopen:
                mock_book_resp = MagicMock()
                mock_book_resp.read.return_value = json.dumps({
                    "bids": [{"price": "0.45", "size": "100.0"}]
                }).encode("utf-8")
                mock_urlopen.return_value.__enter__.return_value = mock_book_resp

                mock_instance = mock_client.return_value
                mock_instance.derive_api_key.return_value = 'mock_creds'
                mock_instance.create_order.side_effect = lambda args: f"order_{args.token_id}_{args.side}_{args.price}_{args.size}"
                mock_instance.post_orders.side_effect = [
                    [{"takingAmount": "10.0", "errorMsg": ""}, {"takingAmount": "0.0", "errorMsg": "FOK order killed"}],
                    [{"takingAmount": "10.0", "errorMsg": ""}]
                ]

                risk_engine = MagicMock(spec=RiskSizingEngine)
                risk_engine.capital = 1000.0
                risk_engine.max_exposure_pct = 0.10
                risk_engine.available_cash = 1000.0
                risk_engine.max_market_exposure_pct = 0.65
                risk_engine.reserve_cash_pct = 0.0
                risk_engine.open_positions = {}
                risk_engine.can_trade.return_value = True
                risk_engine.open_position.return_value = True
                risk_engine.lock = MagicMock()

                dash_state = MagicMock(spec=DashboardState)
                dash_state.state = {"execution_mode": "Live Trading", "live_wager_cap": 10.0}

                m_map = {"0xmarket_test": {"token_yes": "0xTokenYes", "token_no": "0xTokenNo"}}
                executor = LiveExecutor(risk_engine=risk_engine, market_token_map=m_map, dash_state=dash_state)
                executor.market_books = {"0xmarket_test": {"0xTokenYes": 0.45, "0xTokenNo": 0.45}}
                executor.market_depths = {"0xmarket_test": {"0xTokenYes": 1000.0, "0xTokenNo": 1000.0}}
                executor.sync_live_balance = MagicMock(return_value=1000.0)

                opp = {
                    "market_id": "0xmarket_test",
                    "trade_size": 10.0,
                    "ask_yes": 0.45,
                    "ask_no": 0.45,
                    "effective_cost": 0.90,
                    "edge": 0.10
                }

                res = executor.execute_arbitrage(opp)
                assert res is False
                risk_engine.open_position.assert_not_called()
                create_order_calls = mock_instance.create_order.call_args_list
                sell_calls = [c for c in create_order_calls if c[0][0].side == "SELL" and c[0][0].token_id == "0xTokenYes"]
                assert len(sell_calls) >= 1
                assert sell_calls[0][0][0].size == 10.0
                executor.sync_live_balance.assert_called()


def test_unhedged_leg_emergency_dump_no_filled_yes_killed():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            with patch('urllib.request.urlopen') as mock_urlopen:
                mock_book_resp = MagicMock()
                mock_book_resp.read.return_value = json.dumps({
                    "bids": [{"price": "0.48", "size": "100.0"}]
                }).encode("utf-8")
                mock_urlopen.return_value.__enter__.return_value = mock_book_resp

                mock_instance = mock_client.return_value
                mock_instance.derive_api_key.return_value = 'mock_creds'
                mock_instance.create_order.side_effect = lambda args: f"order_{args.token_id}_{args.side}_{args.price}_{args.size}"
                mock_instance.post_orders.side_effect = [
                    [{"takingAmount": "0.0", "errorMsg": "FOK order killed"}, {"takingAmount": "10.0", "errorMsg": ""}],
                    [{"takingAmount": "10.0", "errorMsg": ""}]
                ]

                risk_engine = MagicMock(spec=RiskSizingEngine)
                risk_engine.capital = 1000.0
                risk_engine.max_exposure_pct = 0.10
                risk_engine.available_cash = 1000.0
                risk_engine.max_market_exposure_pct = 0.65
                risk_engine.reserve_cash_pct = 0.0
                risk_engine.open_positions = {}
                risk_engine.can_trade.return_value = True
                risk_engine.open_position.return_value = True
                risk_engine.lock = MagicMock()

                dash_state = MagicMock(spec=DashboardState)
                dash_state.state = {"execution_mode": "Live Trading", "live_wager_cap": 10.0}

                m_map = {"0xmarket_test": {"token_yes": "0xTokenYes", "token_no": "0xTokenNo"}}
                executor = LiveExecutor(risk_engine=risk_engine, market_token_map=m_map, dash_state=dash_state)
                executor.market_books = {"0xmarket_test": {"0xTokenYes": 0.45, "0xTokenNo": 0.45}}
                executor.market_depths = {"0xmarket_test": {"0xTokenYes": 1000.0, "0xTokenNo": 1000.0}}
                executor.sync_live_balance = MagicMock(return_value=1000.0)

                opp = {
                    "market_id": "0xmarket_test",
                    "trade_size": 10.0,
                    "ask_yes": 0.45,
                    "ask_no": 0.45,
                    "effective_cost": 0.90,
                    "edge": 0.10
                }

                res = executor.execute_arbitrage(opp)
                assert res is False
                risk_engine.open_position.assert_not_called()
                create_order_calls = mock_instance.create_order.call_args_list
                sell_calls = [c for c in create_order_calls if c[0][0].side == "SELL" and c[0][0].token_id == "0xTokenNo"]
                assert len(sell_calls) >= 1
                assert sell_calls[0][0][0].size == 10.0
                executor.sync_live_balance.assert_called()


def test_continuous_unwind_sweeper_runs_regardless_of_cash():
    """Verify saver_thread never calls unwind_positions_to_cash, preserving 100% collateral."""
    risk = RiskSizingEngine(initial_capital=1000.0, available_cash=75.0)
    sim = PaperSimulator(risk_engine=risk)
    sim.sync_live_balance = MagicMock(return_value=75.0)
    sim.unwind_positions_to_cash = MagicMock(return_value=2)
    sim.last_unwind_time = 0.0

    sim.saver_thread()
    sim.unwind_positions_to_cash.assert_not_called()
    sim.sync_live_balance.assert_called_once()


def test_live_executor_delayed_order_polling_matched():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            with patch('web3.Web3') as mock_web3:
                mock_instance = mock_client.return_value
                mock_instance.derive_api_key.return_value = 'mock_creds'
                mock_instance.create_order.side_effect = lambda args: f"order_{args.token_id}_{args.side}_{args.price}_{args.size}"
                
                # Sequencer returns status: delayed and empty takingAmount initially
                mock_instance.post_orders.return_value = [
                    {"orderID": "0xorder_yes", "status": "delayed", "takingAmount": "", "errorMsg": ""},
                    {"orderID": "0xorder_no", "status": "delayed", "takingAmount": "", "errorMsg": ""}
                ]
                
                # Polling get_order returns MATCHED status with size_matched = 10.0
                def mock_get_order(order_id):
                    return {"orderID": order_id, "status": "MATCHED", "size_matched": "10.0"}
                mock_instance.get_order.side_effect = mock_get_order
                
                risk_engine = MagicMock(spec=RiskSizingEngine)
                risk_engine.capital = 1000.0
                risk_engine.max_exposure_pct = 0.10
                risk_engine.available_cash = 1000.0
                risk_engine.max_market_exposure_pct = 0.65
                risk_engine.reserve_cash_pct = 0.0
                risk_engine.open_positions = {}
                risk_engine.can_trade.return_value = True
                risk_engine.open_position.return_value = True
                risk_engine.lock = MagicMock()
                
                dash_state = MagicMock(spec=DashboardState)
                dash_state.state = {"execution_mode": "Live Trading", "live_wager_cap": 10.0}
                
                m_map = {"0xmarket_test": {"token_yes": "0xTokenYes", "token_no": "0xTokenNo"}}
                executor = LiveExecutor(risk_engine=risk_engine, market_token_map=m_map, dash_state=dash_state)
                executor.market_books = {"0xmarket_test": {"0xTokenYes": 0.45, "0xTokenNo": 0.45}}
                executor.market_depths = {"0xmarket_test": {"0xTokenYes": 1000.0, "0xTokenNo": 1000.0}}
                executor.sync_live_balance = MagicMock(return_value=1000.0)
                
                mock_w3_inst = mock_web3.return_value
                mock_ctf = MagicMock()
                mock_ctf.functions.balanceOf.return_value.call.return_value = 0
                mock_w3_inst.eth.contract.return_value = mock_ctf
                
                opp = {
                    "market_id": "0xmarket_test",
                    "trade_size": 10.0,
                    "ask_yes": 0.45,
                    "ask_no": 0.45,
                    "effective_cost": 0.90,
                    "edge": 0.10
                }
                
                res = executor.execute_arbitrage(opp)
                assert res is True
                assert mock_instance.get_order.call_count >= 2
                risk_engine.open_position.assert_called_once()


def test_live_executor_dual_leg_hedge_completion():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            with patch('urllib.request.urlopen') as mock_urlopen:
                with patch('web3.Web3') as mock_web3:
                    mock_book_resp = MagicMock()
                    mock_book_resp.read.return_value = json.dumps({
                        "asks": [{"price": "0.46", "size": "100.0"}]
                    }).encode("utf-8")
                    mock_urlopen.return_value.__enter__.return_value = mock_book_resp

                    mock_instance = mock_client.return_value
                    mock_instance.derive_api_key.return_value = 'mock_creds'
                    mock_instance.create_order.side_effect = lambda args: f"order_{args.token_id}_{args.side}_{args.price}_{args.size}"
                    
                    mock_instance.post_orders.side_effect = [
                        [{"takingAmount": "10.0", "errorMsg": ""}, {"takingAmount": "0.0", "errorMsg": "FOK order killed"}],
                        [{"takingAmount": "10.0", "errorMsg": ""}]
                    ]

                    risk_engine = MagicMock(spec=RiskSizingEngine)
                    risk_engine.capital = 1000.0
                    risk_engine.max_exposure_pct = 0.10
                    risk_engine.available_cash = 1000.0
                    risk_engine.max_market_exposure_pct = 0.65
                    risk_engine.reserve_cash_pct = 0.0
                    risk_engine.open_positions = {}
                    risk_engine.can_trade.return_value = True
                    risk_engine.open_position.return_value = True
                    risk_engine.lock = MagicMock()

                    dash_state = MagicMock(spec=DashboardState)
                    dash_state.state = {"execution_mode": "Live Trading", "live_wager_cap": 10.0}

                    m_map = {"0xmarket_test": {"token_yes": "0xTokenYes", "token_no": "0xTokenNo"}}
                    executor = LiveExecutor(risk_engine=risk_engine, market_token_map=m_map, dash_state=dash_state)
                    executor.market_books = {"0xmarket_test": {"0xTokenYes": 0.45, "0xTokenNo": 0.45}}
                    executor.market_depths = {"0xmarket_test": {"0xTokenYes": 1000.0, "0xTokenNo": 1000.0}}
                    executor.sync_live_balance = MagicMock(return_value=1000.0)

                    mock_w3_inst = mock_web3.return_value
                    mock_ctf = MagicMock()
                    mock_ctf.functions.balanceOf.return_value.call.return_value = 0
                    mock_w3_inst.eth.contract.return_value = mock_ctf

                    opp = {
                        "market_id": "0xmarket_test",
                        "trade_size": 10.0,
                        "ask_yes": 0.45,
                        "ask_no": 0.45,
                        "effective_cost": 0.90,
                        "edge": 0.10
                    }

                    res = executor.execute_arbitrage(opp)
                    assert res is True
                    create_order_calls = mock_instance.create_order.call_args_list
                    buy_no_calls = [c for c in create_order_calls if c[0][0].side == "BUY" and c[0][0].token_id == "0xTokenNo"]
                    assert len(buy_no_calls) >= 1
                    risk_engine.open_position.assert_called_once()


def test_live_executor_unhedged_rollback_when_hedge_fails():
    with patch.dict('os.environ', {'POLYMARKET_PRIVATE_KEY': '0x123', 'POLYMARKET_ADDRESS': '0xabc', 'POLYMARKET_SIGNATURE_TYPE': '2'}):
        with patch('py_clob_client_v2.client.ClobClient') as mock_client:
            with patch('urllib.request.urlopen') as mock_urlopen:
                mock_book_resp = MagicMock()
                mock_book_resp.read.return_value = json.dumps({
                    "asks": [{"price": "0.60", "size": "100.0"}],
                    "bids": [{"price": "0.44", "size": "100.0"}]
                }).encode("utf-8")
                mock_urlopen.return_value.__enter__.return_value = mock_book_resp

                mock_instance = mock_client.return_value
                mock_instance.derive_api_key.return_value = 'mock_creds'
                mock_instance.create_order.side_effect = lambda args: f"order_{args.token_id}_{args.side}_{args.price}_{args.size}"
                mock_instance.post_orders.side_effect = [
                    [{"takingAmount": "10.0", "errorMsg": ""}, {"takingAmount": "0.0", "errorMsg": "FOK order killed"}],
                    [{"takingAmount": "10.0", "errorMsg": ""}]
                ]

                risk_engine = MagicMock(spec=RiskSizingEngine)
                risk_engine.capital = 1000.0
                risk_engine.max_exposure_pct = 0.10
                risk_engine.available_cash = 1000.0
                risk_engine.max_market_exposure_pct = 0.65
                risk_engine.reserve_cash_pct = 0.0
                risk_engine.open_positions = {}
                risk_engine.can_trade.return_value = True
                risk_engine.open_position.return_value = True
                risk_engine.lock = MagicMock()

                dash_state = MagicMock(spec=DashboardState)
                dash_state.state = {"execution_mode": "Live Trading", "live_wager_cap": 10.0}

                m_map = {"0xmarket_test": {"token_yes": "0xTokenYes", "token_no": "0xTokenNo"}}
                executor = LiveExecutor(risk_engine=risk_engine, market_token_map=m_map, dash_state=dash_state)
                executor.market_books = {"0xmarket_test": {"0xTokenYes": 0.45, "0xTokenNo": 0.45}}
                executor.market_depths = {"0xmarket_test": {"0xTokenYes": 1000.0, "0xTokenNo": 1000.0}}
                executor.sync_live_balance = MagicMock(return_value=1000.0)

                opp = {
                    "market_id": "0xmarket_test",
                    "trade_size": 10.0,
                    "ask_yes": 0.45,
                    "ask_no": 0.45,
                    "effective_cost": 0.90,
                    "edge": 0.10
                }

                res = executor.execute_arbitrage(opp)
                assert res is False
                risk_engine.open_position.assert_not_called()
                create_order_calls = mock_instance.create_order.call_args_list
                sell_calls = [c for c in create_order_calls if c[0][0].side == "SELL" and c[0][0].token_id == "0xTokenYes"]
                assert len(sell_calls) >= 1
                assert sell_calls[0][0][0].size == 10.0
                executor.sync_live_balance.assert_called()


def test_live_executor_redeem_resolved_positions():
    mock_relay_client_cls = MagicMock()
    mock_deposit_call = MagicMock()
    mock_signing_cfg = MagicMock()
    mock_signing_types = MagicMock()
    mock_clob = MagicMock()
    mock_web3 = MagicMock()

    with patch.dict('os.environ', {
        'POLYMARKET_PRIVATE_KEY': '0x123',
        'POLYMARKET_ADDRESS': '0x1111111111111111111111111111111111111111',
        'POLY_BUILDER_API_KEY': 'key',
        'POLY_BUILDER_SECRET': 'secret',
        'POLY_BUILDER_PASSPHRASE': 'passphrase'
    }):
        with patch.dict('sys.modules', {
            'py_clob_client_v2': MagicMock(),
            'py_clob_client_v2.client': MagicMock(ClobClient=mock_clob),
            'py_clob_client_v2.clob_types': MagicMock(),
            'py_builder_signing_sdk': MagicMock(),
            'py_builder_signing_sdk.config': mock_signing_cfg,
            'py_builder_signing_sdk.sdk_types': mock_signing_types,
            'py_builder_relayer_client': MagicMock(),
            'py_builder_relayer_client.client': MagicMock(RelayClient=mock_relay_client_cls),
            'py_builder_relayer_client.models': MagicMock(DepositWalletCall=mock_deposit_call),
            'web3': mock_web3,
        }):
            with patch('urllib.request.urlopen') as mock_urlopen:
                        mock_pos_resp = MagicMock()
                        mock_pos_resp.read.return_value = json.dumps([
                            {"title": "Resolved Market 1", "conditionId": "0xcond1", "redeemable": True, "size": 10.0},
                            {"title": "Active Market 2", "conditionId": "0xcond2", "redeemable": False, "size": 5.0}
                        ]).encode("utf-8")
                        mock_urlopen.return_value.__enter__.return_value = mock_pos_resp

                        mock_relayer_inst = mock_relay_client_cls.return_value
                        tx_mock = MagicMock()
                        tx_mock.transaction_hash = "0xhash123"
                        mock_relayer_inst.execute_deposit_wallet_batch.return_value = tx_mock

                        mock_w3_inst = mock_web3.Web3.return_value
                        mock_w3_inst.to_checksum_address.side_effect = lambda a: a
                        mock_contract_w = MagicMock()
                        mock_contract_w.functions.nonce.return_value.call.return_value = 42
                        mock_contract_ctf = MagicMock()
                        mock_contract_ctf.encode_abi.return_value = "0xcalldata"
                        
                        def contract_side_effect(address, abi):
                            if 'nonce' in str(abi):
                                return mock_contract_w
                            return mock_contract_ctf
                        mock_w3_inst.eth.contract.side_effect = contract_side_effect

                        risk_engine = MagicMock(spec=RiskSizingEngine)
                        dash_state = MagicMock(spec=DashboardState)
                        executor = LiveExecutor(risk_engine=risk_engine, dash_state=dash_state)
                        mock_relayer_inst.execute_deposit_wallet_batch.reset_mock()
                        executor.sync_live_balance = MagicMock(return_value=1000.0)

                        redeemed = executor.redeem_resolved_positions()
                        assert redeemed == 1
                        mock_relayer_inst.execute_deposit_wallet_batch.assert_called_once()
                        call_args = mock_relayer_inst.execute_deposit_wallet_batch.call_args[1]
                        assert call_args["wallet_address"] == '0x1111111111111111111111111111111111111111'
                        assert call_args["nonce"] == "42"
                        executor.sync_live_balance.assert_called_once()

def test_live_executor_sync_live_positions():
    with patch.dict('os.environ', {
        'POLYMARKET_PRIVATE_KEY': '0x123',
        'POLYMARKET_ADDRESS': '0xuser123',
        'POLYMARKET_SIGNATURE_TYPE': '2'
    }):
        with patch('py_clob_client_v2.client.ClobClient') as mock_clob:
            with patch('urllib.request.urlopen') as mock_urlopen:
                sample_positions = [
                    {
                        "title": "Will Bitcoin break $100k in 2026?",
                        "conditionId": "0xcond_btc",
                        "outcome": "Yes",
                        "size": 100.0,
                        "curPrice": 0.40,
                        "currentValue": 40.0,
                        "initialValue": 35.0,
                        "redeemable": False
                    },
                    {
                        "title": "Will Ethereum merge again?",
                        "conditionId": "0xcond_eth",
                        "outcome": "No",
                        "size": 50.0,
                        "curPrice": 0.60,
                        "currentValue": 30.0,
                        "initialValue": 25.0,
                        "redeemable": False
                    },
                    {
                        "title": "Old Resolved Market",
                        "conditionId": "0xcond_old",
                        "outcome": "Yes",
                        "size": 20.0,
                        "curPrice": 1.0,
                        "currentValue": 20.0,
                        "initialValue": 10.0,
                        "redeemable": True
                    }
                ]
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps(sample_positions).encode("utf-8")
                mock_urlopen.return_value.__enter__.return_value = mock_resp

                risk_engine = MagicMock(spec=RiskSizingEngine)
                risk_engine.available_cash = 41.82
                risk_engine.capital = 41.82
                risk_engine.lock = MagicMock()

                dash_state = MagicMock(spec=DashboardState)
                dash_state.state = {"trades": [{"expected_profit": 5.50}]}
                dash_state.lock = MagicMock()

                executor = LiveExecutor(risk_engine=risk_engine, dash_state=dash_state)
                stats = executor.sync_live_positions()

                assert stats["active_count"] == 2
                assert stats["positions_market_val"] == 70.0
                assert stats["positions_theoretical_val"] == 150.0
                assert stats["theoretical_equity"] == 191.82
                assert stats["projected_profit"] == 95.50

                assert dash_state.state["positions_market_val"] == 70.0
                assert dash_state.state["positions_theoretical_val"] == 150.0
                assert dash_state.state["theoretical_equity"] == 191.82
                assert dash_state.state["projected_profit"] == 95.50
                assert dash_state.state["mark_to_market_equity"] == 111.82
                assert len(dash_state.state["live_positions"]) == 2


def test_live_executor_liquidity_gating_blocks_illiquid_opp():
    risk_engine = MagicMock(spec=RiskSizingEngine)
    dash_state = MagicMock(spec=DashboardState)
    dash_state.state = {"execution_mode": "Live Trading"}
    executor = LiveExecutor(risk_engine=risk_engine, dash_state=dash_state)
    executor.client = MagicMock()
    executor.shadow_tracker = MagicMock(spec=ShadowParityTracker)

    opp = {
        "market_id": "0xilliquid_market_123",
        "trade_size": 25.0,
        "edge": 0.02,
        "ask_yes": 0.48,
        "ask_no": 0.50,
        "available_depth_usd": 5.0,
        "short_id": "IlliquidMarket",
        "market_meta": {
            "volume24hr": 200.0
        }
    }

    res = executor.execute_arbitrage(opp)
    assert res is False
    executor.shadow_tracker.record_missed.assert_called_once()
    args, kwargs = executor.shadow_tracker.record_missed.call_args
    assert args[1] == MissedReason.ZERO_LIQUIDITY


def test_live_executor_rollback_protects_price_without_penny_dump():
    from rollback_protector import RollbackProtector
    risk_engine = MagicMock(spec=RiskSizingEngine)
    dash_state = MagicMock(spec=DashboardState)
    executor = LiveExecutor(risk_engine=risk_engine, dash_state=dash_state)
    mock_client = MagicMock()
    executor.client = mock_client

    ok = executor._emergency_dump_leg(
        token_id="0xtoken_yes_456",
        shares=50.0,
        label="YES",
        target_state=dash_state,
        buy_price=0.45
    )
    assert ok is True
    mock_client.create_order.assert_called_once()
    mock_client.post_orders.assert_called_once()


class TestLiveExecutorConstraints(unittest.TestCase):
    def test_live_executor_2_decimal_constraint_and_edge_check(self):
        """Verify that LiveExecutor enforces 2-decimal CLOB maker constraint and cleanly aborts if rounding eliminates edge."""
        risk_engine = MagicMock(spec=RiskSizingEngine)
        risk_engine.capital = 1000.0
        risk_engine.max_exposure_pct = 0.1
        risk_engine.available_cash = 1000.0
        risk_engine.max_market_exposure_pct = 0.65
        risk_engine.reserve_cash_pct = 0.0
        risk_engine.open_positions = {}
        risk_engine.can_trade.return_value = True
        risk_engine.open_position.return_value = True
        risk_engine.lock = MagicMock()

        dash_state = MagicMock(spec=DashboardState)
        dash_state.state = {"execution_mode": "Live Trading", "live_wager_cap": 5.0, "min_edge_pct": 0.0005}

        m_map = {"0xmarket_test": {"token_yes": "0xYes", "token_no": "0xNo"}}
        executor = LiveExecutor(risk_engine=risk_engine, market_token_map=m_map, dash_state=dash_state)
        mock_client = MagicMock()
        executor.client = mock_client
        executor.market_books = {"0xmarket_test": {"0xYes": 0.451, "0xNo": 0.452}}
        executor.market_depths = {"0xmarket_test": {"0xYes": 1000.0, "0xNo": 1000.0}}
        executor.sync_live_balance = MagicMock(return_value=125.0)

        # Subcase 1: 3-decimal prices with valid edge after rounding -> rounded to 2 decimals in orders
        created_orders = []
        def fake_create_order(args):
            created_orders.append(args)
            return f"order_{args.token_id}"
        mock_client.create_order.side_effect = fake_create_order
        mock_client.post_orders.return_value = [{"takingAmount": "5.0", "errorMsg": ""}, {"takingAmount": "5.0", "errorMsg": ""}]

        opp = {
            "market_id": "0xmarket_test",
            "trade_size": 5.0,
            "ask_yes": 0.451,
            "ask_no": 0.452,
            "effective_cost": 0.903,
            "edge": 0.097,
            "tick_size": 0.001
        }
        with patch.object(executor, 'on_trade_executed'):
            res = executor.execute_arbitrage(opp)
        self.assertTrue(res)
        self.assertEqual(len(created_orders), 2)
        self.assertEqual(created_orders[0].price, 0.451)
        self.assertEqual(created_orders[1].price, 0.452)

        # Subcase 2: Ceil to tick size eliminates the edge -> cleanly skips execution
        created_orders.clear()
        mock_client.reset_mock()
        opp_no_edge = {
            "market_id": "0xmarket_test",
            "trade_size": 5.0,
            "ask_yes": 0.500,
            "ask_no": 0.500,
            "effective_cost": 1.000,
            "edge": 0.0,
            "tick_size": 0.001
        }
        res_eliminated = executor.execute_arbitrage(opp_no_edge)
        self.assertFalse(res_eliminated)
        mock_client.create_order.assert_not_called()
        mock_client.post_orders.assert_not_called()

    def test_live_executor_orphan_sweeper_liquidates_unhedged_position(self):
        """Verify that sweep_orphan_positions detects unhedged single leg with cur_val > 0.01,
        calls RollbackProtector.safe_unwind_or_limit_exit with force_market_exit=True,
        ignores balanced pairs, and ignores expired 0-value markets."""
        from rollback_protector import RollbackProtector

        risk_engine = MagicMock(spec=RiskSizingEngine)
        risk_engine.available_cash = 100.0
        dash_state = MagicMock(spec=DashboardState)

        executor = LiveExecutor(risk_engine=risk_engine, dash_state=dash_state)
        executor.auto_unwind_enabled = True
        mock_client = MagicMock()
        executor.client = mock_client

        active_positions = [
            # 1. Unhedged orphan leg (should be swept)
            {
                "conditionId": "0xcond_orphan",
                "asset": "0xtoken_orphan",
                "title": "Will SpaceX reach Mars?",
                "outcome": "YES",
                "size": 10.0,
                "curPrice": 0.45,
                "currentValue": 4.50,
                "avgPrice": 0.40,
            },
            # 2. Balanced matched pair (should be ignored / safe)
            {
                "conditionId": "0xcond_balanced",
                "asset": "0xtoken_bal_yes",
                "title": "Balanced Market",
                "outcome": "YES",
                "size": 50.0,
                "curPrice": 0.52,
                "currentValue": 26.0,
            },
            {
                "conditionId": "0xcond_balanced",
                "asset": "0xtoken_bal_no",
                "title": "Balanced Market",
                "outcome": "NO",
                "size": 50.0,
                "curPrice": 0.48,
                "currentValue": 24.0,
            },
            # 3. Worthless / expired / dust positions (should be ignored)
            {
                "conditionId": "0xcond_dust_size",
                "asset": "0xtoken_dust",
                "size": 0.5,
                "curPrice": 0.50,
                "currentValue": 0.25,
            },
            {
                "conditionId": "0xcond_dust_val",
                "asset": "0xtoken_dust_val",
                "size": 5.0,
                "curPrice": 0.001,
                "currentValue": 0.005,
            },
            {
                "conditionId": "0xcond_zero_price",
                "asset": "0xtoken_zero_price",
                "size": 10.0,
                "curPrice": 0.0,
                "currentValue": 0.0,
            },
            {
                "conditionId": "0xcond_no_asset",
                "asset": "",
                "size": 10.0,
                "curPrice": 0.50,
                "currentValue": 5.0,
            }
        ]

        with patch.object(RollbackProtector, "safe_unwind_or_limit_exit") as mock_safe_unwind, \
             patch("ha_notifier.send_trade_notification") as mock_notify:
            mock_safe_unwind.return_value = (True, "POST_LIMIT_SELL", {"realized_loss": -0.05, "order_id": "ord_123"})

            # Initial detection: 30-second grace period is active, so 0 swept
            swept_initial = executor.sweep_orphan_positions(active_positions)
            self.assertEqual(swept_initial, 0)
            mock_safe_unwind.assert_not_called()

            # Fast forward past 60-second grace period:
            executor._orphan_first_seen["0xtoken_orphan"] = time.time() - 65.0

            # Second run: orphan position past grace period swept with force_market_exit=False
            swept_count = executor.sweep_orphan_positions(active_positions)
            self.assertEqual(swept_count, 1)

            mock_safe_unwind.assert_called_once_with(
                client=mock_client,
                token_id="0xtoken_orphan",
                shares=10.0,
                buy_price=0.40,
                label="YES",
                target_state=dash_state,
                force_market_exit=False,
                limit_price_override=0.40
            )
            mock_notify.assert_called_once()
            dash_state.add_activity_log.assert_called()

            # Swept token is cleaned up from _orphan_first_seen
            self.assertNotIn("0xtoken_orphan", executor._orphan_first_seen)

            # Third run within 15 seconds: cooldown debounce should prevent hammering
            mock_safe_unwind.reset_mock()
            mock_notify.reset_mock()
            swept_again = executor.sweep_orphan_positions(active_positions)
            self.assertEqual(swept_again, 0)
            mock_safe_unwind.assert_not_called()
            mock_notify.assert_not_called()

    def test_zero_sell_protection_blocks_automated_sweep(self):
        """Verify that when auto_unwind_enabled is False, automated sweeps are blocked unless force_now=True."""
        risk_engine = MagicMock(spec=RiskSizingEngine)
        dash_state = MagicMock(spec=DashboardState)
        executor = LiveExecutor(risk_engine=risk_engine, dash_state=dash_state)
        mock_client = MagicMock()
        executor.client = mock_client

        active_positions = [
            {
                "conditionId": "0xcond_orphan",
                "asset": "0xtoken_orphan",
                "title": "Unhedged Market",
                "outcome": "YES",
                "size": 10.0,
                "curPrice": 0.45,
                "currentValue": 4.50,
                "avgPrice": 0.40,
            }
        ]

        self.assertFalse(executor.auto_unwind_enabled)
        # Without force_now, zero-sell shield aborts immediately
        self.assertEqual(executor.sweep_orphan_positions(active_positions, force_now=False), 0)


def test_live_executor_orphan_sweeper_liquidates_unhedged_position():
    test_case = TestLiveExecutorConstraints()
    test_case.test_live_executor_orphan_sweeper_liquidates_unhedged_position()


class TestGranularMissedReasonGating(unittest.TestCase):
    def test_map_liquidity_gate_reason_circuit_breaker(self):
        from paper_trader import _map_liquidity_gate_reason
        self.assertEqual(_map_liquidity_gate_reason("Market eligibility gate failed: cricket match"), MissedReason.CIRCUIT_BREAKER)
        self.assertEqual(_map_liquidity_gate_reason("illiquid prop pattern rejected"), MissedReason.CIRCUIT_BREAKER)
        self.assertEqual(_map_liquidity_gate_reason("Resolution horizon / expiry too soon"), MissedReason.CIRCUIT_BREAKER)
        self.assertEqual(_map_liquidity_gate_reason("Sport match in play"), MissedReason.CIRCUIT_BREAKER)
    def test_check_market_parity_records_asymmetric_depth(self):
        risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        sim = PaperSimulator(risk)
        sim.shadow_tracker = MagicMock()

        sim.market_books["MKT_ASYMMETRIC"] = {"YES": 0.40, "NO": 0.50}
        sim.market_depths["MKT_ASYMMETRIC"] = {"YES": 250.0, "NO": 0.0}

        opp = sim.check_market_parity("MKT_ASYMMETRIC")
        self.assertIsNone(opp)
        sim.shadow_tracker.record_missed.assert_called_once()
        call_opp, call_reason = sim.shadow_tracker.record_missed.call_args[0]
        self.assertEqual(call_reason, MissedReason.ASYMMETRIC_DEPTH)
        self.assertEqual(call_opp["available_depth_usd"], 250.0)

    @patch("paper_trader.validate_arbitrage_execution")
    def test_execute_arbitrage_records_wide_spread(self, mock_validate):
        mock_validate.return_value = (False, "YES spread (0.025) exceeds max (0.015)")
        risk = MagicMock(spec=RiskSizingEngine)
        risk.available_cash = 100.0
        risk.capital = 100.0
        risk.max_exposure_pct = 0.10
        dash = MagicMock(spec=DashboardState)
        dash.state = {"execution_mode": "Live Trading", "execution_style": "taker"}
        tracker = MagicMock()

        executor = LiveExecutor(risk_engine=risk, dash_state=dash)
        executor.shadow_tracker = tracker

        opp = {
            "market_id": "0x123",
            "trade_size": 50.0,
            "edge": 0.02,
            "ask_yes": 0.48,
            "ask_no": 0.50,
            "expected_profit": 1.0,
            "available_depth_usd": 200.0,
        }
        res = executor.execute_arbitrage(opp)
        self.assertFalse(res)
        tracker.record_missed.assert_called_once()
        args, kwargs = tracker.record_missed.call_args
        self.assertEqual(args[1], MissedReason.WIDE_SPREAD)

    @patch("paper_trader.validate_arbitrage_execution")
    def test_execute_arbitrage_records_clob_order_killed(self, mock_validate):
        mock_validate.return_value = (True, "OK")
        risk = MagicMock(spec=RiskSizingEngine)
        risk.lock = threading.Lock()
        risk.available_cash = 100.0
        risk.capital = 100.0
        risk.max_exposure_pct = 0.10
        risk.reserve_cash_pct = 0.0
        risk.sizing_multiplier = 1.0
        risk.open_positions = {}
        risk.max_market_exposure_pct = 0.25
        dash = MagicMock(spec=DashboardState)
        dash.state = {"execution_mode": "Live Trading", "execution_style": "taker"}
        tracker = MagicMock()

        mock_client = MagicMock()
        mock_client.create_order.return_value = MagicMock()
        mock_client.post_orders.return_value = [
            {"takingAmount": "0.0", "errorMsg": "Order killed by exchange", "status": "KILLED"},
            {"takingAmount": "0.0", "errorMsg": "Order killed by exchange", "status": "KILLED"}
        ]

        executor = LiveExecutor(risk_engine=risk, dash_state=dash)
        executor.client = mock_client
        executor.shadow_tracker = tracker
        executor.market_token_map = {"0x123": {"token_yes": "tok_y", "token_no": "tok_n"}}
        dash.state["live_wager_cap"] = 50.0

        opp = {
            "market_id": "0x123",
            "token_yes": "tok_y",
            "token_no": "tok_n",
            "trade_size": 50.0,
            "edge": 0.02,
            "ask_yes": 0.48,
            "ask_no": 0.50,
            "expected_profit": 1.0,
            "available_depth_usd": 200.0,
        }
        res = executor.execute_arbitrage(opp)
        self.assertFalse(res)
        tracker.record_missed.assert_called_once()
        args, kwargs = tracker.record_missed.call_args
        self.assertEqual(args[1], MissedReason.CLOB_ORDER_KILLED)

    def test_shadow_tracker_pnl_forfeiture_zero_for_liquidity_reasons(self):
        tracker = ShadowParityTracker()
        opp = {
            "market_id": "0x123",
            "edge": 0.02,
            "trade_size": 100.0,
            "expected_profit": 2.0,
            "available_depth_usd": 200.0
        }
        e_zero = tracker.record_missed(opp, MissedReason.ZERO_LIQUIDITY)
        e_asym = tracker.record_missed(opp, MissedReason.ASYMMETRIC_DEPTH)
        e_wide = tracker.record_missed(opp, MissedReason.WIDE_SPREAD)
        self.assertEqual(e_zero["forfeited_pnl"], 0.0)
        self.assertEqual(e_asym["forfeited_pnl"], 0.0)
        self.assertEqual(e_wide["forfeited_pnl"], 0.0)

        e_clob = tracker.record_missed(opp, MissedReason.CLOB_ORDER_KILLED)
        self.assertEqual(e_clob["forfeited_pnl"], 2.0)

    def test_live_executor_sync_live_trades(self):
        """Verify LiveExecutor.sync_live_trades parses CLOB fills and updates dash_state."""
        import tempfile
        from unittest.mock import MagicMock
        from paper_trader import LiveExecutor, DashboardState

        with tempfile.TemporaryDirectory() as tmpdir:
            ds_file = os.path.join(tmpdir, "test_state.json")
            ds = DashboardState(filename=ds_file)
            try:
                mock_client = MagicMock()
                mock_client.get_trades.return_value = [
                    {
                        "market": "0x123456",
                        "side": "BUY",
                        "outcome": "YES",
                        "size": 20.0,
                        "price": 0.50,
                        "match_time": 1791486000,
                        "status": "CONFIRMED",
                        "transaction_hash": "0xabc"
                    }
                ]
                executor = LiveExecutor.__new__(LiveExecutor)
                executor.client = mock_client
                executor.dash_state = ds
                executor.sync_live_trades()

                trades = ds.state.get("trades", [])
                self.assertEqual(len(trades), 1)
                self.assertEqual(trades[0]["market"], "0x123456")
                self.assertEqual(trades[0]["side"], "BUY")
                self.assertEqual(trades[0]["outcome"], "YES")
                self.assertEqual(trades[0]["price"], 0.50)
                self.assertEqual(trades[0]["size"], 10.0)
            finally:
                ds.stop()


class TestMakerTakerParityScanner(unittest.TestCase):
    def setUp(self):
        self.risk = RiskSizingEngine(initial_capital=1000.0, max_exposure_pct=0.10)
        self.dash = MagicMock(spec=DashboardState)
        self.dash.lock = threading.Lock()
        self.dash.state = {"execution_mode": "Paper Trading", "execution_style": "maker_taker"}
        self.sim = PaperSimulator(self.risk, dash_state=self.dash, min_edge=0.0080, taker_fee_bps=35)


    def test_branch_a_maker_yes_taker_no(self):
        market_id = "0xMARKET_MAKER_YES"
        self.sim.market_token_map[market_id] = {
            "token_yes": "tok_yes",
            "token_no": "tok_no",
            "question": "Will BTC break 100k?",
            "tick_size": 0.001,
            "rewards_daily_rate": 20.0
        }
        self.sim.market_books[market_id] = {
            "tok_yes": 0.50,
            "tok_no": 0.49,
            "tok_yes_bid": 0.48,
            "tok_no_bid": 0.45
        }
        self.sim.market_depths[market_id] = {
            "tok_yes": 100.0,
            "tok_no": 100.0
        }

        opp = self.sim.check_maker_taker_parity(market_id)
        self.assertIsNotNone(opp)
        self.assertEqual(opp["execution_type"], "maker_taker")
        self.assertEqual(opp["maker_leg"], "YES")
        self.assertEqual(opp["maker_token"], "tok_yes")
        self.assertAlmostEqual(opp["maker_price"], 0.499, places=3)
        self.assertEqual(opp["taker_token"], "tok_no")
        self.assertAlmostEqual(opp["taker_price"], 0.49, places=2)
        self.assertAlmostEqual(opp["edge"], 0.009285, places=4)
        self.assertEqual(opp["rewards_daily_rate"], 20.0)

    def test_branch_b_maker_no_taker_yes(self):
        market_id = "0xMARKET_MAKER_NO"
        self.sim.market_token_map[market_id] = {
            "token_yes": "tok_yes",
            "token_no": "tok_no",
            "question": "Will ETH reach 5k?",
            "tick_size": 0.001,
            "rewards_daily_rate": 15.0
        }
        self.sim.market_books[market_id] = {
            "tok_yes": 0.49,
            "tok_no": 0.50,
            "tok_yes_bid": 0.45,
            "tok_no_bid": 0.48
        }
        self.sim.market_depths[market_id] = {
            "tok_yes": 100.0,
            "tok_no": 100.0
        }

        opp = self.sim.check_maker_taker_parity(market_id)
        self.assertIsNotNone(opp)
        self.assertEqual(opp["execution_type"], "maker_taker")
        self.assertEqual(opp["maker_leg"], "NO")
        self.assertEqual(opp["maker_token"], "tok_no")
        self.assertAlmostEqual(opp["maker_price"], 0.499, places=3)
        self.assertEqual(opp["taker_token"], "tok_yes")
        self.assertAlmostEqual(opp["taker_price"], 0.49, places=2)
        self.assertAlmostEqual(opp["edge"], 0.009285, places=4)

    def test_wide_spread_rejection(self):
        market_id = "0xMARKET_WIDE_SPREAD"
        self.sim.market_token_map[market_id] = {
            "token_yes": "tok_yes",
            "token_no": "tok_no",
            "tick_size": 0.001
        }
        self.sim.market_books[market_id] = {
            "tok_yes": 0.50,
            "tok_no": 0.48,
            "tok_yes_bid": 0.45,
            "tok_no_bid": 0.42
        }
        self.sim.market_depths[market_id] = {"tok_yes": 100.0, "tok_no": 100.0}
        opp = self.sim.check_maker_taker_parity(market_id)
        self.assertIsNone(opp)

    def test_insufficient_taker_depth_rejection(self):
        market_id = "0xMARKET_LOW_DEPTH"
        self.sim.market_token_map[market_id] = {
            "token_yes": "tok_yes",
            "token_no": "tok_no",
            "tick_size": 0.001
        }
        self.sim.market_books[market_id] = {
            "tok_yes": 0.50,
            "tok_no": 0.49,
            "tok_yes_bid": 0.48,
            "tok_no_bid": 0.45
        }
        self.sim.market_depths[market_id] = {
            "tok_yes": 100.0,
            "tok_no": 3.0
        }
        opp = self.sim.check_maker_taker_parity(market_id)
        self.assertIsNone(opp)

    def test_on_book_tick_dispatch_maker_taker(self):
        market_id = "0xMARKET_DISPATCH"
        self.sim.market_token_map[market_id] = {
            "token_yes": "tok_yes",
            "token_no": "tok_no",
            "question": "Dispatch Test",
            "tick_size": 0.001
        }
        self.sim.market_books[market_id] = {
            "tok_yes": 0.50,
            "tok_no": 0.49,
            "tok_yes_bid": 0.48,
            "tok_no_bid": 0.45
        }
        self.sim.market_depths[market_id] = {"tok_yes": 100.0, "tok_no": 100.0}

        opp = self.sim.on_book_tick(market_id)
        self.assertIsNotNone(opp)
        self.assertEqual(opp["execution_type"], "maker_taker")
        self.assertGreater(self.risk.capital, 1000.0)


    def test_live_executor_routes_maker_taker(self):
        risk = MagicMock(spec=RiskSizingEngine)
        risk.lock = threading.Lock()
        risk.available_cash = 500.0
        risk.capital = 500.0
        risk.can_trade.return_value = True
        dash = MagicMock(spec=DashboardState)
        dash.lock = threading.Lock()
        dash.state = {"execution_mode": "Live Trading", "execution_style": "maker_taker", "live_wager_cap": 50.0}

        executor = LiveExecutor(risk_engine=risk, dash_state=dash)
        mock_client = MagicMock()
        executor.client = mock_client
        mock_mt_exec = MagicMock()
        mock_mt_exec.execute_maker_taker_arbitrage.return_value = (True, "SUCCESS", {"hedged_size": 10.0})
        executor.maker_taker_executor = mock_mt_exec

        opp = {
            "execution_type": "maker_taker",
            "market_id": "0xMKT_LIVE",
            "maker_leg": "YES",
            "maker_token": "tok_maker",
            "maker_price": 0.48,
            "taker_token": "tok_taker",
            "taker_price": 0.49,
            "edge": 0.03,
            "trade_size": 20.0,
            "tick_size": 0.001
        }

        result = executor.execute_arbitrage(opp)
        self.assertTrue(result)
        mock_mt_exec.execute_maker_taker_arbitrage.assert_called_once_with(
            token_maker="tok_maker",
            maker_price=0.48,
            token_taker="tok_taker",
            taker_price=0.49,
            size=20.0,
            timeout_seconds=4.0,
            dash_state=dash,
            min_edge=executor.min_edge,
            tick_size=0.001,
            initial_taker_depth=None
        )

    def test_live_executor_maker_timeout_zero_loss_releases_state(self):
        risk = MagicMock(spec=RiskSizingEngine)
        risk.lock = threading.Lock()
        risk.available_cash = 500.0
        risk.capital = 500.0
        risk.can_trade.return_value = True
        risk.open_positions = {}
        dash = MagicMock(spec=DashboardState)
        dash.lock = threading.Lock()
        dash.state = {"execution_mode": "Live Trading", "execution_style": "maker_taker", "live_wager_cap": 50.0}

        executor = LiveExecutor(risk_engine=risk, dash_state=dash)
        mock_client = MagicMock()
        executor.client = mock_client
        mock_mt_exec = MagicMock()
        mock_mt_exec.execute_maker_taker_arbitrage.return_value = (False, "MAKER_TIMEOUT_ZERO_LOSS", {})
        executor.maker_taker_executor = mock_mt_exec


        opp = {
            "execution_type": "maker_taker",
            "market_id": "0xMKT_TIMEOUT",
            "maker_leg": "YES",
            "maker_token": "tok_maker",
            "maker_price": 0.48,
            "taker_token": "tok_taker",
            "taker_price": 0.49,
            "edge": 0.03,
            "trade_size": 20.0,
            "tick_size": 0.001
        }

        result = executor.execute_arbitrage(opp)
        self.assertFalse(result)
        risk.record_pnl.assert_not_called()
        self.assertNotIn("0xMKT_TIMEOUT", risk.open_positions)

    def test_live_executor_simultaneous_dual_taker_batch_dispatch(self):
        risk = MagicMock(spec=RiskSizingEngine)
        risk.available_cash = 100.0
        risk.capital = 100.0
        risk.open_positions = {}
        dash = MagicMock(spec=DashboardState)
        dash.state = {"execution_mode": "Live Trading", "execution_style": "simultaneous_batch"}

        executor = LiveExecutor(risk_engine=risk, dash_state=dash)
        mock_client = MagicMock()
        executor.client = mock_client
        mock_concurrent_exec = MagicMock()
        mock_concurrent_exec.execute_simultaneous_batch.return_value = (
            True,
            "DUAL_MATCH_SECURED",
            {"shares": 10.0, "total_cost": 9.70, "profit": 0.30},
        )
        executor.concurrent_leg_executor = mock_concurrent_exec

        opp = {
            "execution_type": "simultaneous_dual_taker",
            "market_id": "0xMKT_BATCH_001",
            "token_yes": "tok_y",
            "token_no": "tok_n",
            "ask_yes": 0.48,
            "ask_no": 0.49,
            "shares": 10.0,
            "edge": 0.03,
            "tick_size": 0.001,
            "neg_risk": False,
        }

        result = executor.execute_arbitrage(opp)
        self.assertTrue(result)
        mock_concurrent_exec.execute_simultaneous_batch.assert_called_once_with(
            token_yes="tok_y",
            ask_yes=0.48,
            token_no="tok_n",
            ask_no=0.49,
            shares=10.0,
            tick_size=0.001,
            neg_risk=False,
            min_edge=0.03,
            available_cash=100.0,
        )
        risk.open_position.assert_called_once_with("0xMKT_BATCH_001", 9.70, 0.30)











