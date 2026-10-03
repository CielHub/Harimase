"""Purpose: assemble a device heartbeat payload for the persistent WebSocket client. Dependencies: standard library + process monitor."""

from __future__ import annotations

import os
import time


class HeartbeatReporter:
    VERSION = "1.0.0"

    def __init__(self, config, monitor, config_lock=None) -> None:
        self.config = config
        self.monitor = monitor
        self.config_lock = config_lock
        self.started_at = time.monotonic()

    def payload(self) -> dict:
        if self.config_lock is None:
            items = [dict(item) for item in self.config.get("packages", []) if isinstance(item, dict)]
        else:
            with self.config_lock:
                items = [dict(item) for item in self.config.get("packages", []) if isinstance(item, dict)]
        packages = []
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
                packages.append({"pkg": pkg, "running": False, "pid": None, "enabled": bool(item.get("enabled", False)), "alias": item.get("alias") or pkg})
        return {
            "status": "ok",
            "packages": packages,
            "uptime_sec": int(time.monotonic() - self.started_at),
            "version": self.VERSION,
            "pid": os.getpid(),
        }

    def send(self) -> dict:
        return self.payload()
