"""Minimal Rich Live dashboard for the Termux daemon.

The dashboard consumes AgentState snapshots only. It never reaches into the package
manager, process monitor, or Android shell itself.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import threading
import time
import tty
from contextlib import contextmanager
from typing import Callable

from core.agent_state import AgentState

try:
    from rich.console import Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on minimal installs
    RICH_AVAILABLE = False


CONNECTION_MARKERS = {
    "CONNECTED": "●",
    "READY": "●",
    "CONNECTING": "○",
    "AUTHENTICATING": "◌",
    "RECONNECTING": "↻",
    "STOPPED": "■",
    "AUTH_FAILED": "!",
    "STARTING": "○",
}


def tui_supported() -> bool:
    """Return True only when an interactive terminal and Rich are available."""
    return bool(
        RICH_AVAILABLE
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM", "") not in {"", "dumb"}
    )


def _fmt_uptime(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _age(timestamp: float) -> str:
    if not timestamp:
        return "-"
    seconds = max(0, int(time.time() - timestamp))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h"


def _clip(value: str, width: int) -> str:
    value = str(value)
    if width <= 1:
        return value[:width]
    return value if len(value) <= width else value[: width - 1] + "…"


@contextmanager
def _cbreak_stdin():
    """Put stdin in cbreak mode for single-key hotkeys, then always restore it."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class Dashboard:
    REFRESH_SECONDS = 1.0

    def __init__(
        self,
        state: AgentState,
        stop_event: threading.Event,
        reconnect: Callable[[], None],
        stop_runtime: Callable[[], None],
    ) -> None:
        self.state = state
        self.stop_event = stop_event
        self.reconnect = reconnect
        self.stop_runtime = stop_runtime
        self._thread: threading.Thread | None = None
        self._key_stop = threading.Event()
        self._action_lock = threading.Lock()

    def start(self) -> None:
        if not tui_supported() or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="tui-dashboard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._key_stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.5)

    def _run(self) -> None:
        # The TUI has its own thread so a slow Android shell call cannot freeze rendering.
        try:
            with _cbreak_stdin():
                with Live(
                    self._render(self.state.snapshot()),
                    refresh_per_second=1,
                    transient=False,
                    screen=False,
                    redirect_stdout=False,
                    redirect_stderr=False,
                ) as live:
                    next_render = time.monotonic()
                    while not self.stop_event.is_set() and not self._key_stop.is_set():
                        key = self._read_key(timeout=0.2)
                        if key:
                            self._handle_key(key)
                        now = time.monotonic()
                        if now >= next_render:
                            live.update(self._render(self.state.snapshot()), refresh=True)
                            next_render = now + self.REFRESH_SECONDS
        except (OSError, ValueError, termios.error):
            # A terminal can disappear during SSH/Termux lifecycle changes. Daemon stays alive.
            return

    def _read_key(self, timeout: float) -> str | None:
        try:
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not ready:
            return None
        try:
            return sys.stdin.read(1).lower()
        except (OSError, ValueError):
            return None

    def _handle_key(self, key: str) -> None:
        if key == "q":
            self.state.event("Local hotkey: quit daemon")
            self.stop_event.set()
            return
        if key == "r":
            self.state.event("Local hotkey: reconnect requested")
            self._spawn_action(self.reconnect, "reconnect")
            return
        if key == "l":
            visible = self.state.toggle_logs()
            self.state.event(f"Local hotkey: logs {'ON' if visible else 'OFF'}")
            return
        if key == "s":
            self.state.event("Local hotkey: stopping runtime")
            self._spawn_action(self.stop_runtime, "stop-runtime")

    def _spawn_action(self, callback: Callable[[], None], name: str) -> None:
        if not self._action_lock.acquire(blocking=False):
            self.state.event(f"Local action already running: {name}")
            return

        def runner() -> None:
            try:
                callback()
            except Exception as exc:
                self.state.event(f"Local action failed ({name}): {exc}")
            finally:
                self._action_lock.release()

        threading.Thread(target=runner, name=f"tui-{name}", daemon=True).start()

    def _render(self, snapshot: dict) -> "Group":
        width = max(40, self._terminal_width())
        connection = str(snapshot.get("connection", "UNKNOWN"))
        marker = CONNECTION_MARKERS.get(connection, "?")
        hb = _age(snapshot.get("last_heartbeat", 0.0))
        packages = snapshot.get("packages", [])

        header = Text(
            f"HARIMASE  {marker} {connection}   "
            f"Device: {_clip(snapshot.get('device_name', '-'), 22)}   "
            f"HB: {hb}   Uptime: {_fmt_uptime(snapshot.get('uptime_sec', 0))}",
            style="bold",
        )
        server = Text(f"Server: {_clip(snapshot.get('server_url', '-'), max(20, width - 8))}")

        if snapshot.get("show_logs"):
            body = self._logs(snapshot, width)
            footer = Text("[l] dashboard  [r] reconnect  [q] quit")
            return Group(header, server, body, footer)

        body_lines = []
        for index, item in enumerate(packages[:10], 1):
            alias = _clip(item.get("alias") or item.get("package") or "-", max(12, min(30, width - 24)))
            state = str(item.get("state", "IDLE"))[:12]
            pid = str(item.get("pid") or "-")
            body_lines.append(f"{index:>2}. {alias:<30} {state:<12} {pid:>7}")
        if not body_lines:
            body_lines.append("--  no packages configured")
        package_text = Text("\n".join(body_lines))
        package_count = Text(f"Packages: {len(packages)}   Watchdog: RUNNING")
        last_event = Text(f"Last: {_clip(snapshot.get('last_event', '-'), max(24, width - 7))}")
        footer = Text("[r] reconnect  [l] logs  [s] stop  [q] quit")
        return Group(header, server, package_count, package_text, last_event, footer)

    def _packages(self, snapshot: dict, width: int):
        packages = snapshot.get("packages", [])
        lines = []
        for index, item in enumerate(packages[:10], 1):
            lines.append(f"{index:>2}. {_clip(item.get('alias') or item.get('package') or '-', max(12, width - 8))}")
        return Text("\n".join(lines) or "--  no packages configured")

    def _logs(self, snapshot: dict, width: int):
        logs = snapshot.get("logs", [])[-10:]
        return Text("\n".join(_clip(line, max(20, width - 2)) for line in logs) or "--  no log events yet")

    def _footer(self, snapshot: dict, width: int):
        return Text("[r] reconnect  [l] logs  [s] stop  [q] quit")

    @staticmethod
    def _terminal_width() -> int:
        try:
            return max(40, os.get_terminal_size().columns)
        except OSError:
            return 80
