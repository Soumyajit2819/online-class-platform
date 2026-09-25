import hashlib
import hmac
import json
import os
import sys
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main
from app.exam_auth import _b64encode
from app.config import settings
from app.exam_auth import (
    EXAM_MANAGEMENT_SCOPE,
    TOKEN_AUDIENCE,
    TOKEN_TTL_SECONDS,
    FailedAttemptRateLimiter,
    create_exam_management_token,
    require_exam_management,
    verify_exam_management_token,
)
from app.passcode_service import (
    PasscodeService,
    _hash_exam_password,
    _verify_exam_password_hash,
)


@pytest.fixture(autouse=True)
def configured_exam_auth(monkeypatch):
    monkeypatch.setattr(settings, "EXAM_MANAGEMENT_TOKEN_SECRET", "x" * 40)
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "test-service-role-value")
    main.exam_management_login_limiter = FailedAttemptRateLimiter()


def test_exam_password_hash_is_salted_slow_and_contains_no_plaintext():
    password = "correct horse battery staple"
    first = _hash_exam_password(password)
    second = _hash_exam_password(password)

    assert first != second
    assert password not in first
    assert first.startswith("pbkdf2_sha256$600000$")
    assert _verify_exam_password_hash(password, first)
    assert not _verify_exam_password_hash("wrong password", first)
    assert not _verify_exam_password_hash(password, "sha256$bad")


def test_exam_password_update_stores_only_salted_hash(monkeypatch):
    stored = {}

    class Query:
        def __init__(self, record=None):
            self.record = record

        def upsert(self, record, **_kwargs):
            self.record = record
            return self

        def execute(self):
            stored.update(self.record)

    class Supabase:
        def table(self, _name):
            return Query()

    monkeypatch.setattr("app.passcode_service._get_supabase", Supabase)
    service = PasscodeService()
    password = "new exam password"

    assert service.update_exam_management_password(password)
    assert stored["key"] == service.EXAM_MANAGEMENT_KEY
    assert stored["value"] != password
    assert _verify_exam_password_hash(password, stored["value"])


def test_token_is_scoped_and_expiry_is_enforced():
    now = 1_800_000_000
    token = create_exam_management_token(now)
    assert verify_exam_management_token(token, now)
    assert verify_exam_management_token(token, now + TOKEN_TTL_SECONDS - 1)
    assert not verify_exam_management_token(token, now + TOKEN_TTL_SECONDS)
    assert not verify_exam_management_token(token + "x", now)


def test_verifier_rejects_other_scope_and_invalid_signature():
    payload = _b64encode(json.dumps({
        "scope": "recordings_access",
        "aud": TOKEN_AUDIENCE,
        "iat": 1_800_000_000,
        "exp": 1_800_001_000,
    }).encode())
    signature = hmac.new(b"x" * 40, payload.encode("ascii"), hashlib.sha256).digest()
    other_scope_token = f"{payload}.{_b64encode(signature)}"
    assert not verify_exam_management_token(other_scope_token, 1_800_000_001)
    assert not verify_exam_management_token("bad.token", 1_800_000_001)


@pytest.mark.asyncio
async def test_exam_auth_endpoint_issues_scoped_token(monkeypatch):
    monkeypatch.setattr(main.passcode_service, "verify_exam_management_password", lambda value: value == "valid")
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post("/api/exams/auth", json={"password": "valid"})
    assert response.status_code == 200
    data = response.json()
    assert data["scope"] == EXAM_MANAGEMENT_SCOPE
    assert data["expires_in"] == TOKEN_TTL_SECONDS
    assert verify_exam_management_token(data["token"])


@pytest.mark.asyncio
async def test_exam_auth_fails_closed_without_signing_secret(monkeypatch):
    monkeypatch.setattr(settings, "EXAM_MANAGEMENT_TOKEN_SECRET", "")
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post("/api/exams/auth", json={"password": "anything"})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_exam_auth_rejects_student_credential_payload():
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post("/api/exams/auth", json={"google_credential": "student-token"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_exam_auth_rate_limits_failed_passwords(monkeypatch):
    monkeypatch.setattr(main.passcode_service, "verify_exam_management_password", lambda _value: False)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        responses = [
            await client.post("/api/exams/auth", json={"password": "wrong"})
            for _ in range(6)
        ]
    assert [r.status_code for r in responses[:5]] == [403] * 5
    assert responses[5].status_code == 429
    assert int(responses[5].headers["retry-after"]) > 0


@pytest.mark.asyncio
async def test_exam_management_dependency_accepts_only_its_scoped_token():
    valid = create_exam_management_token()
    await require_exam_management(f"Bearer {valid}")
    with pytest.raises(Exception) as caught:
        await require_exam_management("Bearer invalid")
    assert getattr(caught.value, "status_code", None) == 401


@pytest.mark.asyncio
async def test_admin_password_update_requires_admin_and_never_returns_new_password(monkeypatch):
    monkeypatch.setattr(main.passcode_service, "verify_admin_password", lambda value: value == "admin")
    updater = MagicMock(return_value=True)
    monkeypatch.setattr(main.passcode_service, "update_exam_management_password", updater)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        denied = await client.post("/api/admin/update-exam-management-password", json={
            "admin_password": "wrong", "new_passcode": "new-password",
        })
        updated = await client.post("/api/admin/update-exam-management-password", json={
            "admin_password": "admin", "new_passcode": "new-password",
        })
    assert denied.status_code == 403
    assert updated.status_code == 200
    assert "new-password" not in updated.text
    updater.assert_called_once_with("new-password")


@pytest.mark.asyncio
async def test_startup_bootstrap_is_insert_only_and_never_overwrites_existing_exam_password(monkeypatch):
    existing_exam_row = {
        "key": PasscodeService.EXAM_MANAGEMENT_KEY,
        "value": _hash_exam_password("admin-configured-password"),
        "label": "Exams Management Password",
    }
    rows = {existing_exam_row["key"]: dict(existing_exam_row)}
    calls = []

    class Query:
        def __init__(self):
            self.record = None

        def upsert(self, record, **kwargs):
            self.record = record
            calls.append((dict(record), kwargs))
            return self

        def execute(self):
            if self.record["key"] not in rows:
                rows[self.record["key"]] = self.record

    class Supabase:
        def table(self, _name):
            return Query()

    monkeypatch.setattr("app.passcode_service._get_supabase", Supabase)
    await PasscodeService().init_db()

    exam_bootstrap = next(record for record, _ in calls if record["key"] == PasscodeService.EXAM_MANAGEMENT_KEY)
    exam_kwargs = next(kwargs for record, kwargs in calls if record["key"] == PasscodeService.EXAM_MANAGEMENT_KEY)
    assert exam_kwargs == {"on_conflict": "key", "ignore_duplicates": True}
    assert exam_bootstrap["value"] != PasscodeService.DEFAULT_EXAM_MANAGEMENT_PASSWORD
    assert rows[PasscodeService.EXAM_MANAGEMENT_KEY] == existing_exam_row
