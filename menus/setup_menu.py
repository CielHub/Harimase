"""First-time-only interactive Termux setup for pairing and package discovery."""

from __future__ import annotations

from pathlib import Path

from api.client import FIXED_SERVER_URL, ServerClient
from core.package_scanner import PackageScanner, valid_package_name
from utils.shell import Shell


class SetupMenu:
    def __init__(self, project_dir: Path, config: dict, save_config) -> None:
        self.project_dir = project_dir
        self.config = config
        self.save_config = save_config
        self.shell = Shell(use_su=True)
        self.scanner = PackageScanner(self.shell)

    def run(self) -> None:
        while True:
            print("\n=== Roblox Auto-Rejoin First-Time Setup ===")
            print("Server : nano-1.nura.host:5127")
            print("[1] Pair device")
            print("[2] Scan Roblox packages")
            print("[3] Add package manually")
            print("[4] Exit")
            choice = input("Pilih: ").strip()
            try:
                if choice == "1": self.pair()
                elif choice == "2": self.scan()
                elif choice == "3": self.add_manual()
                elif choice == "4": return
                else: print("Pilihan tidak valid.")
            except Exception as exc:
                print(f"ERROR: {exc}")

    def pair(self) -> None:
        self.config["server_url"] = FIXED_SERVER_URL
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
        self.config["setup_completed"] = True
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


