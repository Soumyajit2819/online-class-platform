"""Shared-password authorization primitives for Exams management APIs."""

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict
from typing import Any

from fastapi import Header, HTTPException

from .config import settings

EXAM_MANAGEMENT_SCOPE = "exam_management"
TOKEN_AUDIENCE = "exams-management"
TOKEN_TTL_SECONDS = 30 * 60
TOKEN_SECRET_MIN_BYTES = 32


def _signing_key() -> bytes:
    secret = settings.EXAM_MANAGEMENT_TOKEN_SECRET
    if not isinstance(secret, str) or len(secret.encode("utf-8")) < TOKEN_SECRET_MIN_BYTES:
        raise RuntimeError("Exam management token signing is not configured")
    return secret.encode("utf-8")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_exam_management_token(now: int | None = None) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    payload = _b64encode(json.dumps({
        "scope": EXAM_MANAGEMENT_SCOPE,
        "aud": TOKEN_AUDIENCE,
        "iat": issued_at,
        "exp": issued_at + TOKEN_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(12),
    }, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(_signing_key(), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64encode(signature)}"


def verify_exam_management_token(token: str, now: int | None = None) -> bool:
    if not isinstance(token, str) or len(token) > 4096 or token.count(".") != 1:
        return False
    try:
        payload, supplied_signature = token.split(".", 1)
        expected_signature = hmac.new(
            _signing_key(), payload.encode("ascii"), hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64decode(supplied_signature), expected_signature):
            return False
        claims: Any = json.loads(_b64decode(payload))
        if not isinstance(claims, dict):
            return False
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        current_time = int(time.time()) if now is None else int(now)
        return (
            claims.get("scope") == EXAM_MANAGEMENT_SCOPE
            and claims.get("aud") == TOKEN_AUDIENCE
            and type(issued_at) is int
            and type(expires_at) is int
            and issued_at <= current_time + 60
            and expires_at > current_time
            and expires_at > issued_at
            and expires_at - issued_at <= TOKEN_TTL_SECONDS
        )
    except (RuntimeError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return False


async def require_exam_management(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency for every teacher-only Exams management endpoint."""
    try:
        _signing_key()
    except RuntimeError:
        raise HTTPException(503, "Exams management authorization is unavailable")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Exams management authorization is required")
    if not verify_exam_management_token(authorization[7:]):
        raise HTTPException(401, "Exams management authorization is invalid or expired")


class FailedAttemptRateLimiter:
    """Process-local IP throttling for the shared Exams password endpoint."""

    def __init__(self, *, max_failures: int = 5, window_seconds: int = 300,
                 max_clients: int = 10_000):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        expired = []
        for client, timestamps in self._failures.items():
            retained = [stamp for stamp in timestamps if now - stamp < self.window_seconds]
            if retained:
                self._failures[client] = retained
            else:
                expired.append(client)
        for client in expired:
            self._failures.pop(client, None)

    def retry_after(self, client: str, now: float | None = None) -> int:
        current = time.monotonic() if now is None else now
        with self._lock:
            self._prune(current)
            failures = self._failures.get(client, [])
            if len(failures) < self.max_failures:
                return 0
            return max(1, int(self.window_seconds - (current - failures[0]) + 0.999))

    def record_failure(self, client: str, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            self._prune(current)
            if client not in self._failures and len(self._failures) >= self.max_clients:
                oldest_client = min(self._failures, key=lambda key: self._failures[key][-1])
                self._failures.pop(oldest_client, None)
            self._failures[client].append(current)

    def clear(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


exam_management_login_limiter = FailedAttemptRateLimiter()
