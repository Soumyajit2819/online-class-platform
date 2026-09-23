from fastapi import FastAPI, HTTPException, Query, Header, BackgroundTasks, Request
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask
from urllib.parse import quote
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel as PydanticBase
import secrets
import asyncio
import math
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from .config import settings
from .models import (
    CreateRoomRequest, JoinRoomRequest, RoomResponse, CreateJoinRequest, ClassroomChatAction,
    JoinRequestTokenRequest, JoinRequestDecision,
    ModerationRequest, MuteAllRequest, SetPolicyRequest,
    StudentMuteRestrictionRequest, StudentUnmuteRestrictionRequest,
    LockClassRequest, EndClassRequest,
    StartRecordingRequest, StopRecordingRequest,
    MicrophonePolicy, CameraPolicy, TranscriptAccessRequest,
)
from .livekit_service import livekit_service, session_state, recording_state
from .passcode_service import passcode_service
from . import class_intelligence
from . import transcription
from livekit.api import WebhookReceiver, TokenVerifier


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

async def _cleanup_loop():
    """Hourly recording-level retention cleanup."""
    while True:
        await asyncio.sleep(3600)
        try:
            removed = await livekit_service.cleanup_expired_recordings()
            if removed:
                print(f"✓ Cleaned {removed} expired recordings")
        except Exception as e:
            print(f"✗ Cleanup error: {e}")


# ---------------------------------------------------------------------------
# Lifespan (startup + shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        settings.validate()
        print("✓ LiveKit configuration validated")
    except ValueError as e:
        print(f"⚠ LiveKit config warning: {e}")

    if settings.validate_storage():
        print("✓ Supabase Storage credentials found")
        await livekit_service.ensure_bucket_exists()

    if settings.validate_supabase_db():
        print("✓ Supabase DB credentials found")
        await passcode_service.init_db()

    task = asyncio.create_task(_cleanup_loop())
    enrollment_retry_task = asyncio.create_task(class_intelligence.enrollment_retry_worker())
    recording_reconciliation_task = asyncio.create_task(livekit_service.reconciliation_worker())
    transcription_task = asyncio.create_task(transcription.transcription_worker())
    print("✓ Application startup complete")

    yield  # app runs here

    # Shutdown
    task.cancel()
    enrollment_retry_task.cancel()
    recording_reconciliation_task.cancel()
    transcription_task.cancel()
    await asyncio.gather(task, enrollment_retry_task, recording_reconciliation_task,
                         transcription_task, return_exceptions=True)
    print("✓ Application shutdown complete")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Online Class Platform API",
    description="Backend API for LiveKit-based online classroom",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs"  if settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=settings.local_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "environment": settings.ENVIRONMENT,
        "services": {
            "livekit":  bool(settings.LIVEKIT_URL and settings.LIVEKIT_API_KEY),
            "storage":  bool(settings.SUPABASE_S3_ENDPOINT and settings.SUPABASE_S3_ACCESS_KEY),
            "database": bool(settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY),
        },
    }


@app.post("/api/livekit/webhook")
async def livekit_webhook(request: Request, authorization: str | None = Header(default=None)):
    """Validate LiveKit's signed webhook, then persist Egress events for processing."""
    if not settings.LIVEKIT_API_KEY or not settings.LIVEKIT_API_SECRET:
        raise HTTPException(503, "LiveKit webhook verification is not configured")
    if not authorization:
        raise HTTPException(401, "Missing LiveKit webhook authorization")
    body = await request.body()
    try:
        receiver = WebhookReceiver(TokenVerifier(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET))
        event = receiver.receive(body.decode("utf-8"), authorization)
    except Exception:
        raise HTTPException(401, "Invalid LiveKit webhook signature or payload")
    try:
        return await livekit_service.receive_egress_webhook(event)
    except Exception as exc:
        print(f"LiveKit webhook persistence failed; sender may retry: {exc}")
        raise HTTPException(503, "Webhook could not be durably recorded")


@app.post("/api/class/{room_code}/meetings/{meeting_id}/transcript")
async def get_meeting_transcript(room_code: str, meeting_id: str,
                                 request: TranscriptAccessRequest):
    """Retrieve transcript/status for its teacher or an enrolled student."""
    student_identity = None
    teacher_identity = request.teacher_identity
    teacher_access_key = request.teacher_access_key
    if request.google_credential:
        if teacher_identity or teacher_access_key:
            raise HTTPException(403, "Provide one class identity")
        student = await asyncio.to_thread(verify_google_credential, request.google_credential)
        student_identity = f"google_{student['sub']}"
    elif not (teacher_identity and teacher_access_key):
        raise HTTPException(401, "Class authorization is required")
    try:
        return await class_intelligence.get_authorized_meeting_transcript(
            room_code=room_code, meeting_id=meeting_id,
            student_identity=student_identity,
            teacher_identity=teacher_identity,
            teacher_access_key=teacher_access_key,
        )
    except class_intelligence.TranscriptAccessDenied:
        raise HTTPException(403, "You are not authorized to access this class transcript")
    except LookupError:
        raise HTTPException(404, "Class meeting not found")
    except Exception:
        raise HTTPException(503, "Transcript service is temporarily unavailable")


