import subprocess
import sys
import time
import os
import socket
import atexit
import webbrowser
import threading
from typing import Optional, List, Dict, Any, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(BASE_DIR, "supervisor.pid")
LOCK_PORT = int(os.environ.get("POLYMARKET_BOT_LOCK_PORT", 48123))
PAPER_LOCK_PORT = int(os.environ.get("POLYMARKET_BOT_PAPER_PORT", 48124))
DASHBOARD_PORT = int(os.environ.get("POLYMARKET_BOT_DASHBOARD_PORT", 8501))
DASHBOARD_URL = os.environ.get("POLYMARKET_BOT_DASHBOARD_URL", f"http://localhost:{DASHBOARD_PORT}")

def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Check if a localhost TCP port is actively accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.error):
        return False

def open_dashboard(url: str = DASHBOARD_URL) -> bool:
    """Open the dashboard URL in the system's default web browser."""
    if os.environ.get("POLYMARKET_BOT_NO_BROWSER") in ("1", "true", "True"):
        return False
    try:
        webbrowser.open(url)
        return True
    except Exception as e:
        print(f"[WARN] Failed to open browser automatically: {e}", flush=True)
        return False

def wait_with_timeout(seconds: int = 5):
    """
    Keep console window open for `seconds` so the user can read the output,
    allowing early dismissal if any key is pressed.
    """
    if os.environ.get("POLYMARKET_BOT_NO_WAIT") == "1":
        return
    if not sys.stdin.isatty():
        return

    print(f"\nClosing window in {seconds} seconds (or press any key to close now)...", flush=True)
    start_t = time.time()
    try:
        import msvcrt
        while time.time() - start_t < seconds:
            if msvcrt.kbhit():
                try:
                    msvcrt.getch()
                except Exception:
                    pass
                break
            time.sleep(0.1)
    except Exception:
        try:
            time.sleep(min(seconds, 2))
        except Exception:
            pass

def launch_browser_when_ready(url: str = DASHBOARD_URL, port: int = DASHBOARD_PORT, timeout: float = 20.0):
    """Wait for the dashboard port to be open, then open the browser."""
    if os.environ.get("POLYMARKET_BOT_NO_BROWSER") in ("1", "true", "True"):
        return
    start_t = time.time()
    opened = False
    while time.time() - start_t < timeout:
        if is_port_open(port, timeout=0.3):
            print(f"\n[OK] Dashboard is live at {url}. Opening in your default browser...", flush=True)
            open_dashboard(url)
            opened = True
            break
        time.sleep(0.5)
    if not opened:
        print(f"\n[INFO] Opening {url} in default browser...", flush=True)
        open_dashboard(url)

