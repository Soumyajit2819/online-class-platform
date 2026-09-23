import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import sys
import os
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from httpx import AsyncClient, ASGITransport
from app.main import app, _remux_hls_to_mp4, _safe_download_filename, verify_google_credential
from app.main import google_id_token
from app.livekit_service import session_state, livekit_service
from app.config import settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def get_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def mock_verified_google_identity(monkeypatch):
    monkeypatch.setattr("app.main.verify_google_credential", lambda _credential: {
        "sub": "test-google-sub", "name": "Alice Google",
    })


def register_approved_identity(room_code, identity):
    session_id = f"approved-session-for-{room_code}-long"
    request = session_state.create_join_request(room_code, "Approved Student", session_id, identity)
    session_state.decide_join_request(request["request_id"], "APPROVED")
    return request["request_id"], session_id


class TestRecordingPlaybackTokens:
    def test_recording_access_token_is_signed_and_expires(self):
        previous = settings.RECORDING_PLAYBACK_SECRET
        settings.RECORDING_PLAYBACK_SECRET = "test-recording-secret"
        try:
            token = livekit_service.create_recordings_access_token()
            assert livekit_service.verify_recordings_access_token(token)
            assert not livekit_service.verify_recordings_access_token(token + "tampered")
        finally:
            settings.RECORDING_PLAYBACK_SECRET = previous


