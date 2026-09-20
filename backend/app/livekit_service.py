import secrets
import hashlib
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta
import aiohttp
from livekit.api import AccessToken, VideoGrants
from livekit.api.room_service import RoomService, CreateRoomRequest
from livekit.api.egress_service import EgressService, RoomCompositeEgressRequest
from livekit.api import EncodedFileOutput, EncodedFileType
from .config import settings
from .models import MicrophonePolicy, CameraPolicy


class RecordingState:
    """In-memory state for class recordings."""
    
    def __init__(self):
        self.recordings: Dict[str, Dict[str, Any]] = {}  # recording_id -> recording_info
        self.class_recordings: Dict[str, List[str]] = {}  # room_code -> [recording_ids]
    
    def add_recording(
        self,
        room_code: str,
        recording_id: str,
        egress_id: str,
        livekit_room_name: str,
        class_name: str,
        teacher_name: str,
    ):
        """Add a new recording."""
        now = datetime.utcnow()
        expiry = now + timedelta(days=3)
        
        self.recordings[recording_id] = {
            "recording_id": recording_id,
            "egress_id": egress_id,
            "room_code": room_code,
            "livekit_room_name": livekit_room_name,
            "class_name": class_name,
            "teacher_name": teacher_name,
            "status": "starting",  # starting, recording, paused, ended, processing, available, failed
            "started_at": now,
            "ended_at": None,
            "expires_at": expiry,
            "duration_seconds": 0,
            "download_url": None,
            "file_size": 0,
        }
        
        if room_code not in self.class_recordings:
            self.class_recordings[room_code] = []
        self.class_recordings[room_code].append(recording_id)
    
    def get_recording(self, recording_id: str) -> Optional[Dict[str, Any]]:
        return self.recordings.get(recording_id)
    
    def update_recording_status(self, recording_id: str, status: str, **kwargs):
        """Update recording status and other fields."""
        if recording_id in self.recordings:
            self.recordings[recording_id]["status"] = status
            for key, value in kwargs.items():
                if key in self.recordings[recording_id]:
                    self.recordings[recording_id][key] = value
    
    def get_class_recordings(self, room_code: str) -> List[Dict[str, Any]]:
        """Get all recordings for a class."""
        recording_ids = self.class_recordings.get(room_code, [])
        return [self.recordings[rid] for rid in recording_ids if rid in self.recordings]
    
    def get_available_recordings(self) -> List[Dict[str, Any]]:
        """Get all available recordings that haven't expired."""
        now = datetime.utcnow()
        available = []
        for recording in self.recordings.values():
            if recording["status"] == "available" and recording["expires_at"] > now:
                available.append(recording)
        return available
    
    def cleanup_expired_recordings(self) -> List[str]:
        """Remove expired recordings and return their IDs."""
        now = datetime.utcnow()
        expired_ids = []
        for recording_id, recording in list(self.recordings.items()):
            if recording["expires_at"] <= now:
                expired_ids.append(recording_id)
                del self.recordings[recording_id]
                # Remove from class_recordings
                room_code = recording["room_code"]
                if room_code in self.class_recordings:
                    self.class_recordings[room_code] = [
                        rid for rid in self.class_recordings[room_code] if rid != recording_id
                    ]
        return expired_ids


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
            "is_recording": False,
            "active_recording_id": None,
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
    
    def is_recording(self, room_code: str) -> bool:
        class_info = self.get_class(room_code)
        return class_info.get("is_recording", False) if class_info else False
    
    def set_recording(self, room_code: str, is_recording: bool, recording_id: Optional[str] = None):
        class_info = self.get_class(room_code)
        if class_info:
            class_info["is_recording"] = is_recording
            class_info["active_recording_id"] = recording_id
    
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
recording_state = RecordingState()


