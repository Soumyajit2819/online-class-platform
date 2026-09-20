from pydantic import BaseModel
from typing import Optional
from enum import Enum


class MicrophonePolicy(str, Enum):
    ALLOWED = "allowed"
    MUTED_BY_DEFAULT = "muted_by_default"
    LOCKED = "locked"


class CameraPolicy(str, Enum):
    ALLOWED = "allowed"
    OFF_BY_DEFAULT = "off_by_default"
    LOCKED = "locked"


class CreateRoomRequest(BaseModel):
    teacher_name: str
    room_name: str
    meeting_passcode: str
    max_participants: int = 50
    student_microphone_policy: MicrophonePolicy = MicrophonePolicy.ALLOWED
    student_camera_policy: CameraPolicy = CameraPolicy.ALLOWED


class JoinRoomRequest(BaseModel):
    student_name: str
    room_code: str
    meeting_passcode: str


class RoomResponse(BaseModel):
    room_code: str
    room_name: str
    token: str
    livekit_url: str


class ModerationRequest(BaseModel):
    room_code: str
    teacher_identity: str
    target_identity: Optional[str] = None


class MuteAllRequest(BaseModel):
    room_code: str
    teacher_identity: str


class SetPolicyRequest(BaseModel):
    room_code: str
    teacher_identity: str
    microphone_policy: Optional[MicrophonePolicy] = None
    camera_policy: Optional[CameraPolicy] = None


class LockClassRequest(BaseModel):
    room_code: str
    teacher_identity: str


class EndClassRequest(BaseModel):
    room_code: str
    teacher_identity: str


class StartRecordingRequest(BaseModel):
    room_code: str
    teacher_identity: str


class StopRecordingRequest(BaseModel):
    room_code: str
    recording_id: str
    teacher_identity: str
