import secrets
import hashlib
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta
import aiohttp
from livekit.api import AccessToken, VideoGrants
from livekit.api.room_service import RoomService, CreateRoomRequest
from .config import settings
from .models import MicrophonePolicy, CameraPolicy


class SessionState:
    """In-memory session state for active classes."""
    
    def __init__(self):
        self.active_classes: Dict[str, Dict[str, Any]] = {}
        self.blocked_participants: Dict[str, List[str]] = {}  # room_code -> [identities]
        self.teacher_sessions: Dict[str, str] = {}  # identity -> room_code
    
    def create_class(
        self,
        room_code: str,
        livekit_room_name: str,
        class_name: str,
        teacher_name: str,
        teacher_identity: str,
        meeting_passcode_hash: str,
        max_participants: int,
        student_microphone_policy: MicrophonePolicy,
        student_camera_policy: CameraPolicy,
    ):
        self.active_classes[room_code] = {
            "livekit_room_name": livekit_room_name,
            "class_name": class_name,
            "teacher_name": teacher_name,
            "teacher_identity": teacher_identity,
            "meeting_passcode_hash": meeting_passcode_hash,
            "max_participants": max_participants,
            "student_microphone_policy": student_microphone_policy,
            "student_camera_policy": student_camera_policy,
            "is_locked": False,
            "is_ended": False,
            "created_at": datetime.utcnow(),
        }
        self.teacher_sessions[teacher_identity] = room_code
    
    def get_class(self, room_code: str) -> Optional[Dict[str, Any]]:
        return self.active_classes.get(room_code)
    
    def is_class_active(self, room_code: str) -> bool:
        class_info = self.get_class(room_code)
        return class_info is not None and not class_info.get("is_ended", False)
    
    def is_class_locked(self, room_code: str) -> bool:
        class_info = self.get_class(room_code)
        return class_info.get("is_locked", False) if class_info else False
    
    def lock_class(self, room_code: str):
        class_info = self.get_class(room_code)
        if class_info:
            class_info["is_locked"] = True
    
    def unlock_class(self, room_code: str):
        class_info = self.get_class(room_code)
        if class_info:
            class_info["is_locked"] = False
    
    def end_class(self, room_code: str):
        class_info = self.get_class(room_code)
        if class_info:
            class_info["is_ended"] = True
    
    def verify_passcode(self, room_code: str, passcode: str) -> bool:
        class_info = self.get_class(room_code)
        if not class_info:
            return False
        passcode_hash = hashlib.sha256(passcode.encode()).hexdigest()
        return class_info["meeting_passcode_hash"] == passcode_hash
    
    def block_participant(self, room_code: str, identity: str):
        if room_code not in self.blocked_participants:
            self.blocked_participants[room_code] = []
        if identity not in self.blocked_participants[room_code]:
            self.blocked_participants[room_code].append(identity)
    
    def unblock_participant(self, room_code: str, identity: str):
        if room_code in self.blocked_participants:
            if identity in self.blocked_participants[room_code]:
                self.blocked_participants[room_code].remove(identity)
    
    def is_participant_blocked(self, room_code: str, identity: str) -> bool:
        return room_code in self.blocked_participants and identity in self.blocked_participants[room_code]
    
    def get_teacher_identity(self, room_code: str) -> Optional[str]:
        class_info = self.get_class(room_code)
        return class_info.get("teacher_identity") if class_info else None
    
    def is_teacher(self, room_code: str, identity: str) -> bool:
        teacher_identity = self.get_teacher_identity(room_code)
        return teacher_identity is not None and teacher_identity == identity
    
    def update_microphone_policy(self, room_code: str, policy: MicrophonePolicy):
        class_info = self.get_class(room_code)
        if class_info:
            class_info["student_microphone_policy"] = policy
    
    def update_camera_policy(self, room_code: str, policy: CameraPolicy):
        class_info = self.get_class(room_code)
        if class_info:
            class_info["student_camera_policy"] = policy
    
    def get_microphone_policy(self, room_code: str) -> MicrophonePolicy:
        class_info = self.get_class(room_code)
        return class_info.get("student_microphone_policy", MicrophonePolicy.ALLOWED) if class_info else MicrophonePolicy.ALLOWED
    
    def get_camera_policy(self, room_code: str) -> CameraPolicy:
        class_info = self.get_class(room_code)
        return class_info.get("student_camera_policy", CameraPolicy.ALLOWED) if class_info else CameraPolicy.ALLOWED
    
    def cleanup_class(self, room_code: str):
        if room_code in self.active_classes:
            teacher_identity = self.active_classes[room_code].get("teacher_identity")
            if teacher_identity and teacher_identity in self.teacher_sessions:
                del self.teacher_sessions[teacher_identity]
            del self.active_classes[room_code]
        if room_code in self.blocked_participants:
            del self.blocked_participants[room_code]


# Global session state
session_state = SessionState()


