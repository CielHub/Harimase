"""Purpose: thread-safe rotating logging with gzip compression. Dependencies: Python standard library."""

from __future__ import annotations

import gzip
import logging
import os
import shutil
from pathlib import Path


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "pkg"):
            record.pkg = "-"
        return True


class GZipRotatingFileHandler(logging.Handler):
    """Rotate one log file at size limit, keep gzip-compressed archives."""

    def __init__(self, filename: str | os.PathLike[str], max_bytes: int = 5 * 1024 * 1024, backups: int = 5) -> None:
        super().__init__()
        self.base_path = Path(filename)
        self.max_bytes = max_bytes
        self.backups = backups
        self.base_path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.base_path.open("a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record) + self.terminator
            encoded = msg.encode("utf-8", errors="replace")
            self.acquire()
            try:
                self._rollover_if_needed(len(encoded))
                self._stream.write(msg)
                self._stream.flush()
            finally:
                self.release()
        except Exception:
            self.handleError(record)

    @property
    def terminator(self) -> str:
        return "\n"

    def _rollover_if_needed(self, incoming: int) -> None:
        try:
            current_size = self.base_path.stat().st_size
        except FileNotFoundError:
            current_size = 0
        if current_size + incoming <= self.max_bytes:
            return

        self._stream.close()
        for index in range(self.backups, 0, -1):
            src_gz = self.base_path.with_name(f"{self.base_path.name}.{index}.gz")
            dst_gz = self.base_path.with_name(f"{self.base_path.name}.{index + 1}.gz")
            if index == self.backups and dst_gz.exists():
                dst_gz.unlink()
            if src_gz.exists():
                src_gz.replace(dst_gz)

        if self.base_path.exists():
            first_gz = self.base_path.with_name(f"{self.base_path.name}.1.gz")
            with self.base_path.open("rb") as src, gzip.open(first_gz, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            self.base_path.unlink()

        self._stream = self.base_path.open("a", encoding="utf-8")

    def close(self) -> None:
        self.acquire()
        try:
            if not self._stream.closed:
                self._stream.close()
        finally:
            self.release()
            super().close()


def setup_logging(log_dir: str = "logs", level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        return root

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(pkg)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    context = _ContextFilter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(context)
    root.addHandler(console)

    file_handler = GZipRotatingFileHandler(Path(log_dir) / "client.log")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context)
    root.addHandler(file_handler)
    return root


def package_logger(pkg: str) -> logging.LoggerAdapter:
    return logging.LoggerAdapter(logging.getLogger("roblox-client"), {"pkg": pkg})
