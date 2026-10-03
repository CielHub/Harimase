"""Purpose: Termux Android daemon entry point with WebSocket transport and package crash monitoring. Dependencies: websockets + local modules + Flask debug API."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from api.client import ServerClient
from api.server import LocalApiServer
from api.ws_client import WebSocketClient
from core.command_handler import CommandHandler
from core.crash_detector import CrashDetector
from core.heartbeat import HeartbeatReporter
from core.killer import PackageKiller
from core.package_scanner import PackageScanner, valid_package_name
from core.process_monitor import ProcessMonitor
from core.rejoiner import Rejoiner
from menus.setup_menu import SetupMenu
from utils.crypto import secure_chmod
from utils.logger import package_logger, setup_logging
from utils.shell import Shell

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
PID_FILE = PROJECT_DIR / "logs" / "daemon.pid"
LOCK_FILE = PROJECT_DIR / "logs" / "daemon.lock"
CONFIG_LOCK = threading.RLock()


def default_config() -> dict:
    return {
        "device_name": "Android Device",
        "device_uuid": str(uuid.uuid4()),
        "server_url": "",
        "device_token": "",
        "device_id": "",
        "server_timeout": 15,
        "heartbeat_interval": 30,
        "boot_grace_sec": 10,
        "packages": [],
        "runtime": {"retry_history": {}, "last_rejoin": {}},
    }


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        config = default_config()
        save_config(config)
        return config
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    config.setdefault("runtime", {"retry_history": {}, "last_rejoin": {}})
    config.setdefault("packages", [])
    config.setdefault("heartbeat_interval", 30)
    config.setdefault("server_timeout", 15)
    return config


def save_config(config: dict) -> None:
    with CONFIG_LOCK:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(CONFIG_PATH)
        secure_chmod(CONFIG_PATH)


def run_local_api(config, stop_event):
    server = LocalApiServer(lambda: {
        "ok": True,
        "device_id": config.get("device_id", ""),
        "device_name": config.get("device_name", ""),
        "packages": config.get("packages", []),
    })
    server.start()
    try:
        stop_event.wait()
    finally:
        server.shutdown()


def run_daemon(config: dict, local_api: bool) -> None:
    adapter = package_logger("-")
    setup_logging(str(PROJECT_DIR / "logs"), logging.INFO)
    shell = Shell(use_su=True)
    scanner = PackageScanner(shell)
    monitor = ProcessMonitor(shell)
    killer = PackageKiller(shell, monitor)
    rejoiner = Rejoiner(shell, monitor, killer, config, lambda: save_config(config), CONFIG_LOCK)
    stop_event = threading.Event()

    if not config.get("server_url") or not config.get("device_token") or config.get("needs_repair"):
        adapter.error("Device requires pairing. Run: python main.py --setup")
        return

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_FILE.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        adapter.error("Another daemon instance is already running")
        lock_handle.close()
        return

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    secure_chmod(PID_FILE)
    secure_chmod(LOCK_FILE)

    try:
        subprocess.run(["termux-wake-lock"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        adapter.warning("termux-wake-lock is unavailable; Android power management may throttle the daemon")

    if local_api:
        threading.Thread(target=run_local_api, args=(config, stop_event), daemon=True).start()

    try:
        startup_candidates = scanner.scan()
        with CONFIG_LOCK:
            before = {item.get("package") for item in config.get("packages", [])}
            config["packages"] = scanner.merge_manual(startup_candidates, config.get("packages", []))
            save_config(config)
        added = [item.package for item in startup_candidates if item.package not in before]
        if added:
            adapter.info("Startup scan detected new packages: %s", ", ".join(added))
    except Exception as exc:
        adapter.warning("Startup package scan failed; keeping existing configuration: %s", exc)

    adapter.info("Daemon started with %d configured packages", len(config.get("packages", [])))
    heartbeat_reporter = HeartbeatReporter(config, monitor, CONFIG_LOCK)
    command_handler = CommandHandler(config, lambda: save_config(config), scanner, monitor, killer, rejoiner, adapter, CONFIG_LOCK)

    def on_auth_failure(_exc):
        stop_event.set()

    ws_client = WebSocketClient(
        config,
        command_handler,
        heartbeat_reporter.payload,
        lambda: save_config(config),
        stop_event,
        on_auth_failure=on_auth_failure,
        logger=logging.getLogger("ws-client"),
        config_lock=CONFIG_LOCK,
    )
    ws_client.start()

    def on_signal(signum, frame):
        adapter.info("Received signal %s; shutting down daemon", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    crash_detector = CrashDetector(shell, monitor)
    start_times: dict[str, float] = {item.get("package"): time.monotonic() for item in config.get("packages", []) if item.get("package")}
    try:
        while not stop_event.is_set():
            try:
                crash_detector.refresh()
            except Exception as exc:
                adapter.warning("Crash log snapshot failed: %s", exc)
            with CONFIG_LOCK:
                packages_to_check = [dict(item) for item in config.get("packages", []) if isinstance(item, dict)]
            for item in packages_to_check:
                if not item.get("enabled", False):
                    continue
                pkg = str(item.get("package", ""))
                if not pkg or not valid_package_name(pkg):
                    continue
                package_log = package_logger(pkg)
                start_times.setdefault(pkg, time.monotonic())
                try:
                    snapshot = monitor.snapshot(pkg)
                    elapsed = time.monotonic() - start_times[pkg]
                    if elapsed < max(10, min(int(config.get("boot_grace_sec", 10)), 60)) or rejoiner.grace_active(pkg):
                        continue
                    evidence = crash_detector.detect(pkg, snapshot, was_expected_running=True)
                    if evidence.confirmed:
                        package_log.warning("Crash confirmed: %s | %s", evidence.reason, ", ".join(evidence.indicators))
                        ws_client.send_event_threadsafe("crash_detected", {"package": pkg, "reason": evidence.reason, "indicators": evidence.indicators})
                        result = rejoiner.rejoin(item, reason=evidence.reason)
                        if result.get("ok"):
                            start_times[pkg] = time.monotonic()
                            ws_client.send_event_threadsafe("rejoin_success", {"package": pkg, "result": result, "automatic": True})
                        elif result.get("retry_exhausted") or result.get("critical"):
                            ws_client.send_event_threadsafe("rejoin_failed", {"package": pkg, "result": result, "automatic": True})
                except Exception as exc:
                    package_log.exception("Monitor cycle failed: %s", exc)
            stop_event.wait(3)
    finally:
        stop_event.set()
        ws_client.stop()
        PID_FILE.unlink(missing_ok=True)
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()
        adapter.info("Daemon stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Roblox Auto-Rejoin Termux client")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--local-api", action="store_true")
    args = parser.parse_args()
    config = load_config()
    if args.setup or (not args.daemon and (not config.get("server_url") or not config.get("device_token"))):
        SetupMenu(PROJECT_DIR, config, lambda: save_config(config)).run()
        return
    run_daemon(config, local_api=args.local_api)


if __name__ == "__main__":
    main()
