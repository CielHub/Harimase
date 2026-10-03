"""Purpose: combine process disappearance, crash-buffer evidence, ANR evidence, and internal logs. Dependencies: shell wrapper and process monitor."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from core.process_monitor import PackageSnapshot, ProcessMonitor
from utils.shell import Shell


@dataclass(slots=True)
class CrashEvidence:
    confirmed: bool
    reason: str
    indicators: list[str]
    fingerprint: str


class CrashDetector:
    def __init__(self, shell: Shell, monitor: ProcessMonitor) -> None:
        self.shell = shell
        self.monitor = monitor
        self._seen_confirmations: set[str] = set()
        self._native_log = ""
        self._java_log = ""

    def refresh(self) -> None:
        self._native_log = self.shell.run("logcat -b crash -d -t 250", timeout=10).stdout
        self._java_log = self.shell.run("logcat -d -t 350", timeout=12).stdout

    def detect(self, package: str, snapshot: PackageSnapshot, was_expected_running: bool) -> CrashEvidence:
        indicators: list[str] = []
        if was_expected_running and not snapshot.running:
            indicators.append("verified PID disappeared")

        matched_lines: list[str] = []
        for line in self._native_log.splitlines():
            if package in line:
                matched_lines.append(line)

        java_lines = self._java_log.splitlines()
        for index, line in enumerate(java_lines):
            window = "\n".join(java_lines[max(0, index - 8): index + 3])
            if package in line and (f"ANR in {package}" in window or "FATAL EXCEPTION" in window):
                matched_lines.append(line)

        new_log_fingerprint = ""
        if matched_lines:
            new_log_fingerprint = hashlib.sha256(
                "\n".join(matched_lines[-25:]).encode("utf-8", errors="ignore")
            ).hexdigest()
            if new_log_fingerprint not in self._seen_confirmations:
                indicators.append("new crash/ANR log evidence")

        if not snapshot.running and self._internal_log_hit(package):
            indicators.append("internal package log evidence")

        cached = self.monitor.cached_processes(package)
        cached_identity = [
            f"{item.pid}:{item.start_time_ticks}:{item.uid}"
            for item in cached
        ]
        fingerprint_material = "|".join(
            [package, new_log_fingerprint, *cached_identity, "not-running" if not snapshot.running else "running"]
        )
        fingerprint = hashlib.sha256(fingerprint_material.encode("utf-8", errors="ignore")).hexdigest()

        uid_verified = snapshot.target_uid is not None and any(item.uid == snapshot.target_uid for item in cached)
        disappeared_confirmed = was_expected_running and not snapshot.running and uid_verified
        log_confirmed = bool(new_log_fingerprint) and new_log_fingerprint not in self._seen_confirmations and not snapshot.running

        if disappeared_confirmed or log_confirmed:
            if fingerprint in self._seen_confirmations:
                return CrashEvidence(False, "confirmation already handled", [], fingerprint)
            self._seen_confirmations.add(fingerprint)
            reason = (
                "PID disappeared after prior UID+cmdline+start-time verification"
                if disappeared_confirmed
                else "new crash/ANR evidence and target process is gone"
            )
            return CrashEvidence(True, reason, indicators, fingerprint)

        return CrashEvidence(False, "no combined confirmation", indicators, fingerprint)

    def _internal_log_hit(self, package: str) -> bool:
        roots = [
            f"/sdcard/Android/data/{package}/files",
            f"/data/data/{package}/files",
        ]
        patterns = re.compile(r"fatal|exception|crash|anr|disconnect|error", re.IGNORECASE)
        import shlex
        for root in roots:
            listing = self.shell.run(
                f"find {shlex.quote(root)} -maxdepth 2 -type f 2>/dev/null | head -n 20", timeout=5
            )
            for raw_path in listing.stdout.splitlines():
                path = raw_path.strip()
                if not path:
                    continue
                content = self.shell.run(f"tail -n 80 -- {shlex.quote(path)}", timeout=4).stdout
                if patterns.search(content):
                    return True
        return False
