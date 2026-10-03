"""Purpose: precisely stop one Android package with verified process identity. Dependencies: process monitor and shell wrapper."""

from __future__ import annotations

import shlex
import time

from core.process_monitor import ProcessInfo, ProcessMonitor
from utils.shell import Shell


class KillError(RuntimeError):
    """Raised when a target package cannot be safely stopped."""


class PackageKiller:
    def __init__(self, shell: Shell, monitor: ProcessMonitor) -> None:
        self.shell = shell
        self.monitor = monitor

    def stop(self, package: str, cached: list[ProcessInfo] | None = None) -> str:
        cached = cached or self.monitor.cached_processes(package)
        force = self.shell.run(f"am force-stop {shlex.quote(package)}", timeout=10)
        time.sleep(1.5)
        after_force = self.monitor.snapshot(package)
        if not after_force.running:
            return "force-stop"

        target_uid = self.monitor.target_uid(package)
        if target_uid is None:
            raise KillError(f"target UID could not be verified for package {package}; refusing kill -9 fallback")
        for proc in cached:
            if proc.start_time_ticks is None or proc.uid != target_uid:
                continue
            verified = self.monitor.verify_pid(
                package,
                proc.pid,
                target_uid,
                expected_start_time=proc.start_time_ticks,
            )
            if verified is None:
                continue
            kill = self.shell.run(f"kill -9 {verified.pid}", timeout=5)
            if not kill.ok:
                raise KillError(f"kill -9 failed for verified PID {verified.pid}: {kill.stderr}")
            time.sleep(0.5)
            same_process = self.monitor.verify_pid(
                package,
                verified.pid,
                target_uid,
                expected_start_time=verified.start_time_ticks,
            )
            if same_process is None:
                # None means the exact PID identity is gone or changed, both safe outcomes.
                continue
            raise KillError(f"verified PID {verified.pid} survived kill -9")

        final = self.monitor.snapshot(package)
        if not final.running:
            return "force-stop + verified kill -9"
        raise KillError(f"unable to stop only target package {package}")
