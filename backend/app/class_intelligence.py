"""Optional persistence and provider boundary for class intelligence.

Database calls run outside the LiveKit media path. Durable work queues and AI
providers are deliberately not introduced in this phase.
"""

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import settings

logger = logging.getLogger(__name__)
_ENROLLMENT_RETRY_LIMIT = 1000
_enrollment_retries: asyncio.Queue[dict[str, str]] = asyncio.Queue(maxsize=_ENROLLMENT_RETRY_LIMIT)


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    language: str | None = None
    provider: str | None = None
    model: str | None = None
    segments: list[dict[str, Any]] | None = None


class SpeechToTextProvider(Protocol):
    def validate_configuration(self) -> bool: ...

    async def transcribe(self, audio_path: Path,
                         metadata: dict[str, Any]) -> TranscriptResult: ...


class ClassNotesProvider(Protocol):
    def validate_configuration(self) -> bool: ...

    async def generate_notes(self, transcript_text: str,
                             segments: list[dict[str, Any]] | None = None) -> "MeetingNotesResult": ...


@dataclass(frozen=True)
class MeetingNotesResult:
    english_notes: str
    bengali_notes: str
    provider: str | None = None
    model: str | None = None


class TranscriptAccessDenied(PermissionError):
    pass


def _client():
    from supabase import create_client
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


async def _run(operation):
    return await asyncio.to_thread(operation)


def _teacher_key_hash(teacher_access_key: str) -> str:
    return hashlib.sha256(teacher_access_key.encode("utf-8")).hexdigest()


async def persist_new_class(*, room_code: str, class_name: str,
                            teacher_identity: str, teacher_access_key: str,
                            room_created_at: str) -> None:
    """Atomically persist a new class and its first room/meeting record."""
    if not settings.validate_supabase_db():
        logger.warning("Class persistence skipped: Supabase DB is not configured (room=%s)", room_code)
        return
    params = {
        "p_room_code": room_code,
        "p_class_name": class_name,
        "p_teacher_identity": teacher_identity,
        "p_teacher_key_hash": _teacher_key_hash(teacher_access_key),
        "p_room_created_at": room_created_at,
    }
    for attempt in range(3):
        try:
            await _run(lambda: _client().rpc("create_class_with_meeting", params).execute())
            return
        except Exception:
            logger.exception("Atomic class/meeting persistence attempt %s/3 failed (room=%s)", attempt + 1, room_code)
            if attempt < 2:
                await asyncio.sleep(attempt + 1)


async def _upsert_approved_student(*, room_code: str, teacher_identity: str,
                                   teacher_access_key: str, student_identity: str,
                                   student_name: str) -> None:
    def write():
        sb = _client()
        result = (sb.table("classes").select("id,teacher_identity,teacher_key_hash")
                  .eq("room_code", room_code).limit(1).execute().data)
        if not result:
            raise LookupError("Persisted class not found yet")
        owner = result[0]
        if (owner["teacher_identity"] != teacher_identity or not hmac.compare_digest(
                owner["teacher_key_hash"], _teacher_key_hash(teacher_access_key))):
            raise PermissionError("Teacher does not own the persisted class")
        sb.table("class_enrollments").upsert({
            "class_id": owner["id"], "student_identity": student_identity,
            "student_name": student_name, "status": "authorized",
        }, on_conflict="class_id,student_identity").execute()

    await _run(write)


async def persist_approved_student(*, room_code: str, teacher_identity: str,
                                   teacher_access_key: str, student_identity: str,
                                   student_name: str, request_id: str) -> bool:
    """Best-effort durable enrollment; never reverses an approved LiveKit join."""
    if not settings.validate_supabase_db():
        logger.error("Enrollment not persisted: Supabase DB is not configured (request=%s)", request_id)
        return False
    try:
        await _upsert_approved_student(
            room_code=room_code, teacher_identity=teacher_identity,
            teacher_access_key=teacher_access_key, student_identity=student_identity,
            student_name=student_name,
        )
        return True
    except PermissionError:
        logger.exception("Enrollment ownership check failed (room=%s request=%s)", room_code, request_id)
        return False
    except Exception:
        logger.exception("Enrollment persistence failed; scheduling bounded retry (room=%s request=%s)", room_code, request_id)
        item = {
            "room_code": room_code, "teacher_identity": teacher_identity,
            "teacher_access_key": teacher_access_key, "student_identity": student_identity,
            "student_name": student_name, "request_id": request_id,
        }
        try:
            _enrollment_retries.put_nowait(item)
        except asyncio.QueueFull:
            logger.critical("Enrollment retry queue is full; manual reconciliation required (room=%s request=%s)", room_code, request_id)
        return False


