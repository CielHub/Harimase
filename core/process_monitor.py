"""Purpose: verified PID/UID/cmdline/start-time snapshots per package. Dependencies: local shell wrapper."""

from __future__ import annotations

import re
import shlex
import threading
from dataclasses import dataclass

from utils.shell import Shell


@dataclass(slots=True)
class ProcessInfo:
    pid: int
    cmdline: str
    uid: int | None
    start_time_ticks: int | None


@dataclass(slots=True)
class PackageSnapshot:
    package: str
    target_uid: int | None
    processes: list[ProcessInfo]
    activity_state: str

    @property
    def running(self) -> bool:
        return bool(self.processes)


class ProcessMonitor:
    def __init__(self, shell: Shell) -> None:
        self.shell = shell
        self.previous: dict[str, PackageSnapshot] = {}
        self.current: dict[str, PackageSnapshot] = {}
        self.last_nonempty: dict[str, PackageSnapshot] = {}
        self._lock = threading.RLock()

    def target_uid(self, package: str) -> int | None:
        output = self.shell.run(f"dumpsys package {shlex.quote(package)}", timeout=8).stdout
        match = re.search(r"(?:^|\n)\s*userId=(\d+)", output)
        if not match:
            match = re.search(r"\buserId=(\d+)", output)
        return int(match.group(1)) if match else None

    def pidof(self, package: str) -> list[int]:
        result = self.shell.run(f"pidof {shlex.quote(package)}", timeout=5)
        values: list[int] = []
        for token in result.stdout.split():
            if token.isdigit():
                values.append(int(token))
        return sorted(set(values))

    def _read_start_time(self, pid: int) -> int | None:
        result = self.shell.run(f"cat /proc/{pid}/stat", timeout=3)
        if not result.ok or not result.stdout:
            return None
        # /proc/PID/stat field 2 is comm and may contain spaces/parentheses.
        line = result.stdout
        close_paren = line.rfind(")")
        if close_paren < 0:
            return None
        fields = line[close_paren + 2 :].split()
        # Original field 22 is fields[19] after removing fields 1-2.
        if len(fields) <= 19 or not fields[19].isdigit():
            return None
        return int(fields[19])

    def verify_pid(
        self,
        package: str,
        pid: int,
        expected_uid: int | None,
        expected_start_time: int | None = None,
    ) -> ProcessInfo | None:
        if pid <= 0:
            return None
        cmdline_result = self.shell.run(f"cat /proc/{pid}/cmdline", timeout=3)
        if not cmdline_result.ok:
            return None
        raw_cmdline = cmdline_result.stdout.replace("\x00", " ").strip()
        command_parts = [part for part in raw_cmdline.split() if part]
        cmdline = command_parts[0] if command_parts else ""
        if not cmdline or not (cmdline == package or cmdline.startswith(package + ":")):
            return None

        status_result = self.shell.run(f"cat /proc/{pid}/status", timeout=3)
        if not status_result.ok:
            return None
        uid_match = re.search(r"^Uid:\s+(\d+)", status_result.stdout, re.MULTILINE)
        uid = int(uid_match.group(1)) if uid_match else None
        if expected_uid is not None and uid != expected_uid:
            return None

        start_time_ticks = self._read_start_time(pid)
        if expected_start_time is not None and start_time_ticks != expected_start_time:
            return None
        return ProcessInfo(pid=pid, cmdline=cmdline, uid=uid, start_time_ticks=start_time_ticks)

    def activity_state(self, package: str) -> str:
        output = self.shell.run("dumpsys activity processes", timeout=8).stdout
        matches = [line.strip() for line in output.splitlines() if package in line]
        return " | ".join(matches[:5])

    def snapshot(self, package: str) -> PackageSnapshot:
        with self._lock:
            return self._snapshot_locked(package)

    def _snapshot_locked(self, package: str) -> PackageSnapshot:
        target_uid = self.target_uid(package)
        processes: list[ProcessInfo] = []
        for pid in self.pidof(package):
            verified = self.verify_pid(package, pid, target_uid)
            if verified:
                processes.append(verified)
        snapshot = PackageSnapshot(
            package=package,
            target_uid=target_uid,
            processes=processes,
            activity_state=self.activity_state(package),
        )
        self.previous[package] = self.current.get(package, snapshot)
        self.current[package] = snapshot
        if snapshot.processes:
            self.last_nonempty[package] = snapshot
        return snapshot

    def previous_snapshot(self, package: str) -> PackageSnapshot | None:
        with self._lock:
            return self.previous.get(package)

    def cached_processes(self, package: str) -> list[ProcessInfo]:
        with self._lock:
            previous = self.last_nonempty.get(package) or self.previous.get(package)
            return list(previous.processes) if previous else []
