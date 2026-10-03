"""Purpose: authenticated HTTP client with bounded retries and request replay protection. Dependencies: httpx."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from utils.crypto import build_headers, sign_request


class AuthRequired(RuntimeError):
    """Raised when the server rejects the device credentials."""


class ServerUnavailable(RuntimeError):
    """Raised for transport and server-side availability failures."""


@dataclass(slots=True)
class ApiResponse:
    status_code: int
    data: dict


class ServerClient:
    BACKOFF = (5, 10, 30, 60, 120, 300)

    def __init__(self, config) -> None:
        self.config = config
        self.base_url = str(config.get("server_url", "")).rstrip("/")
        self.token = str(config.get("device_token", ""))
        self.device_id = str(config.get("device_id", ""))
        self.timeout = max(3.0, min(float(config.get("server_timeout", 15)), 60.0))
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("server_url must be a valid http:// or https:// URL")
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("server_url must use HTTPS unless it points to localhost")
        self.http = httpx.Client(timeout=self.timeout, follow_redirects=False)

    def close(self) -> None:
        self.http.close()

    def health(self) -> dict:
        try:
            response = self.http.get(f"{self.base_url}/health")
        except httpx.HTTPError as exc:
            raise ServerUnavailable(str(exc)) from exc
        if response.is_redirect or response.is_error:
            raise ServerUnavailable(f"health returned HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise ServerUnavailable("invalid health response")
        return data

    def claim_pair(self, pairing_token: str, device_name: str, device_uuid: str) -> dict:
        body = json.dumps(
            {"pairing_token": pairing_token, "device_name": device_name[:80], "device_uuid": device_uuid[:128]},
            separators=(",", ":"),
        ).encode()
        timestamp = int(time.time())
        nonce = __import__("secrets").token_hex(16)
        headers = {
            "Authorization": f"Bearer {pairing_token}",
            "X-Signature": sign_request(body, pairing_token, timestamp, nonce),
            "X-Timestamp": str(timestamp),
            "X-Nonce": nonce,
            "X-Device-Id": device_uuid,
            "Content-Type": "application/json",
        }
        try:
            response = self.http.post(f"{self.base_url}/pair/claim", content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ServerUnavailable(str(exc)) from exc
        if response.status_code in (401, 409):
            raise AuthRequired(response.text[:1000])
        if response.status_code >= 500:
            raise ServerUnavailable(f"pairing server error HTTP {response.status_code}")
        if response.status_code >= 400:
            raise RuntimeError(response.text[:1000])
        data = response.json()
        if not isinstance(data, dict) or not data.get("device_token") or not data.get("device_id"):
            raise RuntimeError("server returned an invalid pairing response")
        return data

    def _request(self, method: str, path: str, body: bytes = b"", params: dict | None = None) -> ApiResponse:
        if not self.base_url or not self.token or not self.device_id:
            raise AuthRequired("device is not paired")
        headers = build_headers(body, self.token, self.device_id)
        if body:
            headers["Content-Type"] = "application/json"
        try:
            response = self.http.request(
                method,
                f"{self.base_url}{path}",
                content=body or None,
                params=params,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise ServerUnavailable(str(exc)) from exc
        if response.is_redirect:
            raise ServerUnavailable(f"unexpected redirect HTTP {response.status_code}")
        if response.status_code == 401:
            raise AuthRequired(response.text[:1000])
        if response.status_code == 409:
            raise RuntimeError(f"request rejected as replay/conflict: {response.text[:1000]}")
        if response.status_code == 429 or response.status_code >= 500:
            raise ServerUnavailable(f"HTTP {response.status_code}: {response.text[:1000]}")
        if response.status_code >= 400:
            raise RuntimeError(response.text[:1000])
        data = response.json() if response.content else {}
        if not isinstance(data, dict):
            raise RuntimeError("server returned a non-object JSON response")
        return ApiResponse(response.status_code, data)

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, b"", params).data

    def post_json(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
        return self._request("POST", path, body).data

    def poll_commands(self, limit: int = 1) -> list[dict]:
        data = self.get("/commands", {"device_id": self.device_id, "limit": str(max(1, min(limit, 1)))})
        commands = data.get("commands", [])
        if not isinstance(commands, list):
            raise RuntimeError("invalid commands response")
        return commands

    def ack(self, command_id: str, status: str, result: str) -> dict:
        return self.post_json("/commands/ack", {"command_id": command_id, "status": status, "result": result})

    def event(self, event_type: str, payload: dict) -> dict:
        return self.post_json("/event", {"type": event_type, "payload": payload})

    def log(self, level: str, message: str, pkg: str = "") -> dict:
        return self.post_json("/log", {"level": level, "message": message, "pkg": pkg, "ts": time.time()})