async def enrollment_retry_worker() -> None:
    """Bounded, process-local retry worker for approval/enrollment races/outages."""
    while True:
        item = await _enrollment_retries.get()
        try:
            for attempt in range(5):
                try:
                    await _upsert_approved_student(**{k: v for k, v in item.items() if k != "request_id"})
                    break
                except PermissionError:
                    logger.exception("Enrollment ownership check failed during retry (request=%s)", item["request_id"])
                    break
                except Exception:
                    logger.exception("Enrollment retry %s/5 failed (request=%s)", attempt + 1, item["request_id"])
                    if attempt < 4:
                        await asyncio.sleep(min(2 ** attempt, 8))
            else:
                logger.error("Enrollment retries exhausted; manual reconciliation required (request=%s)", item["request_id"])
        finally:
            _enrollment_retries.task_done()


async def mark_meeting_ended(*, room_code: str, teacher_identity: str,
                             teacher_access_key: str, recording_id: str | None) -> None:
    """Persist ended state via an ownership-checking, idempotent database function."""
    if not settings.validate_supabase_db():
        logger.error("Meeting end state not persisted: Supabase DB is not configured (room=%s)", room_code)
        return
    params = {
        "p_room_code": room_code,
        "p_teacher_identity": teacher_identity,
        "p_teacher_key_hash": _teacher_key_hash(teacher_access_key),
        "p_recording_id": recording_id,
    }
    for attempt in range(3):
        try:
            await _run(lambda: _client().rpc("end_class_intelligence_meeting", params).execute())
            return
        except Exception:
            logger.exception("Meeting end persistence attempt %s/3 failed (room=%s)", attempt + 1, room_code)
            if attempt < 2:
                await asyncio.sleep(attempt + 1)


async def associate_recording_with_meeting(*, room_code: str, recording_id: str,
                                           recording_started_at: str) -> str:
    """Persist the authoritative many-to-one meeting/recording relationship."""
    if not settings.validate_supabase_db():
        raise RuntimeError("Supabase DB is not configured")
    params = {
        "p_room_code": room_code,
        "p_recording_id": recording_id,
        "p_recording_started_at": recording_started_at,
    }
    response = await _run(lambda: _client().rpc("associate_recording_with_meeting", params).execute())
    return response.data


async def get_authorized_meeting_transcript(*, room_code: str, meeting_id: str,
                                            student_identity: str | None = None,
                                            teacher_identity: str | None = None,
                                            teacher_access_key: str | None = None) -> dict:
    """Return a transcript only to its class teacher or an enrolled student."""
    if not settings.validate_supabase_db():
        raise RuntimeError("Supabase DB is not configured")
    if bool(student_identity) == bool(teacher_identity and teacher_access_key):
        raise TranscriptAccessDenied("A single valid class identity is required")

    def read():
        client = _client()
        classes = (client.table("classes").select("id,teacher_identity,teacher_key_hash")
                   .eq("room_code", room_code.upper()).limit(1).execute().data or [])
        if not classes:
            raise LookupError("Class not found")
        classroom = classes[0]
        meetings = (client.table("class_meetings")
                    .select("id,transcript_status,transcript_error")
                    .eq("id", meeting_id).eq("class_id", classroom["id"])
                    .eq("room_code", room_code.upper()).limit(1).execute().data or [])
        if not meetings:
            raise LookupError("Meeting not found")

        if student_identity:
            enrollment = (client.table("class_enrollments").select("id")
                          .eq("class_id", classroom["id"])
                          .eq("student_identity", student_identity)
                          .eq("status", "authorized").limit(1).execute().data or [])
            if not enrollment:
                raise TranscriptAccessDenied("Student is not authorized for this class")
        else:
            if (classroom["teacher_identity"] != teacher_identity or not hmac.compare_digest(
                    classroom["teacher_key_hash"], _teacher_key_hash(teacher_access_key or ""))):
                raise TranscriptAccessDenied("Teacher ownership could not be verified")

        transcript = (client.table("meeting_transcripts").select(
            "status,transcript_text,language,provider,model,segments,error_code,error,updated_at,completed_at"
        ).eq("meeting_id", meeting_id).limit(1).execute().data or [])
        meeting = meetings[0]
        return transcript[0] if transcript else {
            "status": meeting["transcript_status"], "transcript_text": None,
            "error": meeting.get("transcript_error"),
        }

    return await _run(read)