@app.post("/api/class/{room_code}/meetings/{meeting_id}/notes")
async def get_meeting_notes(room_code: str, meeting_id: str,
                            request: TranscriptAccessRequest):
    """Retrieve permanent English/Bengali notes for its teacher or an enrolled student."""
    student_identity = None
    teacher_identity = request.teacher_identity
    teacher_access_key = request.teacher_access_key
    if request.google_credential:
        if teacher_identity or teacher_access_key:
            raise HTTPException(403, "Provide one class identity")
        student = await asyncio.to_thread(verify_google_credential, request.google_credential)
        student_identity = f"google_{student['sub']}"
    elif not (teacher_identity and teacher_access_key):
        raise HTTPException(401, "Class authorization is required")
    try:
        return await class_intelligence.get_authorized_meeting_notes(
            room_code=room_code, meeting_id=meeting_id,
            student_identity=student_identity,
            teacher_identity=teacher_identity,
            teacher_access_key=teacher_access_key,
        )
    except class_intelligence.TranscriptAccessDenied:
        raise HTTPException(403, "You are not authorized to access these class notes")
    except LookupError:
        raise HTTPException(404, "Class meeting not found")
    except Exception:
        raise HTTPException(503, "Meeting notes service is temporarily unavailable")


# ---------------------------------------------------------------------------
# Teacher — create room
# ---------------------------------------------------------------------------

@app.post("/api/teacher/create-room", response_model=RoomResponse)
async def create_room(request: CreateRoomRequest, background_tasks: BackgroundTasks):
    if not request.teacher_name.strip():
        raise HTTPException(400, "Teacher name is required")
    if not request.room_name.strip():
        raise HTTPException(400, "Class name is required")
    if not (2 <= request.max_participants <= 50):
        raise HTTPException(400, "Max participants must be between 2 and 50")

    room_code        = livekit_service.generate_room_code()
    invite_code      = secrets.token_urlsafe(16)
    teacher_access_key = secrets.token_urlsafe(32)
    livekit_room     = livekit_service.generate_livekit_room_name()
    teacher_identity = f"teacher_{secrets.token_urlsafe(8)}"
    passcode_hash    = livekit_service.hash_passcode(request.meeting_passcode) if request.meeting_passcode and request.meeting_passcode.strip() else None

    await livekit_service.create_room(livekit_room, max_participants=request.max_participants)

    session_state.create_class(
        room_code=room_code,
        livekit_room_name=livekit_room,
        class_name=request.room_name,
        teacher_name=request.teacher_name,
        teacher_identity=teacher_identity,
        meeting_passcode_hash=passcode_hash,
        max_participants=request.max_participants,
        student_microphone_policy=request.student_microphone_policy,
        student_camera_policy=request.student_camera_policy,
        invite_code=invite_code,
        teacher_access_key=teacher_access_key,
    )

    class_info = session_state.get_class(room_code)
    room_created_at = class_info["created_at"].replace(tzinfo=timezone.utc).isoformat()
    background_tasks.add_task(class_intelligence.persist_new_class,
        room_code=room_code, class_name=request.room_name,
        teacher_identity=teacher_identity, teacher_access_key=teacher_access_key,
        room_created_at=room_created_at,
    )

    token = livekit_service.create_access_token(
        identity=teacher_identity, name=request.teacher_name,
        room=livekit_room, role="teacher",
        can_publish=True, can_subscribe=True, can_publish_data=True,
    )

    return {"room_code": room_code, "room_name": request.room_name,
            "token": token, "livekit_url": settings.LIVEKIT_URL,
            "invite_code": invite_code, "teacher_access_key": teacher_access_key}


# ---------------------------------------------------------------------------
# Student — join room
# ---------------------------------------------------------------------------

@app.post("/api/student/join-room", response_model=RoomResponse)
async def join_room(request: JoinRoomRequest):
    """Legacy endpoint deliberately cannot issue student tokens without approval."""
    raise HTTPException(403, "Join approval is required. Submit a join request first.")


def _serialize_join_request(request):
    return {
        "request_id": request["request_id"], "room_code": request["room_code"],
        "student_name": request["student_name"], "status": request["status"],
        "created_at": request["created_at"].isoformat(),
        "updated_at": request["updated_at"].isoformat(),
        "decided_at": request["decided_at"].isoformat() if request["decided_at"] else None,
    }


def _serialize_microphone_restriction(restriction):
    server_time = datetime.now(timezone.utc)
    if not restriction:
        return {"restricted": False, "server_time": server_time.isoformat()}
    expires_at = restriction["expires_at"]
    return {
        "restricted": True,
        "mode": restriction["mode"],
        "expires_at": expires_at.isoformat() if expires_at else None,
        "updated_at": restriction["updated_at"].isoformat(),
        "server_time": server_time.isoformat(),
        "remaining_seconds": (
            max(0, math.ceil((expires_at - server_time).total_seconds()))
            if expires_at else None
        ),
    }


async def _restore_microphone_after_expiry(room_code: str, student_identity: str,
                                           expires_at: datetime):
    """Release a timed restriction at its server-authoritative expiry time."""
    await asyncio.sleep(max(0, (expires_at - datetime.now(timezone.utc)).total_seconds()))
    if not session_state.expire_microphone_restriction(
        room_code, student_identity, expected_expires_at=expires_at
    ):
        return  # The teacher released or replaced this restriction.
    class_info = session_state.get_class(room_code)
    if class_info and session_state.get_microphone_policy(room_code) != MicrophonePolicy.LOCKED:
        # Restore permission only. The student's track stays muted until they
        # explicitly choose Unmute in the client.
        await livekit_service.set_microphone_publish_permission(
            class_info["livekit_room_name"], student_identity, True)