class TestRecordingDownload:
    @staticmethod
    def _record(expires_at=None):
        return {
            "recording_id": "rec_download", "class_name": "Math / Grade 9",
            "playlist_key": "recordings/ROOM/rec_download/index.m3u8",
            "expires_at": expires_at or (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "status": "available",
        }

    @pytest.mark.asyncio
    async def test_download_requires_recordings_access_token(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get("/api/recordings/rec_download/download")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_download_rejects_invalid_recordings_access_token(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get(
                "/api/recordings/rec_download/download",
                headers={"Authorization": "Bearer invalid-token"},
            )
        assert response.status_code == 401

    @pytest.mark.asyncio
    @patch("app.main.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("app.main.livekit_service.object_exists", new_callable=AsyncMock, return_value=True)
    @patch("app.main.livekit_service.get_recording_metadata", new_callable=AsyncMock)
    @patch("app.main._remux_hls_to_mp4", new_callable=AsyncMock)
    async def test_authorized_download_streams_mp4(self, remux, get_recording, _object_exists, _ffmpeg):
        async def fake_remux(_playlist_url, output_path):
            with open(output_path, "wb") as output:
                output.write(b"finalized-mp4-bytes")

        get_recording.return_value = self._record()
        previous = settings.RECORDING_PLAYBACK_SECRET
        settings.RECORDING_PLAYBACK_SECRET = "test-recording-secret"
        try:
            token = livekit_service.create_recordings_access_token()
            remux.side_effect = fake_remux
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get(
                    "/api/recordings/rec_download/download",
                    headers={"Authorization": f"Bearer {token}"},
                )
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("video/mp4")
            assert response.headers["content-disposition"] == 'attachment; filename="Math-Grade-9-rec_download.mp4"'
            assert response.content == b"finalized-mp4-bytes"
            playlist_url = remux.await_args.args[0]
            assert "/hls/index.m3u8?" in playlist_url
            assert "live.m3u8" not in playlist_url
        finally:
            settings.RECORDING_PLAYBACK_SECRET = previous

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.get_object", new_callable=AsyncMock)
    @patch("app.main.livekit_service.get_recording_metadata", new_callable=AsyncMock)
    async def test_hls_proxy_rewrites_and_serves_every_segment_as_media(self, get_recording, get_object):
        record = self._record()
        record["storage_prefix"] = "recordings/ROOM/rec_download/"
        playlist = b"#EXTM3U\n#EXTINF:4.0,\nsegment0.ts\n#EXTINF:4.0,\nsegment1.ts\n#EXT-X-ENDLIST\n"
        objects = {
            record["playlist_key"]: playlist,
            f"{record['storage_prefix']}segment0.ts": b"first-transport-stream",
            f"{record['storage_prefix']}segment1.ts": b"second-transport-stream",
        }
        get_recording.return_value = record
        get_object.side_effect = lambda key: objects[key]
        previous = settings.RECORDING_PLAYBACK_SECRET
        settings.RECORDING_PLAYBACK_SECRET = "test-recording-secret"
        try:
            token = livekit_service.create_playback_token(record["recording_id"], record["expires_at"])
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get(
                    f"/api/recording/{record['recording_id']}/hls/index.m3u8?token={token}"
                )
                assert response.status_code == 200
                segment_urls = [line for line in response.text.splitlines() if line.startswith("/api/")]
                assert len(segment_urls) == 2
                for segment_url, expected in zip(
                    segment_urls,
                    (b"first-transport-stream", b"second-transport-stream"),
                ):
                    segment = await client.get(urlsplit(segment_url).path + "?" + urlsplit(segment_url).query)
                    assert segment.status_code == 200
                    assert segment.headers["content-type"].startswith("video/mp2t")
                    assert segment.content == expected
        finally:
            settings.RECORDING_PLAYBACK_SECRET = previous

    @pytest.mark.asyncio
    async def test_remux_finalizes_mp4_and_converts_adts_aac(self, tmp_path):
        output_path = tmp_path / "recording.mp4"
        process = MagicMock(returncode=0)

        async def communicate():
            output_path.write_bytes(b"finalized mp4")
            return b"", b""

        process.communicate.side_effect = communicate
        with patch("app.main.asyncio.create_subprocess_exec", new_callable=AsyncMock) as create_process:
            create_process.return_value = process
            await _remux_hls_to_mp4("http://127.0.0.1/playlist.m3u8", str(output_path))

        command = create_process.await_args.args
        assert ("-bsf:a", "aac_adtstoasc") == command[command.index("-bsf:a"):command.index("-bsf:a") + 2]
        assert ("-movflags", "+faststart") == command[command.index("-movflags"):command.index("-movflags") + 2]
        assert "empty_moov" not in command

    @pytest.mark.asyncio
    @patch("app.main.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("app.main.livekit_service.get_recording_metadata", new_callable=AsyncMock)
    async def test_download_missing_recording_returns_404(self, get_recording, _ffmpeg):
        get_recording.return_value = None
        previous = settings.RECORDING_PLAYBACK_SECRET
        settings.RECORDING_PLAYBACK_SECRET = "test-recording-secret"
        try:
            token = livekit_service.create_recordings_access_token()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/api/recordings/nope/download", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 404
        finally:
            settings.RECORDING_PLAYBACK_SECRET = previous

    @pytest.mark.asyncio
    @patch("app.main.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("app.main.livekit_service.object_exists", new_callable=AsyncMock)
    @patch("app.main.livekit_service.get_recording_metadata", new_callable=AsyncMock)
    async def test_download_expired_recording_returns_410(self, get_recording, object_exists, _ffmpeg):
        get_recording.return_value = self._record(datetime.now(timezone.utc).isoformat())
        object_exists.return_value = True
        previous = settings.RECORDING_PLAYBACK_SECRET
        settings.RECORDING_PLAYBACK_SECRET = "test-recording-secret"
        try:
            token = livekit_service.create_recordings_access_token()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/api/recordings/rec_download/download", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 410
        finally:
            settings.RECORDING_PLAYBACK_SECRET = previous

    def test_download_filename_is_sanitized(self):
        filename = _safe_download_filename({"class_name": "../../bad:name?", "recording_id": "rec_123"})
        assert filename == "bad-name-rec_123.mp4"
        assert "/" not in filename and ".." not in filename

    def test_playback_token_is_scoped_to_one_recording(self):
        previous = settings.RECORDING_PLAYBACK_SECRET
        settings.RECORDING_PLAYBACK_SECRET = "test-recording-secret"
        try:
            token = livekit_service.create_playback_token(
                "rec_one", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
            assert livekit_service.verify_playback_token(token, "rec_one")
            assert not livekit_service.verify_playback_token(token, "rec_other")
        finally:
            settings.RECORDING_PLAYBACK_SECRET = previous


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
    async def test_direct_join_endpoint_cannot_bypass_waiting_room(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/student/join-room", json={
                "student_name": "Alice", "room_code": "NOPE00",
                "meeting_passcode": "abc123"
            })
        assert r.status_code == 403

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
    async def test_correct_passcode_cannot_issue_direct_token(self, mock_parts, mock_create):
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
        assert r2.status_code == 403


class TestWaitingRoom:
    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_manual_passwordless_join_uses_class_info_and_creates_waiting_request(self, mock_create):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Open Class",
            })
            room_code = created.json()["room_code"]
            class_info = await c.get(f"/api/class/{room_code}")
            session_id = "manual-passwordless-session-01"
            pending = await c.post("/api/student/join-requests", json={
                "google_credential": "mock-google-id-token", "room_code": room_code,
                "session_id": session_id,
            })
            token_before_approval = await c.post(
                f"/api/student/join-requests/{pending.json()['request_id']}/token",
                json={"session_id": session_id},
            )
        assert class_info.status_code == 200
        assert class_info.json()["meeting_passcode_required"] is False
        assert "meeting_passcode" not in class_info.json()
        assert pending.status_code == 200
        assert pending.json()["status"] == "WAITING"
        assert token_before_approval.status_code == 403

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_manual_protected_join_reports_password_required_and_rejects_empty_password(self, mock_create):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Protected Class", "meeting_passcode": "join123",
            })
            room_code = created.json()["room_code"]
            class_info = await c.get(f"/api/class/{room_code}")
            rejected = await c.post("/api/student/join-requests", json={
                "google_credential": "mock-google-id-token", "room_code": room_code,
                "session_id": "manual-protected-session-01",
            })
        assert class_info.status_code == 200
        assert class_info.json()["meeting_passcode_required"] is True
        assert rejected.status_code == 403
        assert rejected.json()["detail"] == "Incorrect meeting passcode"

    @pytest.mark.asyncio
    async def test_manual_join_invalid_room_code_returns_not_found(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.post("/api/student/join-requests", json={
                "google_credential": "mock-google-id-token", "room_code": "NOPE00",
                "session_id": "manual-invalid-room-session",
            })
        assert response.status_code == 404
        assert response.json()["detail"] == "Class not found or has ended"

    def test_valid_google_identity_uses_configured_audience(self, monkeypatch):
        previous = settings.GOOGLE_CLIENT_ID
        settings.GOOGLE_CLIENT_ID = "student-web-client.apps.googleusercontent.com"
        try:
            with patch("app.main.google_id_token.verify_oauth2_token", return_value={
                "sub": "stable-google-sub", "name": "Verified Name", "aud": settings.GOOGLE_CLIENT_ID,
            }) as verify:
                identity = verify_google_credential("opaque-id-token")
            assert identity == {"sub": "stable-google-sub", "name": "Verified Name"}
            assert verify.call_args.kwargs["audience"] == settings.GOOGLE_CLIENT_ID
        finally:
            settings.GOOGLE_CLIENT_ID = previous

    def test_invalid_google_token_is_rejected(self):
        previous = settings.GOOGLE_CLIENT_ID
        settings.GOOGLE_CLIENT_ID = "expected-web-client-id"
        try:
            with patch("app.main.google_id_token.verify_oauth2_token", side_effect=ValueError("invalid signature")) as verify:
                with pytest.raises(Exception) as exc:
                    verify_google_credential("invalid-id-token")
            assert getattr(exc.value, "status_code", None) == 401
            assert verify.call_args.kwargs["audience"] == "expected-web-client-id"
        finally:
            settings.GOOGLE_CLIENT_ID = previous

    def test_wrong_google_token_audience_is_rejected(self):
        previous = settings.GOOGLE_CLIENT_ID
        settings.GOOGLE_CLIENT_ID = "expected-web-client-id"
        try:
            with patch("app.main.google_id_token.verify_oauth2_token", return_value={
                "aud": "different-web-client-id", "sub": "subject", "name": "Student",
            }):
                with pytest.raises(Exception) as exc:
                    verify_google_credential("wrong-audience-token")
            assert getattr(exc.value, "status_code", None) == 401
        finally:
            settings.GOOGLE_CLIENT_ID = previous

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_password_protected_class_rejects_bad_password_before_waiting(self, mock_create):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Protected Class", "meeting_passcode": "right-pass",
            })
            room_code = created.json()["room_code"]
            session_id = "w" * 36
            rejected = await c.post("/api/student/join-requests", json={
                "google_credential": "mock-google-id-token", "room_code": room_code,
                "meeting_passcode": "wrong-pass", "session_id": session_id,
            })
        assert rejected.status_code == 403
        assert session_state.get_join_request_for_session(room_code, session_id) is None

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    @patch("app.main.livekit_service.get_participants", new_callable=AsyncMock)
    async def test_approval_is_required_before_student_token(self, mock_parts, mock_create):
        mock_create.return_value = True
        mock_parts.return_value = []
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Math", "meeting_passcode": "join123"
            })
            room_code = created.json()["room_code"]
            invite_code = created.json()["invite_code"]
            session_id = "a" * 36
            pending = await c.post("/api/student/join-requests", json={
                "google_credential": "mock-google-id-token", "student_name": "untrusted browser name", "invite_code": invite_code,
                "meeting_passcode": "join123", "session_id": session_id,
            })
            assert pending.status_code == 200
            assert pending.json()["student_name"] == "Alice Google"
            assert "google_credential" not in pending.json()
            assert "student_identity" not in pending.json()
            request_id = pending.json()["request_id"]
            assert session_state.get_join_request(request_id)["student_identity"] == "google_test-google-sub"
            denied = await c.post(f"/api/student/join-requests/{request_id}/token", json={"session_id": session_id})
            assert denied.status_code == 403
            teacher_identity = session_state.get_class(room_code)["teacher_identity"]
            teacher_access_key = created.json()["teacher_access_key"]
            approved = await c.post("/api/teacher/approve-join-request", json={
                "room_code": room_code, "teacher_identity": teacher_identity,
                "teacher_access_key": teacher_access_key, "request_id": request_id,
            })
            assert approved.status_code == 200
            token = await c.post(f"/api/student/join-requests/{request_id}/token", json={"session_id": session_id})
            assert token.status_code == 200
            assert "token" in token.json()


