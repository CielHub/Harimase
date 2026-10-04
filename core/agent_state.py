"""Thread-safe daemon state store for the terminal dashboard.

The daemon owns this state. The TUI only consumes immutable-ish snapshots and never
polls Android package/process APIs directly.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class PackageState:
    package: str
    alias: str
    state: str = "IDLE"
    pid: int | None = None
    enabled: bool = False
    last_error: str = ""
    recovery_count: int = 0
    last_event: str = ""
    updated_at: float = 0.0


class AgentState:
    """Single source of truth for TUI observability, updated by daemon workers."""

    MAX_LOG_LINES = 120

    def __init__(self, device_name: str, device_id: str, server_url: str, daemon_pid: int) -> None:
        self._lock = threading.RLock()
        self._started_at = time.monotonic()
        self._device_name = device_name or "Android Device"
        self._device_id = device_id or "-"
        self._server_url = server_url or "-"
        self._daemon_pid = daemon_pid
        self._connection = "STARTING"
        self._last_heartbeat = 0.0
        self._reconnect_count = 0
        self._last_event = "Agent starting"
        self._last_event_at = time.time()
        self._logs: deque[str] = deque(maxlen=self.MAX_LOG_LINES)
        self._packages: dict[str, PackageState] = {}
        self._tui_show_logs = False

    def set_connection(self, state: str, *, note: str = "", reconnect_count: int | None = None) -> None:
        with self._lock:
            self._connection = str(state).upper()[:32]
            if reconnect_count is not None:
                self._reconnect_count = max(0, int(reconnect_count))
            if note:
                self._set_event_locked(note)

    def mark_heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat = time.time()

    def set_package(
        self,
        package: str,
        *,
        alias: str | None = None,
        state: str | None = None,
        pid: int | None = None,
        enabled: bool | None = None,
        last_error: str | None = None,
        recovery_count: int | None = None,
        last_event: str | None = None,
    ) -> None:
        package = str(package).strip()
        if not package:
            return
        with self._lock:
            current = self._packages.get(package)
            if current is None:
                current = PackageState(package=package, alias=alias or package, updated_at=time.time())
                self._packages[package] = current
            if alias is not None:
                current.alias = str(alias)[:80] or package
            if state is not None:
                current.state = str(state).upper()[:24]
            if pid is not None or state in {"STOPPED", "IDLE", "CRASHED", "RECOVERING"}:
                current.pid = pid
            if enabled is not None:
                current.enabled = bool(enabled)
            if last_error is not None:
                current.last_error = str(last_error)[:240]
            if recovery_count is not None:
                current.recovery_count = max(0, int(recovery_count))
            if last_event:
                current.last_event = str(last_event)[:240]
                self._set_event_locked(f"{current.alias}: {current.last_event}")
            current.updated_at = time.time()

    def sync_packages(self, items: list[dict[str, Any]]) -> None:
        """Sync configured metadata without querying PackageManager."""
        for item in items:
            package = str(item.get("package", "")).strip()
            if not package:
                continue
            self.set_package(
                package,
                alias=str(item.get("alias") or package),
                enabled=bool(item.get("enabled", False)),
            )

    def remove_package(self, package: str) -> None:
        with self._lock:
            self._packages.pop(package, None)

    def set_logs_visible(self, visible: bool) -> None:
        with self._lock:
            self._tui_show_logs = bool(visible)

    def toggle_logs(self) -> bool:
        with self._lock:
            self._tui_show_logs = not self._tui_show_logs
            return self._tui_show_logs

    def append_log(self, line: str) -> None:
        line = str(line).rstrip()
        if not line:
            return
        with self._lock:
            self._logs.append(line[-500:])

    def event(self, message: str) -> None:
        with self._lock:
            self._set_event_locked(message)
            self._logs.append(str(message)[-500:])

    def _set_event_locked(self, message: str) -> None:
        self._last_event = str(message)[:500]
        self._last_event_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            packages = [asdict(item) for item in self._packages.values()]
            packages.sort(key=lambda item: (not item["enabled"], item["alias"].lower(), item["package"]))
            return {
                "device_name": self._device_name,
                "device_id": self._device_id,
                "server_url": self._server_url,
                "daemon_pid": self._daemon_pid,
                "uptime_sec": max(0, int(time.monotonic() - self._started_at)),
                "connection": self._connection,
                "last_heartbeat": self._last_heartbeat,
                "reconnect_count": self._reconnect_count,
                "last_event": self._last_event,
                "last_event_at": self._last_event_at,
                "packages": deepcopy(packages),
                "logs": list(self._logs),
                "show_logs": self._tui_show_logs,
            }