def _schedule_microphone_restriction_expiry(room_code: str, student_identity: str,
                                            restriction) -> None:
    if restriction["expires_at"]:
        asyncio.create_task(_restore_microphone_after_expiry(
            room_code, student_identity, restriction["expires_at"]))


def verify_google_credential(credential: str) -> dict[str, str]:
    """Verify GIS-issued ID tokens and return only the session identity fields."""
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google sign-in is not configured")
    if not credential or len(credential) > 8192:
        raise HTTPException(401, "Google sign-in failed. Please try again.")
    try:
        claims = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), audience=settings.GOOGLE_CLIENT_ID
        )
    except Exception:
        # Never log or echo the credential. Verification failures all have the
        # same public response, including signature, expiry, and audience errors.
        raise HTTPException(401, "Google sign-in failed. Please try again.")
    if claims.get("aud") != settings.GOOGLE_CLIENT_ID:
        raise HTTPException(401, "Google sign-in failed. Please try again.")
    subject = claims.get("sub")
    name = claims.get("name")
    if not isinstance(subject, str) or not subject or not isinstance(name, str) or not name.strip():
        raise HTTPException(401, "Google account did not provide a usable identity.")
    return {"sub": subject, "name": name.strip()[:256]}


def _validate_join_request(request: CreateJoinRequest):
    if not request.session_id.strip() or len(request.session_id) < 16:
        raise HTTPException(400, "Invalid join session")
    room_code = request.room_code.upper().strip() if request.room_code else None
    if request.invite_code:
        invited_room = session_state.get_room_code_for_invite(request.invite_code)
        if not invited_room:
            raise HTTPException(404, "This class link is invalid or no longer available")
        if room_code and room_code != invited_room:
            raise HTTPException(400, "Invite link does not match the room code")
        room_code = invited_room
    if not room_code:
        raise HTTPException(400, "Room code or invite link is required")
    if not session_state.is_class_active(room_code):
        raise HTTPException(404, "Class not found or has ended")
    if session_state.is_class_locked(room_code):
        raise HTTPException(403, "Class is locked. New students cannot join.")
    if not session_state.verify_passcode(room_code, request.meeting_passcode or ""):
        raise HTTPException(403, "Incorrect meeting passcode")
    return room_code


@app.get("/api/join/{invite_code}")
async def get_invite_info(invite_code: str):
    room_code = session_state.get_room_code_for_invite(invite_code)
    if not room_code or not session_state.is_class_active(room_code):
        raise HTTPException(404, "This class link is invalid or no longer available")
    c = session_state.get_class(room_code)
    return {"room_code": room_code, "class_name": c["class_name"], "teacher_name": c["teacher_name"], "meeting_passcode_required": c["meeting_passcode_hash"] is not None}


@app.post("/api/student/join-requests")
async def create_join_request(request: CreateJoinRequest):
    room_code = _validate_join_request(request)
    student = await asyncio.to_thread(verify_google_credential, request.google_credential)
    existing = session_state.get_join_request_for_session(room_code, request.session_id)
    if existing and existing["status"] == "REJECTED":
        raise HTTPException(403, "Your request to join this class was declined.")
    join_request = session_state.create_join_request(
        room_code, student["name"], request.session_id,
        student_identity=f"google_{student['sub']}",
    )
    return _serialize_join_request(join_request)


@app.get("/api/student/join-requests/{request_id}")
async def get_join_request_status(request_id: str, session_id: str = Query(...)):
    request = session_state.get_join_request(request_id)
    if not request or not secrets.compare_digest(request["session_id"], session_id):
        raise HTTPException(404, "Join request not found")
    if not session_state.is_class_active(request["room_code"]):
        raise HTTPException(410, "This class has ended")
    return _serialize_join_request(request)


@app.post("/api/student/join-requests/{request_id}/token", response_model=RoomResponse)
async def get_approved_join_token(request_id: str, request: JoinRequestTokenRequest):
    join_request = session_state.get_join_request(request_id)
    if not join_request or not secrets.compare_digest(join_request["session_id"], request.session_id):
        raise HTTPException(404, "Join request not found")
    if join_request["status"] != "APPROVED":
        raise HTTPException(403, "Teacher approval is required before joining")
    room_code = join_request["room_code"]
    if not session_state.is_class_active(room_code):
        raise HTTPException(410, "This class has ended")
    if session_state.is_participant_blocked(room_code, join_request["student_identity"]):
        raise HTTPException(403, "You have been removed from this class")
    class_info = session_state.get_class(room_code)
    participants = await livekit_service.get_participants(class_info["livekit_room_name"])
    if len(participants) >= class_info["max_participants"]:
        raise HTTPException(403, "Class is full. Maximum 50 participants reached.")
    expired_restriction = session_state.expire_microphone_restriction(
        room_code, join_request["student_identity"])
    mic_policy = session_state.get_microphone_policy(room_code)
    if expired_restriction and mic_policy != MicrophonePolicy.LOCKED:
        await livekit_service.set_microphone_publish_permission(
            class_info["livekit_room_name"], join_request["student_identity"], True)
    microphone_restricted = session_state.get_microphone_restriction(
        room_code, join_request["student_identity"]) is not None
    if microphone_restricted:
        session_state.reset_microphone_restriction_enforcement(
            room_code, join_request["student_identity"], join_request["session_id"])
    camera_policy = session_state.get_camera_policy(room_code)
    publish_sources = ["screen_share", "screen_share_audio"]
    # Timed/temporary restrictions are enforced with LiveKit's participant
    # publish permission. Keep the token capability so expiry can restore
    # microphone use without forcing the student to rejoin. The client still
    # requires the student's explicit unmute action.
    if mic_policy != MicrophonePolicy.LOCKED:
        publish_sources.append("microphone")
    if camera_policy != CameraPolicy.LOCKED:
        publish_sources.append("camera")
    token = livekit_service.create_access_token(
        identity=join_request["student_identity"], name=join_request["student_name"],
        room=class_info["livekit_room_name"], role="student",
        can_publish=True,
        can_subscribe=True, can_publish_data=True,
        is_muted=(microphone_restricted or mic_policy in (MicrophonePolicy.MUTED_BY_DEFAULT, MicrophonePolicy.LOCKED)),
        is_camera_off=(camera_policy in (CameraPolicy.OFF_BY_DEFAULT, CameraPolicy.LOCKED)),
        can_publish_sources=publish_sources,
    )
    return RoomResponse(room_code=room_code, room_name=class_info["class_name"], token=token, livekit_url=settings.LIVEKIT_URL)


