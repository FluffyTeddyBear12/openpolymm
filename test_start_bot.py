import unittest
from unittest.mock import patch, MagicMock
import socket
import os
import sys

import start_bot

class TestStartBot(unittest.TestCase):
    def test_is_port_open_free_port(self):
        # Find a free port
        temp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        temp_sock.bind(("127.0.0.1", 0))
        free_port = temp_sock.getsockname()[1]
        temp_sock.close()

        # It should not be open
        self.assertFalse(start_bot.is_port_open(free_port))

    def test_is_port_open_bound_port(self):
        temp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        temp_sock.bind(("127.0.0.1", 0))
        temp_sock.listen(1)
        bound_port = temp_sock.getsockname()[1]
        try:
            self.assertTrue(start_bot.is_port_open(bound_port))
        finally:
            temp_sock.close()

    def test_acquire_instance_lock(self):
        # Choose a random dynamic port
        s_temp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s_temp.bind(("127.0.0.1", 0))
        test_port = s_temp.getsockname()[1]
        s_temp.close()

        lock1 = start_bot.acquire_instance_lock(test_port)
        self.assertIsNotNone(lock1)
        try:
            # Second attempt on same port should fail
            lock2 = start_bot.acquire_instance_lock(test_port)
            self.assertIsNone(lock2)
        finally:
            lock1.close()

    @patch("webbrowser.open")
    def test_open_dashboard_enabled(self, mock_webbrowser):
        with patch.dict(os.environ, {"POLYMARKET_BOT_NO_BROWSER": "0"}, clear=False):
            res = start_bot.open_dashboard("http://localhost:8501")
            self.assertTrue(res)
            mock_webbrowser.assert_called_once_with("http://localhost:8501")

    @patch("webbrowser.open")
    def test_open_dashboard_disabled_by_env(self, mock_webbrowser):
        with patch.dict(os.environ, {"POLYMARKET_BOT_NO_BROWSER": "1"}, clear=False):
            res = start_bot.open_dashboard("http://localhost:8501")
            self.assertFalse(res)
            mock_webbrowser.assert_not_called()

    def test_wait_with_timeout_skips_when_no_tty(self):
        # When sys.stdin is not a tty (standard in test runners), it should return immediately
        start_bot.wait_with_timeout(seconds=5)

    def test_wait_with_timeout_skips_with_env(self):
        with patch.dict(os.environ, {"POLYMARKET_BOT_NO_WAIT": "1"}, clear=False):
            start_bot.wait_with_timeout(seconds=5)

    @patch("start_bot.acquire_instance_lock")
    @patch("start_bot.get_port_owner_pid")
    @patch("start_bot.open_dashboard")
    @patch("start_bot.wait_with_timeout")
    def test_main_already_running_exits_cleanly(
        self,
        mock_wait,
        mock_open_dashboard,
        mock_get_pid,
        mock_acquire
    ):
        mock_acquire.return_value = None  # Lock cannot be acquired -> already running
        mock_get_pid.return_value = 12345

        with patch.dict(os.environ, {"POLYMARKET_BOT_NO_WAIT": "1"}):
            with self.assertRaises(SystemExit) as cm:
                start_bot.main()

            # Must exit with code 0 (clean success) rather than code 1
            self.assertEqual(cm.exception.code, 0)
            mock_open_dashboard.assert_called_once_with(start_bot.DASHBOARD_URL)
            mock_wait.assert_called_once_with(seconds=5)

    @patch("webbrowser.open", side_effect=Exception("Browser launch error"))
    def test_open_dashboard_exception_handled(self, mock_webbrowser):
        res = start_bot.open_dashboard("http://localhost:8501")
        self.assertFalse(res)

    @patch("start_bot.acquire_instance_lock")
    @patch("start_bot.get_port_owner_pid")
    @patch("start_bot.open_dashboard")
    @patch("start_bot.wait_with_timeout")
    @patch("os.path.exists")
    def test_main_already_running_pid_fallback_to_file(
        self,
        mock_exists,
        mock_wait,
        mock_open_dashboard,
        mock_get_pid,
        mock_acquire
    ):
        mock_acquire.return_value = None
        mock_get_pid.return_value = None
        mock_exists.return_value = True

        with patch("builtins.open", unittest.mock.mock_open(read_data="99999")):
            with patch.dict(os.environ, {"POLYMARKET_BOT_NO_WAIT": "1"}):
                with self.assertRaises(SystemExit) as cm:
                    start_bot.main()
                self.assertEqual(cm.exception.code, 0)
                mock_open_dashboard.assert_called_once_with(start_bot.DASHBOARD_URL)

    @patch("start_bot.is_port_open", return_value=False)
    @patch("start_bot.open_dashboard")
    def test_launch_browser_when_ready_timeout_fallback(self, mock_open_dashboard, mock_is_port_open):
        with patch.dict(os.environ, {"POLYMARKET_BOT_NO_BROWSER": "0"}):
            start_bot.launch_browser_when_ready("http://localhost:8501", 8501, timeout=0.2)
            mock_open_dashboard.assert_called_once_with("http://localhost:8501")

    @patch("start_bot.is_port_open")
    @patch("start_bot.open_dashboard")
    def test_launch_browser_when_ready_opens_when_port_available(self, mock_open_dashboard, mock_is_port_open):
        mock_is_port_open.side_effect = [False, True]
        with patch.dict(os.environ, {"POLYMARKET_BOT_NO_BROWSER": "0"}):
            start_bot.launch_browser_when_ready("http://localhost:8501", 8501, timeout=5.0)
            mock_open_dashboard.assert_called_once_with("http://localhost:8501")

    def test_configure_cpu_budget(self):
        """Verify configure_cpu_budget caps CPU affinity to <= 20% logical cores."""
        cores = start_bot.configure_cpu_budget(target_pct=0.20)
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

    def test_apply_cpu_budget_to_pid(self):
        mock_proc = MagicMock()
        mock_psutil = MagicMock()
        mock_psutil.Process.return_value = mock_proc
        mock_psutil.BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            start_bot.apply_cpu_budget_to_pid(99999, [0, 1, 2])
            mock_psutil.Process.assert_called_once_with(99999)
            mock_proc.cpu_affinity.assert_called_once_with([0, 1, 2])

    def test_apply_job_memory_limit(self):
        """Verify apply_job_memory_limit executes safely and returns boolean."""
        res = start_bot.apply_job_memory_limit(max_gb=5.0)
        self.assertIsInstance(res, bool)

    def test_start_supervisor_ram_watchdog(self):
        """Verify supervisor RAM watchdog thread starts and cleanly terminates."""
        import threading
        stop_ev = threading.Event()
        t = start_bot.start_supervisor_ram_watchdog(
            child_pids_func=lambda: [],
            max_gb=5.0,
            stop_event=stop_ev
        )
        if t is not None:
            self.assertTrue(t.is_alive())
            stop_ev.set()
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive())

if __name__ == "__main__":
    unittest.main()