class LiveKitService:
    """Service for interacting with LiveKit Cloud."""
    
    def __init__(self):
        self.url = settings.LIVEKIT_URL
        self.api_key = settings.LIVEKIT_API_KEY
        self.api_secret = settings.LIVEKIT_API_SECRET
        self._session: Optional[aiohttp.ClientSession] = None
        self._room_service: Optional[RoomService] = None
    
    async def get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def get_room_service(self) -> RoomService:
        """Get or create RoomService."""
        if self._room_service is None:
            session = await self.get_session()
            self._room_service = RoomService(
                session=session,
                url=self.url,
                api_key=self.api_key,
                api_secret=self.api_secret
            )
        return self._room_service
    
    def generate_room_code(self) -> str:
        """Generate a unique application-level room code."""
        return secrets.token_urlsafe(6).upper()
    
    def generate_livekit_room_name(self) -> str:
        """Generate a secure LiveKit room name."""
        return f"class_{secrets.token_urlsafe(16)}"
    
    def hash_passcode(self, passcode: str) -> str:
        """Hash a meeting passcode for storage."""
        return hashlib.sha256(passcode.encode()).hexdigest()
    
    def create_access_token(
        self,
        identity: str,
        name: str,
        room: str,
        role: str,
        can_publish: bool = True,
        can_subscribe: bool = True,
        can_publish_data: bool = True,
        is_muted: bool = False,
        is_camera_off: bool = False,
    ) -> str:
        """Generate a LiveKit access token."""
        token = AccessToken(self.api_key, self.api_secret)
        
        # Set identity and name
        token = token.with_identity(identity).with_name(name)
        
        # Add metadata for role identification
        metadata = f"role:{role}"
        if is_muted:
            metadata += ",mic:muted"
        if is_camera_off:
            metadata += ",camera:off"
        token = token.with_metadata(metadata)
        
        # Set video grants
        grants = VideoGrants(
            room_join=True,
            room=room,
            can_publish=can_publish,
            can_subscribe=can_subscribe,
            can_publish_data=can_publish_data,
        )
        token = token.with_grants(grants)
        
        # Set token expiration (6 hours for MVP)
        token = token.with_ttl(timedelta(hours=6))
        
        return token.to_jwt()
    
    async def create_room(
        self,
        livekit_room_name: str,
        max_participants: int = 50,
    ) -> bool:
        """Create a LiveKit room."""
        try:
            room_service = await self.get_room_service()
            await room_service.create_room(CreateRoomRequest(
                name=livekit_room_name,
                max_participants=max_participants,
            ))
            return True
        except Exception as e:
            # Room might already exist, which is fine
            print(f"Room creation note: {e}")
            return True
    
    async def delete_room(self, livekit_room_name: str):
        """Delete a LiveKit room."""
        try:
            from livekit.api.room_service import DeleteRoomRequest
            room_service = await self.get_room_service()
            await room_service.delete_room(DeleteRoomRequest(room=livekit_room_name))
        except Exception as e:
            print(f"Room deletion note: {e}")
    
    async def get_participants(self, livekit_room_name: str) -> List[Dict[str, Any]]:
        """Get list of participants in a room."""
        try:
            from livekit.api.room_service import ListParticipantsRequest
            room_service = await self.get_room_service()
            participants = await room_service.list_participants(ListParticipantsRequest(room=livekit_room_name))
            return [
                {
                    "identity": p.identity,
                    "name": p.name,
                    "state": p.state.name if hasattr(p.state, 'name') else str(p.state),
                    "metadata": p.metadata,
                }
                for p in participants.participants
            ]
        except Exception as e:
            print(f"Error listing participants: {e}")
            return []
    
    async def remove_participant(self, livekit_room_name: str, identity: str):
        """Remove a participant from the room."""
        try:
            from livekit.api.room_service import RoomParticipantIdentity
            room_service = await self.get_room_service()
            await room_service.remove_participant(RoomParticipantIdentity(
                room=livekit_room_name,
                identity=identity,
            ))
        except Exception as e:
            print(f"Error removing participant: {e}")
    
    async def mute_participant(self, livekit_room_name: str, identity: str, muted: bool = True):
        """Mute/unmute a participant's microphone."""
        try:
            from livekit.api.room_service import MuteRoomTrackRequest
            room_service = await self.get_room_service()
            await room_service.mute_room_track(MuteRoomTrackRequest(
                room=livekit_room_name,
                identity=identity,
                track_sid="microphone",
                muted=muted,
            ))
        except Exception as e:
            print(f"Error muting participant: {e}")
    
    async def mute_all_students(self, livekit_room_name: str, student_identities: List[str]):
        """Mute all student microphones."""
        for identity in student_identities:
            await self.mute_participant(livekit_room_name, identity, True)
    
    async def disable_all_cameras(self, livekit_room_name: str, student_identities: List[str]):
        """Disable all student cameras."""
        for identity in student_identities:
            try:
                from livekit.api.room_service import MuteRoomTrackRequest
                room_service = await self.get_room_service()
                await room_service.mute_room_track(MuteRoomTrackRequest(
                    room=livekit_room_name,
                    identity=identity,
                    track_sid="camera",
                    muted=True,
                ))
            except Exception as e:
                print(f"Error disabling camera for {identity}: {e}")


# Global LiveKit service instance
livekit_service = LiveKitService()