class TestClassroomChat:
    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_teacher_chat_can_toggle_off_and_on_and_server_rejects_student_sends(self, mock_create):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Chat Test",
            })
            room_code = created.json()["room_code"]
            teacher = {
                "teacher_identity": session_state.get_class(room_code)["teacher_identity"],
                "teacher_access_key": created.json()["teacher_access_key"],
            }
            session_id = "chat-student-session-0001"
            pending = await c.post("/api/student/join-requests", json={
                "google_credential": "mock-google-id-token", "room_code": room_code,
                "session_id": session_id,
            })
            request_id = pending.json()["request_id"]
            await c.post("/api/teacher/approve-join-request", json={
                "room_code": room_code, **teacher, "request_id": request_id,
            })
            student = {"join_request_id": request_id, "session_id": session_id}

            first = await c.post(f"/api/class/{room_code}/chat", json={
                **student, "action": "send_message", "text": "Before pause",
            })
            assert first.status_code == 200
            assert len(first.json()["messages"]) == 1

            off = await c.post(f"/api/class/{room_code}/chat", json={
                **teacher, "action": "set_enabled", "enabled": False,
            })
            assert off.status_code == 200 and off.json()["enabled"] is False
            blocked_send = await c.post(f"/api/class/{room_code}/chat", json={
                **student, "action": "send_message", "text": "Draft remains local",
            })
            assert blocked_send.status_code == 403
            snapshot = await c.post(f"/api/class/{room_code}/chat", json={
                **student, "action": "snapshot",
            })
            assert snapshot.json()["enabled"] is False
            assert [item["text"] for item in snapshot.json()["messages"]] == ["Before pause"]

            on = await c.post(f"/api/class/{room_code}/chat", json={
                **teacher, "action": "set_enabled", "enabled": True,
            })
            assert on.status_code == 200 and on.json()["enabled"] is True
            resumed = await c.post(f"/api/class/{room_code}/chat", json={
                **student, "action": "send_message", "text": "After resume",
            })
            assert resumed.status_code == 200
            assert [item["text"] for item in resumed.json()["messages"]] == ["Before pause", "After resume"]

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_student_cannot_change_teacher_chat_state(self, mock_create):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Chat Authorization",
            })
            room_code = created.json()["room_code"]
            session_id = "chat-student-session-0002"
            pending = await c.post("/api/student/join-requests", json={
                "google_credential": "mock-google-id-token", "room_code": room_code,
                "session_id": session_id,
            })
            request_id = pending.json()["request_id"]
            await c.post("/api/teacher/approve-join-request", json={
                "room_code": room_code,
                "teacher_identity": session_state.get_class(room_code)["teacher_identity"],
                "teacher_access_key": created.json()["teacher_access_key"],
                "request_id": request_id,
            })
            response = await c.post(f"/api/class/{room_code}/chat", json={
                "join_request_id": request_id, "session_id": session_id,
                "action": "set_enabled", "enabled": False,
            })
            assert response.status_code == 403
            assert session_state.get_chat(room_code)["enabled"] is True


