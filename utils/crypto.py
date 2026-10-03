"""Purpose: HMAC request signing and secure config permissions. Dependencies: Python standard library."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path


def sign_request(body: bytes, key: str, timestamp: int, nonce: str) -> str:
    message = str(timestamp).encode("ascii") + b"." + nonce.encode("utf-8") + b"." + body
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_headers(body: bytes, token: str, device_id: str) -> dict[str, str]:
    timestamp = int(time.time())
    nonce = secrets.token_hex(16)
    return {
        "Authorization": f"Bearer {token}",
        "X-Device-Id": device_id,
        "X-Signature": sign_request(body, token, timestamp, nonce),
        "X-Timestamp": str(timestamp),
        "X-Nonce": nonce,
    }


def secure_chmod(path: str | Path) -> None:
    target = Path(path)
    try:
        target.chmod(0o600)
    except OSError:
        return
