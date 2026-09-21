from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel as PydanticBase
import secrets
import asyncio

from .config import settings
from .models import (
    CreateRoomRequest, JoinRoomRequest, RoomResponse, CreateJoinRequest,
    JoinRequestTokenRequest, JoinRequestDecision,
    ModerationRequest, MuteAllRequest, SetPolicyRequest,
    LockClassRequest, EndClassRequest,
    StartRecordingRequest, StopRecordingRequest,
    MicrophonePolicy, CameraPolicy,
)
from .livekit_service import livekit_service, session_state, recording_state
from .passcode_service import passcode_service


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

async def _cleanup_loop():
    """Hourly: remove expired recording metadata + delete S3 files."""
    while True:
        await asyncio.sleep(3600)
        try:
            expired = recording_state.cleanup_expired()
            if expired:
                print(f"✓ Cleaned {len(expired)} expired recording entries")
            await livekit_service.delete_expired_recordings_from_storage()
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
    print("✓ Application startup complete")

    yield  # app runs here

    # Shutdown
    task.cancel()
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


# ---------------------------------------------------------------------------
# Teacher — create room
# ---------------------------------------------------------------------------

@app.post("/api/teacher/create-room", response_model=RoomResponse)
async def create_room(request: CreateRoomRequest):
    if not request.teacher_name.strip():
        raise HTTPException(400, "Teacher name is required")
    if not request.room_name.strip():
        raise HTTPException(400, "Class name is required")
    if not request.meeting_passcode.strip():
        raise HTTPException(400, "Meeting passcode is required")
    if not (2 <= request.max_participants <= 50):
        raise HTTPException(400, "Max participants must be between 2 and 50")

    room_code        = livekit_service.generate_room_code()
    invite_code      = secrets.token_urlsafe(16)
    teacher_access_key = secrets.token_urlsafe(32)
    livekit_room     = livekit_service.generate_livekit_room_name()
    teacher_identity = f"teacher_{secrets.token_urlsafe(8)}"
    passcode_hash    = livekit_service.hash_passcode(request.meeting_passcode)

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


def _validate_join_request(request: CreateJoinRequest):
    if not request.student_name.strip():
        raise HTTPException(400, "Student name is required")
    if not request.meeting_passcode.strip():
        raise HTTPException(400, "Meeting passcode is required")
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
    if not session_state.verify_passcode(room_code, request.meeting_passcode):
        raise HTTPException(403, "Incorrect meeting passcode")
    return room_code


@app.get("/api/join/{invite_code}")
async def get_invite_info(invite_code: str):
    room_code = session_state.get_room_code_for_invite(invite_code)
    if not room_code or not session_state.is_class_active(room_code):
        raise HTTPException(404, "This class link is invalid or no longer available")
    c = session_state.get_class(room_code)
    return {"room_code": room_code, "class_name": c["class_name"], "teacher_name": c["teacher_name"]}


@app.post("/api/student/join-requests")
async def create_join_request(request: CreateJoinRequest):
    room_code = _validate_join_request(request)
    existing = session_state.get_join_request_for_session(room_code, request.session_id)
    if existing and existing["status"] == "REJECTED":
        raise HTTPException(403, "Your request to join this class was declined.")
    join_request = session_state.create_join_request(room_code, request.student_name, request.session_id)
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
    mic_policy = session_state.get_microphone_policy(room_code)
    camera_policy = session_state.get_camera_policy(room_code)
    token = livekit_service.create_access_token(
        identity=join_request["student_identity"], name=join_request["student_name"],
        room=class_info["livekit_room_name"], role="student",
        can_publish=(mic_policy != MicrophonePolicy.LOCKED and camera_policy != CameraPolicy.LOCKED),
        can_subscribe=True, can_publish_data=True,
        is_muted=(mic_policy in (MicrophonePolicy.MUTED_BY_DEFAULT, MicrophonePolicy.LOCKED)),
        is_camera_off=(camera_policy in (CameraPolicy.OFF_BY_DEFAULT, CameraPolicy.LOCKED)),
    )
    return RoomResponse(room_code=room_code, room_name=class_info["class_name"], token=token, livekit_url=settings.LIVEKIT_URL)


@app.get("/api/teacher/{room_code}/join-requests")
async def get_waiting_join_requests(room_code: str, teacher_identity: str = Query(...)):
    _verify_teacher(room_code, teacher_identity)
    return {"requests": [_serialize_join_request(r) for r in session_state.get_waiting_join_requests(room_code.upper())]}


async def _decide_join_request(request: JoinRequestDecision, status: str):
    class_info = _verify_teacher(request.room_code, request.teacher_identity)
    if not secrets.compare_digest(class_info["teacher_access_key"], request.teacher_access_key):
        raise HTTPException(403, "Only the authorized teacher can handle join requests")
    join_request = session_state.get_join_request(request.request_id)
    if not join_request or join_request["room_code"] != request.room_code.upper():
        raise HTTPException(404, "Join request not found")
    updated = session_state.decide_join_request(request.request_id, status)
    if not updated:
        raise HTTPException(409, "This join request has already been handled")
    return _serialize_join_request(updated)


@app.post("/api/teacher/approve-join-request")
async def approve_join_request(request: JoinRequestDecision):
    return await _decide_join_request(request, "APPROVED")


@app.post("/api/teacher/reject-join-request")
async def reject_join_request(request: JoinRequestDecision):
    return await _decide_join_request(request, "REJECTED")


# ---------------------------------------------------------------------------
# Class info
# ---------------------------------------------------------------------------

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
    }


@app.get("/api/class/{room_code}/participants")
async def get_participants(room_code: str):
    c = session_state.get_class(room_code.upper())
    if not c:
        raise HTTPException(404, "Class not found")
    participants = await livekit_service.get_participants(c["livekit_room_name"])
    for p in participants:
        p["role"] = "teacher" if p["identity"] == c["teacher_identity"] else "student"
    return {"room_code": room_code, "participants": participants,
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
async def end_class(request: EndClassRequest):
    c  = _verify_teacher(request.room_code, request.teacher_identity)
    rc = request.room_code.upper()
    if session_state.is_recording(rc):
        active_rec_id = c.get("active_recording_id")
        if active_rec_id:
            await livekit_service.stop_recording(rc, active_rec_id)
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
# Recordings — student dashboard (reads from Supabase S3)
# ---------------------------------------------------------------------------

@app.get("/api/recordings")
async def get_all_recordings():
    recordings = await livekit_service.list_recordings_from_storage()
    return {"recordings": recordings, "total": len(recordings)}


@app.get("/api/recordings/{room_code}")
async def get_recordings_by_room(room_code: str):
    all_recs = await livekit_service.list_recordings_from_storage()
    filtered = [r for r in all_recs if r["room_code"] == room_code.upper()]
    return {"recordings": filtered, "total": len(filtered), "room_code": room_code.upper()}


@app.get("/api/recording/{recording_id}/download")
async def get_download_url(recording_id: str):
    all_recs = await livekit_service.list_recordings_from_storage()
    rec = next((r for r in all_recs if r["recording_id"] == recording_id), None)
    if not rec:
        raise HTTPException(404, "Recording not found or has expired")
    if not rec.get("download_url"):
        raise HTTPException(400, "Download URL could not be generated")
    return {
        "recording_id": recording_id,
        "download_url": rec["download_url"],
        "expires_at":   rec["expires_at"],
        "hours_left":   rec["hours_left"],
        "file_size_mb": rec["file_size_mb"],
    }


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
        return {"success": True, "message": "Access granted"}
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
