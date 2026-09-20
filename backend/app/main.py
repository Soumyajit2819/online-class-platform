from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Any, List
import secrets
import asyncio

from .config import settings
from .models import (
    CreateRoomRequest, JoinRoomRequest, RoomResponse,
    ModerationRequest, MuteAllRequest, SetPolicyRequest,
    LockClassRequest, EndClassRequest,
    StartRecordingRequest, StopRecordingRequest,
    MicrophonePolicy, CameraPolicy,
)
from .livekit_service import livekit_service, session_state, recording_state
from .passcode_service import passcode_service

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Online Class Platform API",
    description="Backend API for LiveKit-based online classroom",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
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

    asyncio.create_task(_cleanup_loop())


async def _cleanup_loop():
    """Hourly cleanup: removes expired in-memory metadata AND deletes files from Supabase S3."""
    while True:
        await asyncio.sleep(3600)  # run every hour
        try:
            # 1. Clean expired in-memory metadata
            expired = recording_state.cleanup_expired()
            if expired:
                print(f"✓ Cleaned up {len(expired)} expired recording entries from memory")

            # 2. Delete expired files from Supabase S3
            await livekit_service.delete_expired_recordings_from_storage()

        except Exception as e:
            print(f"✗ Error in cleanup task: {e}")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health_check():
    return {"status": "ok"}


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
    )

    token = livekit_service.create_access_token(
        identity=teacher_identity, name=request.teacher_name,
        room=livekit_room, role="teacher",
        can_publish=True, can_subscribe=True, can_publish_data=True,
    )

    return RoomResponse(room_code=room_code, room_name=request.room_name,
                        token=token, livekit_url=settings.LIVEKIT_URL)


# ---------------------------------------------------------------------------
# Student — join room
# ---------------------------------------------------------------------------

@app.post("/api/student/join-room", response_model=RoomResponse)
async def join_room(request: JoinRoomRequest):
    if not request.student_name.strip():
        raise HTTPException(400, "Student name is required")
    if not request.room_code.strip():
        raise HTTPException(400, "Room code is required")
    if not request.meeting_passcode.strip():
        raise HTTPException(400, "Meeting passcode is required")

    rc = request.room_code.upper()

    if not session_state.is_class_active(rc):
        raise HTTPException(404, "Class not found or has ended")
    if session_state.is_class_locked(rc):
        raise HTTPException(403, "Class is locked. New students cannot join.")
    if not session_state.verify_passcode(rc, request.meeting_passcode):
        raise HTTPException(403, "Invalid meeting passcode")

    class_info = session_state.get_class(rc)
    if not class_info:
        raise HTTPException(404, "Class not found")

    student_identity = f"student_{secrets.token_urlsafe(8)}"

    if session_state.is_participant_blocked(rc, student_identity):
        raise HTTPException(403, "You have been removed from this class")

    participants = await livekit_service.get_participants(class_info["livekit_room_name"])
    if len(participants) >= class_info["max_participants"]:
        raise HTTPException(403, "Class is full. Maximum 50 participants reached.")

    mic_policy    = session_state.get_microphone_policy(rc)
    camera_policy = session_state.get_camera_policy(rc)

    token = livekit_service.create_access_token(
        identity=student_identity, name=request.student_name,
        room=class_info["livekit_room_name"], role="student",
        can_publish=(mic_policy != MicrophonePolicy.LOCKED and camera_policy != CameraPolicy.LOCKED),
        can_subscribe=True, can_publish_data=True,
        is_muted=(mic_policy in (MicrophonePolicy.MUTED_BY_DEFAULT, MicrophonePolicy.LOCKED)),
        is_camera_off=(camera_policy in (CameraPolicy.OFF_BY_DEFAULT, CameraPolicy.LOCKED)),
    )

    return RoomResponse(room_code=rc, room_name=class_info["class_name"],
                        token=token, livekit_url=settings.LIVEKIT_URL)


# ---------------------------------------------------------------------------
# Class info
# ---------------------------------------------------------------------------

