"""Purpose: safe Android shell wrapper that scopes every privileged command. Dependencies: Python standard library."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass


@dataclass(slots=True)
class CommandResult:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class Shell:
    def __init__(self, use_su: bool = True) -> None:
        self.use_su = use_su

    def run(self, command: str, timeout: float = 15.0) -> CommandResult:
        if not command.strip():
            raise ValueError("command must not be empty")
        argv = ["su", "-c", command] if self.use_su else ["sh", "-c", command]
        try:
            completed = subprocess.run(
                argv,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            return CommandResult(completed.returncode, completed.stdout.strip(), completed.stderr.strip())
        except subprocess.TimeoutExpired as exc:
            return CommandResult(124, (exc.stdout or "").strip(), "command timeout")

    def run_argv(self, args: list[str], timeout: float = 15.0) -> CommandResult:
        quoted = " ".join(shlex.quote(item) for item in args)
        return self.run(quoted, timeout=timeout)

    def read_file(self, path: str, timeout: float = 10.0) -> CommandResult:
        return self.run(f"cat -- {shlex.quote(path)}", timeout=timeout)