async def get_authorized_meeting_notes(*, room_code: str, meeting_id: str,
                                       student_identity: str | None = None,
                                       teacher_identity: str | None = None,
                                       teacher_access_key: str | None = None) -> dict:
    """Return permanent meeting notes only to the teacher or an enrolled student."""
    if not settings.validate_supabase_db():
        raise RuntimeError("Supabase DB is not configured")
    if bool(student_identity) == bool(teacher_identity and teacher_access_key):
        raise TranscriptAccessDenied("A single valid class identity is required")

    def read():
        client = _client()
        classes = (client.table("classes").select("id,teacher_identity,teacher_key_hash")
                   .eq("room_code", room_code.upper()).limit(1).execute().data or [])
        if not classes:
            raise LookupError("Class not found")
        classroom = classes[0]
        meetings = (client.table("class_meetings")
                    .select("id,notes_status,notes_error")
                    .eq("id", meeting_id).eq("class_id", classroom["id"])
                    .eq("room_code", room_code.upper()).limit(1).execute().data or [])
        if not meetings:
            raise LookupError("Meeting not found")

        if student_identity:
            enrollment = (client.table("class_enrollments").select("id")
                          .eq("class_id", classroom["id"])
                          .eq("student_identity", student_identity)
                          .eq("status", "authorized").limit(1).execute().data or [])
            if not enrollment:
                raise TranscriptAccessDenied("Student is not authorized for this class")
        elif (classroom["teacher_identity"] != teacher_identity or not hmac.compare_digest(
                classroom["teacher_key_hash"], _teacher_key_hash(teacher_access_key or ""))):
            raise TranscriptAccessDenied("Teacher ownership could not be verified")

        rows = (client.table("meeting_notes").select(
            "status,english_notes,bengali_notes,provider,model,error_code,updated_at,completed_at"
        ).eq("meeting_id", meeting_id).limit(1).execute().data or [])
        if rows:
            return rows[0]
        meeting = meetings[0]
        return {
            "status": meeting.get("notes_status", "pending"),
            "english_notes": None,
            "bengali_notes": None,
            "error_code": None,
        }

    return await _run(read)


async def list_recordings_meeting_notes() -> list[dict[str, Any]]:
    """List permanent meeting-note records for the recordings-passcode area.

    This deliberately does not read recordings or apply their expiration
    window. Recording availability is joined by ID later in the UI from the
    separately authorized, expiring recordings response.
    """
    if not settings.validate_supabase_db():
        raise RuntimeError("Supabase DB is not configured")

    def read() -> list[dict[str, Any]]:
        client = _client()
        def select_all(table: str, columns: str, order_by: tuple[str, ...]) -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            offset = 0
            page_size = 1000
            while True:
                query = client.table(table).select(columns)
                for column in order_by:
                    query = query.order(column)
                page = query.range(offset, offset + page_size - 1).execute().data or []
                rows.extend(page)
                if len(page) < page_size:
                    return rows
                offset += page_size

        classes = select_all("classes", "id,room_code,class_name", ("id",))
        meetings = select_all(
            "class_meetings",
            "id,class_id,room_code,room_created_at,ended_at,notes_status,notes_error,recording_id",
            ("id",),
        )
        notes = select_all(
            "meeting_notes",
            "meeting_id,status,english_notes,bengali_notes,error_code,updated_at,completed_at",
            ("meeting_id",),
        )
        recording_links = select_all(
            "meeting_recordings", "class_meeting_id,recording_id", ("class_meeting_id", "recording_id")
        )

        classes_by_id = {row["id"]: row for row in classes}
        notes_by_meeting = {row["meeting_id"]: row for row in notes}
        recordings_by_meeting: dict[str, list[str]] = {}
        for link in recording_links:
            recordings_by_meeting.setdefault(link["class_meeting_id"], []).append(
                link["recording_id"]
            )

        result = []
        for meeting in meetings:
            # Meeting Notes are permanent and may outlive both the recording
            # row and its storage. Only include meetings that have a notes row.
            note = notes_by_meeting.get(meeting["id"])
            classroom = classes_by_id.get(meeting["class_id"])
            if note is None or classroom is None:
                continue
            recording_ids = recordings_by_meeting.get(meeting["id"], [])
            legacy_recording_id = meeting.get("recording_id")
            if legacy_recording_id and legacy_recording_id not in recording_ids:
                recording_ids.append(legacy_recording_id)
            result.append({
                "meeting_id": meeting["id"],
                "room_code": meeting["room_code"],
                "class_name": classroom["class_name"],
                "meeting_started_at": meeting["room_created_at"],
                "ended_at": meeting.get("ended_at"),
                "notes_status": note.get("status") or meeting.get("notes_status", "pending"),
                "english_notes": note.get("english_notes"),
                "bengali_notes": note.get("bengali_notes"),
                "error_code": note.get("error_code"),
                "updated_at": note.get("updated_at"),
                "completed_at": note.get("completed_at"),
                "recording_ids": recording_ids,
            })
        result.sort(key=lambda row: row.get("meeting_started_at") or "", reverse=True)
        return result

    return await _run(read)


async def get_recordings_meeting_note(meeting_id: str) -> dict[str, Any] | None:
    """Fetch one persisted note pair for an already recordings-authorized request."""
    if not settings.validate_supabase_db():
        raise RuntimeError("Supabase DB is not configured")

    def read() -> dict[str, Any] | None:
        client = _client()
        meetings = (client.table("class_meetings").select("id,class_id")
                    .eq("id", meeting_id).limit(1).execute().data or [])
        if not meetings:
            return None
        notes = (client.table("meeting_notes").select(
            "status,english_notes,bengali_notes"
        ).eq("meeting_id", meeting_id).limit(1).execute().data or [])
        if not notes:
            return None
        classes = (client.table("classes").select("class_name")
                   .eq("id", meetings[0]["class_id"]).limit(1).execute().data or [])
        if not classes:
            return None
        return {**notes[0], "class_name": classes[0]["class_name"]}

    return await _run(read)