def acquire_instance_lock(port: int = LOCK_PORT):
    """
    Acquire an exclusive localhost socket lock on `port`.
    Returns the socket object if acquired, or None if already bound by another process.
    The OS automatically releases the bound socket if the process crashes or terminates.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE") and sys.platform == "win32":
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except Exception:
            pass
    elif hasattr(socket, "SO_REUSEADDR"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
    try:
        s.bind(("127.0.0.1", port))
        s.listen(5)
        # Drain probing connections so socket backlog never saturates into CLOSE_WAIT
        def _drain():
            while True:
                try:
                    conn, _ = s.accept()
                    try:
                        conn.close()
                    except Exception:
                        pass
                except Exception:
                    break
        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        return s
    except (OSError, socket.error):
        try:
            s.close()
        except Exception:
            pass
        return None

def get_port_owner_pid(port: int):
    """Find the PID of the process listening on `port` using netstat."""
    try:
        res = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=3
        )
        for line in res.stdout.splitlines():
            tokens = line.strip().split()
            if len(tokens) >= 5 and tokens[0] == "TCP" and tokens[3] == "LISTENING":
                if tokens[1].endswith(f":{port}"):
                    try:
                        return int(tokens[4])
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass
    return None

def terminate_pid_tree(pid: int):
    """Terminate a process tree cleanly by PID."""
    if not pid or pid <= 0 or pid == os.getpid():
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5
            )
        else:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    except Exception:
        pass

def cleanup_orphaned_bot_processes():
    """Terminate any stray paper_trader.py processes left behind by an earlier hard-crashed supervisor."""
    paper_pid = get_port_owner_pid(PAPER_LOCK_PORT)
    if paper_pid and paper_pid != os.getpid():
        print(f"[INFO] Cleaning up orphaned Paper Trader process (PID: {paper_pid}, Port: {PAPER_LOCK_PORT})...", flush=True)
        terminate_pid_tree(paper_pid)

    paper_pid_file = os.path.join(BASE_DIR, "paper_trader.pid")
    if os.path.exists(paper_pid_file):
        try:
            with open(paper_pid_file, "r", encoding="utf-8") as f:
                saved_pid = int(f.read().strip())
            if saved_pid and saved_pid != os.getpid():
                terminate_pid_tree(saved_pid)
            os.remove(paper_pid_file)
        except Exception:
            pass

def cleanup_pid_file():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass

def terminate_process_tree(proc):
    """Cleanly terminate a subprocess and any of its child processes on Windows or POSIX."""
    if proc is None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass

def configure_cpu_budget(target_pct: float = 0.20):
    """
    Enforce CPU capping (default <= 20% max CPU limit) and BELOW_NORMAL process priority.
    On 16-thread host (e.g. AMD Ryzen 7 9800X3D), restricts process to 3 cores (18.75%).
    Gracefully degrades if psutil is not available or OS denies affinity changes.
    """
    try:
        import psutil
        p = psutil.Process()
        total_cores = psutil.cpu_count(logical=True) or 1
        num_cores = max(1, int(total_cores * target_pct))
        assigned_cores = list(range(min(num_cores, total_cores)))
        try:
            p.cpu_affinity(assigned_cores)
        except Exception:
            pass
        try:
            if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS") and sys.platform == "win32":
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            elif hasattr(p, "nice"):
                p.nice(10)
        except Exception:
            pass
        return assigned_cores
    except Exception:
        return None

def apply_cpu_budget_to_pid(pid: int, assigned_cores: list):
    """Apply CPU affinity and process priority to a child PID."""
    if not pid or not assigned_cores:
        return
    try:
        import psutil
        p = psutil.Process(pid)
        p.cpu_affinity(assigned_cores)
        if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS") and sys.platform == "win32":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        elif hasattr(p, "nice"):
            p.nice(10)
    except Exception:
        pass

def apply_job_memory_limit(max_gb: float = 8.0) -> bool:
    """
    Windows Job Object memory limiting (JOB_OBJECT_LIMIT_JOB_MEMORY) is deliberately
    NOT applied to the supervisor process. A hard kernel limit terminates the supervisor
    with exit code 1 without recovery. Memory management is handled dynamically by
    start_supervisor_ram_watchdog, which gracefully recycles child processes.
    """
    return False

def start_supervisor_ram_watchdog(
    child_pids_func=None,
    max_gb: float = 8.0,
    stop_event: Optional[threading.Event] = None,
    get_backend=None,
    get_frontend=None
) -> Optional[threading.Thread]:
    """
    Monitor the RAM usage (RSS) of supervisor and its active child processes (backend & frontend).
    If backend RSS exceeds 2.5 GB (or combined RSS exceeds 4.0 GB):
        Recycles backend process gracefully using terminate_process_tree(backend).
        The supervisor while True loop will automatically restart backend in 2 seconds with fresh memory.
    If total RAM exceeds 80% of max_gb, logs warning and triggers garbage collection.
    """
    import gc
    try:
        import psutil
    except ImportError:
        return None

    limit_bytes = int(max_gb * 1024 * 1024 * 1024)
    backend_limit_bytes = int(2.5 * 1024 * 1024 * 1024)  # 2.5 GB
    combined_recycle_bytes = int(4.0 * 1024 * 1024 * 1024)  # 4.0 GB

    def _watchdog_loop():
        supervisor_proc = psutil.Process()
        while True:
            if stop_event and stop_event.wait(5.0):
                break
            elif not stop_event:
                time.sleep(5.0)

            try:
                backend_proc = get_backend() if callable(get_backend) else None
                frontend_proc = get_frontend() if callable(get_frontend) else None

                backend_rss = 0
                if backend_proc and backend_proc.poll() is None:
                    try:
                        backend_rss = psutil.Process(backend_proc.pid).memory_info().rss
                    except Exception:
                        backend_rss = 0

                frontend_rss = 0
                if frontend_proc and frontend_proc.poll() is None:
                    try:
                        frontend_rss = psutil.Process(frontend_proc.pid).memory_info().rss
                    except Exception:
                        frontend_rss = 0

                supervisor_rss = supervisor_proc.memory_info().rss
                combined_rss = supervisor_rss + backend_rss + frontend_rss

                if callable(child_pids_func):
                    for pid in child_pids_func():
                        if (backend_proc and pid == backend_proc.pid) or (frontend_proc and pid == frontend_proc.pid):
                            continue
                        try:
                            combined_rss += psutil.Process(pid).memory_info().rss
                        except Exception:
                            pass

                # Graceful backend recycling on memory ceiling
                if backend_proc and backend_proc.poll() is None:
                    if backend_rss > backend_limit_bytes:
                        print(
                            f"\n[SUPERVISOR] Backend process memory exceeded 2.5 GB ({backend_rss / (1024**3):.2f} GB). "
                            "Gracefully recycling backend process...",
                            flush=True
                        )
                        terminate_process_tree(backend_proc)
                        continue

                    if combined_rss > combined_recycle_bytes:
                        print(
                            f"\n[SUPERVISOR] Combined bot memory exceeded 4.0 GB ({combined_rss / (1024**3):.2f} GB). "
                            "Gracefully recycling backend process...",
                            flush=True
                        )
                        terminate_process_tree(backend_proc)
                        continue

                if combined_rss >= limit_bytes * 0.80:
                    gc.collect()
            except Exception:
                pass

    t = threading.Thread(target=_watchdog_loop, daemon=True)
    t.start()
    return t

def main():
    lock_sock = acquire_instance_lock(LOCK_PORT)
    if lock_sock is None:
        owner_pid = get_port_owner_pid(LOCK_PORT)
        existing_pid = str(owner_pid) if owner_pid else "unknown"
        if existing_pid == "unknown" and os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r", encoding="utf-8") as f:
                    existing_pid = f.read().strip()
            except Exception:
                pass

        dashboard_status = (
            f"Active (Listening on port {DASHBOARD_PORT})"
            if is_port_open(DASHBOARD_PORT)
            else f"Starting up on port {DASHBOARD_PORT}..."
        )

        print("====================================================================", flush=True)
        print("  Polymarket Parity Arbitrage Bot & Dashboard is already running!", flush=True)
        print(f"  Supervisor PID   : {existing_pid}", flush=True)
        print(f"  Supervisor Port  : {LOCK_PORT}", flush=True)
        print(f"  Dashboard URL    : {DASHBOARD_URL}", flush=True)
        print(f"  Dashboard Status : {dashboard_status}", flush=True)
        print("====================================================================", flush=True)
        print(f"\nOpening dashboard in your web browser: {DASHBOARD_URL} ...", flush=True)
        open_dashboard(DASHBOARD_URL)
        print(f"[OK] Dashboard opened: {DASHBOARD_URL}", flush=True)
        print(f"\nTip: To shut down the bot, close the original supervisor window or run 'taskkill /F /PID {existing_pid}'.", flush=True)
        wait_with_timeout(seconds=5)
        sys.exit(0)

    # Clean up any orphaned child processes from prior crashes
    cleanup_orphaned_bot_processes()

    # Write current supervisor PID
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    atexit.register(cleanup_pid_file)

    linux_python = os.path.join(BASE_DIR, "venv", "bin", "python")
    win_python = os.path.join(BASE_DIR, "venv", "Scripts", "python.exe")
    python_exe = linux_python if os.path.exists(linux_python) else (win_python if os.path.exists(win_python) else sys.executable)

    linux_streamlit = os.path.join(BASE_DIR, "venv", "bin", "streamlit")
    win_streamlit = os.path.join(BASE_DIR, "venv", "Scripts", "streamlit.exe")
    streamlit_exe = linux_streamlit if os.path.exists(linux_streamlit) else (win_streamlit if os.path.exists(win_streamlit) else None)

    frontend_cmd = (
        [streamlit_exe, "run", "dashboard.py", "--server.headless=true"]
        if streamlit_exe and os.path.exists(streamlit_exe)
        else [python_exe, "-m", "streamlit", "run", "dashboard.py", "--server.headless=true"]
    )

    print("Starting Polymarket Parity Arbitrage Bot & Dashboard Supervisor...", flush=True)
    print(f"Single-instance lock acquired on 127.0.0.1:{LOCK_PORT} (PID {os.getpid()}).", flush=True)

    assigned_cores = configure_cpu_budget(0.20)
    if assigned_cores:
        print(f"[INFO] Supervisor CPU budget enforced: affinity pinned to cores {assigned_cores} (max 20% CPU limit).", flush=True)

    max_ram_gb = float(os.environ.get("POLYMARKET_BOT_MAX_RAM_GB", 8.0))
    job_applied = apply_job_memory_limit(max_ram_gb)
    if job_applied:
        print(f"[INFO] Supervisor Windows Job Object memory quota enforced: {max_ram_gb:.1f} GB hard RAM limit.", flush=True)

    backend = subprocess.Popen([python_exe, "paper_trader.py"], cwd=BASE_DIR, close_fds=True)
    frontend = subprocess.Popen(frontend_cmd, cwd=BASE_DIR, close_fds=True)
    if assigned_cores:
        apply_cpu_budget_to_pid(backend.pid, assigned_cores)
        apply_cpu_budget_to_pid(frontend.pid, assigned_cores)

    ram_stop_event = threading.Event()
    start_supervisor_ram_watchdog(
        child_pids_func=lambda: [p.pid for p in [backend, frontend] if p and p.poll() is None],
        max_gb=max_ram_gb,
        stop_event=ram_stop_event,
        get_backend=lambda: backend,
        get_frontend=lambda: frontend
    )

    # Launch browser once the dashboard server is responding
    browser_thread = threading.Thread(
        target=launch_browser_when_ready,
        args=(DASHBOARD_URL, DASHBOARD_PORT, 20.0),
        daemon=True
    )
    browser_thread.start()

    try:
        while True:
            time.sleep(2)
            if backend.poll() is not None:
                print(f"Backend exited (code {backend.returncode}). Restarting in 2s...", flush=True)
                time.sleep(2)
                backend = subprocess.Popen([python_exe, "paper_trader.py"], cwd=BASE_DIR, close_fds=True)
                if assigned_cores:
                    apply_cpu_budget_to_pid(backend.pid, assigned_cores)
            if frontend.poll() is not None:
                print(f"Frontend exited (code {frontend.returncode}). Restarting in 2s...", flush=True)
                time.sleep(2)
                frontend = subprocess.Popen(frontend_cmd, cwd=BASE_DIR, close_fds=True)
                if assigned_cores:
                    apply_cpu_budget_to_pid(frontend.pid, assigned_cores)
    except KeyboardInterrupt:
        print("Supervisor shutting down upon user signal...", flush=True)
    except Exception as e:
        print(f"Supervisor error: {e}", flush=True)
    finally:
        print("Terminating child processes...", flush=True)
        ram_stop_event.set()
        terminate_process_tree(backend)
        terminate_process_tree(frontend)
        cleanup_orphaned_bot_processes()
        cleanup_pid_file()
        try:
            lock_sock.close()
        except Exception:
            pass
        print("Supervisor shutdown complete.", flush=True)

if __name__ == "__main__":
    main()