class LiveKitService:
    """Service for interacting with LiveKit Cloud."""
    
    def __init__(self):
        self.url = settings.LIVEKIT_URL
        self.api_key = settings.LIVEKIT_API_KEY
        self.api_secret = settings.LIVEKIT_API_SECRET
        self._session: Optional[aiohttp.ClientSession] = None
        self._room_service: Optional[RoomService] = None
        self._egress_service: Optional[EgressService] = None
    
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
    
    async def get_egress_service(self) -> EgressService:
        """Get or create EgressService."""
        if self._egress_service is None:
            session = await self.get_session()
            self._egress_service = EgressService(
                session=session,
                url=self.url,
                api_key=self.api_key,
                api_secret=self.api_secret
            )
        return self._egress_service
    
    def generate_room_code(self) -> str:
        """Generate a unique application-level room code."""
        return secrets.token_urlsafe(6).upper()
    
    def generate_livekit_room_name(self) -> str:
        """Generate a secure LiveKit room name."""
        return f"class_{secrets.token_urlsafe(16)}"
    
    def generate_recording_id(self) -> str:
        """Generate a unique recording ID."""
        return f"rec_{secrets.token_urlsafe(8)}"
    
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
    
    # Recording methods
    
    async def start_recording(
        self,
        room_code: str,
        livekit_room_name: str,
        class_name: str,
        teacher_name: str,
    ) -> Dict[str, Any]:
        """Start recording a class."""
        try:
            recording_id = self.generate_recording_id()
            egress_service = await self.get_egress_service()
            
            # Create room composite egress request for recording
            # This records all participants' video and audio
            request = RoomCompositeEgressRequest(
                room_name=livekit_room_name,
                # Output to MP4 file
                file=EncodedFileOutput(
                    file_type=EncodedFileType.MP4,
                    filepath=f"recordings/{room_code}/{recording_id}.mp4",
                    # For MVP, we'll use LiveKit's built-in storage
                    # In production, you'd configure S3/GCP/Azure upload
                    # s3=S3Upload(...) if you have S3 configured
                )
            )
            
            # Start the egress
            egress_info = await egress_service.start_room_composite_egress(request)
            
            # Store recording state
            recording_state.add_recording(
                room_code=room_code,
                recording_id=recording_id,
                egress_id=egress_info.egress_id,
                livekit_room_name=livekit_room_name,
                class_name=class_name,
                teacher_name=teacher_name,
            )
            
            # Update class state
            session_state.set_recording(room_code, True, recording_id)
            
            return {
                "success": True,
                "recording_id": recording_id,
                "egress_id": egress_info.egress_id,
                "status": "recording",
            }
        except Exception as e:
            print(f"Error starting recording: {e}")
            return {
                "success": False,
                "error": str(e),
            }
    
    async def stop_recording(self, room_code: str, recording_id: str) -> Dict[str, Any]:
        """Stop recording a class."""
        try:
            recording = recording_state.get_recording(recording_id)
            if not recording:
                return {"success": False, "error": "Recording not found"}
            
            egress_service = await self.get_egress_service()
            
            # Stop the egress
            from livekit.api.egress_service import StopEgressRequest
            egress_info = await egress_service.stop_egress(StopEgressRequest(
                egress_id=recording["egress_id"]
            ))
            
            # Update recording state
            recording_state.update_recording_status(
                recording_id,
                status="ended",
                ended_at=datetime.utcnow(),
                download_url=egress_info.file.location if hasattr(egress_info, 'file') else None,
            )
            
            # Update class state
            session_state.set_recording(room_code, False, None)
            
            return {
                "success": True,
                "recording_id": recording_id,
                "status": "ended",
                "download_url": egress_info.file.location if hasattr(egress_info, 'file') else None,
            }
        except Exception as e:
            print(f"Error stopping recording: {e}")
            return {
                "success": False,
                "error": str(e),
            }
    
    async def get_recording_status(self, recording_id: str) -> Dict[str, Any]:
        """Get the status of a recording."""
        try:
            recording = recording_state.get_recording(recording_id)
            if not recording:
                return {"success": False, "error": "Recording not found"}
            
            # If recording is active, check LiveKit status
            if recording["status"] in ["starting", "recording", "paused"]:
                egress_service = await self.get_egress_service()
                from livekit.api.egress_service import ListEgressRequest
                egress_list = await egress_service.list_egress(ListEgressRequest(
                    room_name=recording["livekit_room_name"],
                    egress_id=recording["egress_id"],
                ))
                
                if egress_list.items:
                    egress_info = egress_list.items[0]
                    # Update status based on LiveKit response
                    status_map = {
                        "EGRESS_STARTING": "starting",
                        "EGRESS_ACTIVE": "recording",
                        "EGRESS_ENDING": "ending",
                        "EGRESS_COMPLETE": "available",
                        "EGRESS_FAILED": "failed",
                        "EGRESS_ABORTED": "failed",
                    }
                    new_status = status_map.get(egress_info.status.name, recording["status"])
                    
                    recording_state.update_recording_status(
                        recording_id,
                        status=new_status,
                        download_url=egress_info.file.location if hasattr(egress_info, 'file') and egress_info.file else None,
                    )
            
            return {
                "success": True,
                "recording": recording_state.get_recording(recording_id),
            }
        except Exception as e:
            print(f"Error getting recording status: {e}")
            return {
                "success": False,
                "error": str(e),
            }
    
    def get_class_recordings(self, room_code: str) -> List[Dict[str, Any]]:
        """Get all recordings for a class."""
        return recording_state.get_class_recordings(room_code)
    
    def get_all_available_recordings(self) -> List[Dict[str, Any]]:
        """Get all available recordings."""
        return recording_state.get_available_recordings()


# Global LiveKit service instance
livekit_service = LiveKitService()