@app.get("/api/teacher/{room_code}/join-requests")
async def get_waiting_join_requests(room_code: str, teacher_identity: str = Query(...)):
    _verify_teacher(room_code, teacher_identity)
    return {"requests": [_serialize_join_request(r) for r in session_state.get_waiting_join_requests(room_code.upper())]}


async def _decide_join_request(request: JoinRequestDecision, status: str,
                               background_tasks: BackgroundTasks | None = None):
    class_info = _verify_teacher(request.room_code, request.teacher_identity)
    if not secrets.compare_digest(class_info["teacher_access_key"], request.teacher_access_key):
        raise HTTPException(403, "Only the authorized teacher can handle join requests")
    join_request = session_state.get_join_request(request.request_id)
    if not join_request or join_request["room_code"] != request.room_code.upper():
        raise HTTPException(404, "Join request not found")
    updated = session_state.decide_join_request(request.request_id, status)
    if not updated:
        raise HTTPException(409, "This join request has already been handled")
    if status == "APPROVED" and background_tasks is not None:
        # The approved status remains authoritative for this live session. The
        # persistence attempt happens after the approval response is sent.
        background_tasks.add_task(class_intelligence.persist_approved_student,
            room_code=request.room_code.upper(),
            teacher_identity=request.teacher_identity,
            teacher_access_key=request.teacher_access_key,
            student_identity=updated["student_identity"],
            student_name=updated["student_name"],
            request_id=request.request_id,
        )
    return _serialize_join_request(updated)


@app.post("/api/teacher/approve-join-request")
async def approve_join_request(request: JoinRequestDecision, background_tasks: BackgroundTasks):
    return await _decide_join_request(request, "APPROVED", background_tasks)


@app.post("/api/teacher/reject-join-request")
async def reject_join_request(request: JoinRequestDecision):
    return await _decide_join_request(request, "REJECTED")


# ---------------------------------------------------------------------------
# Class info
# ---------------------------------------------------------------------------

def _authorize_chat_actor(room_code: str, request: ClassroomChatAction):
    normalized = room_code.upper()
    class_info = session_state.get_class(normalized)
    if not class_info or not session_state.is_class_active(normalized):
        raise HTTPException(404, "Class not found or has ended")
    if request.teacher_identity and request.teacher_access_key:
        teacher = _verify_teacher(normalized, request.teacher_identity)
        if not secrets.compare_digest(teacher["teacher_access_key"], request.teacher_access_key):
            raise HTTPException(403, "Only the authorized teacher can control classroom chat")
        return "teacher", teacher, None
    join_request = session_state.get_join_request(request.join_request_id) if request.join_request_id else None
    if (not join_request or join_request["room_code"] != normalized or
            not request.session_id or
            not secrets.compare_digest(join_request["session_id"], request.session_id) or
            join_request["status"] != "APPROVED"):
        raise HTTPException(403, "An approved student session is required for classroom chat")
    return "student", class_info, join_request


@app.post("/api/class/{room_code}/chat")
async def classroom_chat(room_code: str, request: ClassroomChatAction):
    role, class_info, join_request = _authorize_chat_actor(room_code, request)
    normalized = room_code.upper()
    if request.action == "set_enabled":
        if role != "teacher":
            raise HTTPException(403, "Only the teacher can control classroom chat")
        if request.enabled is None:
            raise HTTPException(400, "Chat enabled state is required")
        session_state.set_chat_enabled(normalized, request.enabled)
    elif request.action == "send_message":
        message = (request.text or "").strip()
        if not message:
            raise HTTPException(400, "Message cannot be empty")
        if len(message) > 2000:
            raise HTTPException(400, "Message is too long")
        if not session_state.get_chat(normalized)["enabled"]:
            raise HTTPException(403, "Chat is turned off by the teacher")
        name = class_info["teacher_name"] if role == "teacher" else join_request["student_name"]
        if not session_state.add_chat_message(normalized, name, message):
            raise HTTPException(403, "Chat is turned off by the teacher")
    chat = session_state.get_chat(normalized)
    return chat

