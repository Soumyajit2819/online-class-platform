from pydantic import BaseModel, Field
from typing import Optional, Literal
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
    meeting_passcode: Optional[str] = None
    max_participants: int = 50
    student_microphone_policy: MicrophonePolicy = MicrophonePolicy.ALLOWED
    student_camera_policy: CameraPolicy = CameraPolicy.ALLOWED


class JoinRoomRequest(BaseModel):
    student_name: str
    room_code: str
    meeting_passcode: str


class JoinRequestStatus(str, Enum):
    WAITING = "WAITING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class CreateJoinRequest(BaseModel):
    google_credential: str
    meeting_passcode: Optional[str] = None
    room_code: Optional[str] = None
    invite_code: Optional[str] = None
    session_id: str


class JoinRequestTokenRequest(BaseModel):
    session_id: str


class ClassroomChatAction(BaseModel):
    action: Literal["snapshot", "set_enabled", "send_message"]
    teacher_identity: Optional[str] = None
    teacher_access_key: Optional[str] = None
    join_request_id: Optional[str] = None
    session_id: Optional[str] = None
    enabled: Optional[bool] = None
    text: Optional[str] = None


class JoinRequestDecision(BaseModel):
    room_code: str
    teacher_identity: str
    teacher_access_key: str
    request_id: str


class RoomResponse(BaseModel):
    room_code: str
    room_name: str
    token: str
    livekit_url: str
    invite_code: Optional[str] = None
    teacher_access_key: Optional[str] = None


class ModerationRequest(BaseModel):
    room_code: str
    teacher_identity: str
    target_identity: Optional[str] = None


class StudentMuteRestrictionRequest(BaseModel):
    room_code: str
    teacher_identity: str
    target_identity: str
    duration_minutes: Optional[Literal[1, 5, 10, 15, 30]] = None


class StudentUnmuteRestrictionRequest(BaseModel):
    room_code: str
    teacher_identity: str
    target_identity: str


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
    teacher_access_key: str


class TranscriptAccessRequest(BaseModel):
    google_credential: Optional[str] = Field(default=None, max_length=8192)
    teacher_identity: Optional[str] = Field(default=None, max_length=128)
    teacher_access_key: Optional[str] = Field(default=None, min_length=1, max_length=256)


class ExamManagementPasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class StartRecordingRequest(BaseModel):
    room_code: str
    teacher_identity: str


class StopRecordingRequest(BaseModel):
    room_code: str
    recording_id: str
    teacher_identity: str
