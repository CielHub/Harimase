"""Purpose: persistent authenticated WebSocket client with reconnect, heartbeat, ping/pong, idempotency, and outbound queue. Dependencies: websockets."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import secrets
import time
from collections import OrderedDict, deque
from typing import Callable
from urllib.parse import urlparse, urlunparse

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from api.client import AuthRequired, ServerUnavailable, validate_server_url
from utils.crypto import sign_request

MAX_FRAME_BYTES = 64 * 1024
RECONNECT_BACKOFF = (5, 10, 30, 60, 120, 300)
HEARTBEAT_INTERVAL = 30
SERVER_PING_TIMEOUT = 60
IDEMPOTENCY_MAX = 1000
OUTBOUND_QUEUE_MAX = 200


class WebSocketClient:
    VERSION = "1.0.0"

    def __init__(self, config: dict, command_handler, heartbeat_builder: Callable[[], dict], save_config, stop_event: threading.Event, on_auth_failure=None, logger=None, config_lock=None):
        self.config = config
        self.command_handler = command_handler
        self.heartbeat_builder = heartbeat_builder
        self.save_config = save_config
        self.stop_event = stop_event
        self.on_auth_failure = on_auth_failure
        self.logger = logger or logging.getLogger("ws-client")
        self.config_lock = config_lock
        self.base_url, self.insecure_transport = validate_server_url(config.get("server_url", ""))
        self.ws_url = self._to_ws_url(self.base_url)
        self.token = str(config.get("device_token", ""))
        self.device_id = str(config.get("device_id", ""))
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self._send_lock: asyncio.Lock | None = None
        self._last_ping = 0.0
        self._stop_async = None
        self._idempotency: OrderedDict[str, dict] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}
        self._outbound: deque[dict] = deque(maxlen=OUTBOUND_QUEUE_MAX)
        self._outbound_lock = threading.Lock()

    @staticmethod
    def _to_ws_url(base_url: str) -> str:
        parsed = urlparse(base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunparse((scheme, parsed.netloc, "/ws", "", "", ""))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._thread_main, name="websocket-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._close_socket(), self._loop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def send_event_threadsafe(self, event_type: str, payload: dict) -> None:
        self.send_frame_threadsafe({"type": "event", "payload": {"event": event_type, **payload}})

    def send_log_threadsafe(self, level: str, message: str, pkg: str = "") -> None:
        self.send_frame_threadsafe({"type": "log", "payload": {"level": level, "message": message, "pkg": pkg, "ts": time.time()}})

    def send_frame_threadsafe(self, frame: dict) -> None:
        with self._outbound_lock:
            self._outbound.append(frame)

    async def _flush_outbound(self) -> None:
        while True:
            with self._outbound_lock:
                if not self._outbound:
                    break
                frame = self._outbound.popleft()
            await self._send(frame)

    async def _send(self, frame: dict) -> None:
        if not self._ws or not self._send_lock:
            return
        try:
            frame_text = json.dumps(frame, separators=(",", ":"))
            async with self._send_lock:
                await asyncio.wait_for(self._ws.send(frame_text), timeout=10)
        except Exception as exc:
            self.logger.warning("Send failed: %s", exc)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_loop())
        finally:
            self._loop.close()

    async def _connect_loop(self) -> None:
        backoff_index = 0
        while not self.stop_event.is_set():
            try:
                await self._connect_once()
                backoff_index = 0
            except (AuthRequired, ServerUnavailable) as exc:
                self.logger.error("Connection error: %s", exc)
                if isinstance(exc, AuthRequired):
                    if self.on_auth_failure:
                        self.on_auth_failure()
                    return
                backoff = RECONNECT_BACKOFF[min(backoff_index, len(RECONNECT_BACKOFF) - 1)]
                self.logger.info("Reconnecting in %ds", backoff)
                if not self.stop_event.wait(backoff):
                    backoff_index += 1
            except Exception as exc:
                self.logger.exception("Unexpected error: %s", exc)
                backoff = RECONNECT_BACKOFF[min(backoff_index, len(RECONNECT_BACKOFF) - 1)]
                if not self.stop_event.wait(backoff):
                    backoff_index += 1

    async def _close_socket(self) -> None:
        if self._ws:
            try:
                await asyncio.wait_for(self._ws.close(code=1000, reason="closing"), timeout=5)
            except Exception:
                pass
            self._ws = None

    async def _connect_once(self) -> None:
        if not self.token or not self.device_id:
            raise AuthRequired("device is not paired")
        
        # ✅ PHASE 2 FIX: Cleanup old connection before opening new one
        old_ws = self._ws
        self._ws = None
        if old_ws is not None:
            try:
                await old_ws.close(code=1000, reason="reconnecting")
            except Exception as exc:
                self.logger.warning("Failed to close old WebSocket: %s", exc)
        
        headers = self._handshake_headers()
        self.logger.info("Connecting WebSocket %s", self.ws_url)
        try:
            async with connect(
                self.ws_url,
                additional_headers=headers,
                open_timeout=15,
                close_timeout=5,
                ping_interval=None,
                max_size=MAX_FRAME_BYTES,
                max_queue=32,
            ) as ws:
                self._ws = ws  # ✅ Now guaranteed old socket is closed
                self._send_lock = asyncio.Lock()
                self._last_ping = time.monotonic()
                hello = await asyncio.wait_for(ws.recv(), timeout=10)
                self._handle_server_hello(json.loads(hello))
                await self._send({"type": "hello", "payload": self._hello_payload()})
                await self._send({"type": "ready", "payload": {}})
                await self._flush_outbound()
                heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="ws-heartbeat")
                ping_watchdog = asyncio.create_task(self._ping_watchdog(), name="ws-ping-watchdog")
                try:
                    async for raw in ws:
                        self._last_ping = self._last_ping if self._last_ping else time.monotonic()
                        await self._handle_message(raw)
                finally:
                    heartbeat_task.cancel()
                    ping_watchdog.cancel()
                    for task in (heartbeat_task, ping_watchdog):
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                    # ✅ Safety: ensure socket is cleared if it was still current
                    if self._ws is ws:
                        self._ws = None
                    self._send_lock = None
        finally:
            # ✅ Final safety: ensure cleanup even if exception before assignment
            if self._ws is None or not hasattr(self._ws, 'open') or not self._ws.open:
                self._ws = None
        
        if not self.stop_event.is_set():
            raise ServerUnavailable("WebSocket connection closed")

    def _handshake_headers(self) -> dict:
        nonce = secrets.token_hex(8)
        request_line = f"GET /ws HTTP/1.1"
        signature = sign_request(request_line, nonce, self.token)
        return {
            "X-Device-ID": self.device_id,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }

    async def _handle_server_hello(self, message: dict) -> None:
        if not isinstance(message, dict) or message.get("type") != "hello":
            raise RuntimeError("server did not send a valid hello frame")
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        if str(payload.get("device_id", "")) != self.device_id:
            raise AuthRequired("server hello device_id mismatch")
        self._last_ping = time.monotonic()

    def _hello_payload(self) -> dict:
        packages = []
        for item in self._copy_packages():
            if item.get("package"):
                packages.append({
                    "pkg": item["package"],
                    "alias": item.get("alias") or item["package"],
                    "enabled": bool(item.get("enabled", False)),
                })
        return {
            "device_id": self.device_id,
            "packages": packages,
            "version": self.VERSION,
        }

    def _copy_packages(self) -> list:
        if self.config_lock is None:
            return self.config.get("packages", [])
        with self.config_lock:
            return [dict(p) for p in self.config.get("packages", [])]

    async def _handle_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            self.logger.warning("Invalid JSON: %s", raw[:100])
            return
        
        message_type = message.get("type", "")
        payload = message.get("payload", {})
        
        if message_type == "command":
            await self._handle_command(payload)
        elif message_type == "ping":
            await self._send({"type": "pong", "payload": {}})
        else:
            self.logger.debug("Unknown message type: %s", message_type)

    async def _handle_command(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        
        command_id = str(payload.get("id", "")).strip()
        if not command_id:
            return
        
        # Check idempotency
        if command_id in self._idempotency:
            cached = self._idempotency[command_id]
            await self._send({
                "type": "command_ack",
                "payload": {"id": command_id, "status": cached.get("status"), "result": cached.get("result")},
            })
            return
        
        # Execute command
        result = self.command_handler.execute(payload) if self.command_handler else {"status": "error", "result": "no handler"}
        
        # Cache result
        self._idempotency[command_id] = result
        while len(self._idempotency) > IDEMPOTENCY_MAX:
            self._idempotency.popitem(last=False)
        
        # Send ACK
        await self._send({
            "type": "command_ack",
            "payload": {"id": command_id, "status": result.get("status"), "result": result.get("result")},
        })
        
        # Send events if any
        events = result.get("events", [])
        for event_type, event_payload in events:
            await self._send({
                "type": "event",
                "payload": {"event": event_type, **event_payload},
            })

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if not self._ws or self.stop_event.is_set():
                    break
                payload = self.heartbeat_builder() if self.heartbeat_builder else {}
                await self._send({"type": "heartbeat", "payload": payload})
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.warning("Heartbeat failed: %s", exc)
                break

    async def _ping_watchdog(self) -> None:
        while True:
            try:
                await asyncio.sleep(SERVER_PING_TIMEOUT)
                now = time.monotonic()
                if self._last_ping and now - self._last_ping > SERVER_PING_TIMEOUT:
                    self.logger.warning("Server ping timeout")
                    break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.warning("Ping watchdog failed: %s", exc)
                break
