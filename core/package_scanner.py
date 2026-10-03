"""Purpose: discover third-party Roblox/executor packages without hardcoding package names. Dependencies: local shell wrapper."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


from utils.shell import Shell


@dataclass(slots=True)
class ScanCandidate:
    package: str
    score: int
    reasons: list[str]


def valid_package_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", value))


class PackageScanner:
    ROBLOX_WORDS = ("roblox", "executor", "delta", "hydrogen", "fluxus", "codex", "arceus", "evon", "trigon")
    ACTIVITY_WORDS = ("roblox", "executor", "delta", "hydrogen", "fluxus", "codex", "arceus", "evon", "trigon")

    def __init__(self, shell: Shell) -> None:
        self.shell = shell

    def scan(self) -> list[ScanCandidate]:
        packages = self._third_party_packages()
        candidates: dict[str, ScanCandidate] = {}
        for pkg in packages:
            score, reasons = self._score_package(pkg)
            if score >= 2:
                candidates[pkg] = ScanCandidate(pkg, score, reasons)
        return sorted(candidates.values(), key=lambda item: (-item.score, item.package))

    def _third_party_packages(self) -> list[str]:
        result = self.shell.run("pm list packages -3", timeout=20)
        values = []
        for line in result.stdout.splitlines():
            value = line.strip()
            if value.startswith("package:"):
                value = value.split(":", 1)[1]
            if re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", value):
                values.append(value)
        return sorted(set(values))

    def _score_package(self, package: str) -> tuple[int, list[str]]:
        if not valid_package_name(package):
            return 0, ["invalid package name skipped"]
        score = 0
        reasons: list[str] = []
        import shlex
        data_root = f"/sdcard/Android/data/{package}/files"
        files_result = self.shell.run(f"ls -1 {shlex.quote(data_root)}", timeout=5)
        lower_listing = files_result.stdout.lower()
        subfolders = ("logs", "cache", "assets")
        matched = [name for name in subfolders if name in lower_listing]
        if len(matched) >= 2:
            score += 3
            reasons.append(f"Android/data files contains {', '.join(matched)}")
        elif matched:
            score += 1
            reasons.append(f"Android/data files contains {matched[0]}")

        dump = self.shell.run(f"dumpsys package {shlex.quote(package)}", timeout=8).stdout
        lower = dump.lower()
        activity_matches = [word for word in self.ACTIVITY_WORDS if word in lower]
        if activity_matches:
            score += 2
            reasons.append(f"activity/package metadata matches {', '.join(sorted(set(activity_matches)))}")

        if "android.permission.system_alert_window" in lower or "android.permission.action_manage_overlay_permission" in lower:
            score += 1
            reasons.append("overlay permission present")

        name_matches = [word for word in self.ROBLOX_WORDS if word in package.lower()]
        if name_matches:
            score += 4
            reasons.append(f"package name contains {', '.join(name_matches)}")
        return score, reasons

    @staticmethod
    def merge_manual(candidates: list[ScanCandidate], configured: list[dict]) -> list[dict]:
        existing = {item.get("package"): item for item in configured if item.get("package")}
        for candidate in candidates:
            if candidate.package not in existing:
                existing[candidate.package] = {
                    "package": candidate.package,
                    "alias": candidate.package,
                    "enabled": False,
                    "status": "detected",
                    "place_id": "",
                    "job_id": "",
                    "mode": "deep_link",
                    "cooldown_sec": 25,
                    "max_retry_per_30min": 5,
                    "detect_methods": ["pid", "logcat_crash", "dumpsys"],
                }
        return list(existing.values())