class TestTimedMicrophoneTokenPermissions:
    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_access_token", return_value="student-token")
    @patch("app.main.livekit_service.get_participants", new_callable=AsyncMock, return_value=[])
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_timed_mute_keeps_mic_capability_for_expiry_restoration(self, mock_room, _participants, create_token):
        mock_room.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Timed Mic",
            })
            room_code = created.json()["room_code"]
            session_id = "timed-mic-student-session-01"
            pending = await c.post("/api/student/join-requests", json={
                "google_credential": "mock-google-id-token", "room_code": room_code,
                "session_id": session_id,
            })
            request_id = pending.json()["request_id"]
            await c.post("/api/teacher/approve-join-request", json={
                "room_code": room_code,
                "teacher_identity": session_state.get_class(room_code)["teacher_identity"],
                "teacher_access_key": created.json()["teacher_access_key"],
                "request_id": request_id,
            })
            identity = session_state.get_join_request(request_id)["student_identity"]
            session_state.set_microphone_restriction(room_code, identity, 1)
            token = await c.post(f"/api/student/join-requests/{request_id}/token", json={"session_id": session_id})
        assert token.status_code == 200
        assert create_token.call_args.kwargs["is_muted"] is True
        assert "microphone" in create_token.call_args.kwargs["can_publish_sources"]

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_rejected_request_cannot_be_recreated_or_tokenized(self, mock_create):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Math", "meeting_passcode": "join123"
            })
            room_code = created.json()["room_code"]
            session_id = "b" * 36
            pending = await c.post("/api/student/join-requests", json={
                "google_credential": "mock-google-id-token", "room_code": room_code,
                "meeting_passcode": "join123", "session_id": session_id,
            })
            request_id = pending.json()["request_id"]
            teacher_identity = session_state.get_class(room_code)["teacher_identity"]
            teacher_access_key = created.json()["teacher_access_key"]
            rejected = await c.post("/api/teacher/reject-join-request", json={
                "room_code": room_code, "teacher_identity": teacher_identity,
                "teacher_access_key": teacher_access_key, "request_id": request_id,
            })
            assert rejected.status_code == 200
            repeated = await c.post("/api/student/join-requests", json={
                "google_credential": "mock-google-id-token", "room_code": room_code,
                "meeting_passcode": "join123", "session_id": session_id,
            })
            assert repeated.status_code == 403
            token = await c.post(f"/api/student/join-requests/{request_id}/token", json={"session_id": session_id})
            assert token.status_code == 403

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    @patch("app.main.livekit_service.get_participants", new_callable=AsyncMock, return_value=[])
    async def test_passwordless_class_still_requires_teacher_approval(self, _participants, mock_create):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={
                "teacher_name": "John", "room_name": "Passwordless Class",
            })
            room_code = created.json()["room_code"]
            session_id = "p" * 36
            pending = await c.post("/api/student/join-requests", json={
                "google_credential": "mock-google-id-token", "room_code": room_code,
                "session_id": session_id,
            })
            assert pending.status_code == 200
            request_id = pending.json()["request_id"]
            denied = await c.post(f"/api/student/join-requests/{request_id}/token", json={"session_id": session_id})
            assert denied.status_code == 403
            approved = await c.post("/api/teacher/approve-join-request", json={
                "room_code": room_code,
                "teacher_identity": session_state.get_class(room_code)["teacher_identity"],
                "teacher_access_key": created.json()["teacher_access_key"],
                "request_id": request_id,
            })
            assert approved.status_code == 200
            token = await c.post(f"/api/student/join-requests/{request_id}/token", json={"session_id": session_id})
            assert token.status_code == 200


