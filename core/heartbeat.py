"""Purpose: assemble and transmit periodic device health snapshots. Dependencies: server client and process monitor."""

from __future__ import annotations

import os
import time


class HeartbeatReporter:
    VERSION = "1.0.0"

    def __init__(self, client, config, monitor, config_lock=None) -> None:
        self.client = client
        self.config = config
        self.monitor = monitor
        self.config_lock = config_lock
        self.started_at = time.monotonic()

    def send(self) -> None:
        packages = []
        items = list(self.config.get("packages", [])) if self.config_lock is None else self._copy_packages()
        for item in items:
            pkg = item.get("package")
            if not pkg:
                continue
            try:
                snapshot = self.monitor.snapshot(pkg)
                packages.append({
                    "pkg": pkg,
                    "running": snapshot.running,
                    "pid": snapshot.processes[0].pid if snapshot.processes else None,
                    "enabled": bool(item.get("enabled", False)),
                    "alias": item.get("alias") or pkg,
                })
            except Exception:
                packages.append({"pkg": pkg, "running": False, "pid": None})
        self.client.post_json("/heartbeat", {
            "status": "ok",
            "packages": packages,
            "uptime_sec": int(time.monotonic() - self.started_at),
            "version": self.VERSION,
            "pid": os.getpid(),
        })

    def _copy_packages(self) -> list[dict]:
        with self.config_lock:
            return [dict(item) for item in self.config.get("packages", []) if isinstance(item, dict)]
