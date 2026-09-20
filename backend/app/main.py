from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, Any, List
import secrets
import asyncio
from datetime import datetime

from .config import settings
from .models import (
    CreateRoomRequest,
    JoinRoomRequest,
    RoomResponse,
    ModerationRequest,
    MuteAllRequest,
    SetPolicyRequest,
    LockClassRequest,
    EndClassRequest,
    StartRecordingRequest,
    StopRecordingRequest,
    MicrophonePolicy,
    CameraPolicy,
)
from .livekit_service import livekit_service, session_state, recording_state

# Create FastAPI app
app = FastAPI(
    title="Online Class Platform API",
    description="Backend API for LiveKit-based online classroom",
    version="1.0.0",
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    """Validate environment variables on startup."""
    try:
        settings.validate()
        print("✓ LiveKit configuration validated successfully")
    except ValueError as e:
        print(f"⚠ Configuration warning: {e}")
        print("  Some features may not work correctly without proper LiveKit credentials")
    
    # Start background cleanup task for expired recordings
    asyncio.create_task(cleanup_expired_recordings_task())


# Health check endpoint
@app.get("/api/health")
async def health_check():
    return {"status": "ok"}


# Teacher creates a new class
@app.post("/api/teacher/create-room", response_model=RoomResponse)
async def create_room(request: CreateRoomRequest):
    """Create a new class room for teacher."""
    # Validate inputs
    if not request.teacher_name or not request.teacher_name.strip():
        raise HTTPException(status_code=400, detail="Teacher name is required")
    if not request.room_name or not request.room_name.strip():
        raise HTTPException(status_code=400, detail="Class name is required")
    if not request.meeting_passcode or not request.meeting_passcode.strip():
        raise HTTPException(status_code=400, detail="Meeting passcode is required")
    if request.max_participants < 2 or request.max_participants > 50:
        raise HTTPException(status_code=400, detail="Max participants must be between 2 and 50")
    
    # Generate room code and LiveKit room name
    room_code = livekit_service.generate_room_code()
    livekit_room_name = livekit_service.generate_livekit_room_name()
    
    # Generate teacher identity
    teacher_identity = f"teacher_{secrets.token_urlsafe(8)}"
    
    # Hash the meeting passcode
    passcode_hash = livekit_service.hash_passcode(request.meeting_passcode)
    
    # Create LiveKit room
    await livekit_service.create_room(
        livekit_room_name,
        max_participants=request.max_participants,
    )
    
    # Store session state
    session_state.create_class(
        room_code=room_code,
        livekit_room_name=livekit_room_name,
        class_name=request.room_name,
        teacher_name=request.teacher_name,
        teacher_identity=teacher_identity,
        meeting_passcode_hash=passcode_hash,
        max_participants=request.max_participants,
        student_microphone_policy=request.student_microphone_policy,
        student_camera_policy=request.student_camera_policy,
    )
    
    # Determine if teacher should start muted/camera off based on policies
    is_muted = request.student_microphone_policy == MicrophonePolicy.MUTED_BY_DEFAULT
    is_camera_off = request.student_camera_policy == CameraPolicy.OFF_BY_DEFAULT
    
    # Generate teacher token
    token = livekit_service.create_access_token(
        identity=teacher_identity,
        name=request.teacher_name,
        room=livekit_room_name,
        role="teacher",
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )
    
    return RoomResponse(
        room_code=room_code,
        room_name=request.room_name,
        token=token,
        livekit_url=settings.LIVEKIT_URL,
    )


# Student joins a class
@app.post("/api/student/join-room", response_model=RoomResponse)
async def join_room(request: JoinRoomRequest):
    """Student joins an existing class room."""
    # Validate inputs
    if not request.student_name or not request.student_name.strip():
        raise HTTPException(status_code=400, detail="Student name is required")
    if not request.room_code or not request.room_code.strip():
        raise HTTPException(status_code=400, detail="Room code is required")
    if not request.meeting_passcode or not request.meeting_passcode.strip():
        raise HTTPException(status_code=400, detail="Meeting passcode is required")
    
    # Check if class exists and is active
    if not session_state.is_class_active(request.room_code):
        raise HTTPException(status_code=404, detail="Class not found or has ended")
    
    # Check if class is locked
    if session_state.is_class_locked(request.room_code):
        raise HTTPException(status_code=403, detail="Class is locked. New students cannot join.")
    
    # Verify meeting passcode
    if not session_state.verify_passcode(request.room_code, request.meeting_passcode):
        raise HTTPException(status_code=403, detail="Invalid meeting passcode")
    
    # Get class info
    class_info = session_state.get_class(request.room_code)
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Generate student identity
    student_identity = f"student_{secrets.token_urlsafe(8)}"
    
    # Check if student is blocked
    if session_state.is_participant_blocked(request.room_code, student_identity):
        raise HTTPException(status_code=403, detail="You have been removed from this class")
    
    # Get current participant count from LiveKit
    participants = await livekit_service.get_participants(class_info["livekit_room_name"])
    current_count = len(participants)
    
    if current_count >= class_info["max_participants"]:
        raise HTTPException(status_code=403, detail="Class is full. Maximum participants reached.")
    
    # Get policies
    mic_policy = session_state.get_microphone_policy(request.room_code)
    camera_policy = session_state.get_camera_policy(request.room_code)
    
    # Determine publishing permissions based on policies
    can_publish_audio = mic_policy != MicrophonePolicy.LOCKED
    can_publish_video = camera_policy != CameraPolicy.LOCKED
    
    # Determine initial states
    is_muted = mic_policy == MicrophonePolicy.MUTED_BY_DEFAULT or mic_policy == MicrophonePolicy.LOCKED
    is_camera_off = camera_policy == CameraPolicy.OFF_BY_DEFAULT or camera_policy == CameraPolicy.LOCKED
    
    # Generate student token
    token = livekit_service.create_access_token(
        identity=student_identity,
        name=request.student_name,
        room=class_info["livekit_room_name"],
        role="student",
        can_publish=can_publish_audio and can_publish_video,
        can_subscribe=True,
        can_publish_data=True,
        is_muted=is_muted,
        is_camera_off=is_camera_off,
    )
    
    return RoomResponse(
        room_code=request.room_code,
        room_name=class_info["class_name"],
        token=token,
        livekit_url=settings.LIVEKIT_URL,
    )


# Get class info
@app.get("/api/class/{room_code}")
async def get_class_info(room_code: str):
    """Get information about a class."""
    class_info = session_state.get_class(room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    return {
        "room_code": room_code,
        "class_name": class_info["class_name"],
        "teacher_name": class_info["teacher_name"],
        "is_locked": class_info["is_locked"],
        "is_ended": class_info["is_ended"],
        "max_participants": class_info["max_participants"],
        "student_microphone_policy": class_info["student_microphone_policy"].value,
        "student_camera_policy": class_info["student_camera_policy"].value,
    }


# Get participants in a class
@app.get("/api/class/{room_code}/participants")
async def get_participants(room_code: str):
    """Get list of participants in a class."""
    class_info = session_state.get_class(room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    participants = await livekit_service.get_participants(class_info["livekit_room_name"])
    
    # Enhance with role information
    for p in participants:
        if p["identity"] == class_info["teacher_identity"]:
            p["role"] = "teacher"
        else:
            p["role"] = "student"
    
    return {
        "room_code": room_code,
        "participants": participants,
        "count": len(participants),
        "max_participants": class_info["max_participants"],
    }


# Teacher moderation: Mute all students
@app.post("/api/teacher/mute-all")
async def mute_all_students(request: MuteAllRequest):
    """Mute all student microphones."""
    class_info = session_state.get_class(request.room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify this is the teacher
    if not session_state.is_teacher(request.room_code.upper(), request.teacher_identity):
        raise HTTPException(status_code=403, detail="Only the teacher can perform this action")
    
    # Get all participants
    participants = await livekit_service.get_participants(class_info["livekit_room_name"])
    
    # Filter students
    student_identities = [
        p["identity"] for p in participants
        if p["identity"] != class_info["teacher_identity"]
    ]
    
    # Mute all students
    await livekit_service.mute_all_students(class_info["livekit_room_name"], student_identities)
    
    return {"success": True, "message": "All students muted"}


# Teacher moderation: Disable all student cameras
@app.post("/api/teacher/disable-all-cameras")
async def disable_all_cameras(request: MuteAllRequest):
    """Disable all student cameras."""
    class_info = session_state.get_class(request.room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify this is the teacher
    if not session_state.is_teacher(request.room_code.upper(), request.teacher_identity):
        raise HTTPException(status_code=403, detail="Only the teacher can perform this action")
    
    # Get all participants
    participants = await livekit_service.get_participants(class_info["livekit_room_name"])
    
    # Filter students
    student_identities = [
        p["identity"] for p in participants
        if p["identity"] != class_info["teacher_identity"]
    ]
    
    # Disable all student cameras
    await livekit_service.disable_all_cameras(class_info["livekit_room_name"], student_identities)
    
    return {"success": True, "message": "All student cameras disabled"}


# Teacher moderation: Remove participant
@app.post("/api/teacher/remove-participant")
async def remove_participant(request: ModerationRequest):
    """Remove a participant from the class."""
    class_info = session_state.get_class(request.room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify this is the teacher
    if not session_state.is_teacher(request.room_code.upper(), request.teacher_identity):
        raise HTTPException(status_code=403, detail="Only the teacher can perform this action")
    
    if not request.target_identity:
        raise HTTPException(status_code=400, detail="Target participant identity is required")
    
    # Cannot remove the teacher
    if request.target_identity == class_info["teacher_identity"]:
        raise HTTPException(status_code=400, detail="Cannot remove the teacher")
    
    # Remove participant from LiveKit room
    await livekit_service.remove_participant(class_info["livekit_room_name"], request.target_identity)
    
    # Block the participant from rejoining
    session_state.block_participant(request.room_code.upper(), request.target_identity)
    
    return {"success": True, "message": f"Participant {request.target_identity} removed"}


# Teacher moderation: Mute individual participant
@app.post("/api/teacher/mute-participant")
async def mute_participant(request: ModerationRequest):
    """Mute a specific participant."""
    class_info = session_state.get_class(request.room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify this is the teacher
    if not session_state.is_teacher(request.room_code.upper(), request.teacher_identity):
        raise HTTPException(status_code=403, detail="Only the teacher can perform this action")
    
    if not request.target_identity:
        raise HTTPException(status_code=400, detail="Target participant identity is required")
    
    # Mute participant
    await livekit_service.mute_participant(class_info["livekit_room_name"], request.target_identity, True)
    
    return {"success": True, "message": f"Participant {request.target_identity} muted"}


# Teacher moderation: Set microphone policy
@app.post("/api/teacher/set-microphone-policy")
async def set_microphone_policy(request: SetPolicyRequest):
    """Set student microphone policy."""
    class_info = session_state.get_class(request.room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify this is the teacher
    if not session_state.is_teacher(request.room_code.upper(), request.teacher_identity):
        raise HTTPException(status_code=403, detail="Only the teacher can perform this action")
    
    if request.microphone_policy:
        session_state.update_microphone_policy(request.room_code.upper(), request.microphone_policy)
    
    return {
        "success": True,
        "message": f"Microphone policy updated to {request.microphone_policy.value if request.microphone_policy else 'no change'}"
    }


# Teacher moderation: Set camera policy
@app.post("/api/teacher/set-camera-policy")
async def set_camera_policy(request: SetPolicyRequest):
    """Set student camera policy."""
    class_info = session_state.get_class(request.room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify this is the teacher
    if not session_state.is_teacher(request.room_code.upper(), request.teacher_identity):
        raise HTTPException(status_code=403, detail="Only the teacher can perform this action")
    
    if request.camera_policy:
        session_state.update_camera_policy(request.room_code.upper(), request.camera_policy)
    
    return {
        "success": True,
        "message": f"Camera policy updated to {request.camera_policy.value if request.camera_policy else 'no change'}"
    }


# Teacher moderation: Lock class
@app.post("/api/teacher/lock-class")
async def lock_class(request: LockClassRequest):
    """Lock the class to prevent new students from joining."""
    class_info = session_state.get_class(request.room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify this is the teacher
    if not session_state.is_teacher(request.room_code.upper(), request.teacher_identity):
        raise HTTPException(status_code=403, detail="Only the teacher can perform this action")
    
    session_state.lock_class(request.room_code.upper())
    
    return {"success": True, "message": "Class locked"}


# Teacher moderation: Unlock class
@app.post("/api/teacher/unlock-class")
async def unlock_class(request: LockClassRequest):
    """Unlock the class to allow new students to join."""
    class_info = session_state.get_class(request.room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify this is the teacher
    if not session_state.is_teacher(request.room_code.upper(), request.teacher_identity):
        raise HTTPException(status_code=403, detail="Only the teacher can perform this action")
    
    session_state.unlock_class(request.room_code.upper())
    
    return {"success": True, "message": "Class unlocked"}


# Teacher moderation: End class
@app.post("/api/teacher/end-class")
async def end_class(request: EndClassRequest):
    """End the class for everyone."""
    class_info = session_state.get_class(request.room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify this is the teacher
    if not session_state.is_teacher(request.room_code.upper(), request.teacher_identity):
        raise HTTPException(status_code=403, detail="Only the teacher can end the class")
    
    # Mark class as ended
    session_state.end_class(request.room_code.upper())
    
    # Delete the LiveKit room (disconnects all participants)
    await livekit_service.delete_room(class_info["livekit_room_name"])
    
    return {"success": True, "message": "Class ended"}


# Unblock participant (optional feature for teacher)
@app.post("/api/teacher/unblock-participant")
async def unblock_participant(request: ModerationRequest):
    """Allow a removed participant to rejoin."""
    class_info = session_state.get_class(request.room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify this is the teacher
    if not session_state.is_teacher(request.room_code.upper(), request.teacher_identity):
        raise HTTPException(status_code=403, detail="Only the teacher can perform this action")
    
    if request.target_identity:
        session_state.unblock_participant(request.room_code.upper(), request.target_identity)
    
    return {"success": True, "message": f"Participant {request.target_identity} unblocked"}



# Recording endpoints

@app.post("/api/teacher/start-recording")
async def start_recording(request: StartRecordingRequest):
    """Start recording the class."""
    class_info = session_state.get_class(request.room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify this is the teacher
    if not session_state.is_teacher(request.room_code.upper(), request.teacher_identity):
        raise HTTPException(status_code=403, detail="Only the teacher can start recording")
    
    # Check if already recording
    if session_state.is_recording(request.room_code.upper()):
        raise HTTPException(status_code=400, detail="Class is already being recorded")
    
    result = await livekit_service.start_recording(
        room_code=request.room_code.upper(),
        livekit_room_name=class_info["livekit_room_name"],
        class_name=class_info["class_name"],
        teacher_name=class_info["teacher_name"],
    )
    
    if not result["success"]:
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to start recording"))
    
    return result


@app.post("/api/teacher/stop-recording")
async def stop_recording(request: StopRecordingRequest):
    """Stop recording the class."""
    class_info = session_state.get_class(request.room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify this is the teacher
    if not session_state.is_teacher(request.room_code.upper(), request.teacher_identity):
        raise HTTPException(status_code=403, detail="Only the teacher can stop recording")
    
    result = await livekit_service.stop_recording(
        room_code=request.room_code.upper(),
        recording_id=request.recording_id,
    )
    
    if not result["success"]:
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to stop recording"))
    
    return result


@app.get("/api/class/{room_code}/recordings")
async def get_class_recordings(room_code: str):
    """Get all recordings for a class."""
    class_info = session_state.get_class(room_code.upper())
    if not class_info:
        raise HTTPException(status_code=404, detail="Class not found")
    
    recordings = livekit_service.get_class_recordings(room_code.upper())
    
    return {
        "room_code": room_code,
        "class_name": class_info["class_name"],
        "recordings": recordings,
    }


@app.get("/api/recordings")
async def get_all_recordings():
    """Get all available recordings."""
    recordings = livekit_service.get_all_available_recordings()
    return {
        "recordings": recordings,
        "total": len(recordings),
    }


@app.get("/api/recording/{recording_id}")
async def get_recording_status(recording_id: str):
    """Get the status of a specific recording."""
    result = await livekit_service.get_recording_status(recording_id)
    
    if not result["success"]:
        raise HTTPException(status_code=404, detail=result.get("error", "Recording not found"))
    
    return result


@app.get("/api/recording/{recording_id}/download")
async def get_recording_download_url(recording_id: str):
    """Get download URL for a recording."""
    result = await livekit_service.get_recording_status(recording_id)
    
    if not result["success"]:
        raise HTTPException(status_code=404, detail=result.get("error", "Recording not found"))
    
    recording = result["recording"]
    
    if recording["status"] != "available":
        raise HTTPException(status_code=400, detail="Recording is not yet available for download")
    
    if not recording["download_url"]:
        raise HTTPException(status_code=404, detail="Download URL not available")
    
    return {
        "recording_id": recording_id,
        "download_url": recording["download_url"],
        "expires_at": recording["expires_at"].isoformat() if recording.get("expires_at") else None,
    }



# Background task for cleaning up expired recordings
async def cleanup_expired_recordings_task():
    """Background task that runs every hour to clean up expired recordings."""
    while True:
        try:
            # Sleep for 1 hour
            await asyncio.sleep(3600)
            
            # Cleanup expired recordings
            expired_ids = recording_state.cleanup_expired_recordings()
            if expired_ids:
                print(f"✓ Cleaned up {len(expired_ids)} expired recordings: {', '.join(expired_ids)}")
        except Exception as e:
            print(f"✗ Error in cleanup task: {e}")


# Manual cleanup endpoint (for testing/admin)
@app.post("/api/admin/cleanup-recordings")
async def cleanup_recordings():
    """Manually trigger cleanup of expired recordings."""
    expired_ids = recording_state.cleanup_expired_recordings()
    return {
        "success": True,
        "cleaned_count": len(expired_ids),
        "cleaned_ids": expired_ids,
    }