@app.get("/api/class/{room_code}")
async def get_class_info(room_code: str):
    c = session_state.get_class(room_code.upper())
    if not c:
        raise HTTPException(404, "Class not found")
    return {
        "room_code":                room_code,
        "class_name":               c["class_name"],
        "teacher_name":             c["teacher_name"],
        "is_locked":                c["is_locked"],
        "is_ended":                 c["is_ended"],
        "is_recording":             c["is_recording"],
        "max_participants":         c["max_participants"],
        "student_microphone_policy": c["student_microphone_policy"].value,
        "student_camera_policy":    c["student_camera_policy"].value,
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
# Teacher moderation
# ---------------------------------------------------------------------------

def _verify_teacher(room_code: str, teacher_identity: str):
    c = session_state.get_class(room_code.upper())
    if not c:
        raise HTTPException(404, "Class not found")
    if not session_state.is_teacher(room_code.upper(), teacher_identity):
        raise HTTPException(403, "Only the teacher can perform this action")
    return c


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
    c = _verify_teacher(request.room_code, request.teacher_identity)
    rc = request.room_code.upper()

    # Auto-stop active recording before ending class
    if session_state.is_recording(rc):
        active_rec_id = c.get("active_recording_id")
        if active_rec_id:
            print(f"Auto-stopping recording {active_rec_id} before ending class...")
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
        room_code=rc,
        livekit_room_name=c["livekit_room_name"],
        class_name=c["class_name"],
        teacher_name=c["teacher_name"],
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
# Recordings — student dashboard (reads directly from Supabase S3)
# ---------------------------------------------------------------------------

@app.get("/api/recordings")
async def get_all_recordings():
    """
    Returns all recordings stored in Supabase S3 bucket.
    Works even after server restart — Supabase is the source of truth.
    Recordings older than 3 days are automatically excluded.
    """
    recordings = await livekit_service.list_recordings_from_storage()
    return {"recordings": recordings, "total": len(recordings)}


@app.get("/api/recordings/{room_code}")
async def get_recordings_by_room(room_code: str):
    """Get recordings for a specific room code."""
    all_recs = await livekit_service.list_recordings_from_storage()
    filtered = [r for r in all_recs if r["room_code"] == room_code.upper()]
    return {"recordings": filtered, "total": len(filtered), "room_code": room_code.upper()}


@app.get("/api/recording/{recording_id}/download")
async def get_download_url(recording_id: str):
    """Get a fresh pre-signed download URL for a recording."""
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
# Passcode verification endpoints
# ---------------------------------------------------------------------------

from pydantic import BaseModel as PydanticBase

class PasscodeRequest(PydanticBase):
    passcode: str

class AdminLoginRequest(PydanticBase):
    password: str

class UpdatePasscodeRequest(PydanticBase):
    admin_password: str
    new_passcode: str


@app.post("/api/auth/verify-teacher-passcode")
async def verify_teacher_passcode(request: PasscodeRequest):
    """
    Verify teacher access passcode.
    Frontend calls this before showing the teacher/create-class page.
    """
    if not request.passcode or not request.passcode.strip():
        raise HTTPException(400, "Passcode is required")

    if passcode_service.verify_teacher_passcode(request.passcode):
        return {"success": True, "message": "Access granted"}
    else:
        raise HTTPException(403, "Invalid passcode")


@app.post("/api/auth/verify-recordings-passcode")
async def verify_recordings_passcode(request: PasscodeRequest):
    """
    Verify recordings access passcode.
    Frontend calls this before showing the recordings page.
    """
    if not request.passcode or not request.passcode.strip():
        raise HTTPException(400, "Passcode is required")

    if passcode_service.verify_recordings_passcode(request.passcode):
        return {"success": True, "message": "Access granted"}
    else:
        raise HTTPException(403, "Invalid passcode")


@app.post("/api/admin/login")
async def admin_login(request: AdminLoginRequest):
    """Verify admin dashboard password."""
    if not request.password or not request.password.strip():
        raise HTTPException(400, "Password is required")

    if not settings.ADMIN_DASHBOARD_PASSWORD:
        raise HTTPException(503, "Admin dashboard is not configured. Set ADMIN_DASHBOARD_PASSWORD in .env")

    if passcode_service.verify_admin_password(request.password):
        return {"success": True, "message": "Admin access granted"}
    else:
        raise HTTPException(403, "Invalid admin password")


@app.post("/api/admin/update-teacher-passcode")
async def update_teacher_passcode(request: UpdatePasscodeRequest):
    """Update teacher access passcode (admin only)."""
    if not passcode_service.verify_admin_password(request.admin_password):
        raise HTTPException(403, "Invalid admin password")

    if not request.new_passcode or len(request.new_passcode.strip()) < 6:
        raise HTTPException(400, "New passcode must be at least 6 characters")

    if passcode_service.update_teacher_passcode(request.new_passcode):
        return {"success": True, "message": "Teacher passcode updated"}
    else:
        raise HTTPException(500, "Failed to update passcode")


@app.post("/api/admin/update-recordings-passcode")
async def update_recordings_passcode(request: UpdatePasscodeRequest):
    """Update recordings access passcode (admin only)."""
    if not passcode_service.verify_admin_password(request.admin_password):
        raise HTTPException(403, "Invalid admin password")

    if not request.new_passcode or len(request.new_passcode.strip()) < 6:
        raise HTTPException(400, "New passcode must be at least 6 characters")

    if passcode_service.update_recordings_passcode(request.new_passcode):
        return {"success": True, "message": "Recordings passcode updated"}
    else:
        raise HTTPException(500, "Failed to update passcode")


@app.get("/api/admin/passcode-status")
async def get_passcode_status():
    """
    Returns whether passcodes are configured.
    Does NOT return the passcode or hash — safe to call publicly.
    """
    return passcode_service.get_passcode_info()
