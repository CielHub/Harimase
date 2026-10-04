"""Purpose: Standardized structured event logging for package monitoring, crash detection, and recovery.

Provides:
  - EventType enum: All possible event types
  - ErrorCode enum: Standard error classifications
  - Event dataclass: Complete event context
  - EventLogger: Centralized event logging

This module ensures all package actions are logged with complete context:
timestamp, device_id, package, PID, event_type, action, status, error_code, details.

No changes to config or existing logging; purely additive infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
from typing import Optional
import json
import logging
import time


class EventType(Enum):
    """All possible event types in package lifecycle."""
    
    # Crash/error detection
    CRASH_DETECTED = "crash_detected"
    PROCESS_DISAPPEARED = "process_disappeared"
    LOG_EVIDENCE_FOUND = "log_evidence_found"
    
    # Recovery actions
    REJOIN_INITIATED = "rejoin_initiated"
    KILL_STARTED = "kill_started"
    KILL_COMPLETED = "kill_completed"
    LAUNCH_STARTED = "launch_started"
    LAUNCH_COMPLETED = "launch_completed"
    
    # State transitions
    STATE_CHANGED = "state_changed"
    COOLDOWN_ACTIVATED = "cooldown_activated"
    GRACE_PERIOD_ACTIVATED = "grace_period_activated"
    
    # Results
    REJOIN_SUCCESS = "rejoin_success"
    REJOIN_FAILED = "rejoin_failed"
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    
    # Errors
    KILLER_ERROR = "killer_error"
    MONITOR_ERROR = "monitor_error"
    HANDLER_ERROR = "handler_error"
    COMMAND_ERROR = "command_error"


class ErrorCode(Enum):
    """Standard error classification for consistent error handling."""
    
    # Killer errors
    KILL_FAILED = "KILL_FAILED"
    UID_NOT_FOUND = "UID_NOT_FOUND"
    PID_VERIFICATION_FAILED = "PID_VERIFICATION_FAILED"
    PID_SURVIVED_KILL = "PID_SURVIVED_KILL"
    
    # Rejoin errors (cooldown/blocking)
    REJOIN_BLOCKED_COOLDOWN = "REJOIN_BLOCKED_COOLDOWN"
    REJOIN_BLOCKED_GRACE = "REJOIN_BLOCKED_GRACE"
    REJOIN_BLOCKED_RETRY_LIMIT = "REJOIN_BLOCKED_RETRY_LIMIT"
    
    # Rejoin errors (execution)
    REJOIN_LAUNCH_FAILED = "REJOIN_LAUNCH_FAILED"
    REJOIN_LAUNCH_TIMEOUT = "REJOIN_LAUNCH_TIMEOUT"
    
    # Monitor errors
    MONITOR_SNAPSHOT_FAILED = "MONITOR_SNAPSHOT_FAILED"
    CRASH_DETECTION_FAILED = "CRASH_DETECTION_FAILED"
    
    # Command errors
    INVALID_PACKAGE = "INVALID_PACKAGE"
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
    COMMAND_FAILED = "COMMAND_FAILED"


@dataclass(slots=True)
class Event:
    """Standardized event with complete context.
    
    All fields except timestamp are set before logging. Timestamp is auto-set if not provided.
    """
    
    # Core fields
    timestamp: float  # time.time()
    device_id: str
    package: str
    
    # Action fields
    event_type: EventType
    action: str  # e.g., "force_stop", "verify_kill", "launch_uri", "monitor_detect"
    status: str  # "initiated", "success", "failed", "detected"
    
    # Process fields
    pid: Optional[int] = None  # Current PID if relevant
    old_pid: Optional[int] = None  # Previous PID if changed
    
    # Error fields
    error_code: Optional[ErrorCode] = None
    error_message: str = ""
    
    # Additional context
    details: dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        d = asdict(self)
        d['timestamp'] = self.timestamp
        d['event_type'] = self.event_type.value
        if self.error_code:
            d['error_code'] = self.error_code.value
        else:
            d['error_code'] = None
        return d
    
    def to_json(self) -> str:
        """Convert to JSON string for structured logging."""
        return json.dumps(self.to_dict(), separators=(",", ":"), default=str)
    
    def to_log_line(self) -> str:
        """Format as single-line log for readability."""
        return f"EVENT={self.event_type.value} pkg={self.package} pid={self.pid or '-'} status={self.status} error={self.error_code.value if self.error_code else '-'}"


class EventLogger:
    """Centralized event logging with structured format.
    
    All events are logged both to Python logging (for files) and as structured JSON (for parsing).
    
    Usage:
        event_logger = EventLogger(device_id, logging.getLogger("events"))
        event_logger.crash_detected(package, reason, indicators, pid)
        event_logger.rejoin_success(package, old_pid, new_pid)
    """
    
    def __init__(self, device_id: str, logger: logging.LoggerAdapter | logging.Logger):
        self.device_id = device_id
        self.logger = logger if isinstance(logger, logging.LoggerAdapter) else logging.LoggerAdapter(logger, {})
    
    def log(self, event: Event) -> None:
        """Log event with structured format to both human and machine readers."""
        # Ensure device_id and timestamp are set
        event.device_id = self.device_id
        if event.timestamp == 0 or not event.timestamp:
            event.timestamp = time.time()
        
        # Log readable format (human)
        self.logger.info(event.to_log_line())
        
        # Log JSON format (machine parsing)
        self.logger.debug(event.to_json())
    
    def crash_detected(self, package: str, reason: str, indicators: list, pid: Optional[int] = None) -> Event:
        """Log crash detection event.
        
        Args:
            package: Package name
            reason: Human-readable crash reason
            indicators: List of indicators that confirmed crash
            pid: Process ID if known
        """
        event = Event(
            timestamp=time.time(),
            device_id=self.device_id,
            package=package,
            event_type=EventType.CRASH_DETECTED,
            action="monitor_detect_crash",
            status="detected",
            pid=pid,
            error_message=reason,
            details={"indicators": indicators}
        )
        self.log(event)
        return event
    
    def rejoin_initiated(self, package: str, old_pid: Optional[int], reason: str) -> Event:
        """Log rejoin attempt initiation.
        
        Args:
            package: Package name
            old_pid: Previous process ID
            reason: Why rejoin was triggered
        """
        event = Event(
            timestamp=time.time(),
            device_id=self.device_id,
            package=package,
            event_type=EventType.REJOIN_INITIATED,
            action="rejoin_start",
            status="initiated",
            old_pid=old_pid,
            details={"reason": reason}
        )
        self.log(event)
        return event
    
    def rejoin_success(self, package: str, old_pid: Optional[int], new_pid: Optional[int]) -> Event:
        """Log successful rejoin.
        
        Args:
            package: Package name
            old_pid: Previous process ID
            new_pid: New process ID after recovery
        """
        event = Event(
            timestamp=time.time(),
            device_id=self.device_id,
            package=package,
            event_type=EventType.REJOIN_SUCCESS,
            action="rejoin_complete",
            status="success",
            old_pid=old_pid,
            pid=new_pid,
            details={}
        )
        self.log(event)
        return event
    
    def rejoin_failed(self, package: str, error_code: ErrorCode, error_msg: str, details: dict = None) -> Event:
        """Log failed rejoin attempt.
        
        Args:
            package: Package name
            error_code: StandardErrorCode enum value
            error_msg: Error message
            details: Additional context (cooldown_remaining_sec, retry_count, etc.)
        """
        event = Event(
            timestamp=time.time(),
            device_id=self.device_id,
            package=package,
            event_type=EventType.REJOIN_FAILED,
            action="rejoin_complete",
            status="failed",
            error_code=error_code,
            error_message=error_msg,
            details=details or {}
        )
        self.log(event)
        return event
    
    def killer_error(self, package: str, error_code: ErrorCode, error_msg: str, pid: Optional[int] = None) -> Event:
        """Log killer/process termination error.
        
        Args:
            package: Package name
            error_code: ErrorCode (UID_NOT_FOUND, KILL_FAILED, PID_VERIFICATION_FAILED, etc.)
            error_msg: Error details
            pid: Process ID if relevant
        """
        event = Event(
            timestamp=time.time(),
            device_id=self.device_id,
            package=package,
            event_type=EventType.KILLER_ERROR,
            action="killer_stop",
            status="failed",
            pid=pid,
            error_code=error_code,
            error_message=error_msg,
            details={}
        )
        self.log(event)
        return event
    
    def monitor_error(self, package: str, error_code: ErrorCode, error_msg: str) -> Event:
        """Log process monitoring error.
        
        Args:
            package: Package name
            error_code: ErrorCode (MONITOR_SNAPSHOT_FAILED, etc.)
            error_msg: Error details
        """
        event = Event(
            timestamp=time.time(),
            device_id=self.device_id,
            package=package,
            event_type=EventType.MONITOR_ERROR,
            action="monitor_snapshot",
            status="failed",
            error_code=error_code,
            error_message=error_msg,
            details={}
        )
        self.log(event)
        return event
    
    def command_error(self, package: str, error_code: ErrorCode, error_msg: str) -> Event:
        """Log command execution error.
        
        Args:
            package: Package name
            error_code: ErrorCode (COMMAND_FAILED, etc.)
            error_msg: Error details
        """
        event = Event(
            timestamp=time.time(),
            device_id=self.device_id,
            package=package,
            event_type=EventType.COMMAND_ERROR,
            action="command_execute",
            status="failed",
            error_code=error_code,
            error_message=error_msg,
            details={}
        )
        self.log(event)
        return event