@app.get("/api/class/{room_code}")
async def get_class_info(room_code: str):
    c = session_state.get_class(room_code.upper())
    if not c:
        raise HTTPException(404, "Class not found")
    return {
        "room_code":                 room_code,
        "class_name":                c["class_name"],
        "teacher_name":              c["teacher_name"],
        "is_locked":                 c["is_locked"],
        "is_ended":                  c["is_ended"],
        "is_recording":              c["is_recording"],
        "max_participants":          c["max_participants"],
        "student_microphone_policy": c["student_microphone_policy"].value,
        "student_camera_policy":     c["student_camera_policy"].value,
        "meeting_passcode_required": c["meeting_passcode_hash"] is not None,
    }


@app.get("/api/class/{room_code}/participants")
async def get_participants(room_code: str):
    c = session_state.get_class(room_code.upper())
    if not c:
        raise HTTPException(404, "Class not found")
    participants = await livekit_service.get_participants(c["livekit_room_name"])
    for p in participants:
        p["role"] = "teacher" if p["identity"] == c["teacher_identity"] else "student"
    restrictions = {
        identity: _serialize_microphone_restriction(restriction)
        for identity, restriction in session_state.get_class_microphone_restrictions(room_code.upper()).items()
    }
    return {"room_code": room_code, "participants": participants,
            "microphone_restrictions": restrictions,
            "count": len(participants), "max_participants": c["max_participants"]}


# ---------------------------------------------------------------------------
# Teacher moderation helper
# ---------------------------------------------------------------------------

def _verify_teacher(room_code: str, teacher_identity: str):
    c = session_state.get_class(room_code.upper())
    if not c:
        raise HTTPException(404, "Class not found")
    if not session_state.is_teacher(room_code.upper(), teacher_identity):
        raise HTTPException(403, "Only the teacher can perform this action")
    return c


# ---------------------------------------------------------------------------
# Teacher moderation endpoints
# ---------------------------------------------------------------------------

@app.post("/api/teacher/mute-all")
async def mute_all_students(request: MuteAllRequest):
    c = _verify_teacher(request.room_code, request.teacher_identity)
    participants = await livekit_service.get_participants(c["livekit_room_name"])
    students = [p["identity"] for p in participants if p["identity"] != c["teacher_identity"]]
    await livekit_service.mute_all_students(c["livekit_room_name"], students)
    return {"success": True, "message": "All students muted"}


@app.post("/api/teacher/disable-all-cameras")
async def disable_all_cameras(request: MuteAllRequest):
    c = _verify_teacher(request.room_code, request.teacher_identity)
    participants = await livekit_service.get_participants(c["livekit_room_name"])
    students = [p["identity"] for p in participants if p["identity"] != c["teacher_identity"]]
    await livekit_service.disable_all_cameras(c["livekit_room_name"], students)
    return {"success": True, "message": "All student cameras disabled"}


@app.post("/api/teacher/remove-participant")
async def remove_participant(request: ModerationRequest):
    c = _verify_teacher(request.room_code, request.teacher_identity)
    if not request.target_identity:
        raise HTTPException(400, "Target identity required")
    if request.target_identity == c["teacher_identity"]:
        raise HTTPException(400, "Cannot remove the teacher")
    await livekit_service.remove_participant(c["livekit_room_name"], request.target_identity)
    session_state.block_participant(request.room_code.upper(), request.target_identity)
    return {"success": True, "message": "Participant removed"}


@app.post("/api/teacher/mute-participant")
async def mute_participant(request: ModerationRequest):
    c = _verify_teacher(request.room_code, request.teacher_identity)
    if not request.target_identity:
        raise HTTPException(400, "Target identity required")
    await livekit_service.mute_participant(c["livekit_room_name"], request.target_identity, True)
    return {"success": True, "message": "Participant muted"}


@app.post("/api/teacher/restrict-student-microphone")
async def restrict_student_microphone(request: StudentMuteRestrictionRequest):
    c = _verify_teacher(request.room_code, request.teacher_identity)
    if request.target_identity == c["teacher_identity"]:
        raise HTTPException(400, "Cannot restrict the teacher microphone")
    participants = await livekit_service.get_participants(c["livekit_room_name"])
    if not any(p["identity"] == request.target_identity for p in participants):
        raise HTTPException(404, "Student is not in this class")
    restriction = session_state.set_microphone_restriction(
        request.room_code.upper(), request.target_identity, request.duration_minutes)
    _schedule_microphone_restriction_expiry(
        request.room_code.upper(), request.target_identity, restriction)
    await livekit_service.mute_participant(c["livekit_room_name"], request.target_identity, True)
    await livekit_service.set_microphone_publish_permission(
        c["livekit_room_name"], request.target_identity, False)
    return _serialize_microphone_restriction(restriction)


@app.post("/api/teacher/unrestrict-student-microphone")
async def unrestrict_student_microphone(request: StudentUnmuteRestrictionRequest):
    c = _verify_teacher(request.room_code, request.teacher_identity)
    if request.target_identity == c["teacher_identity"]:
        raise HTTPException(400, "Cannot change the teacher microphone")
    session_state.clear_microphone_restriction(request.room_code.upper(), request.target_identity)
    can_publish_microphone = session_state.get_microphone_policy(request.room_code.upper()) != MicrophonePolicy.LOCKED
    if can_publish_microphone:
        await livekit_service.set_microphone_publish_permission(
            c["livekit_room_name"], request.target_identity, True)
        await livekit_service.mute_participant(c["livekit_room_name"], request.target_identity, False)
    return {"success": True, "message": "Student microphone restriction removed"}


