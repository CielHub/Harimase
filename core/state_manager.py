"""Purpose: Separate runtime state from persistent config.json.

Provides:
  - PackageRuntimeState: Per-package runtime data
  - StateManager: Thread-safe state management

Config.json should contain only:
  - device_name, device_uuid, token, server_url
  - packages list (static configuration)
  - settings (boot_grace_sec, heartbeat_interval, etc.)

Runtime state (volatile, not persisted):
  - current_pid, pid_start_time
  - lifecycle state (idle, starting, running, crashed, recovering)
  - retry counters and timestamps
  - grace period expiration
  - last error/crash info

This separation prevents stale state pollution and makes state lifecycle clear.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import threading
import time


@dataclass(slots=True)
class PackageRuntimeState:
    """Per-package runtime state (NOT persisted in config).
    
    Fresh instance created on startup; discarded on shutdown.
    Thread-safe updates via StateManager.
    """
    
    package: str
    
    # Current process information
    current_pid: Optional[int] = None  # Current running PID (None if not running)
    pid_start_time: Optional[float] = None  # When current PID started (time.time())
    
    # Lifecycle state
    state: str = "idle"  # "idle", "starting", "lobby", "joining", "running", "crashed", "recovering"
    
    # Recovery/retry tracking
    retry_count_30m: int = 0  # Number of rejoin attempts in last 30 minutes
    last_rejoin_at: float = 0.0  # When last rejoin was attempted (time.time())
    grace_until: float = 0.0  # Until this timestamp, rejoin is blocked (grace period)
    
    # Error tracking
    last_crash_at: Optional[float] = None  # When crash was detected
    last_error: str = ""  # Human-readable error message
    error_code: Optional[str] = None  # ErrorCode enum string (e.g., "KILL_FAILED")
    
    def is_grace_active(self) -> bool:
        """Check if grace period is still active (rejoin blocked)."""
        return time.time() < self.grace_until
    
    def grace_remaining_sec(self) -> float:
        """Remaining grace period in seconds (0 if expired)."""
        remaining = self.grace_until - time.time()
        return max(0.0, remaining)


class StateManager:
    """Manages per-package runtime state independently from config.
    
    Thread-safe. All operations protected by RLock.
    
    Usage:
        state_mgr = StateManager()
        state = state_mgr.get_state("com.roblox.1")
        state_mgr.set_pid("com.roblox.1", 12345)
        state_mgr.record_crash("com.roblox.1", "process disappeared", error_code="PROCESS_DIED")
        state_mgr.set_grace_period("com.roblox.1", 30)
    """
    
    def __init__(self):
        self.states: dict[str, PackageRuntimeState] = {}
        self._lock = threading.RLock()
    
    def get_state(self, package: str) -> PackageRuntimeState:
        """Get or create state for package.
        
        Args:
            package: Package name (e.g., "com.roblox.1")
            
        Returns:
            PackageRuntimeState instance (created if doesn't exist)
        """
        with self._lock:
            if package not in self.states:
                self.states[package] = PackageRuntimeState(package=package)
            return self.states[package]
    
    def set_pid(self, package: str, pid: Optional[int]) -> None:
        """Update current PID and record when it started.
        
        Args:
            package: Package name
            pid: New process ID (or None if not running)
        """
        state = self.get_state(package)
        with self._lock:
            state.current_pid = pid
            state.pid_start_time = time.time() if pid else None
    
    def set_state(self, package: str, new_state: str) -> None:
        """Update package lifecycle state.
        
        Args:
            package: Package name
            new_state: New state (idle, starting, running, crashed, recovering, etc.)
        """
        state = self.get_state(package)
        with self._lock:
            state.state = new_state
    
    def record_crash(self, package: str, reason: str, error_code: Optional[str] = None) -> None:
        """Record crash event.
        
        Args:
            package: Package name
            reason: Human-readable crash reason
            error_code: Optional error code string (e.g., "PROCESS_DISAPPEARED")
        """
        state = self.get_state(package)
        with self._lock:
            state.last_crash_at = time.time()
            state.last_error = reason
            state.error_code = error_code
            state.state = "crashed"
    
    def record_rejoin_attempt(self, package: str) -> None:
        """Record rejoin attempt for cooldown tracking.
        
        Args:
            package: Package name
        """
        state = self.get_state(package)
        with self._lock:
            state.last_rejoin_at = time.time()
            state.retry_count_30m += 1
    
    def set_grace_period(self, package: str, duration_sec: float) -> None:
        """Set grace period (prevents rapid rejoin spam).
        
        Args:
            package: Package name
            duration_sec: Grace period duration in seconds
        """
        state = self.get_state(package)
        with self._lock:
            state.grace_until = time.time() + duration_sec
    
    def prune_old_retries(self, package: str, window_sec: float = 1800) -> None:
        """Prune retries older than window (cleanup 30-minute counter).
        
        Args:
            package: Package name
            window_sec: Sliding window (default 30 minutes = 1800 seconds)
        """
        state = self.get_state(package)
        with self._lock:
            now = time.time()
            if now - state.last_rejoin_at > window_sec:
                state.retry_count_30m = 0
    
    def get_all_states(self) -> dict[str, PackageRuntimeState]:
        """Get snapshot of all package states (read-only).
        
        Returns:
            Dictionary of package_name -> PackageRuntimeState
        """
        with self._lock:
            return dict(self.states)
    
    def clear_state(self, package: str) -> None:
        """Clear runtime state for a package (on shutdown/removal).
        
        Args:
            package: Package name
        """
        with self._lock:
            self.states.pop(package, None)
    
    def reset_all(self) -> None:
        """Clear all runtime states (on startup/reset)."""
        with self._lock:
            self.states.clear()
