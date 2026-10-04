"""Compact interactive entry menu for the Termux client."""

from __future__ import annotations

from typing import Callable


class MainMenu:
    def __init__(self, config: dict, save_config: Callable[[], None], start_runtime: Callable[[], None], pairing_flow: Callable[[], None]) -> None:
        self.config = config
        self.save_config = save_config
        self.start_runtime = start_runtime
        self.pairing_flow = pairing_flow

    def run(self) -> None:
        while True:
            print("\nHARIMASE")
            print("────────────")
            paired = bool(
                self.config.get("server_url")
                and self.config.get("device_token")
                and self.config.get("device_id")
                and not self.config.get("needs_repair")
            )
            device_id = self.config.get("device_id") or "-"
            print(f"Device : {device_id}")
            print(f"Status : {'Paired' if paired else 'Not paired'}")
            print()
            print("1. Start")
            print("2. Pairing Device")
            print("3. Exit")
            choice = input("Select: ").strip()

            if choice == "1":
                if not paired:
                    print("\nDevice belum dipairing. Pilih '2. Pairing Device' terlebih dahulu.")
                    continue
                self.start_runtime()
            elif choice == "2":
                try:
                    self.pairing_flow()
                except Exception as exc:
                    print(f"Pairing gagal: {exc}")
            elif choice == "3":
                return
            else:
                print("Pilihan tidak valid.")
