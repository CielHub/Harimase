#!/data/data/com.termux/files/usr/bin/bash
set -eu
PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
termux-wake-lock || true
cd "$PROJECT_DIR"
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
else
  PYTHON_BIN="python"
fi
exec "$PYTHON_BIN" main.py --daemon
