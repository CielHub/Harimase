"""Purpose: interactive Termux setup, scan, configuration, and daemon controls. Dependencies: local client modules."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from api.client import ServerClient, ServerUnavailable, AuthRequired
from api.ws_client import WebSocketClient
from core.package_scanner import PackageScanner, valid_package_name
from core.process_monitor import ProcessMonitor
from utils.shell import Shell


class SetupMenu:
    def __init__(self, project_dir: Path, config: dict, save_config) -> None:
        self.project_dir = project_dir
        self.config = config
        self.save_config = save_config
        self.shell = Shell(use_su=True)
        self.scanner = PackageScanner(self.shell)
        self.monitor = ProcessMonitor(self.shell)
        self.pid_file = project_dir / "logs" / "daemon.pid"

    def run(self) -> None:
        while True:
            print("\n=== Roblox Auto-Rejoin Setup ===")
            print("[1] Pairing device")
            print("[2] Scan package otomatis")
            print("[3] Tambah package manual")
            print("[4] Edit config package")
            print("[5] Test koneksi ke server")
            print("[6] Lihat status semua package")
            print("[7] Start daemon di background")
            print("[8] Stop daemon")
            print("[9] Keluar")
            choice = input("Pilih: ").strip()
            try:
                if choice == "1": self.pair()
                elif choice == "2": self.scan()
                elif choice == "3": self.add_manual()
                elif choice == "4": self.edit_package()
                elif choice == "5": self.test_connection()
                elif choice == "6": self.status()
                elif choice == "7": self.start_daemon()
                elif choice == "8": self.stop_daemon()
                elif choice == "9": return
                else: print("Pilihan tidak valid.")
            except Exception as exc:
                print(f"ERROR: {exc}")

    def pair(self) -> None:
        if not self.config.get("server_url"):
            self.config["server_url"] = input("Server URL: ").strip().rstrip("/")
        token = input("Pairing token: ").strip().upper()
        if not token:
            return
        uuid = self.config.setdefault("device_uuid", "")
        if not uuid:
            import uuid as uuid_module
            uuid = str(uuid_module.uuid4())
            self.config["device_uuid"] = uuid
        client = ServerClient(self.config)
        result = client.claim_pair(token, self.config.get("device_name", "Android Device"), uuid)
        self.config["device_token"] = result["device_token"]
        self.config["device_id"] = result["device_id"]
        self.config.pop("needs_repair", None)
        self.save_config()
        print(f"Pairing berhasil. device_id={result['device_id']}")

    def scan(self) -> None:
        candidates = self.scanner.scan()
        if not candidates:
            print("Tidak menemukan kandidat.")
            return
        for idx, item in enumerate(candidates, 1):
            print(f"{idx}. {item.package} score={item.score} | {'; '.join(item.reasons)}")
        selected = input("Nomor yang ingin ditambahkan (contoh 1,2), kosong=semua: ").strip()
        if selected:
            indexes = {int(item.strip()) for item in selected.split(",") if item.strip().isdigit()}
            candidates = [item for idx, item in enumerate(candidates, 1) if idx in indexes]
        self.config["packages"] = PackageScanner.merge_manual(candidates, self.config.get("packages", []))
        self.save_config()
        print(f"Ditambahkan/di-update {len(candidates)} kandidat sebagai status detected.")

    def add_manual(self) -> None:
        pkg = input("Package name: ").strip()
        if not pkg:
            return
        if not valid_package_name(pkg):
            raise ValueError("invalid Android package name")
        alias = input("Alias: ").strip() or pkg
        place_id = input("Place ID: ").strip()
        job_id = input("Job ID (boleh kosong): ").strip()
        mode = input("Mode [deep_link/executor_only]: ").strip() or "deep_link"
        if mode not in {"deep_link", "executor_only"}:
            raise ValueError("mode harus deep_link atau executor_only")
        new_item = {
            "package": pkg,
            "alias": alias,
            "enabled": False,
            "status": "configured",
            "place_id": place_id,
            "job_id": job_id,
            "mode": mode,
            "cooldown_sec": 25,
            "max_retry_per_30min": 5,
            "detect_methods": ["pid", "logcat_crash", "dumpsys"],
        }
        packages = [item for item in self.config.setdefault("packages", []) if item.get("package") != pkg]
        packages.append(new_item)
        self.config["packages"] = packages
        self.save_config()
        print("Package tersimpan.")

    def edit_package(self) -> None:
        packages = self.config.get("packages", [])
        if not packages:
            print("Belum ada package.")
            return
        for idx, item in enumerate(packages, 1):
            print(f"{idx}. {item.get('package')} | {item.get('alias')} | enabled={item.get('enabled')}")
        raw = input("Nomor: ").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= len(packages):
            return
        item = packages[int(raw) - 1]
        for key in ("alias", "place_id", "job_id", "mode"):
            value = input(f"{key} [{item.get(key, '')}]: ").strip()
            if value:
                item[key] = value
        enabled = input(f"enabled [{item.get('enabled')}], y/n/kosong: ").strip().lower()
        if enabled in {"y", "n"}:
            item["enabled"] = enabled == "y"
        if item.get("mode") not in {"deep_link", "executor_only"}:
            raise ValueError("mode harus deep_link atau executor_only")
        self.save_config()
        print("Config package diperbarui.")

    def test_connection(self):
        if not self.config.get("server_url") or not self.config.get("device_token") or not self.config.get("device_id"):
            print("ERROR: device belum paired. Pilih [1] dulu.")
            return
        try:
            result = WebSocketClient.test_connection_sync(self.config)
            print(f"OK: WebSocket connected. ping/pong={result.get('ping_pong')}")
        except Exception as exc:
            print(f"ERROR: WebSocket test failed: {exc}")

    def status(self) -> None:
        packages = self.config.get("packages", [])
        if not packages:
            print("Belum ada package.")
            return
        for item in packages:
            pkg = str(item.get("package"))
            snap = self.monitor.snapshot(pkg)
            pids = ",".join(str(process.pid) for process in snap.processes) or "-"
            print(f"{pkg} | enabled={item.get('enabled')} | running={snap.running} | pid={pids}")

    def start_daemon(self) -> None:
        if self.pid_file.exists():
            pid = self._read_pid()
            if pid and self._is_our_daemon(pid):
                print(f"Daemon sudah berjalan (PID {pid}).")
                return
        proc = subprocess.Popen(
            [sys.executable, str(self.project_dir / "main.py"), "--daemon"],
            cwd=self.project_dir,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"Daemon start request sent (PID {proc.pid}).")

    def stop_daemon(self) -> None:
        pid = self._read_pid()
        if not pid:
            print("PID daemon tidak ditemukan.")
            return
        if not self._is_our_daemon(pid):
            self.pid_file.unlink(missing_ok=True)
            print("PID file bukan daemon project ini.")
            return
        os.kill(pid, signal.SIGTERM)
        self.pid_file.unlink(missing_ok=True)
        print("Sinyal stop dikirim ke daemon.")

    def _read_pid(self) -> int | None:
        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _is_our_daemon(self, pid: int) -> bool:
        result = self.shell.run(f"cat /proc/{pid}/cmdline", timeout=3)
        cmdline = result.stdout.replace("\x00", " ")
        return str(self.project_dir / "main.py") in cmdline and "--daemon" in cmdline
