"""Purpose: execute remote commands without owning the transport; returns ACK data and events for the WebSocket client. Dependencies: local core modules."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from collections import deque

from core.package_scanner import valid_package_name


class CommandHandler:
    def __init__(self, config: dict, save_config, scanner, monitor, killer, rejoiner, logger: logging.LoggerAdapter, config_lock=None) -> None:
        self.config = config
        self.save_config = save_config
        self.scanner = scanner
        self.monitor = monitor
        self.killer = killer
        self.rejoiner = rejoiner
        self.logger = logger
        self.config_lock = config_lock

    def execute(self, command: dict) -> dict:
        command_id = str(command.get("id", "")).strip()
        command_type = str(command.get("type", "")).strip()
        payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
        if not command_id or len(command_id) > 128:
            return {"status": "failed", "result": json.dumps({"ok": False, "reason": "invalid command id"})}
        try:
            result = self._dispatch(command_type, payload)
            status = "success" if result.get("ok") else "failed"
            events = result.pop("_events", []) if isinstance(result, dict) else []
            result_text = json.dumps(result, separators=(",", ":"), ensure_ascii=True)[:8_000]
            return {"status": status, "result": result_text, "events": events}
        except Exception as exc:
            self.logger.exception("Command %s failed: %s", command_id, exc)
            return {"status": "failed", "result": json.dumps({"ok": False, "reason": str(exc)})[:8_000], "events": []}

    def _dispatch(self, command_type: str, payload: dict) -> dict:
        if command_type == "ping":
            return {"ok": True, "received_at": time.time()}
        if command_type == "rejoin":
            package = self._package(payload.get("package", ""))
            result = self.rejoiner.rejoin(package, reason=str(payload.get("reason", "remote command"))[:200])
            events = []
            if result.get("ok"):
                events.append(("rejoin_success", {"package": package["package"], "result": result}))
            elif result.get("critical") or result.get("retry_exhausted"):
                events.append(("rejoin_failed", {"package": package["package"], "result": result}))
            result["_events"] = events
            return result
        if command_type == "log":
            try:
                lines = int(payload.get("lines", payload.get("n", 20)))
            except (TypeError, ValueError):
                lines = 20
            lines = max(1, min(lines, 200))
            log_path = Path(__file__).resolve().parents[1] / "logs" / "client.log"
            if not log_path.exists():
                return {"ok": True, "lines": [], "count": 0}
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                recent = deque(handle, maxlen=lines)
            return {"ok": True, "lines": [line.rstrip("\n") for line in recent], "count": len(recent)}
        if command_type in {"scan", "scan_packages"}:
            candidates = self.scanner.scan()
            with self.config_lock if self.config_lock is not None else _NullLock():
                self.config["packages"] = self.scanner.merge_manual(candidates, self.config.get("packages", []))
                self.save_config()
            return {"ok": True, "packages": [{"package": c.package, "score": c.score, "reasons": c.reasons} for c in candidates]}
        if command_type == "package_add":
            item = dict(payload.get("config") or {})
            pkg = str(item.get("package", "")).strip()
            if not valid_package_name(pkg):
                return {"ok": False, "reason": "invalid package name"}
            item["package"] = pkg
            with self.config_lock if self.config_lock is not None else _NullLock():
                self.config["packages"] = [x for x in self.config.get("packages", []) if x.get("package") != pkg] + [item]
                self.save_config()
            return {"ok": True, "package": pkg}
        if command_type == "package_remove":
            pkg = str(payload.get("package", "")).strip()
            if not valid_package_name(pkg):
                return {"ok": False, "reason": "invalid package name"}
            with self.config_lock if self.config_lock is not None else _NullLock():
                self.config["packages"] = [x for x in self.config.get("packages", []) if x.get("package") != pkg]
                self.save_config()
            return {"ok": True, "package": pkg}
        if command_type == "package_config":
            pkg = str(payload.get("package", "")).strip()
            if not valid_package_name(pkg):
                return {"ok": False, "reason": "invalid package name"}
            new_config = dict(payload.get("config") or {})
            changed = False
            with self.config_lock if self.config_lock is not None else _NullLock():
                for index, item in enumerate(self.config.get("packages", [])):
                    if item.get("package") == pkg:
                        self.config["packages"][index] = {**item, **new_config}
                        changed = True
                        break
                if changed:
                    self.save_config()
            return {"ok": changed, "package": pkg}
        if command_type in {"start", "stop", "package_set_enabled"}:
            pkg = str(payload.get("package", "")).strip()
            if not valid_package_name(pkg):
                return {"ok": False, "reason": "invalid package name"}
            enabled = bool(payload.get("enabled")) if command_type == "package_set_enabled" else command_type == "start"
            changed = False
            with self.config_lock if self.config_lock is not None else _NullLock():
                for item in self.config.get("packages", []):
                    if item.get("package") == pkg:
                        item["enabled"] = enabled
                        item["status"] = "active" if enabled else "stopped"
                        changed = True
                        break
                if changed:
                    self.save_config()
            return {"ok": changed, "package": pkg, "enabled": enabled}
        return {"ok": False, "reason": f"unknown command type: {command_type}"}

    def _package(self, package: str) -> dict:
        package = str(package).strip()
        for item in self.config.get("packages", []):
            if item.get("package") == package:
                return dict(item)
        raise KeyError(f"package not configured: {package}")


class _NullLock:
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False
