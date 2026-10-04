# TUI Phase Changelog

## Scope
AgentState + Rich Live dashboard + startup/menu behavior requested for the Termux side.

## Implemented

- Added `core/agent_state.py` as thread-safe daemon-owned state store.
- Added `tui/dashboard.py` using `rich.Live`, 1s refresh, cbreak hotkeys, and snapshot-only rendering.
- TUI never calls package manager/process monitor/Android shell APIs directly.
- Added log sink from the normal Python logger into AgentState while TUI is active.
- Added non-TTY/headless fallback to normal console + file logging.
- Added local hotkeys:
  - `q`: graceful daemon shutdown
  - `r`: request clean WebSocket reconnect
  - `l`: toggle compact log panel
  - `s`: selective stop of all enabled packages, without global kill
- `python main.py`:
  - paired + TTY => TUI daemon
  - paired + non-TTY => headless daemon
  - first-time unpaired + TTY => setup menu
  - existing partial/broken config => no setup auto-fallback
- `python main.py --setup` is first-time-only and never used as repair flow.
- `python main.py --daemon` always runs headless.
- Fixed server endpoint to `http://nano-1.nura.host:5127` and WS endpoint resolves to `ws://nano-1.nura.host:5127/ws`.
- Removed server URL input from setup menu.
- Reduced setup menu to pairing, package scan, manual package add, and exit.
- Added `rich` dependency.
- Lazy-loaded optional local Flask API so default daemon startup is not blocked by an unused optional dependency.

## Verification

- Python `compileall`: PASS
- Default module import smoke test: PASS
- AgentState snapshot smoke test: PASS
- Rich TUI render smoke test at 40 columns: PASS
- 7-package snapshot/render smoke test: PASS
- Startup first-time/paired/partial-config rule smoke test: PASS
- Search for old polling/global Roblox kill/setup URL prompt: no matches

## Not live-tested here

Android-specific commands (`am`, `pidof`, `dumpsys`, `/proc`, `logcat`), real Roblox processes, and the production NuraHost/Discord deployment still require integration testing on the target environments.
