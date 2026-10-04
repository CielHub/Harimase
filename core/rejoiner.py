"""Purpose: perform scoped Roblox rejoin launches with cooldown, retry limits, and launch grace. Dependencies: shell wrapper and package killer."""

from __future__ import annotations

import shlex
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from core.killer import PackageKiller
from core.package_scanner import valid_package_name
from core.process_monitor import ProcessMonitor
from utils.shell import Shell


@dataclass(slots=True)
class RetryState:
    history: deque[float]
    last_rejoin: float = 0.0
    grace_until: float = 0.0


class Rejoiner:
    def __init__(self, shell: Shell, monitor: ProcessMonitor, killer: PackageKiller, config, save_config, config_lock=None) -> None:
        self.shell = shell
        self.monitor = monitor
        self.killer = killer
        self.config = config
        self.save_config = save_config
        self.config_lock = config_lock
        self.states: dict[str, RetryState] = defaultdict(lambda: RetryState(deque()))
        self._lock = threading.RLock()
        self._load_state()

    def rejoin(self, package_cfg: dict, reason: str = "manual") -> dict:
        package = str(package_cfg.get("package", ""))
        if not valid_package_name(package):
            return {"ok": False, "reason": "invalid package name"}

        try:
            cooldown = max(25, min(int(package_cfg.get("cooldown_sec", 25)), 3600))
            max_retry = max(1, min(int(package_cfg.get("max_retry_per_30min", 5)), 20))
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid retry configuration"}

        mode = str(package_cfg.get("mode", "deep_link"))
        place_id = str(package_cfg.get("place_id", "")).strip()
        job_id = str(package_cfg.get("job_id", "")).strip()
        if mode not in {"deep_link", "executor_only"}:
            return {"ok": False, "reason": "unsupported mode"}
        if mode == "deep_link":
            if not place_id or len(place_id) > 32 or not place_id.isdigit():
                return {"ok": False, "reason": "place_id must be numeric and <=32 characters"}
            if len(job_id) > 256 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in job_id):
                return {"ok": False, "reason": "job_id contains unsupported characters"}

        with self._lock:
            state = self.states[package]
            now = time.time()
            self._prune(state, now)
            if now < state.grace_until and reason.startswith("verified"):
                return {"ok": False, "reason": "launch grace active", "throttled": True}
            if now - state.last_rejoin < cooldown:
                remaining = max(1, int(cooldown - (now - state.last_rejoin)))
                return {"ok": False, "reason": f"cooldown active for {remaining}s", "throttled": True}
            if len(state.history) >= max_retry:
                return {"ok": False, "reason": "max retry per 30 minutes reached", "retry_exhausted": True}

            # Reserve the retry slot before any external process action so two threads cannot race.
            state.history.append(now)
            state.last_rejoin = now
            self._prune(state, now)
            self._persist_state()

            cached = self.monitor.cached_processes(package)
            try:
                kill_mode = self.killer.stop(package, cached=cached)
            except Exception as exc:
                return {"ok": False, "reason": str(exc), "critical": True}

            time.sleep(3)
            if mode == "executor_only":
                launch = self._launch_executor(package)
            else:
                uri = f"roblox://experiences/start?placeId={place_id}"
                if job_id:
                    uri += f"&gameInstanceId={job_id}"
                launch = self.shell.run(
                    f"am start -W -a android.intent.action.VIEW -d {shlex.quote(uri)}", timeout=20
                )

            if not launch.ok:
                return {"ok": False, "reason": launch.stderr or launch.stdout or "launch failed", "critical": True}

            state.grace_until = time.time() + max(10, min(int(self.config.get("boot_grace_sec", 10)), 60))
            self._persist_state()
            return {"ok": True, "reason": reason, "kill": kill_mode, "launch": launch.stdout[-500:]}

    def grace_active(self, package: str) -> bool:
        with self._lock:
            return time.time() < self.states[package].grace_until

    def _launch_executor(self, package: str):
        resolved = self.shell.run(f"cmd package resolve-activity --brief {shlex.quote(package)}", timeout=8)
        component = ""
        for line in reversed(resolved.stdout.splitlines()):
            line = line.strip()
            if "/" in line and not line.startswith("priority="):
                component = line
                break
        if not component:
            from utils.shell import CommandResult
            return CommandResult(1, "", "No launchable activity found")
        return self.shell.run(f"am start -W -n {shlex.quote(component)}", timeout=20)

    def _prune(self, state: RetryState, now: float) -> None:
        while state.history and now - state.history[0] > 1800:
            state.history.popleft()

    def _load_state(self) -> None:
        raw = self.config.get("runtime", {}).get("retry_history", {})
        last = self.config.get("runtime", {}).get("last_rejoin", {})
        for package, values in raw.items():
            if not valid_package_name(str(package)):
                continue
            state = self.states[str(package)]
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, (int, float)):
                        state.history.append(float(item))
            try:
                state.last_rejoin = float(last.get(package, 0.0))
            except (TypeError, ValueError):
                state.last_rejoin = 0.0
            self._prune(state, time.time())

    def _persist_state(self) -> None:
        def persist() -> None:
            runtime = self.config.setdefault("runtime", {})
            runtime["retry_history"] = {pkg: list(state.history) for pkg, state in self.states.items()}
            runtime["last_rejoin"] = {pkg: state.last_rejoin for pkg, state in self.states.items()}
            self.save_config()
        if self.config_lock is None:
            persist()
        else:
            with self.config_lock:
                persist()
