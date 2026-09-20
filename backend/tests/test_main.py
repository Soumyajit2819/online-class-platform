import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.livekit_service import session_state, livekit_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def get_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_returns_services(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/health")
        data = r.json()
        assert "services" in data
        assert "livekit" in data["services"]
        assert "storage" in data["services"]
        assert "database" in data["services"]


# ---------------------------------------------------------------------------
# Create room
# ---------------------------------------------------------------------------

class TestCreateRoom:
    @pytest.mark.asyncio
    async def test_missing_teacher_name_returns_400(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/teacher/create-room", json={
                "room_name": "Math", "meeting_passcode": "abc123"
            })
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_teacher_name_returns_400(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/teacher/create-room", json={
                "teacher_name": "", "room_name": "Math", "meeting_passcode": "abc123"
            })
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_max_participants_over_50_returns_400(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Math",
                "meeting_passcode": "abc123", "max_participants": 100
            })
        assert r.status_code == 400

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_create_room_success(self, mock_create):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Math Class",
                "meeting_passcode": "math123"
            })
        assert r.status_code == 200
        data = r.json()
        assert "room_code" in data
        assert "token" in data
        assert "livekit_url" in data


# ---------------------------------------------------------------------------
# Join room
# ---------------------------------------------------------------------------

class TestJoinRoom:
    @pytest.mark.asyncio
    async def test_nonexistent_room_returns_404(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/student/join-room", json={
                "student_name": "Alice", "room_code": "NOPE00",
                "meeting_passcode": "abc123"
            })
        assert r.status_code == 404

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    @patch("app.main.livekit_service.get_participants", new_callable=AsyncMock)
    async def test_wrong_passcode_returns_403(self, mock_parts, mock_create):
        mock_create.return_value = True
        mock_parts.return_value = []
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Math",
                "meeting_passcode": "correct123"
            })
            room_code = r.json()["room_code"]
            r2 = await c.post("/api/student/join-room", json={
                "student_name": "Alice", "room_code": room_code,
                "meeting_passcode": "wrong123"
            })
        assert r2.status_code == 403

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    @patch("app.main.livekit_service.get_participants", new_callable=AsyncMock)
    async def test_correct_passcode_joins(self, mock_parts, mock_create):
        mock_create.return_value = True
        mock_parts.return_value = []
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Math",
                "meeting_passcode": "join123"
            })
            room_code = r.json()["room_code"]
            r2 = await c.post("/api/student/join-room", json={
                "student_name": "Alice", "room_code": room_code,
                "meeting_passcode": "join123"
            })
        assert r2.status_code == 200
        assert "token" in r2.json()


# ---------------------------------------------------------------------------
# Moderation — 403 for non-teachers
# ---------------------------------------------------------------------------

class TestModeration:
    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    @patch("app.main.livekit_service.get_participants", new_callable=AsyncMock)
    async def test_non_teacher_mute_all_returns_403(self, mock_parts, mock_create):
        mock_create.return_value = True
        mock_parts.return_value = []
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Math",
                "meeting_passcode": "abc123"
            })
            room_code = r.json()["room_code"]
            r2 = await c.post("/api/teacher/mute-all", json={
                "room_code": room_code,
                "teacher_identity": "fake_identity_not_teacher"
            })
        assert r2.status_code == 403

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_non_teacher_end_class_returns_403(self, mock_create):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Math",
                "meeting_passcode": "abc123"
            })
            room_code = r.json()["room_code"]
            r2 = await c.post("/api/teacher/end-class", json={
                "room_code": room_code,
                "teacher_identity": "fake_identity"
            })
        assert r2.status_code == 403


# ---------------------------------------------------------------------------
# Participant limit
# ---------------------------------------------------------------------------

class TestParticipantLimit:
    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_default_max_is_50(self, mock_create):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Math",
                "meeting_passcode": "abc123"
            })
        room_code = r.json()["room_code"]
        info = session_state.get_class(room_code)
        assert info["max_participants"] == 50


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------

class TestTokenGeneration:
    def test_generate_room_code(self):
        code = livekit_service.generate_room_code()
        assert isinstance(code, str)
        assert len(code) > 0

    def test_generate_livekit_room_name(self):
        name = livekit_service.generate_livekit_room_name()
        assert name.startswith("class_")

    def test_hash_passcode_consistent(self):
        h1 = livekit_service.hash_passcode("test123")
        h2 = livekit_service.hash_passcode("test123")
        h3 = livekit_service.hash_passcode("other456")
        assert h1 == h2
        assert h1 != h3

    def test_access_token_generated(self):
        token = livekit_service.create_access_token(
            identity="test_user", name="Test User",
            room="test_room", role="teacher"
        )
        assert isinstance(token, str)
        assert len(token) > 50


# ---------------------------------------------------------------------------
# Passcode endpoints
# ---------------------------------------------------------------------------

class TestPasscodeEndpoints:
    @pytest.mark.asyncio
    async def test_verify_teacher_passcode_empty_returns_400(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/auth/verify-teacher-passcode", json={"passcode": ""})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_verify_recordings_passcode_wrong_returns_403(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/auth/verify-recordings-passcode",
                             json={"passcode": "definitelywrong999"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_login_wrong_returns_403(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/admin/login", json={"password": "wrongpassword"})
        assert r.status_code in (403, 503)  # 503 if ADMIN_DASHBOARD_PASSWORD not set in test env
