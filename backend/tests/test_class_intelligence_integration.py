import secrets
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from app import main
from app.livekit_service import session_state
from app.models import CreateRoomRequest, EndClassRequest, JoinRequestDecision


@pytest.mark.asyncio
async def test_room_creation_returns_without_waiting_for_database(monkeypatch):
    monkeypatch.setattr(main.livekit_service, "create_room", AsyncMock(return_value=True))
    monkeypatch.setattr(main.livekit_service, "create_access_token", lambda **_kwargs: "teacher-token")
    background = BackgroundTasks()

    response = await main.create_room(
        CreateRoomRequest(teacher_name="Teacher", room_name="Math"), background,
    )

    assert response["room_code"]
    assert len(background.tasks) == 1
    assert background.tasks[0].func is main.class_intelligence.persist_new_class


@pytest.mark.asyncio
async def test_approved_live_join_schedules_durable_enrollment_after_decision():
    room_code = f"T{secrets.token_hex(4).upper()}"
    teacher_identity = f"teacher_{secrets.token_hex(4)}"
    access_key = secrets.token_urlsafe(24)
    session_state.create_class(
        room_code=room_code, livekit_room_name=f"class_{room_code}", class_name="Math",
        teacher_name="Teacher", teacher_identity=teacher_identity,
        meeting_passcode_hash=None, max_participants=50,
        student_microphone_policy="allowed", student_camera_policy="allowed",
        invite_code=f"invite_{room_code}", teacher_access_key=access_key,
    )
    join_request = session_state.create_join_request(
        room_code, "Student", f"session-{room_code}-long-enough", "google_subject",
    )
    background = BackgroundTasks()

    response = await main._decide_join_request(JoinRequestDecision(
        room_code=room_code, teacher_identity=teacher_identity,
        teacher_access_key=access_key, request_id=join_request["request_id"],
    ), "APPROVED", background)

    assert response["status"] == "APPROVED"
    assert len(background.tasks) == 1
    assert background.tasks[0].func is main.class_intelligence.persist_approved_student


@pytest.mark.asyncio
async def test_meeting_end_schedules_persistence_after_livekit_teardown(monkeypatch):
    room_code = f"E{secrets.token_hex(4).upper()}"
    identity = f"teacher_{secrets.token_hex(4)}"
    class_info = {"livekit_room_name": f"class_{room_code}", "teacher_access_key": "key"}
    delete_room = AsyncMock()
    monkeypatch.setattr(main, "_verify_teacher", lambda *_args: class_info)
    monkeypatch.setattr(main.session_state, "is_recording", lambda _room: False)
    monkeypatch.setattr(main.session_state, "end_class", lambda _room: None)
    monkeypatch.setattr(main.livekit_service, "delete_room", delete_room)
    background = BackgroundTasks()

    result = await main.end_class(EndClassRequest(
        room_code=room_code, teacher_identity=identity, teacher_access_key="key",
    ), background)

    assert result["success"] is True
    delete_room.assert_awaited_once_with(class_info["livekit_room_name"])
    assert len(background.tasks) == 1
    assert background.tasks[0].func is main.class_intelligence.mark_meeting_ended


@pytest.mark.asyncio
async def test_recording_stop_failure_does_not_block_class_teardown(monkeypatch):
    room_code = f"F{secrets.token_hex(4).upper()}"
    identity = f"teacher_{secrets.token_hex(4)}"
    class_info = {
        "livekit_room_name": f"class_{room_code}", "teacher_access_key": "key",
        "active_recording_id": "rec-pending",
    }
    stop_recording = AsyncMock(return_value={"success": False, "error": "timeout"})
    delete_room = AsyncMock()
    monkeypatch.setattr(main, "_verify_teacher", lambda *_args: class_info)
    monkeypatch.setattr(main.recording_state, "class_recordings", {room_code: ["rec-pending"]})
    monkeypatch.setattr(main.session_state, "is_recording", lambda _room: True)
    monkeypatch.setattr(main.session_state, "end_class", lambda _room: None)
    monkeypatch.setattr(main.livekit_service, "stop_recording", stop_recording)
    monkeypatch.setattr(main.livekit_service, "delete_room", delete_room)
    background = BackgroundTasks()

    result = await main.end_class(EndClassRequest(
        room_code=room_code, teacher_identity=identity, teacher_access_key="key",
    ), background)

    assert result["success"] is True
    stop_recording.assert_awaited_once_with(room_code, "rec-pending")
    delete_room.assert_awaited_once_with(class_info["livekit_room_name"])


@pytest.mark.asyncio
async def test_class_end_requires_the_per_class_teacher_key(monkeypatch):
    delete_room = AsyncMock()
    monkeypatch.setattr(main, "_verify_teacher", lambda *_args: {
        "livekit_room_name": "class_ROOM", "teacher_access_key": "real-key",
    })
    monkeypatch.setattr(main.livekit_service, "delete_room", delete_room)
    background = BackgroundTasks()

    with pytest.raises(HTTPException) as error:
        await main.end_class(EndClassRequest(
            room_code="ROOM", teacher_identity="teacher", teacher_access_key="wrong-key",
        ), background)

    assert error.value.status_code == 403
    delete_room.assert_not_awaited()
    assert not background.tasks
