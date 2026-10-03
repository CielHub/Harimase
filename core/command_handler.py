"""Purpose: poll leased remote commands, execute them serially, and acknowledge results. Dependencies: local core modules and API client."""

from __future__ import annotations

import gzip
import json
import logging
import time
from pathlib import Path

from api.client import AuthRequired, ServerClient, ServerUnavailable
from core.package_scanner import valid_package_name


class CommandHandler:
    def __init__(self, client: ServerClient, config: dict, save_config, scanner, monitor, killer, rejoiner, logger: logging.LoggerAdapter, config_lock=None) -> None:
        self.client = client
        self.config = config
        self.save_config = save_config
        self.scanner = scanner
        self.monitor = monitor
        self.killer = killer
        self.rejoiner = rejoiner
        self.logger = logger
        self.config_lock = config_lock
        self.last_heartbeat = 0.0

    def run_forever(self, heartbeat_reporter, stop_event) -> None:
        backoff_index = 0
        poll_interval = max(2, min(int(self.config.get("poll_interval", 5)), 60))
        heartbeat_interval = max(10, min(int(self.config.get("heartbeat_interval", 30)), 300))
        while not stop_event.is_set():
            try:
                now = time.monotonic()
                if now - self.last_heartbeat >= heartbeat_interval:
                    heartbeat_reporter.send()
                    self.last_heartbeat = now

                commands = self.client.poll_commands(limit=1)
                for command in commands:
                    self._execute(command)
                backoff_index = 0
                stop_event.wait(poll_interval)
            except AuthRequired as exc:
                self.logger.error("Authentication rejected. Run setup pairing again: %s", exc)
                self.config["needs_repair"] = True
                self.save_config()
                stop_event.set()
            except (ServerUnavailable, RuntimeError) as exc:
                delay = ServerClient.BACKOFF[min(backoff_index, len(ServerClient.BACKOFF) - 1)]
                self.logger.warning("Server communication problem: %s; retry in %ss", exc, delay)
                backoff_index = min(backoff_index + 1, len(ServerClient.BACKOFF) - 1)
                stop_event.wait(delay)
            except Exception as exc:
                self.logger.exception("Unexpected command loop failure: %s", exc)
                stop_event.wait(30)

    def _execute(self, command: dict) -> None:
        command_id = str(command.get("id", "")).strip()
        command_type = str(command.get("type", "")).strip()
        payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
        if not command_id or len(command_id) > 128:
            self.logger.error("Rejected malformed remote command")
            return
        try:
            result = self._dispatch(command_type, payload)
            status = "success" if result.get("ok") else "failed"
            result_text = json.dumps(result, separators=(",", ":"), ensure_ascii=True)[:8_000]
            self.client.ack(command_id, status, result_text)
        except Exception as exc:
            self.logger.exception("Command %s failed: %s", command_id, exc)
            try:
                self.client.ack(command_id, "failed", json.dumps({"ok": False, "reason": str(exc)})[:8_000])
            except Exception as ack_exc:
                self.logger.error("Unable to ACK failed command %s: %s", command_id, ack_exc)

    def _dispatch(self, command_type: str, payload: dict) -> dict:
        if command_type == "ping":
            return {"ok": True, "received_at": time.time()}
        if command_type == "log":
            try:
                lines = max(1, min(int(payload.get("lines", 20)), 100))
            except (TypeError, ValueError):
                lines = 20
            return {"ok": True, "lines": self._tail_logs(lines)}
        if command_type == "rejoin":
            package = self._package(payload.get("package", ""))
            result = self.rejoiner.rejoin(package, reason=str(payload.get("reason", "remote command"))[:200])
            if result.get("ok"):
                self.client.event("rejoin_success", {"package": package["package"], "result": result})
            elif result.get("critical") or result.get("retry_exhausted"):
                self.client.event("rejoin_failed", {"package": package["package"], "result": result})
            return result
        if command_type in {"scan", "scan_packages"}:
            candidates = self.scanner.scan()
            if self.config_lock is None:
                self.config["packages"] = self.scanner.merge_manual(candidates, self.config.get("packages", []))
                self.save_config()
            else:
                with self.config_lock:
                    self.config["packages"] = self.scanner.merge_manual(candidates, self.config.get("packages", []))
                    self.save_config()
            return {"ok": True, "packages": [{"package": c.package, "score": c.score, "reasons": c.reasons} for c in candidates]}
        if command_type == "package_add":
            item = dict(payload.get("config") or {})
            pkg = str(item.get("package", "")).strip()
            if not valid_package_name(pkg):
                return {"ok": False, "reason": "invalid package name"}
            item["package"] = pkg
            items = [x for x in self.config.get("packages", []) if x.get("package") != pkg]
            items.append(item)
            if self.config_lock is None:
                self.config["packages"] = items
                self.save_config()
            else:
                with self.config_lock:
                    self.config["packages"] = items
                    self.save_config()
            return {"ok": True, "package": pkg}
        if command_type == "package_remove":
            pkg = str(payload.get("package", "")).strip()
            if not valid_package_name(pkg):
                return {"ok": False, "reason": "invalid package name"}
            if self.config_lock is None:
                self.config["packages"] = [x for x in self.config.get("packages", []) if x.get("package") != pkg]
                self.save_config()
            else:
                with self.config_lock:
                    self.config["packages"] = [x for x in self.config.get("packages", []) if x.get("package") != pkg]
                    self.save_config()
            return {"ok": True, "package": pkg}
        if command_type == "package_config":
            pkg = str(payload.get("package", "")).strip()
            if not valid_package_name(pkg):
                return {"ok": False, "reason": "invalid package name"}
            new_config = dict(payload.get("config") or {})
            changed = False
            def apply_config() -> None:
                nonlocal changed
                for index, item in enumerate(self.config.get("packages", [])):
                    if item.get("package") == pkg:
                        self.config["packages"][index] = {**item, **new_config}
                        changed = True
                        break
            if self.config_lock is None:
                apply_config()
            else:
                with self.config_lock:
                    apply_config()
            return self._save_change(changed, pkg)
        if command_type in {"start", "stop", "package_set_enabled"}:
            pkg = str(payload.get("package", "")).strip()
            if not valid_package_name(pkg):
                return {"ok": False, "reason": "invalid package name"}
            enabled = command_type == "start" if command_type in {"start", "stop"} else bool(payload.get("enabled"))
            changed = False
            def apply_enabled() -> None:
                nonlocal changed
                for item in self.config.get("packages", []):
                    if item.get("package") == pkg:
                        item["enabled"] = enabled
                        item["status"] = "active" if enabled else "stopped"
                        changed = True
                        if enabled:
                            item.setdefault("place_id", "")
                        break
            if self.config_lock is None:
                apply_enabled()
            else:
                with self.config_lock:
                    apply_enabled()
            return self._save_change(changed, pkg, enabled=enabled)
        return {"ok": False, "reason": f"unknown command type: {command_type}"}

    def _save_change(self, changed: bool, pkg: str, **extra) -> dict:
        if changed:
            self.save_config()
        return {"ok": changed, "package": pkg, **extra}

    def _tail_logs(self, lines: int) -> str:
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        base = log_dir / "client.log"
        collected: list[str] = []
        paths = [base] + [log_dir / f"client.log.{index}.gz" for index in range(1, 6)]
        for path in paths:
            if len(collected) >= lines:
                break
            try:
                if path.suffix == ".gz":
                    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                        chunk = handle.readlines()
                else:
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        chunk = handle.readlines()
                collected[0:0] = [line.rstrip("\n") for line in chunk[-lines:]]
            except OSError:
                continue
        return "\n".join(collected[-lines:])

    def _package(self, package: str) -> dict:
        package = str(package).strip()
        items = self.config.get("packages", []) if self.config_lock is None else list(self.config.get("packages", []))
        for item in items:
            if item.get("package") == package:
                return dict(item)
        raise KeyError(f"package not configured: {package}")
