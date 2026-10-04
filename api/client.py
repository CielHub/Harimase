"""Purpose: small HTTP client used only for /pair/claim and /health; WebSocket traffic lives in ws_client.py. Dependencies: standard library."""

from __future__ import annotations

import json
import secrets
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from utils.crypto import sign_request

FIXED_SERVER_URL = "http://nano-1.nura.host:5127"


class AuthRequired(RuntimeError):
    """Raised when server credentials are rejected."""


class ServerUnavailable(RuntimeError):
    """Raised for network or server availability failures."""


@dataclass(slots=True)
class ApiResponse:
    status_code: int
    data: dict


def validate_server_url(value: str) -> tuple[str, bool]:
    normalized = str(value).strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("server_url must be a valid http:// or https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("server_url must not contain username or password")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("server_url must be a base URL without path, query, or fragment")
    hostname = parsed.hostname
    if not hostname or any(ord(char) < 32 or char.isspace() for char in hostname) or len(hostname) > 253:
        raise ValueError("server_url host is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("server_url must use a numeric port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("server_url port must be between 1 and 65535")
    return normalized, parsed.scheme == "http"


class ServerClient:
    BACKOFF = (5, 10, 30, 60, 120, 300)

    def __init__(self, config) -> None:
        self.config = config
        self.config["server_url"] = FIXED_SERVER_URL
        self.base_url, self.insecure_transport = validate_server_url(FIXED_SERVER_URL)
        self.token = str(config.get("device_token", ""))
        self.device_id = str(config.get("device_id", ""))
        self.timeout = max(3, min(int(config.get("server_timeout", 15)), 60))

    def _request(self, method: str, path: str, body: bytes = b"") -> ApiResponse:
        timestamp = int(time.time())
        nonce = secrets.token_hex(16)
        token_for_signing = self.token
        if not token_for_signing:
            raise AuthRequired("device is not paired")
        headers = {
            "Authorization": f"Bearer {token_for_signing}",
            "X-Timestamp": str(timestamp),
            "X-Nonce": nonce,
            "X-Signature": sign_request(body, token_for_signing, timestamp, nonce),
        }
        if self.device_id:
            headers["X-Device-Id"] = self.device_id
        if body:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.base_url}{path}", data=body or None, headers=headers, method=method)
        context = ssl.create_default_context() if self.base_url.startswith("https://") else None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                raw = response.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                return ApiResponse(response.status, payload if isinstance(payload, dict) else {})
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")[:1000]
            if exc.code == 401:
                raise AuthRequired(body_text) from exc
            if exc.code in {409, 429} or exc.code >= 500:
                raise ServerUnavailable(f"HTTP {exc.code}: {body_text}") from exc
            raise RuntimeError(body_text) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ServerUnavailable(str(exc)) from exc

    def health(self) -> dict:
        try:
            request = urllib.request.Request(f"{self.base_url}/health", method="GET")
            context = ssl.create_default_context() if self.base_url.startswith("https://") else None
            with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise ServerUnavailable("invalid health response")
            return payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ServerUnavailable(str(exc)) from exc

    def claim_pair(self, pairing_token: str, device_name: str, device_uuid: str) -> dict:
        body = json.dumps(
            {"pairing_token": pairing_token.strip().upper(), "device_name": device_name[:80], "device_uuid": device_uuid[:128]},
            separators=(",", ":"),
        ).encode()
        self.token = pairing_token.strip().upper()
        response = self._request("POST", "/pair/claim", body)
        data = response.data
        if not data.get("device_token") or not data.get("device_id"):
            raise RuntimeError("server returned an invalid pairing response")
        return data
