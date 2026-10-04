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
        stop_packages: Callable[[], None],
    ) -> None:
        self.state = state
        self.stop_event = stop_event
        self.reconnect = reconnect
        self.stop_packages = stop_packages
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
                    screen=True,
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
            self.state.event("Local hotkey: stopping enabled packages")
            self._spawn_action(self.stop_packages, "stop-packages")

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
        root = Layout(name="root")
        root.split_column(
            Layout(self._header(snapshot, width), name="header", size=7),
            Layout(self._packages(snapshot, width), name="packages", ratio=3),
            Layout(self._footer(snapshot, width), name="footer", size=3),
        )
        if snapshot.get("show_logs"):
            root["packages"].split_column(
                Layout(self._packages(snapshot, width), name="table", ratio=2),
                Layout(self._logs(snapshot, width), name="logs", ratio=1),
            )
        return root

    def _header(self, snapshot: dict, width: int):
        connection = str(snapshot.get("connection", "UNKNOWN"))
        marker = CONNECTION_MARKERS.get(connection, "?")
        hb = _age(snapshot.get("last_heartbeat", 0.0))
        title = Text("ROBLOX AUTO-REJOIN", style="bold")
        meta = Table.grid(padding=(0, 1))
        meta.add_column(no_wrap=True)
        meta.add_column(ratio=1)
        meta.add_row("Device", _clip(snapshot.get("device_name", "-"), max(12, width - 24)))
        meta.add_row("WS", f"{marker} {connection}   hb {hb}   uptime {_fmt_uptime(snapshot.get('uptime_sec', 0))}")
        meta.add_row("Server", _clip(snapshot.get("server_url", "-"), max(18, width - 24)))
        return Panel(Group(title, meta), border_style="bright_black", padding=(0, 1))

    def _packages(self, snapshot: dict, width: int):
        table = Table(
            expand=True,
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 1),
            collapse_padding=True,
        )
        table.add_column("#", width=3, justify="right", no_wrap=True)
        table.add_column("PACKAGE", ratio=3, no_wrap=True)
        table.add_column("STATE", width=13, no_wrap=True)
        table.add_column("PID", width=7, justify="right", no_wrap=True)
        table.add_column("ERR", ratio=2, no_wrap=True)
        packages = snapshot.get("packages", [])
        if not packages:
            table.add_row("-", "no packages configured", "IDLE", "-", "")
            return Panel(table, title=f"PACKAGES ({len(packages)})", border_style="bright_black", padding=(0, 0))
        alias_width = max(12, min(24, width - 39))
        for index, item in enumerate(packages, 1):
            state = str(item.get("state", "IDLE"))
            err = _clip(item.get("last_error", "") or "", max(8, width // 3))
            pid = str(item.get("pid") or "-")
            if item.get("recovery_count"):
                err = _clip(f"R{item['recovery_count']} {err}".strip(), max(8, width // 3))
            table.add_row(
                str(index),
                _clip(item.get("alias") or item.get("package") or "-", alias_width),
                state,
                pid,
                err,
            )
        return Panel(table, title=f"PACKAGES ({len(packages)})", border_style="bright_black", padding=(0, 0))

    def _logs(self, snapshot: dict, width: int):
        logs = snapshot.get("logs", [])[-8:]
        text = Text("\n".join(_clip(line, max(20, width - 4)) for line in logs) or "No log events yet.")
        return Panel(text, title="LOG", border_style="bright_black", padding=(0, 1))

    def _footer(self, snapshot: dict, width: int):
        event = _clip(snapshot.get("last_event", "-"), max(20, width - 34))
        hotkeys = "q quit  r reconnect  l logs  s stop"
        row = Table.grid(expand=True)
        row.add_column(ratio=1)
        row.add_column(justify="right", no_wrap=True)
        row.add_row(event, hotkeys)
        return Panel(row, border_style="bright_black", padding=(0, 1))

    @staticmethod
    def _terminal_width() -> int:
        try:
            return max(40, os.get_terminal_size().columns)
        except OSError:
            return 80