class TestMicrophoneRestrictions:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("duration", [1, 5, 10, 15, 30])
    @patch("app.main._schedule_microphone_restriction_expiry")
    @patch("app.main.livekit_service.set_microphone_publish_permission", new_callable=AsyncMock)
    @patch("app.main.livekit_service.mute_participant", new_callable=AsyncMock)
    @patch("app.main.livekit_service.get_participants", new_callable=AsyncMock)
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_teacher_can_create_timed_microphone_restriction(self, mock_create, mock_participants, mock_mute, mock_permission, mock_schedule, duration):
        mock_create.return_value = True
        mock_participants.return_value = [{"identity": "student_1", "name": "Alice"}]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={"teacher_name": "John", "room_name": "Math", "meeting_passcode": "abc123"})
            room_code = created.json()["room_code"]
            teacher_identity = session_state.get_class(room_code)["teacher_identity"]
            response = await c.post("/api/teacher/restrict-student-microphone", json={"room_code": room_code, "teacher_identity": teacher_identity, "target_identity": "student_1", "duration_minutes": duration})
        assert response.status_code == 200
        assert response.json()["mode"] == "TIMED"
        payload = response.json()
        expires_at = datetime.fromisoformat(payload["expires_at"])
        assert expires_at.tzinfo is not None
        assert duration * 60 - 1 <= payload["remaining_seconds"] <= duration * 60
        assert expires_at > datetime.now(timezone.utc)
        mock_schedule.assert_called_once()
        mock_mute.assert_awaited_once()
        mock_permission.assert_awaited_once_with(session_state.get_class(room_code)["livekit_room_name"], "student_1", False)

    @pytest.mark.asyncio
    @patch("app.main._schedule_microphone_restriction_expiry")
    @patch("app.main.livekit_service.set_microphone_publish_permission", new_callable=AsyncMock)
    @patch("app.main.livekit_service.mute_participant", new_callable=AsyncMock)
    @patch("app.main.livekit_service.get_participants", new_callable=AsyncMock)
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_until_teacher_unmutes_restriction_and_manual_release(self, mock_create, mock_participants, mock_mute, mock_permission, mock_schedule):
        mock_create.return_value = True
        mock_participants.return_value = [{"identity": "student_1", "name": "Alice"}]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={"teacher_name": "John", "room_name": "Math", "meeting_passcode": "abc123"})
            room_code = created.json()["room_code"]
            teacher_identity = session_state.get_class(room_code)["teacher_identity"]
            restricted = await c.post("/api/teacher/restrict-student-microphone", json={"room_code": room_code, "teacher_identity": teacher_identity, "target_identity": "student_1"})
            assert restricted.status_code == 200
            assert restricted.json()["mode"] == "UNTIL_TEACHER"
            request_id, session_id = register_approved_identity(room_code, "student_1")
            unauthorized = await c.get(f"/api/class/{room_code}/microphone-restriction/student_1", params={"request_id": request_id, "session_id": "wrong-session-id-000000"})
            assert unauthorized.status_code == 403
            status = await c.get(f"/api/class/{room_code}/microphone-restriction/student_1", params={"request_id": request_id, "session_id": session_id})
            assert status.json()["restricted"] is True
            released = await c.post("/api/teacher/unrestrict-student-microphone", json={"room_code": room_code, "teacher_identity": teacher_identity, "target_identity": "student_1"})
        assert released.status_code == 200
        assert session_state.get_microphone_restriction(room_code, "student_1") is None
        assert mock_permission.await_count == 3
        room_name = session_state.get_class(room_code)["livekit_room_name"]
        assert mock_permission.await_args_list == [
            ((room_name, "student_1", False),),
            ((room_name, "student_1", False),),
            ((room_name, "student_1", True),),
        ]

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.set_microphone_publish_permission", new_callable=AsyncMock)
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_expired_restriction_is_inactive_and_restores_permission(self, mock_create, mock_permission):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={"teacher_name": "John", "room_name": "Math", "meeting_passcode": "abc123"})
            room_code = created.json()["room_code"]
            request_id, session_id = register_approved_identity(room_code, "student_1")
            restriction = session_state.set_microphone_restriction(room_code, "student_1", 1)
            restriction["expires_at"] = restriction["expires_at"] - timedelta(minutes=2)
            status = await c.get(f"/api/class/{room_code}/microphone-restriction/student_1", params={"request_id": request_id, "session_id": session_id})
        assert status.status_code == 200
        assert status.json()["restricted"] is False
        mock_permission.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.set_microphone_publish_permission", new_callable=AsyncMock)
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_expiry_task_restores_permission_without_client_polling(self, mock_create, mock_permission):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={"teacher_name": "John", "room_name": "Math", "meeting_passcode": "abc123"})
        room_code = created.json()["room_code"]
        restriction = session_state.set_microphone_restriction(room_code, "student_1", 1)
        restriction["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        await __import__("app.main", fromlist=["_restore_microphone_after_expiry"])._restore_microphone_after_expiry(
            room_code, "student_1", restriction["expires_at"])
        assert session_state.get_microphone_restriction(room_code, "student_1") is None
        mock_permission.assert_awaited_once_with(
            session_state.get_class(room_code)["livekit_room_name"], "student_1", True)

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.set_microphone_publish_permission", new_callable=AsyncMock)
    @patch("app.main.livekit_service.get_participants", new_callable=AsyncMock)
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_teacher_poll_cannot_consume_expiry_before_callback_restores_livekit(self, mock_create, mock_participants, mock_permission):
        mock_create.return_value = True
        mock_participants.return_value = []
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={"teacher_name": "John", "room_name": "Math", "meeting_passcode": "abc123"})
            room_code = created.json()["room_code"]
            restriction = session_state.set_microphone_restriction(room_code, "student_1", 1)
            restriction["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
            await c.get(f"/api/class/{room_code}/participants")
        assert session_state.get_microphone_restriction(room_code, "student_1") is not None
        await __import__("app.main", fromlist=["_restore_microphone_after_expiry"])._restore_microphone_after_expiry(
            room_code, "student_1", restriction["expires_at"])
        assert session_state.get_microphone_restriction(room_code, "student_1") is None
        mock_permission.assert_awaited_once_with(
            session_state.get_class(room_code)["livekit_room_name"], "student_1", True)

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.set_microphone_publish_permission", new_callable=AsyncMock)
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_old_expiry_callback_cannot_release_newer_restriction(self, mock_create, mock_permission):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={"teacher_name": "John", "room_name": "Math", "meeting_passcode": "abc123"})
        room_code = created.json()["room_code"]
        old = session_state.set_microphone_restriction(room_code, "student_1", 1)
        newer = session_state.set_microphone_restriction(room_code, "student_1", 1)
        old["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        await __import__("app.main", fromlist=["_restore_microphone_after_expiry"])._restore_microphone_after_expiry(
            room_code, "student_1", old["expires_at"])
        # A callback only releases the exact expires_at value it scheduled.
        assert session_state.get_microphone_restriction(room_code, "student_1") == newer
        assert newer != old
        mock_permission.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_unauthorized_user_cannot_change_restriction(self, mock_create):
        mock_create.return_value = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={"teacher_name": "John", "room_name": "Math", "meeting_passcode": "abc123"})
            room_code = created.json()["room_code"]
            response = await c.post("/api/teacher/restrict-student-microphone", json={"room_code": room_code, "teacher_identity": "student_1", "target_identity": "student_2", "duration_minutes": 1})
        assert response.status_code == 403

    @staticmethod
    async def _approved_student(client, room_code, teacher_identity, teacher_access_key, session_id="z" * 36):
        pending = await client.post("/api/student/join-requests", json={
            "google_credential": "mock-google-id-token", "room_code": room_code,
            "meeting_passcode": "abc123", "session_id": session_id,
        })
        request_id = pending.json()["request_id"]
        approved = await client.post("/api/teacher/approve-join-request", json={
            "room_code": room_code, "teacher_identity": teacher_identity,
            "teacher_access_key": teacher_access_key, "request_id": request_id,
        })
        assert approved.status_code == 200
        return request_id, session_id

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_access_token", return_value="student-token")
    @patch("app.main.livekit_service.get_participants", new_callable=AsyncMock)
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_rejoin_before_expiry_token_excludes_only_microphone(self, mock_create, mock_participants, mock_token):
        mock_create.return_value = True
        mock_participants.return_value = []
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={"teacher_name": "John", "room_name": "Math", "meeting_passcode": "abc123"})
            room_code = created.json()["room_code"]
            teacher_identity = session_state.get_class(room_code)["teacher_identity"]
            request_id, session_id = await self._approved_student(c, room_code, teacher_identity, created.json()["teacher_access_key"])
            student_identity = session_state.get_join_request(request_id)["student_identity"]
            session_state.set_microphone_restriction(room_code, student_identity, 1)
            token = await c.post(f"/api/student/join-requests/{request_id}/token", json={"session_id": session_id})
        assert token.status_code == 200
        assert mock_token.call_args.kwargs["can_publish_sources"] == ["screen_share", "screen_share_audio", "microphone", "camera"]
        assert mock_token.call_args.kwargs["is_muted"] is True

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_access_token", return_value="student-token")
    @patch("app.main.livekit_service.set_microphone_publish_permission", new_callable=AsyncMock)
    @patch("app.main.livekit_service.get_participants", new_callable=AsyncMock)
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_rejoin_after_expiry_restores_microphone_permission_but_starts_muted(self, mock_create, mock_participants, mock_permission, mock_token):
        mock_create.return_value = True
        mock_participants.return_value = []
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={"teacher_name": "John", "room_name": "Math", "meeting_passcode": "abc123"})
            room_code = created.json()["room_code"]
            teacher_identity = session_state.get_class(room_code)["teacher_identity"]
            request_id, session_id = await self._approved_student(c, room_code, teacher_identity, created.json()["teacher_access_key"])
            student_identity = session_state.get_join_request(request_id)["student_identity"]
            restriction = session_state.set_microphone_restriction(room_code, student_identity, 1)
            restriction["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
            token = await c.post(f"/api/student/join-requests/{request_id}/token", json={"session_id": session_id})
        assert token.status_code == 200
        assert "microphone" in mock_token.call_args.kwargs["can_publish_sources"]
        assert mock_permission.await_args == ((session_state.get_class(room_code)["livekit_room_name"], student_identity, True),)

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_access_token", return_value="student-token")
    @patch("app.main.livekit_service.get_participants", new_callable=AsyncMock)
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_until_teacher_restriction_survives_rejoin(self, mock_create, mock_participants, mock_token):
        mock_create.return_value = True
        mock_participants.return_value = []
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={"teacher_name": "John", "room_name": "Math", "meeting_passcode": "abc123"})
            room_code = created.json()["room_code"]
            teacher_identity = session_state.get_class(room_code)["teacher_identity"]
            request_id, session_id = await self._approved_student(c, room_code, teacher_identity, created.json()["teacher_access_key"])
            student_identity = session_state.get_join_request(request_id)["student_identity"]
            session_state.set_microphone_restriction(room_code, student_identity, None)
            token = await c.post(f"/api/student/join-requests/{request_id}/token", json={"session_id": session_id})
        assert token.status_code == 200
        assert "microphone" in mock_token.call_args.kwargs["can_publish_sources"]
        assert mock_token.call_args.kwargs["is_muted"] is True

    @pytest.mark.asyncio
    @patch("app.main.livekit_service.create_access_token", return_value="student-token")
    @patch("app.main.livekit_service.get_participants", new_callable=AsyncMock)
    @patch("app.main.livekit_service.create_room", new_callable=AsyncMock)
    async def test_normal_student_token_keeps_microphone_camera_and_screen_share(self, mock_create, mock_participants, mock_token):
        mock_create.return_value = True
        mock_participants.return_value = []
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            created = await c.post("/api/teacher/create-room", json={"teacher_name": "John", "room_name": "Math", "meeting_passcode": "abc123"})
            room_code = created.json()["room_code"]
            teacher_identity = session_state.get_class(room_code)["teacher_identity"]
            request_id, session_id = await self._approved_student(c, room_code, teacher_identity, created.json()["teacher_access_key"])
            token = await c.post(f"/api/student/join-requests/{request_id}/token", json={"session_id": session_id})
        assert token.status_code == 200
        assert mock_token.call_args.kwargs["can_publish_sources"] == ["screen_share", "screen_share_audio", "microphone", "camera"]


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

    @pytest.mark.asyncio
    async def test_livekit_microphone_restore_preserves_existing_sources_and_verifies_response(self):
        from livekit.protocol.models import ParticipantPermission, TrackSource

        existing = MagicMock(permission=ParticipantPermission(
            can_subscribe=True, can_publish=True, can_publish_data=True,
            can_publish_sources=[TrackSource.CAMERA, TrackSource.SCREEN_SHARE],
        ))
        updated = MagicMock(permission=ParticipantPermission(
            can_subscribe=True, can_publish=True, can_publish_data=True,
            can_publish_sources=[TrackSource.CAMERA, TrackSource.SCREEN_SHARE, TrackSource.MICROPHONE],
        ))
        room_service = MagicMock()
        room_service.get_participant = AsyncMock(return_value=existing)
        room_service.update_participant = AsyncMock(return_value=updated)
        with patch.object(livekit_service, "get_room_service", new_callable=AsyncMock, return_value=room_service):
            await livekit_service.set_microphone_publish_permission("room_1", "student_1", True)

        update = room_service.update_participant.await_args.args[0]
        assert TrackSource.MICROPHONE in update.permission.can_publish_sources
        assert TrackSource.CAMERA in update.permission.can_publish_sources
        assert TrackSource.SCREEN_SHARE in update.permission.can_publish_sources


# ---------------------------------------------------------------------------
# Passcode endpoints
# ---------------------------------------------------------------------------

class TestPasscodeEndpoints:
    @pytest.mark.asyncio
    async def test_teacher_passcode_preflight_allows_local_development_origin(self):
        headers = {
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.options("/api/auth/verify-teacher-passcode", headers=headers)
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
        assert "POST" in r.headers["access-control-allow-methods"]

    @pytest.mark.asyncio
    async def test_teacher_passcode_post_reaches_verification_after_local_preflight(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/api/auth/verify-teacher-passcode",
                headers={"Origin": "http://localhost:3000"},
                json={"passcode": ""},
            )
        assert r.status_code == 400
        assert r.headers["access-control-allow-origin"] == "http://localhost:3000"

    @pytest.mark.asyncio
    async def test_teacher_passcode_preflight_rejects_untrusted_origin(self):
        headers = {
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "POST",
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.options("/api/auth/verify-teacher-passcode", headers=headers)
        assert r.status_code == 400

    def test_production_cors_does_not_enable_localhost_regex(self):
        previous = settings.ENVIRONMENT
        settings.ENVIRONMENT = "production"
        try:
            assert settings.local_origin_regex is None
        finally:
            settings.ENVIRONMENT = previous

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