@app.get("/api/class/{room_code}/microphone-restriction/{student_identity}")
async def get_student_microphone_restriction(
    room_code: str, student_identity: str,
    request_id: str = Query(...), session_id: str = Query(...),
):
    c = session_state.get_class(room_code.upper())
    if not c or not session_state.is_class_active(room_code.upper()):
        raise HTTPException(404, "Class not found or has ended")
    join_request = session_state.get_join_request(request_id)
    if (not join_request or join_request["room_code"] != room_code.upper() or
            join_request["student_identity"] != student_identity or
            join_request["status"] != "APPROVED" or
            not secrets.compare_digest(join_request["session_id"], session_id)):
        raise HTTPException(403, "An approved student session is required")
    expired = session_state.expire_microphone_restriction(room_code.upper(), student_identity)
    if expired and session_state.get_microphone_policy(room_code.upper()) != MicrophonePolicy.LOCKED:
        await livekit_service.set_microphone_publish_permission(c["livekit_room_name"], student_identity, True)
    elif (session_state.get_microphone_restriction(room_code.upper(), student_identity) and
          not session_state.microphone_restriction_enforced(room_code.upper(), student_identity, session_id)):
        # A student can reconnect while a restriction is active. Reassert the
        # room permission after their new LiveKit participant is present.
        await livekit_service.set_microphone_publish_permission(c["livekit_room_name"], student_identity, False)
        session_state.mark_microphone_restriction_enforced(room_code.upper(), student_identity, session_id)
    return _serialize_microphone_restriction(
        session_state.get_microphone_restriction(room_code.upper(), student_identity))


@app.post("/api/teacher/set-microphone-policy")
async def set_microphone_policy(request: SetPolicyRequest):
    _verify_teacher(request.room_code, request.teacher_identity)
    if request.microphone_policy:
        session_state.update_microphone_policy(request.room_code.upper(), request.microphone_policy)
    return {"success": True}


@app.post("/api/teacher/set-camera-policy")
async def set_camera_policy(request: SetPolicyRequest):
    _verify_teacher(request.room_code, request.teacher_identity)
    if request.camera_policy:
        session_state.update_camera_policy(request.room_code.upper(), request.camera_policy)
    return {"success": True}


@app.post("/api/teacher/lock-class")
async def lock_class(request: LockClassRequest):
    _verify_teacher(request.room_code, request.teacher_identity)
    session_state.lock_class(request.room_code.upper())
    return {"success": True, "message": "Class locked"}


@app.post("/api/teacher/unlock-class")
async def unlock_class(request: LockClassRequest):
    _verify_teacher(request.room_code, request.teacher_identity)
    session_state.unlock_class(request.room_code.upper())
    return {"success": True, "message": "Class unlocked"}


@app.post("/api/teacher/end-class")
async def end_class(request: EndClassRequest, background_tasks: BackgroundTasks):
    c  = _verify_teacher(request.room_code, request.teacher_identity)
    if not secrets.compare_digest(c["teacher_access_key"], request.teacher_access_key):
        raise HTTPException(403, "Only the authorized teacher can end this class")
    rc = request.room_code.upper()
    recent_recordings = recording_state.class_recordings.get(rc, [])
    final_recording_id = c.get("active_recording_id") or (recent_recordings[-1] if recent_recordings else None)
    if session_state.is_recording(rc):
        active_rec_id = final_recording_id
        if active_rec_id:
            await livekit_service.stop_recording(rc, active_rec_id)
    background_tasks.add_task(class_intelligence.mark_meeting_ended,
        room_code=rc, teacher_identity=request.teacher_identity,
        teacher_access_key=c["teacher_access_key"], recording_id=final_recording_id,
    )
    session_state.end_class(rc)
    await livekit_service.delete_room(c["livekit_room_name"])
    return {"success": True, "message": "Class ended"}


@app.post("/api/teacher/unblock-participant")
async def unblock_participant(request: ModerationRequest):
    _verify_teacher(request.room_code, request.teacher_identity)
    if request.target_identity:
        session_state.unblock_participant(request.room_code.upper(), request.target_identity)
    return {"success": True}


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

@app.post("/api/teacher/start-recording")
async def start_recording(request: StartRecordingRequest):
    c  = _verify_teacher(request.room_code, request.teacher_identity)
    rc = request.room_code.upper()
    if session_state.is_recording(rc):
        raise HTTPException(400, "Class is already being recorded")
    result = await livekit_service.start_recording(
        room_code=rc, livekit_room_name=c["livekit_room_name"],
        class_name=c["class_name"], teacher_name=c["teacher_name"],
    )
    if not result["success"]:
        raise HTTPException(500, result.get("error", "Failed to start recording"))
    return result


@app.post("/api/teacher/stop-recording")
async def stop_recording(request: StopRecordingRequest):
    c  = _verify_teacher(request.room_code, request.teacher_identity)
    rc = request.room_code.upper()
    result = await livekit_service.stop_recording(rc, request.recording_id)
    if not result["success"]:
        raise HTTPException(500, result.get("error", "Failed to stop recording"))
    return result


# ---------------------------------------------------------------------------
# Recordings — authenticated metadata and private HLS proxy
# ---------------------------------------------------------------------------

def _recordings_access_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Recordings access is required")
    token = authorization[7:]
    if not livekit_service.verify_recordings_access_token(token):
        raise HTTPException(401, "Recordings access has expired")
    return token

