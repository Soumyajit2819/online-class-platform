import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import class_intelligence


class FakeQuery:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.upserted = None

    def select(self, *_args): return self
    def eq(self, *_args): return self
    def limit(self, *_args): return self
    def upsert(self, data, **_kwargs):
        self.upserted = data
        return self
    def execute(self): return SimpleNamespace(data=self.rows)


@pytest.mark.asyncio
async def test_class_and_meeting_are_created_in_one_atomic_rpc(monkeypatch):
    calls = []

    class Client:
        def rpc(self, name, params):
            calls.append((name, params))
            return SimpleNamespace(execute=lambda: None)

    monkeypatch.setattr(class_intelligence.settings, "validate_supabase_db", lambda: True)
    monkeypatch.setattr(class_intelligence, "_client", lambda: Client())

    await class_intelligence.persist_new_class(
        room_code="ROOM1", class_name="Math", teacher_identity="teacher_1",
        teacher_access_key="secret", room_created_at="2026-09-23T10:00:00+00:00",
    )

    assert len(calls) == 1
    assert calls[0][0] == "create_class_with_meeting"
    assert calls[0][1]["p_teacher_key_hash"] != "secret"
    assert calls[0][1]["p_room_created_at"] == "2026-09-23T10:00:00+00:00"


@pytest.mark.asyncio
async def test_enrollment_requires_matching_persisted_teacher_owner(monkeypatch):
    owner_query = FakeQuery([{
        "id": "class-id", "teacher_identity": "teacher_1",
        "teacher_key_hash": "wrong-hash",
    }])
    monkeypatch.setattr(class_intelligence, "_client", lambda: SimpleNamespace(table=lambda _name: owner_query))

    with pytest.raises(PermissionError):
        await class_intelligence._upsert_approved_student(
            room_code="ROOM1", teacher_identity="teacher_1", teacher_access_key="secret",
            student_identity="google_sub", student_name="Student",
        )
    assert owner_query.upserted is None


@pytest.mark.asyncio
async def test_enrollment_is_linked_to_its_owned_class(monkeypatch):
    owner_query = FakeQuery([{
        "id": "class-id", "teacher_identity": "teacher_1",
        "teacher_key_hash": class_intelligence._teacher_key_hash("secret"),
    }])
    monkeypatch.setattr(class_intelligence, "_client", lambda: SimpleNamespace(table=lambda _name: owner_query))

    await class_intelligence._upsert_approved_student(
        room_code="ROOM1", teacher_identity="teacher_1", teacher_access_key="secret",
        student_identity="google_sub", student_name="Student",
    )

    assert owner_query.upserted == {
        "class_id": "class-id", "student_identity": "google_sub",
        "student_name": "Student", "status": "authorized",
    }


@pytest.mark.asyncio
async def test_enrollment_retries_are_bounded_and_live_approval_is_not_reversed(monkeypatch):
    queue = asyncio.Queue(maxsize=2)
    monkeypatch.setattr(class_intelligence, "_enrollment_retries", queue)
    monkeypatch.setattr(class_intelligence.settings, "validate_supabase_db", lambda: True)
    monkeypatch.setattr(class_intelligence, "_upsert_approved_student", AsyncMock(side_effect=RuntimeError("offline")))

    persisted = await class_intelligence.persist_approved_student(
        room_code="ROOM1", teacher_identity="teacher_1", teacher_access_key="secret",
        student_identity="google_sub", student_name="Student", request_id="request-1",
    )

    assert persisted is False
    assert queue.qsize() == 1
    assert (await queue.get())["request_id"] == "request-1"


@pytest.mark.asyncio
async def test_meeting_end_rpc_checks_same_teacher_owner(monkeypatch):
    calls = []

    class Client:
        def rpc(self, name, params):
            calls.append((name, params))
            return SimpleNamespace(execute=lambda: None)

    monkeypatch.setattr(class_intelligence.settings, "validate_supabase_db", lambda: True)
    monkeypatch.setattr(class_intelligence, "_client", lambda: Client())

    await class_intelligence.mark_meeting_ended(
        room_code="ROOM1", teacher_identity="teacher_1",
        teacher_access_key="secret", recording_id="rec-1",
    )

    assert calls[0][0] == "end_class_intelligence_meeting"
    assert calls[0][1]["p_teacher_identity"] == "teacher_1"
    assert calls[0][1]["p_teacher_key_hash"] == class_intelligence._teacher_key_hash("secret")


@pytest.mark.asyncio
async def test_recording_association_uses_dedicated_meeting_relationship(monkeypatch):
    calls = []

    class Client:
        def rpc(self, name, params):
            calls.append((name, params))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data="meeting-id"))

    monkeypatch.setattr(class_intelligence.settings, "validate_supabase_db", lambda: True)
    monkeypatch.setattr(class_intelligence, "_client", lambda: Client())

    meeting_id = await class_intelligence.associate_recording_with_meeting(
        room_code="ROOM1", recording_id="rec-1",
        recording_started_at="2026-09-23T10:00:00+00:00",
    )

    assert meeting_id == "meeting-id"
    assert calls == [("associate_recording_with_meeting", {
        "p_room_code": "ROOM1", "p_recording_id": "rec-1",
        "p_recording_started_at": "2026-09-23T10:00:00+00:00",
    })]


def test_migration_has_atomic_create_owner_checks_fk_and_closed_browser_access():
    migration = (Path(__file__).parents[1] / "migrations" / "003_create_class_intelligence.sql").read_text()
    assert "public.create_class_with_meeting" in migration
    assert "FOREIGN KEY (class_id, room_code)" in migration
    assert "public.end_class_intelligence_meeting" in migration
    assert "SET status = 'ended'" in migration
    assert "room_created_at" in migration
    assert "FROM PUBLIC, anon, authenticated" in migration
    assert "CREATE POLICY" not in migration
    assert "public.recordings" in migration
