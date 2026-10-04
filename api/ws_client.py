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

    def __init__(self, config: dict, command_handler, heartbeat_builder: Callable[[], dict], save_config, stop_event: threading.Event, on_auth_failure=None, logger=None, config_lock=None, on_state_change=None, on_session_ready: Callable[[], dict | None] | None = None):
        self.config = config
        self.command_handler = command_handler
        self.heartbeat_builder = heartbeat_builder
        self.save_config = save_config
        self.stop_event = stop_event
        self.on_auth_failure = on_auth_failure
        self.logger = logger or logging.getLogger("ws-client")
        self.config_lock = config_lock
        self.on_state_change = on_state_change
        self.on_session_ready = on_session_ready
        self._reconnect_count = 0
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

    def _set_state(self, state: str, note: str = "") -> None:
        try:
            if self.on_state_change:
                self.on_state_change(
                    state,
                    note=note,
                    reconnect_count=self._reconnect_count,
                )
        except Exception:
            self.logger.exception("WebSocket state callback failed")

    def request_reconnect(self) -> None:
        """Close only the current socket; the normal loop then performs a clean reconnect."""
        self._set_state("RECONNECTING", "Manual reconnect requested")
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._close_socket(), self._loop)
        else:
            self._set_state("DISCONNECTED", "Reconnect deferred until transport loop starts")

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
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._send_or_queue(frame), self._loop)
            future.add_done_callback(lambda _: None)
        else:
            with self._outbound_lock:
                self._outbound.append(frame)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_async = asyncio.Event()
        try:
            self._loop.run_until_complete(self._run_forever())
        finally:
            self._loop.run_until_complete(self._close_socket())
            self._loop.close()
            self._loop = None

    async def _run_forever(self) -> None:
        backoff_index = 0
        while not self.stop_event.is_set():
            self._set_state("CONNECTING")
            try:
                connected = await self._connect_once()
                if connected and not self.stop_event.is_set():
                    backoff_index = 0
                    self._reconnect_count = 0
                    delay = RECONNECT_BACKOFF[0]
                    self._set_state("RECONNECTING", f"Reconnect in {delay}s after clean disconnect")
                    await self._sleep_interruptible(delay)
            except AuthRequired as exc:
                self._set_state("AUTH_FAILED", f"Authentication rejected: {exc}")
                self.logger.error("Authentication rejected. Run setup pairing again: %s", exc)
                self.config["needs_repair"] = True
                self.save_config()
                if self.on_auth_failure:
                    self.on_auth_failure(exc)
                return
            except asyncio.CancelledError:
                raise
            except (InvalidStatus, ConnectionClosed, WebSocketException, OSError, TimeoutError, ServerUnavailable, RuntimeError) as exc:
                delay = RECONNECT_BACKOFF[min(backoff_index, len(RECONNECT_BACKOFF) - 1)]
                self._reconnect_count += 1
                self._set_state("RECONNECTING", f"Reconnect in {delay}s: {exc}")
                self.logger.warning("WebSocket disconnected: %s; reconnect in %ss", exc, delay)
                backoff_index = min(backoff_index + 1, len(RECONNECT_BACKOFF) - 1)
                await self._sleep_interruptible(delay)
            except Exception as exc:
                delay = RECONNECT_BACKOFF[min(backoff_index, len(RECONNECT_BACKOFF) - 1)]
                self._reconnect_count += 1
                self._set_state("RECONNECTING", f"Unexpected transport failure; reconnect in {delay}s")
                self.logger.exception("Unexpected WebSocket loop failure: %s", exc)
                backoff_index = min(backoff_index + 1, len(RECONNECT_BACKOFF) - 1)
                await self._sleep_interruptible(delay)

    async def _sleep_interruptible(self, seconds: int) -> None:
        deadline = time.monotonic() + max(0, seconds)
        while not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(1.0, remaining))

    def _handshake_headers(self) -> dict[str, str]:
        timestamp = int(time.time())
        nonce = secrets.token_hex(16)
        signature = sign_request(b"GET /ws", self.token, timestamp, nonce)
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Device-Id": self.device_id,
            "X-Timestamp": str(timestamp),
            "X-Nonce": nonce,
            "X-Signature": signature,
        }

    async def _connect_once(self) -> None:
        if not self.token or not self.device_id:
            raise AuthRequired("device is not paired")
        headers = self._handshake_headers()
        self.logger.info("Connecting WebSocket %s", self.ws_url)
        self._set_state("AUTHENTICATING", "WebSocket connected; awaiting server hello")
        async with connect(
            self.ws_url,
            additional_headers=headers,
            open_timeout=15,
            close_timeout=5,
            ping_interval=None,
            max_size=MAX_FRAME_BYTES,
            max_queue=32,
        ) as ws:
            self._ws = ws
            self._send_lock = asyncio.Lock()
            self._last_ping = time.monotonic()
            hello = await asyncio.wait_for(ws.recv(), timeout=10)
            self._handle_server_hello(json.loads(hello))
            await self._send({"type": "hello", "payload": self._hello_payload()})
            await self._send({"type": "ready", "payload": {}})
            self._set_state("READY", "WebSocket ready")
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="ws-heartbeat")
            ping_watchdog = asyncio.create_task(self._ping_watchdog(), name="ws-ping-watchdog")
            package_scan_task = None
            if self.on_session_ready is not None:
                package_scan_task = asyncio.create_task(self._auto_scan_and_sync(), name="ws-package-auto-scan")
            await self._flush_outbound()
            try:
                async for raw in ws:
                    self._last_ping = self._last_ping if self._last_ping else time.monotonic()
                    await self._handle_message(raw)
            finally:
                heartbeat_task.cancel()
                ping_watchdog.cancel()
                if package_scan_task is not None:
                    package_scan_task.cancel()
                for task in (heartbeat_task, ping_watchdog, package_scan_task):
                    if task is None:
                        continue
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                self._ws = None
                self._send_lock = None
        if self.stop_event.is_set():
            return False
        return True

    async def _auto_scan_and_sync(self) -> None:
        try:
            inventory = await asyncio.to_thread(self.on_session_ready)
            if inventory:
                await self._send_or_queue({
                    "type": "event",
                    "payload": {"event": "package_inventory", **inventory},
                })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.exception("Automatic package scan/sync failed: %s", exc)

    def _handle_server_hello(self, message: dict) -> None:
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
        return {"device_id": self.device_id, "device_name": self.config.get("device_name", "Android Device"), "version": self.VERSION, "packages": packages}

    def _copy_packages(self) -> list[dict]:
        if self.config_lock:
            with self.config_lock:
                return [dict(item) for item in self.config.get("packages", []) if isinstance(item, dict)]
        return [dict(item) for item in self.config.get("packages", []) if isinstance(item, dict)]

    async def _handle_message(self, raw) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="strict")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
            raise RuntimeError("server frame too large")
        message = json.loads(raw)
        if not isinstance(message, dict):
            return
        frame_type = str(message.get("type", ""))
        if frame_type == "ping":
            self._last_ping = time.monotonic()
            await self._send({"type": "pong", "id": str(message.get("id", "")), "payload": {}})
            return
        if frame_type == "pong":
            self._last_ping = time.monotonic()
            return
        if frame_type == "command":
            command_id = str(message.get("id", "")).strip()
            payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
            action = str(payload.get("action", "")).strip()
            if not command_id or not action:
                return
            command = {"id": command_id, "type": action, "payload": {k: v for k, v in payload.items() if k != "action"}}
            if command_id in self._idempotency:
                cached = self._idempotency[command_id]
                self._idempotency.move_to_end(command_id)
                await self._send(cached)
                return
            if command_id in self._inflight:
                return
            task = asyncio.create_task(self._execute_and_ack(command), name=f"command-{command_id}")
            self._inflight[command_id] = task
            task.add_done_callback(lambda done, command_id=command_id: self._command_done(command_id, done))
            return

    async def _execute_and_ack(self, command: dict) -> None:
        ack_frame = await self._execute_command(command)
        await self._send_or_queue(ack_frame)

    def _command_done(self, command_id: str, task: asyncio.Task) -> None:
        self._inflight.pop(command_id, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error:
            self.logger.error("Command %s task failed: %s", command_id, error)

    async def _execute_command(self, command: dict) -> dict:
        try:
            result = await asyncio.to_thread(self.command_handler.execute, command)
            ack_payload = {"status": result.get("status", "failed"), "result": str(result.get("result", ""))[:8000]}
            frame = {"type": "ack", "id": command["id"], "payload": ack_payload}
            self._remember(command["id"], frame)
            for event_type, payload in result.get("events", []):
                await self._send_or_queue({"type": "event", "payload": {"event": event_type, **payload}})
            return frame
        except Exception as exc:
            frame = {"type": "ack", "id": command["id"], "payload": {"status": "failed", "result": json.dumps({"ok": False, "reason": str(exc)})[:8000]}}
            self._remember(command["id"], frame)
            return frame

    def _remember(self, command_id: str, frame: dict) -> None:
        self._idempotency[command_id] = frame
        self._idempotency.move_to_end(command_id)
        while len(self._idempotency) > IDEMPOTENCY_MAX:
            self._idempotency.popitem(last=False)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            payload = await asyncio.to_thread(self.heartbeat_builder)
            if self.on_state_change:
                try:
                    self.on_state_change("CONNECTED", note="Heartbeat sent", reconnect_count=self._reconnect_count, heartbeat=True)
                except Exception:
                    pass
            await self._send_or_queue({"type": "heartbeat", "payload": payload})

    async def _ping_watchdog(self) -> None:
        while True:
            await asyncio.sleep(5)
            if time.monotonic() - self._last_ping > SERVER_PING_TIMEOUT:
                self.logger.warning("Server ping timeout; closing WebSocket")
                await self._close_socket()
                return

    async def _send(self, frame: dict) -> None:
        if not self._ws or not self._send_lock:
            raise RuntimeError("WebSocket is not connected")
        raw = json.dumps(frame, separators=(",", ":"), ensure_ascii=True)
        if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
            raise ValueError("frame too large")
        async with self._send_lock:
            await self._ws.send(raw)

    async def _send_or_queue(self, frame: dict) -> None:
        try:
            await self._send(frame)
        except Exception:
            with self._outbound_lock:
                self._outbound.append(frame)

    async def _flush_outbound(self) -> None:
        while True:
            with self._outbound_lock:
                if not self._outbound:
                    return
                frame = self._outbound.popleft()
            try:
                await self._send(frame)
            except Exception:
                with self._outbound_lock:
                    self._outbound.appendleft(frame)
                return

    async def _close_socket(self) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.close(1000, "client shutdown")
            except Exception:
                pass

    @staticmethod
    def test_connection_sync(config: dict) -> dict:
        client = WebSocketClient(config, command_handler=None, heartbeat_builder=lambda: {}, save_config=lambda: None, stop_event=threading.Event())
        async def probe() -> dict:
            headers = client._handshake_headers()
            async with connect(client.ws_url, additional_headers=headers, open_timeout=15, close_timeout=5, ping_interval=None, max_size=MAX_FRAME_BYTES) as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if hello.get("type") != "hello":
                    raise RuntimeError("server did not send hello")
                await ws.send(json.dumps({"type": "hello", "payload": client._hello_payload()}, separators=(",", ":")))
                await ws.send(json.dumps({"type": "ready", "payload": {}}, separators=(",", ":")))
                probe_id = f"probe_{int(time.time()*1000)}"
                await ws.send(json.dumps({"type": "ping", "id": probe_id, "payload": {}}, separators=(",", ":")))
                response = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if response.get("type") != "pong" or response.get("id") != probe_id:
                    raise RuntimeError("ping/pong probe failed")
                return {"ok": True, "hello": hello.get("payload", {}), "ping_pong": True}
        try:
            return asyncio.run(probe())
        except Exception as exc:
            raise ServerUnavailable(str(exc)) from exc