def _safe_download_filename(record: dict) -> str:
    """Build a browser-safe filename without leaking storage paths."""
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", record.get("class_name") or "class-recording")
    label = label.strip("._-")[:80] or "class-recording"
    return f"{label}-{record['recording_id']}.mp4"

async def _remux_hls_to_mp4(playlist_url: str, output_path: str):
    """Create a finalized MP4 from the private HLS proxy without retaining it."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-protocol_whitelist", "file,http,https,tcp,tls",
        "-i", playlist_url,
        # HLS transport streams carry AAC in ADTS framing; MP4 requires the
        # AudioSpecificConfig form produced by this no-reencode filter.
        "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart",
        output_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        # Do not log playlist_url: it contains the server-only playback token.
        detail = stderr.decode("utf-8", errors="replace").strip()
        print(f"FFmpeg HLS remux failed (exit {process.returncode}): {detail}")
        raise RuntimeError("FFmpeg could not remux this recording")
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        print("FFmpeg HLS remux produced no MP4 data")
        raise RuntimeError("FFmpeg could not remux this recording")
    # stdout is intentionally captured as part of the process diagnostics. It
    # should be empty because the finalized MP4 is written to output_path.
    if stdout:
        print(f"FFmpeg HLS remux produced unexpected stdout ({len(stdout)} bytes)")

def _delete_temporary_mp4(path: str):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass

@app.get("/api/recordings")
async def get_all_recordings(authorization: str | None = Header(default=None)):
    _recordings_access_token(authorization)
    recordings = await livekit_service.list_recordings()
    payload = []
    for rec in recordings:
        # Do not disclose storage layout, Egress IDs, or other internal fields.
        item = {key: rec.get(key) for key in (
            "recording_id", "room_code", "class_name", "teacher_name", "status",
            "started_at", "ended_at", "expires_at", "hours_left",
        )}
        if rec["status"] == "available":
            item["playback_url"] = f"/api/recording/{rec['recording_id']}/hls/index.m3u8?token={livekit_service.create_playback_token(rec['recording_id'], rec['expires_at'])}"
        else:
            item["playback_url"] = None
        payload.append(item)
    return {"recordings": payload, "total": len(payload)}


@app.get("/api/recordings/meeting-notes")
async def get_recordings_meeting_notes(authorization: str | None = Header(default=None)):
    """List permanent meeting notes using the existing recordings access token."""
    _recordings_access_token(authorization)
    try:
        meetings = await class_intelligence.list_recordings_meeting_notes()
    except Exception:
        raise HTTPException(503, "Meeting notes are temporarily unavailable")
    return {"meetings": meetings, "total": len(meetings)}


@app.get("/api/recordings/meeting-notes/{meeting_id}/download/{language}")
async def get_recordings_meeting_note_download(
    meeting_id: str, language: str, authorization: str | None = Header(default=None),
):
    """Return one persisted notes language after revalidating recordings access."""
    _recordings_access_token(authorization)
    if language not in {"english", "bengali"}:
        raise HTTPException(400, "Unsupported notes language")
    try:
        note = await class_intelligence.get_recordings_meeting_note(meeting_id)
    except Exception:
        raise HTTPException(503, "Meeting notes are temporarily unavailable")
    if note is None:
        raise HTTPException(404, "Meeting notes not found")
    if note.get("status") != "ready":
        raise HTTPException(409, "Meeting notes are not ready for download")
    content = note.get("english_notes" if language == "english" else "bengali_notes")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(404, "Requested notes language is not available")
    return {"class_name": note["class_name"], "content": content}


@app.get("/api/recordings/{room_code}")
async def get_recordings_by_room(room_code: str, authorization: str | None = Header(default=None)):
    _recordings_access_token(authorization)
    all_recs = await livekit_service.list_recordings()
    filtered = [r for r in all_recs if r["room_code"] == room_code.upper()]
    return {"recordings": filtered, "total": len(filtered), "room_code": room_code.upper()}


@app.get("/api/recordings/{recording_id}/download")
async def download_recording(recording_id: str, authorization: str | None = Header(default=None)):
    """Authenticated, on-demand HLS-to-MP4 remux; output is never stored."""
    _recordings_access_token(authorization)
    if not shutil.which("ffmpeg"):
        raise HTTPException(503, "Recording downloads are temporarily unavailable")

    record = await livekit_service.get_recording_metadata(recording_id)
    if not record:
        raise HTTPException(404, "Recording not found")
    expires_at = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(410, "Recording has expired")
    if record.get("status") != "available" or not await livekit_service.object_exists(record["playlist_key"]):
        raise HTTPException(404, "Recording is not available")

    # FFmpeg reads only from this service's loopback HLS proxy. The short-lived
    # token is generated server-side and never sent to the browser or storage.
    playback_token = livekit_service.create_playback_token(
        recording_id, record["expires_at"], ttl_seconds=20 * 60)
    port = os.getenv("PORT", "8080")
    playlist_url = (
        f"http://127.0.0.1:{port}/api/recording/{quote(recording_id, safe='')}/"
        f"hls/index.m3u8?token={quote(playback_token, safe='')}"
    )
    file_descriptor, output_path = tempfile.mkstemp(prefix="recording-download-", suffix=".mp4")
    os.close(file_descriptor)
    try:
        await _remux_hls_to_mp4(playlist_url, output_path)
    except (OSError, asyncio.SubprocessError, RuntimeError) as exc:
        _delete_temporary_mp4(output_path)
        print(f"Could not create MP4 download: {exc}")
        raise HTTPException(503, "Recording downloads are temporarily unavailable")
    return FileResponse(
        output_path,
        media_type="video/mp4",
        background=BackgroundTask(_delete_temporary_mp4, output_path),
        headers={
            "Content-Disposition": f'attachment; filename="{_safe_download_filename(record)}"',
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/api/recording/{recording_id}/hls/{object_name:path}")
async def get_hls_object(recording_id: str, object_name: str, token: str):
    if not livekit_service.verify_playback_token(token, recording_id):
        raise HTTPException(403, "Playback link expired")
    rec = await livekit_service.get_recording_metadata(recording_id)
    if not rec or rec["status"] not in ("available", "processing"):
        raise HTTPException(404, "Recording is not available")
    if ".." in object_name.split("/") or not object_name:
        raise HTTPException(400, "Invalid HLS object")
    # Egress versions differ on whether playlist URIs are relative or include
    # the configured prefix. Normalize both without ever allowing a cross-prefix
    # read.
    if object_name.startswith(rec["storage_prefix"]):
        object_name = object_name[len(rec["storage_prefix"]):]
    key = f"{rec['storage_prefix']}{object_name}"
    try:
        content = await livekit_service.get_object(key)
    except Exception:
        raise HTTPException(404, "HLS object not found")
    if object_name.endswith(".m3u8"):
        # Browser-native HLS cannot attach headers. Rewrite every media URI to
        # this same short-lived, recording-scoped proxy URL.
        text = content.decode("utf-8")
        base = f"/api/recording/{recording_id}/hls/"
        def proxy_uri(uri: str) -> str:
            return f"{base}{quote(uri, safe='/')}?token={quote(token)}"

        lines = []
        for line in text.splitlines():
            if line and not line.startswith("#"):
                line = proxy_uri(line)
            elif "URI=\"" in line:
                # Covers EXT-X-MAP/init files and encryption-key files should
                # the Egress configuration ever emit them.
                import re
                line = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{proxy_uri(m.group(1))}"', line)
            lines.append(line)
        return Response("\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "private, no-store"})
    media_type = "video/mp2t" if object_name.endswith(".ts") else (
        "video/iso.segment" if object_name.endswith((".m4s", ".mp4")) else "application/octet-stream")
    return Response(content, media_type=media_type, headers={"Cache-Control": "private, no-store"})


# ---------------------------------------------------------------------------
# Passcode models
# ---------------------------------------------------------------------------

class PasscodeRequest(PydanticBase):
    passcode: str

class AdminLoginRequest(PydanticBase):
    password: str

class UpdatePasscodeRequest(PydanticBase):
    admin_password: str
    new_passcode: str


# ---------------------------------------------------------------------------
# Passcode endpoints
# ---------------------------------------------------------------------------

@app.post("/api/auth/verify-teacher-passcode")
async def verify_teacher_passcode(request: PasscodeRequest):
    if not request.passcode or not request.passcode.strip():
        raise HTTPException(400, "Passcode is required")
    if passcode_service.verify_teacher_passcode(request.passcode):
        return {"success": True, "message": "Access granted"}
    raise HTTPException(403, "Invalid passcode")


@app.post("/api/auth/verify-recordings-passcode")
async def verify_recordings_passcode(request: PasscodeRequest):
    if not request.passcode or not request.passcode.strip():
        raise HTTPException(400, "Passcode is required")
    if passcode_service.verify_recordings_passcode(request.passcode):
        # This gates metadata requests. Individual media objects are further
        # protected by recording-scoped, short-lived HLS URLs.
        return {"success": True, "message": "Access granted", "access_token": livekit_service.create_recordings_access_token()}
    raise HTTPException(403, "Invalid passcode")


@app.post("/api/admin/login")
async def admin_login(request: AdminLoginRequest):
    if not request.password or not request.password.strip():
        raise HTTPException(400, "Password is required")
    if not settings.ADMIN_DASHBOARD_PASSWORD:
        raise HTTPException(503, "Admin not configured. Set ADMIN_DASHBOARD_PASSWORD in .env")
    if passcode_service.verify_admin_password(request.password):
        return {"success": True, "message": "Admin access granted"}
    raise HTTPException(403, "Invalid admin password")


@app.post("/api/admin/update-teacher-passcode")
async def update_teacher_passcode(request: UpdatePasscodeRequest):
    if not passcode_service.verify_admin_password(request.admin_password):
        raise HTTPException(403, "Invalid admin password")
    if not request.new_passcode or len(request.new_passcode.strip()) < 6:
        raise HTTPException(400, "Passcode must be at least 6 characters")
    if passcode_service.update_teacher_passcode(request.new_passcode):
        return {"success": True, "message": "Teacher passcode updated"}
    raise HTTPException(500, "Failed to update passcode")


@app.post("/api/admin/update-recordings-passcode")
async def update_recordings_passcode(request: UpdatePasscodeRequest):
    if not passcode_service.verify_admin_password(request.admin_password):
        raise HTTPException(403, "Invalid admin password")
    if not request.new_passcode or len(request.new_passcode.strip()) < 6:
        raise HTTPException(400, "Passcode must be at least 6 characters")
    if passcode_service.update_recordings_passcode(request.new_passcode):
        return {"success": True, "message": "Recordings passcode updated"}
    raise HTTPException(500, "Failed to update passcode")


@app.get("/api/admin/passcode-status")
async def get_passcode_status():
    return passcode_service.get_passcode_info()
