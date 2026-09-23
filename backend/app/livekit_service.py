import secrets
import hashlib
import asyncio
import hmac
import base64
import json
import posixpath
from pathlib import Path
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta, timezone
import aiohttp
from livekit.api import AccessToken, VideoGrants, WebhookReceiver, TokenVerifier
from livekit.api.room_service import RoomService, CreateRoomRequest
from livekit.api.egress_service import EgressService, RoomCompositeEgressRequest
from livekit.api import (
    SegmentedFileOutput, SegmentedFileProtocol, S3Upload, EncodingOptions,
    AudioCodec, VideoCodec,
)
from livekit.protocol.egress import EgressStatus, ListEgressRequest, StopEgressRequest
from livekit.protocol.webhook import WebhookEvent
from google.protobuf.json_format import MessageToDict, ParseDict
from .config import settings
from .models import MicrophonePolicy, CameraPolicy


# ---------------------------------------------------------------------------
# Recording State
# ---------------------------------------------------------------------------

class RecordingState:
    """Small cache for active sessions; Supabase is the recording source of truth."""

    def __init__(self):
        self.recordings: Dict[str, Dict[str, Any]] = {}       # recording_id -> info
        self.class_recordings: Dict[str, List[str]] = {}      # room_code -> [recording_ids]

    def add_recording(self, room_code: str, recording_id: str, egress_id: str,
                      livekit_room_name: str, class_name: str, teacher_name: str):
        now = datetime.utcnow()
        self.recordings[recording_id] = {
            "recording_id":      recording_id,
            "egress_id":         egress_id,
            "room_code":         room_code,
            "livekit_room_name": livekit_room_name,
            "class_name":        class_name,
            "teacher_name":      teacher_name,
            "status":            "recording",
            "started_at":        now,
            "ended_at":          None,
        }
        self.class_recordings.setdefault(room_code, []).append(recording_id)

    def get_recording(self, recording_id: str) -> Optional[Dict[str, Any]]:
        return self.recordings.get(recording_id)

    def update_status(self, recording_id: str, status: str, ended_at: Optional[datetime] = None):
        rec = self.recordings.get(recording_id)
        if rec:
            rec["status"] = status
            if ended_at:
                rec["ended_at"] = ended_at

# ---------------------------------------------------------------------------
# Session State
# ---------------------------------------------------------------------------

class SessionState:
    """In-memory state for active class sessions."""

    def __init__(self):
        self.active_classes: Dict[str, Dict[str, Any]] = {}
        self.blocked_participants: Dict[str, List[str]] = {}
        self.teacher_sessions: Dict[str, str] = {}
        self.invite_codes: Dict[str, str] = {}
        self.join_requests: Dict[str, Dict[str, Any]] = {}
        self.join_request_sessions: Dict[tuple[str, str], str] = {}
        self.microphone_restrictions: Dict[tuple[str, str], Dict[str, Any]] = {}

    def create_class(self, room_code, livekit_room_name, class_name, teacher_name,
                     teacher_identity, meeting_passcode_hash, max_participants,
                     student_microphone_policy, student_camera_policy, invite_code,
                     teacher_access_key):
        self.active_classes[room_code] = {
            "livekit_room_name":        livekit_room_name,
            "class_name":               class_name,
            "teacher_name":             teacher_name,
            "teacher_identity":         teacher_identity,
            "meeting_passcode_hash":    meeting_passcode_hash,
            "max_participants":         max_participants,
            "student_microphone_policy": student_microphone_policy,
            "student_camera_policy":    student_camera_policy,
            "is_locked":                False,
            "is_ended":                 False,
            "is_recording":             False,
            "active_recording_id":      None,
            "created_at":               datetime.utcnow(),
            "invite_code":              invite_code,
            "teacher_access_key":       teacher_access_key,
            "chat_enabled":              True,
            "chat_messages":             [],
        }
        self.teacher_sessions[teacher_identity] = room_code
        self.invite_codes[invite_code] = room_code

    def get_class(self, room_code: str) -> Optional[Dict[str, Any]]:
        return self.active_classes.get(room_code)

    def is_class_active(self, room_code: str) -> bool:
        c = self.get_class(room_code)
        return c is not None and not c.get("is_ended", False)

    def is_class_locked(self, room_code: str) -> bool:
        c = self.get_class(room_code)
        return c.get("is_locked", False) if c else False

    def is_recording(self, room_code: str) -> bool:
        c = self.get_class(room_code)
        return c.get("is_recording", False) if c else False

    def set_recording(self, room_code: str, is_rec: bool, recording_id: Optional[str] = None):
        c = self.get_class(room_code)
        if c:
            c["is_recording"]       = is_rec
            c["active_recording_id"] = recording_id

    def lock_class(self, room_code: str):
        c = self.get_class(room_code)
        if c: c["is_locked"] = True

    def unlock_class(self, room_code: str):
        c = self.get_class(room_code)
        if c: c["is_locked"] = False

    def end_class(self, room_code: str):
        c = self.get_class(room_code)
        if c: c["is_ended"] = True

    def verify_passcode(self, room_code: str, passcode: str) -> bool:
        c = self.get_class(room_code)
        if not c: return False
        if c["meeting_passcode_hash"] is None:
            return not (passcode or "").strip()
        if not passcode: return False
        return c["meeting_passcode_hash"] == hashlib.sha256(passcode.encode()).hexdigest()

    def block_participant(self, room_code: str, identity: str):
        self.blocked_participants.setdefault(room_code, [])
        if identity not in self.blocked_participants[room_code]:
            self.blocked_participants[room_code].append(identity)

    def unblock_participant(self, room_code: str, identity: str):
        if room_code in self.blocked_participants:
            self.blocked_participants[room_code] = [
                x for x in self.blocked_participants[room_code] if x != identity
            ]

    def is_participant_blocked(self, room_code: str, identity: str) -> bool:
        return identity in self.blocked_participants.get(room_code, [])

    def set_microphone_restriction(self, room_code: str, identity: str,
                                   duration_minutes: Optional[int]) -> Dict[str, Any]:
        # These values cross the API boundary.  Keep them timezone-aware so an
        # ISO value is always interpreted as UTC by browser clients.
        now = datetime.now(timezone.utc)
        restriction = {
            "room_code": room_code,
            "identity": identity,
            "mode": "TIMED" if duration_minutes else "UNTIL_TEACHER",
            "expires_at": now + timedelta(minutes=duration_minutes) if duration_minutes else None,
            "updated_at": now,
            "enforced_sessions": set(),
        }
        self.microphone_restrictions[(room_code, identity)] = restriction
        return restriction

    def get_microphone_restriction(self, room_code: str, identity: str) -> Optional[Dict[str, Any]]:
        # Reading a restriction must not expire it.  The timed-expiry callback
        # owns the transition so it can restore LiveKit permission atomically
        # with removing the server-side restriction.
        return self.microphone_restrictions.get((room_code, identity))

    def microphone_restriction_enforced(self, room_code: str, identity: str, session_id: str) -> bool:
        restriction = self.get_microphone_restriction(room_code, identity)
        return bool(restriction and session_id in restriction.setdefault("enforced_sessions", set()))

    def mark_microphone_restriction_enforced(self, room_code: str, identity: str, session_id: str) -> None:
        restriction = self.get_microphone_restriction(room_code, identity)
        if restriction:
            restriction.setdefault("enforced_sessions", set()).add(session_id)

    def reset_microphone_restriction_enforcement(self, room_code: str, identity: str, session_id: str) -> None:
        restriction = self.get_microphone_restriction(room_code, identity)
        if restriction:
            restriction.setdefault("enforced_sessions", set()).discard(session_id)

    def expire_microphone_restriction(self, room_code: str, identity: str,
                                      expected_expires_at: Optional[datetime] = None) -> bool:
        key = (room_code, identity)
        restriction = self.microphone_restrictions.get(key)
        if expected_expires_at is not None and (
            not restriction or restriction["expires_at"] != expected_expires_at
        ):
            return False
        if restriction and restriction["expires_at"] and restriction["expires_at"] <= datetime.now(timezone.utc):
            del self.microphone_restrictions[key]
            return True
        return False

    def clear_microphone_restriction(self, room_code: str, identity: str) -> bool:
        return self.microphone_restrictions.pop((room_code, identity), None) is not None

    def get_class_microphone_restrictions(self, room_code: str) -> Dict[str, Dict[str, Any]]:
        active = {}
        for (request_room_code, identity) in list(self.microphone_restrictions):
            if request_room_code != room_code:
                continue
            restriction = self.get_microphone_restriction(room_code, identity)
            if restriction:
                active[identity] = restriction
        return active

    def get_room_code_for_invite(self, invite_code: str) -> Optional[str]:
        return self.invite_codes.get(invite_code)

    def get_join_request_for_session(self, room_code: str, session_id: str) -> Optional[Dict[str, Any]]:
        request_id = self.join_request_sessions.get((room_code, session_id))
        return self.join_requests.get(request_id) if request_id else None

    def create_join_request(self, room_code: str, student_name: str, session_id: str,
                            student_identity: Optional[str] = None) -> Dict[str, Any]:
        existing = self.get_join_request_for_session(room_code, session_id)
        if existing:
            return existing
        request_id = f"jr_{secrets.token_urlsafe(16)}"
        request = {
            "request_id": request_id,
            "room_code": room_code,
            "student_name": student_name.strip(),
            "student_identity": student_identity or f"student_{secrets.token_urlsafe(8)}",
            "session_id": session_id,
            "status": "WAITING",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "decided_at": None,
        }
        self.join_requests[request_id] = request
        self.join_request_sessions[(room_code, session_id)] = request_id
        return request

    def get_join_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self.join_requests.get(request_id)

    def get_waiting_join_requests(self, room_code: str) -> List[Dict[str, Any]]:
        return [r for r in self.join_requests.values()
                if r["room_code"] == room_code and r["status"] == "WAITING"]

    def decide_join_request(self, request_id: str, status: str) -> Optional[Dict[str, Any]]:
        request = self.get_join_request(request_id)
        if not request or request["status"] != "WAITING":
            return None
        request["status"] = status
        request["updated_at"] = datetime.utcnow()
        request["decided_at"] = request["updated_at"]
        return request

    def get_teacher_identity(self, room_code: str) -> Optional[str]:
        c = self.get_class(room_code)
        return c.get("teacher_identity") if c else None

    def is_teacher(self, room_code: str, identity: str) -> bool:
        return self.get_teacher_identity(room_code) == identity

    def update_microphone_policy(self, room_code: str, policy: MicrophonePolicy):
        c = self.get_class(room_code)
        if c: c["student_microphone_policy"] = policy

    def update_camera_policy(self, room_code: str, policy: CameraPolicy):
        c = self.get_class(room_code)
        if c: c["student_camera_policy"] = policy

    def get_microphone_policy(self, room_code: str) -> MicrophonePolicy:
        c = self.get_class(room_code)
        return c.get("student_microphone_policy", MicrophonePolicy.ALLOWED) if c else MicrophonePolicy.ALLOWED

    def get_camera_policy(self, room_code: str) -> CameraPolicy:
        c = self.get_class(room_code)
        return c.get("student_camera_policy", CameraPolicy.ALLOWED) if c else CameraPolicy.ALLOWED

    def set_chat_enabled(self, room_code: str, enabled: bool) -> bool:
        c = self.get_class(room_code)
        if not c:
            return False
        c["chat_enabled"] = enabled
        return True

    def add_chat_message(self, room_code: str, name: str, text: str) -> Optional[Dict[str, Any]]:
        c = self.get_class(room_code)
        if not c or not c["chat_enabled"]:
            return None
        message = {
            "id": f"chat_{secrets.token_urlsafe(10)}",
            "name": name,
            "text": text,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        c["chat_messages"].append(message)
        c["chat_messages"] = c["chat_messages"][-200:]
        return message

    def get_chat(self, room_code: str) -> Optional[Dict[str, Any]]:
        c = self.get_class(room_code)
        if not c:
            return None
        return {"enabled": c["chat_enabled"], "messages": list(c["chat_messages"])}


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

session_state   = SessionState()
recording_state = RecordingState()


# ---------------------------------------------------------------------------
# LiveKit Service
# ---------------------------------------------------------------------------

class LiveKitService:
    """All LiveKit + Supabase S3 operations."""

    def __init__(self):
        self.url            = settings.LIVEKIT_URL
        self.api_key        = settings.LIVEKIT_API_KEY
        self.api_secret     = settings.LIVEKIT_API_SECRET
        self.s3_endpoint    = settings.SUPABASE_S3_ENDPOINT
        self.s3_access_key  = settings.SUPABASE_S3_ACCESS_KEY
        self.s3_secret_key  = settings.SUPABASE_S3_SECRET_KEY
        self.s3_region      = settings.SUPABASE_S3_REGION
        self.s3_bucket      = settings.SUPABASE_S3_BUCKET
        self._session:        Optional[aiohttp.ClientSession] = None
        self._room_service:   Optional[RoomService]           = None
        self._egress_service: Optional[EgressService]         = None

    # ---- internal helpers ------------------------------------------------

    async def _session_(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_room_service(self) -> RoomService:
        if self._room_service is None:
            s = await self._session_()
            self._room_service = RoomService(
                session=s, url=self.url,
                api_key=self.api_key, api_secret=self.api_secret)
        return self._room_service

    async def get_egress_service(self) -> EgressService:
        if self._egress_service is None:
            s = await self._session_()
            self._egress_service = EgressService(
                session=s, url=self.url,
                api_key=self.api_key, api_secret=self.api_secret)
        return self._egress_service

    def _s3_client(self):
        """Return a boto3 S3 client configured for Supabase."""
        import boto3
        from botocore.config import Config
        return boto3.client(
            's3',
            endpoint_url=self.s3_endpoint,
            aws_access_key_id=self.s3_access_key,
            aws_secret_access_key=self.s3_secret_key,
            region_name=self.s3_region,
            config=Config(signature_version='s3v4'),
        )

    async def _run_in_thread(self, fn):
        """Run a sync callable in a thread pool (keeps async loop free)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, fn)

    # ---- startup check ---------------------------------------------------

    async def ensure_bucket_exists(self) -> bool:
        """Verify Supabase bucket is reachable on startup."""
        from botocore.exceptions import ClientError

        def _check():
            try:
                s3 = self._s3_client()
                s3.head_bucket(Bucket=self.s3_bucket)
                return True, f"✓ Supabase bucket '{self.s3_bucket}' is ready for recordings"
            except ClientError as e:
                code = e.response['Error']['Code']
                if code in ('403', '200'):
                    return True, f"✓ Supabase bucket '{self.s3_bucket}' exists"
                return False, f"⚠ Bucket '{self.s3_bucket}' not found — create it in Supabase → Storage"
            except Exception as e:
                return False, f"⚠ Storage check error: {e}"

        try:
            ok, msg = await self._run_in_thread(_check)
            print(msg)
            return ok
        except ImportError:
            print("⚠ boto3 not installed — run: pip install boto3")
            return False

    # ---- generators ------------------------------------------------------

    def generate_room_code(self)      -> str: return secrets.token_urlsafe(6).upper()
    def generate_livekit_room_name(self) -> str: return f"class_{secrets.token_urlsafe(16)}"
    def generate_recording_id(self)   -> str: return f"rec_{secrets.token_urlsafe(8)}"
    def hash_passcode(self, p: str)   -> str: return hashlib.sha256(p.encode()).hexdigest()

    # ---- token -----------------------------------------------------------

    def create_access_token(self, identity, name, room, role,
                             can_publish=True, can_subscribe=True,
                             can_publish_data=True,
                             is_muted=False, is_camera_off=False,
                             can_publish_sources: Optional[List[str]] = None) -> str:
        token = (AccessToken(self.api_key, self.api_secret)
                 .with_identity(identity)
                 .with_name(name)
                 .with_metadata(f"role:{role}" +
                                (",mic:muted"   if is_muted      else "") +
                                (",camera:off"  if is_camera_off else ""))
                 .with_grants(VideoGrants(
                     room_join=True, room=room,
                     can_publish=can_publish,
                     can_subscribe=can_subscribe,
                     can_publish_data=can_publish_data,
                     can_publish_sources=can_publish_sources))
                 .with_ttl(timedelta(hours=6)))
        return token.to_jwt()

    # ---- room ------------------------------------------------------------

    async def create_room(self, livekit_room_name: str, max_participants: int = 50) -> bool:
        try:
            rs = await self.get_room_service()
            await rs.create_room(CreateRoomRequest(name=livekit_room_name, max_participants=max_participants))
            return True
        except Exception as e:
            print(f"Room creation note: {e}")
            return True

    async def delete_room(self, livekit_room_name: str):
        try:
            from livekit.api.room_service import DeleteRoomRequest
            rs = await self.get_room_service()
            await rs.delete_room(DeleteRoomRequest(room=livekit_room_name))
        except Exception as e:
            print(f"Room deletion note: {e}")

    async def get_participants(self, livekit_room_name: str) -> List[Dict[str, Any]]:
        try:
            from livekit.api.room_service import ListParticipantsRequest
            rs = await self.get_room_service()
            resp = await rs.list_participants(ListParticipantsRequest(room=livekit_room_name))
            return [{"identity": p.identity, "name": p.name,
                     "state": p.state.name if hasattr(p.state, 'name') else str(p.state),
                     "metadata": p.metadata}
                    for p in resp.participants]
        except Exception as e:
            print(f"Error listing participants: {e}")
            return []

    async def remove_participant(self, livekit_room_name: str, identity: str):
        try:
            from livekit.api.room_service import RoomParticipantIdentity
            rs = await self.get_room_service()
            await rs.remove_participant(RoomParticipantIdentity(room=livekit_room_name, identity=identity))
        except Exception as e:
            print(f"Error removing participant: {e}")

    async def mute_participant(self, livekit_room_name: str, identity: str, muted: bool = True):
        """
        Mute/unmute a participant's microphone.
        Fetches the real track SID first, then calls mute_published_track.
        """
        try:
            from livekit.api.room_service import MuteRoomTrackRequest, ListParticipantsRequest
            rs   = await self.get_room_service()
            resp = await rs.list_participants(ListParticipantsRequest(room=livekit_room_name))

            participant = next((p for p in resp.participants if p.identity == identity), None)
            if not participant:
                print(f"Participant {identity} not found")
                return

            # Find microphone track: type=0 (AUDIO) + source=2 (MICROPHONE)
            audio_tracks = [t for t in participant.tracks if t.type == 0 and t.source == 2]
            if not audio_tracks:
                audio_tracks = [t for t in participant.tracks if t.type == 0]

            if not audio_tracks:
                print(f"No audio tracks found for {identity}")
                return

            for track in audio_tracks:
                print(f"  {'Muting' if muted else 'Unmuting'} {track.sid} for {identity}")
                await rs.mute_published_track(MuteRoomTrackRequest(
                    room=livekit_room_name,
                    identity=identity,
                    track_sid=track.sid,
                    muted=muted,
                ))

        except Exception as e:
            print(f"Error muting participant {identity}: {e}")

    async def set_microphone_publish_permission(self, livekit_room_name: str,
                                                 identity: str, allowed: bool):
        """Change only microphone publishing and verify LiveKit accepted it."""
        from livekit.api.room_service import (
            UpdateParticipantRequest, RoomParticipantIdentity,
        )
        from livekit.protocol.models import ParticipantPermission, TrackSource

        rs = await self.get_room_service()
        participant = await rs.get_participant(RoomParticipantIdentity(
            room=livekit_room_name, identity=identity))
        current = participant.permission
        sources = list(current.can_publish_sources)
        if not sources:
            # Compatibility fallback for participants who joined before
            # source-level grants were introduced.
            sources = [TrackSource.CAMERA, TrackSource.SCREEN_SHARE, TrackSource.SCREEN_SHARE_AUDIO]
            if current.can_publish:
                sources.append(TrackSource.MICROPHONE)
        sources = [source for source in sources if source != TrackSource.MICROPHONE]
        if allowed:
            sources.append(TrackSource.MICROPHONE)

        updated = await rs.update_participant(UpdateParticipantRequest(
            room=livekit_room_name,
            identity=identity,
            permission=ParticipantPermission(
                can_subscribe=current.can_subscribe,
                can_publish=True,
                can_publish_data=current.can_publish_data,
                can_publish_sources=sources,
            ),
        ))
        if allowed and TrackSource.MICROPHONE not in updated.permission.can_publish_sources:
            raise RuntimeError(
                f"LiveKit did not restore microphone publish permission for {identity}")
        if not allowed and TrackSource.MICROPHONE in updated.permission.can_publish_sources:
            raise RuntimeError(
                f"LiveKit did not remove microphone publish permission for {identity}")
        return updated

    async def mute_all_students(self, livekit_room_name: str, identities: List[str]):
        for identity in identities:
            await self.mute_participant(livekit_room_name, identity, True)

    async def disable_all_cameras(self, livekit_room_name: str, identities: List[str]):
        """Disable camera tracks. Fetches real track SIDs first."""
        try:
            from livekit.api.room_service import MuteRoomTrackRequest, ListParticipantsRequest
            rs   = await self.get_room_service()
            resp = await rs.list_participants(ListParticipantsRequest(room=livekit_room_name))

            for participant in resp.participants:
                if participant.identity not in identities:
                    continue

                # type=1 (VIDEO) + source=1 (CAMERA)
                video_tracks = [t for t in participant.tracks if t.type == 1 and t.source == 1]
                if not video_tracks:
                    video_tracks = [t for t in participant.tracks if t.type == 1]

                for track in video_tracks:
                    print(f"  Disabling camera {track.sid} for {participant.identity}")
                    try:
                        await rs.mute_published_track(MuteRoomTrackRequest(
                            room=livekit_room_name,
                            identity=participant.identity,
                            track_sid=track.sid,
                            muted=True,
                        ))
                    except Exception as e:
                        print(f"  Error: {e}")

        except Exception as e:
            print(f"Error disabling cameras: {e}")

    # ---- recording -------------------------------------------------------

    def _recordings_table(self):
        from supabase import create_client
        return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY).table("recordings")

    async def _recording_query(self, operation):
        return await self._run_in_thread(operation)

    async def _create_recording_metadata(self, record: Dict[str, Any]):
        await self._recording_query(lambda: self._recordings_table().insert(record).execute())

    async def _update_recording_metadata(self, recording_id: str, values: Dict[str, Any]):
        await self._recording_query(lambda: self._recordings_table().update(values).eq("recording_id", recording_id).execute())

    async def get_recording_metadata(self, recording_id: str) -> Optional[Dict[str, Any]]:
        response = await self._recording_query(
            lambda: self._recordings_table().select("*").eq("recording_id", recording_id).limit(1).execute())
        return response.data[0] if response.data else None

    def _s3_upload(self) -> S3Upload:
        return S3Upload(access_key=self.s3_access_key, secret=self.s3_secret_key,
                        region=self.s3_region, bucket=self.s3_bucket,
                        endpoint=self.s3_endpoint, force_path_style=True)

    async def start_recording(self, room_code: str, livekit_room_name: str,
                               class_name: str, teacher_name: str) -> Dict[str, Any]:
        """Start one continuous HLS egress; metadata is committed before Egress starts."""
        recording_id = self.generate_recording_id()
        prefix = f"recordings/{room_code}/{recording_id}/"
        now = datetime.now(timezone.utc)
        record = {
            "recording_id": recording_id, "room_code": room_code,
            "class_name": class_name, "teacher_name": teacher_name,
            "storage_prefix": prefix, "playlist_key": f"{prefix}index.m3u8",
            "egress_id": None, "started_at": now.isoformat(), "ended_at": None,
            "expires_at": (now + timedelta(hours=20)).isoformat(), "status": "starting",
            "livekit_room_name": livekit_room_name,
        }
        try:
            await self._create_recording_metadata(record)
            output = SegmentedFileOutput(
                protocol=SegmentedFileProtocol.HLS_PROTOCOL,
                filename_prefix=f"{prefix}segment",
                playlist_name=f"{prefix}index.m3u8",
                live_playlist_name=f"{prefix}live.m3u8",
                segment_duration=8,
                s3=self._s3_upload(),
            )
            request = RoomCompositeEgressRequest(
                room_name=livekit_room_name,
                segment_outputs=[output],
                advanced=EncodingOptions(width=640, height=360, framerate=24,
                                         video_codec=VideoCodec.H264_MAIN,
                                         video_bitrate=250, audio_codec=AudioCodec.AAC,
                                         audio_bitrate=40, audio_frequency=48000,
                                         key_frame_interval=8),
            )
            egress = await (await self.get_egress_service()).start_room_composite_egress(request)
            egress_status = int(getattr(egress, "status", EgressStatus.EGRESS_STARTING))
            if egress_status in (EgressStatus.EGRESS_FAILED, EgressStatus.EGRESS_ABORTED,
                                 EgressStatus.EGRESS_LIMIT_REACHED):
                await self._update_recording_metadata(recording_id, {
                    "egress_id": egress.egress_id, "status": "failed",
                    "finalization_last_error": getattr(egress, "error", "Egress failed during start"),
                })
                return {"success": False, "recording_id": recording_id,
                        "error": getattr(egress, "error", "LiveKit Egress failed to start")}
            app_status = "recording" if egress_status == EgressStatus.EGRESS_ACTIVE else "starting"
            await self._update_recording_metadata(recording_id, {
                "egress_id": egress.egress_id, "status": app_status,
                "finalization_last_error": None,
            })
            recording_state.add_recording(room_code, recording_id, egress.egress_id,
                                          livekit_room_name, class_name, teacher_name)
            session_state.set_recording(room_code, True, recording_id)
            try:
                await self._associate_recording(record)
            except Exception as association_error:
                # Recording must remain usable if class-history persistence is
                # temporarily unavailable. Reconciliation retries association.
                print(f"Could not associate recording with meeting yet: {association_error}")
            return {"success": True, "recording_id": recording_id, "egress_id": egress.egress_id, "status": app_status}
        except Exception as e:
            # A start RPC can time out after LiveKit accepted the request.
            # Retain the durable starting row so reconciliation can discover an
            # Egress by room name instead of deleting evidence of a live job.
            try:
                await self._update_recording_metadata(recording_id, {
                    "finalization_last_error": f"Egress start outcome unknown: {e}",
                    "finalization_next_attempt_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                })
            except Exception as cleanup_error:
                print(f"Could not persist uncertain Egress start state: {cleanup_error}")
            print(f"Error starting HLS recording: {e}")
            return {"success": False, "error": str(e)}

    async def stop_recording(self, room_code: str, recording_id: str) -> Dict[str, Any]:
        try:
            record = await self.get_recording_metadata(recording_id)
            if not record or record["room_code"] != room_code or not record.get("egress_id"):
                return {"success": False, "error": "Recording not found"}
            now = datetime.now(timezone.utc)
            await self._update_recording_metadata(recording_id, {
                "status": "processing", "stop_requested_at": now.isoformat(),
                "finalization_next_attempt_at": now.isoformat(),
                "finalization_lease_until": None,
            })
            egress_info = await (await self.get_egress_service()).stop_egress(
                StopEgressRequest(egress_id=record["egress_id"]))
            ended = datetime.now(timezone.utc)
            await self._update_recording_metadata(recording_id, {
                "status": "processing", "ended_at": ended.isoformat(),
                "expires_at": (ended + timedelta(hours=20)).isoformat(),
                "finalization_lease_until": None,
            })
            recording_state.update_status(recording_id, "processing", datetime.utcnow())
            session_state.set_recording(room_code, False, None)
            status = int(egress_info.status)
            if status in (EgressStatus.EGRESS_FAILED, EgressStatus.EGRESS_ABORTED,
                          EgressStatus.EGRESS_LIMIT_REACHED):
                await self._mark_egress_failed(recording_id, egress_info)
                return {"success": False, "recording_id": recording_id, "status": "failed",
                        "error": getattr(egress_info, "error", "LiveKit Egress failed")}
            if status == EgressStatus.EGRESS_COMPLETE:
                await self._finalize_egress(record, egress_info)
                current = await self.get_recording_metadata(recording_id)
                return {"success": True, "recording_id": recording_id,
                        "status": current["status"] if current else "processing",
                        "message": "Recording stop acknowledged; final output is being verified."}
            return {"success": True, "recording_id": recording_id, "status": "processing",
                    "message": "Recording stop requested; final output is being verified asynchronously."}
        except Exception as e:
            # Keep a durable processing row and let the worker retry the stop
            # or reconcile Egress state. The class-ending route can continue.
            try:
                await self._update_recording_metadata(recording_id, {
                    "status": "processing",
                    "finalization_last_error": f"Stop request failed: {e}",
                    "finalization_next_attempt_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                    "finalization_lease_until": None,
                })
            except Exception:
                pass
            print(f"Error stopping recording: {e}")
            return {"success": False, "error": str(e)}

    async def list_recordings(self) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        response = await self._recording_query(
            lambda: self._recordings_table().select("*").gt("expires_at", now.isoformat()).order("started_at", desc=True).execute())
        recordings = []
        for rec in response.data:
            expires = datetime.fromisoformat(rec["expires_at"].replace("Z", "+00:00"))
            rec["hours_left"] = max(0, int((expires - now).total_seconds() // 3600))
            rec["playback_url"] = None
            recordings.append(rec)
        return recordings

    async def object_exists(self, key: str) -> bool:
        def _exists():
            try:
                self._s3_client().head_object(Bucket=self.s3_bucket, Key=key)
                return True
            except Exception:
                return False
        return await self._run_in_thread(_exists)

    async def _associate_recording(self, record: Dict[str, Any]) -> Optional[str]:
        from . import class_intelligence
        return await class_intelligence.associate_recording_with_meeting(
            room_code=record["room_code"], recording_id=record["recording_id"],
            recording_started_at=record["started_at"],
        )

    async def _mark_egress_failed(self, recording_id: str, info) -> None:
        detail = getattr(info, "error", "") or getattr(info, "details", "") or "LiveKit Egress failed"
        ended_at = self._egress_datetime(getattr(info, "ended_at", 0)) or datetime.now(timezone.utc)
        await self._update_recording_metadata(recording_id, {
            "status": "failed", "finalization_last_error": str(detail)[:2000],
            "ended_at": ended_at.isoformat(),
            "expires_at": (ended_at + timedelta(hours=20)).isoformat(),
            "finalization_lease_until": None,
        })
        recording_state.update_status(recording_id, "failed", datetime.utcnow())

    @staticmethod
    def _egress_datetime(timestamp: int | float | None) -> Optional[datetime]:
        if not timestamp:
            return None
        value = float(timestamp)
        if value > 10_000_000_000:
            value /= 1000
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    async def _validate_final_hls(self, record: Dict[str, Any], info) -> bool:
        """Validate terminal Egress metadata and every listed HLS segment cheaply."""
        results = list(getattr(info, "segment_results", []) or [])
        if not results:
            legacy_result = getattr(info, "segments", None)
            has_field = getattr(info, "HasField", None)
            if legacy_result is not None:
                try:
                    legacy_present = has_field("segments") if callable(has_field) else True
                except (TypeError, ValueError):
                    legacy_present = False
                if legacy_present:
                    results = [legacy_result]
        if not results:
            raise RuntimeError("Successful Egress response has no HLS segment results")
        result = results[0]
        segment_count = int(getattr(result, "segment_count", 0))
        if segment_count <= 0:
            raise RuntimeError("Successful Egress response reports no HLS segments")
        playlist_name = getattr(result, "playlist_name", "")
        if playlist_name and posixpath.basename(playlist_name) != posixpath.basename(record["playlist_key"]):
            raise RuntimeError("LiveKit finalized a different playlist than the configured recording playlist")

        prefix = record["storage_prefix"]
        playlist_key = record["playlist_key"]

        def _inspect_storage():
            s3 = self._s3_client()
            playlist_obj = s3.get_object(Bucket=self.s3_bucket, Key=playlist_key)
            playlist = playlist_obj["Body"].read().decode("utf-8")
            if "#EXT-X-ENDLIST" not in playlist:
                raise RuntimeError("Final HLS playlist does not contain EXT-X-ENDLIST")
            segment_uris = [
                line.strip() for line in playlist.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if len(segment_uris) != segment_count:
                raise RuntimeError("HLS playlist segment count does not match LiveKit Egress results")
            expected_keys = set()
            for uri in segment_uris:
                uri_path = uri.split("?", 1)[0]
                if "://" in uri_path or uri_path.startswith("/"):
                    raise RuntimeError("Unexpected non-relative segment URI in finalized HLS playlist")
                key = posixpath.normpath(posixpath.join(prefix, uri_path))
                if key == ".." or key.startswith("../") or not key.startswith(prefix):
                    raise RuntimeError("Unsafe segment path in finalized HLS playlist")
                expected_keys.add(key)
            actual_keys = set()
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.s3_bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    key = item["Key"]
                    if key.endswith((".ts", ".m4s", ".mp4")):
                        actual_keys.add(key)
            return expected_keys.issubset(actual_keys)

        return await self._run_in_thread(_inspect_storage)

    async def _finalize_egress(self, record: Dict[str, Any], info) -> bool:
        status = int(info.status)
        if status in (EgressStatus.EGRESS_FAILED, EgressStatus.EGRESS_ABORTED,
                      EgressStatus.EGRESS_LIMIT_REACHED):
            await self._mark_egress_failed(record["recording_id"], info)
            return True
        if status != EgressStatus.EGRESS_COMPLETE:
            await self._update_recording_metadata(record["recording_id"], {
                "status": "processing", "finalization_next_attempt_at":
                    (datetime.now(timezone.utc) + timedelta(seconds=45)).isoformat(),
                "finalization_lease_until": None,
            })
            return False

        try:
            valid = await self._validate_final_hls(record, info)
        except Exception as exc:
            # Missing objects and transient storage errors stay retryable. Do
            # not delete or permanently fail a recording on a storage check.
            await self._update_recording_metadata(record["recording_id"], {
                "status": "processing", "finalization_last_error": str(exc)[:2000],
                "finalization_next_attempt_at":
                    (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
                "finalization_lease_until": None,
            })
            return False
        if not valid:
            await self._update_recording_metadata(record["recording_id"], {
                "status": "processing", "finalization_last_error": "One or more finalized HLS segments are not accessible yet",
                "finalization_next_attempt_at":
                    (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
                "finalization_lease_until": None,
            })
            return False

        await self._update_recording_metadata(record["recording_id"], {
            "status": "available", "finalized_at": datetime.now(timezone.utc).isoformat(),
            "ended_at": (self._egress_datetime(getattr(info, "ended_at", 0)) or
                         datetime.now(timezone.utc)).isoformat(),
            "finalization_last_error": None, "finalization_lease_until": None,
        })
        recording_state.update_status(record["recording_id"], "available", datetime.utcnow())
        await self._queue_ready_recording(record["recording_id"])
        return True

    async def _queue_ready_recording(self, recording_id: str) -> None:
        from supabase import create_client
        await self._recording_query(lambda: create_client(
            settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY
        ).rpc("enqueue_ready_recording", {"p_recording_id": recording_id}).execute())

    async def receive_egress_webhook(self, event: WebhookEvent) -> Dict[str, Any]:
        event_name = event.event
        if event_name not in ("egress_started", "egress_updated", "egress_ended"):
            return {"success": True, "ignored": True}
        if not event.HasField("egress_info") or not event.egress_info.egress_id:
            return {"success": True, "ignored": True}

        from supabase import create_client
        payload = MessageToDict(event, preserving_proto_field_name=True)
        event_id = event.id or f"{event.egress_info.egress_id}:{event_name}:{event.egress_info.status}"

        def _save_event():
            table = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY).table("livekit_egress_events")
            table.upsert({
                "event_id": event_id, "event_name": event_name,
                "egress_id": event.egress_info.egress_id, "payload": payload,
                "status": "received",
            }, on_conflict="event_id", ignore_duplicates=True).execute()
            result = table.select("*").eq("event_id", event_id).limit(1).execute()
            return result.data[0] if result.data else None

        row = await self._recording_query(_save_event)
        if not row or row.get("status") in ("processed", "ignored"):
            return {"success": True, "duplicate": True}
        await self._process_egress_event_row(row)
        return {"success": True, "received": True}

    async def _process_egress_event_row(self, row: Dict[str, Any]) -> None:
        from supabase import create_client
        event = ParseDict(row["payload"], WebhookEvent(), ignore_unknown_fields=True)
        info = event.egress_info
        record = await self.get_recording_by_egress_id(info.egress_id)
        if record is None:
            # Webhook can arrive before the start response's ID update commits.
            # The Egress may finish before list_egress can recover it, so use
            # the webhook's room and start time to repair that missing link.
            record = await self._match_starting_recording(info)
            if record is None:
                await self._defer_egress_event(row, "No recording row matches this Egress ID yet")
                return
        if event.event == "egress_started":
            if record["status"] == "starting":
                await self._update_recording_metadata(record["recording_id"], {
                    "egress_id": info.egress_id, "status": "recording",
                    "finalization_last_error": None,
                })
            try:
                await self._associate_recording(record)
            except Exception as exc:
                print(f"Could not associate Egress-started recording yet: {exc}")
        elif event.event == "egress_updated":
            if int(info.status) in (EgressStatus.EGRESS_COMPLETE, EgressStatus.EGRESS_FAILED,
                                    EgressStatus.EGRESS_ABORTED, EgressStatus.EGRESS_LIMIT_REACHED):
                await self._finalize_egress(record, info)
        elif event.event == "egress_ended":
            await self._finalize_egress(record, info)

        await self._recording_query(lambda: create_client(
            settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY
        ).table("livekit_egress_events").update({
            "status": "processed", "processed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("event_id", row["event_id"]).execute())

    async def _match_starting_recording(self, info: Any) -> Optional[Dict[str, Any]]:
        room_name = getattr(info, "room_name", "")
        egress_started_at = float(getattr(info, "started_at", 0) or 0)
        if not room_name or not egress_started_at:
            return None
        if egress_started_at > 10_000_000_000:
            egress_started_at /= 1000

        response = await self._recording_query(lambda: self._recordings_table()
            .select("*").eq("livekit_room_name", room_name).eq("status", "starting")
            .is_("egress_id", "null").execute())
        matches = []
        for candidate in response.data or []:
            try:
                started_at = datetime.fromisoformat(
                    candidate["started_at"].replace("Z", "+00:00")
                ).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            if abs(started_at - egress_started_at) <= 300:
                matches.append(candidate)
        if len(matches) != 1:
            return None

        record = matches[0]
        await self._update_recording_metadata(record["recording_id"], {
            "egress_id": info.egress_id,
            "status": "processing",
            "finalization_last_error": None,
        })
        record.update({"egress_id": info.egress_id, "status": "processing"})
        return record

    async def _defer_egress_event(self, row: Dict[str, Any], error: str) -> None:
        from supabase import create_client
        attempts = int(row.get("attempts", 0))
        delay = min(60 * (2 ** min(attempts, 6)), 3600)
        await self._recording_query(lambda: create_client(
            settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY
        ).table("livekit_egress_events").update({
            "last_error": error[:2000],
            "next_attempt_at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(),
            "lease_until": None,
        }).eq("event_id", row["event_id"]).execute())

    async def get_recording_by_egress_id(self, egress_id: str) -> Optional[Dict[str, Any]]:
        response = await self._recording_query(
            lambda: self._recordings_table().select("*").eq("egress_id", egress_id).limit(1).execute())
        return response.data[0] if response.data else None

    async def _recover_starting_egress(self, record: Dict[str, Any]) -> Optional[Any]:
        room_name = record.get("livekit_room_name")
        if not room_name:
            return None
        service = await self.get_egress_service()
        response = await service.list_egress(ListEgressRequest(room_name=room_name))
        start_time = datetime.fromisoformat(record["started_at"].replace("Z", "+00:00")).timestamp()
        matches = []
        for item in response.items:
            item_start = float(item.started_at)
            # LiveKit protocol timestamps are milliseconds; tolerate seconds
            # for SDK/server combinations that serialize them differently.
            if item_start > 10_000_000_000:
                item_start /= 1000
            if abs(item_start - start_time) <= 300:
                matches.append(item)
        if len(matches) != 1:
            return None
        info = matches[0]
        await self._update_recording_metadata(record["recording_id"], {
            "egress_id": info.egress_id, "status": "processing",
            "finalization_last_error": None,
        })
        return info

    async def reconcile_pending_recordings(self) -> int:
        from supabase import create_client

        def _claim():
            return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY).rpc(
                "claim_recording_finalization_batch", {"p_batch_size": 20}
            ).execute().data

        records = await self._recording_query(_claim) or []
        completed = 0
        for record in records:
            try:
                info = None
                if record.get("egress_id"):
                    response = await (await self.get_egress_service()).list_egress(
                        ListEgressRequest(egress_id=record["egress_id"]))
                    info = next((item for item in response.items if item.egress_id == record["egress_id"]), None)
                elif record["status"] == "starting":
                    info = await self._recover_starting_egress(record)

                if info is None:
                    # Completed Egress may no longer be listed. Absence is not
                    # evidence of failure; retain and back off for another poll.
                    await self._update_recording_metadata(record["recording_id"], {
                        "finalization_last_error": "Egress not currently visible; awaiting webhook or next reconciliation",
                        "finalization_next_attempt_at":
                            (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
                        "finalization_lease_until": None,
                    })
                    continue

                status = int(info.status)
                if status == EgressStatus.EGRESS_STARTING or (
                    status == EgressStatus.EGRESS_ACTIVE and not record.get("stop_requested_at")
                ):
                    await self._update_recording_metadata(record["recording_id"], {
                        "status": "starting" if status == EgressStatus.EGRESS_STARTING else "recording",
                        "finalization_lease_until": None,
                        "finalization_next_attempt_at":
                            (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
                    })
                    continue
                if status in (EgressStatus.EGRESS_STARTING, EgressStatus.EGRESS_ACTIVE) and record.get("stop_requested_at"):
                    try:
                        info = await (await self.get_egress_service()).stop_egress(
                            StopEgressRequest(egress_id=info.egress_id))
                    except Exception as exc:
                        await self._update_recording_metadata(record["recording_id"], {
                            "finalization_last_error": f"Retry stop request failed: {exc}"[:2000],
                            "finalization_next_attempt_at":
                                (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
                            "finalization_lease_until": None,
                        })
                        continue
                done = await self._finalize_egress(record, info)
                completed += int(done)
                if record.get("status") == "available":
                    try:
                        await self._associate_recording(record)
                        await self._queue_ready_recording(record["recording_id"])
                    except Exception:
                        pass
            except Exception as exc:
                # Release the lease and back off; do not turn transient DB,
                # Egress API, or storage errors into permanent recording loss.
                try:
                    await self._update_recording_metadata(record["recording_id"], {
                        "finalization_last_error": str(exc)[:2000],
                        "finalization_next_attempt_at":
                            (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
                        "finalization_lease_until": None,
                    })
                except Exception:
                    pass
        await self._reconcile_received_webhooks()
        await self._queue_available_recordings_without_jobs()
        return completed

    async def _reconcile_received_webhooks(self) -> None:
        from supabase import create_client
        def _claim():
            return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY).rpc(
                "claim_livekit_egress_events_batch", {"p_batch_size": 50}
            ).execute().data
        for row in await self._recording_query(_claim) or []:
            try:
                await self._process_egress_event_row(row)
            except Exception as exc:
                print(f"Could not process persisted Egress webhook: {exc}")
                try:
                    await self._defer_egress_event(row, str(exc))
                except Exception:
                    pass

    async def _queue_available_recordings_without_jobs(self) -> None:
        from supabase import create_client
        def _recover_and_enqueue():
            client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
            client.rpc("associate_unlinked_available_recordings", {"p_limit": 50}).execute()
            return client.rpc("enqueue_available_recordings_batch", {"p_limit": 50}).execute()
        await self._recording_query(_recover_and_enqueue)

    async def _prune_processed_webhook_events(self) -> None:
        from supabase import create_client
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        await self._recording_query(lambda: create_client(
            settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY
        ).table("livekit_egress_events").delete().lt("received_at", cutoff)
         .neq("status", "received").execute())

    async def reconciliation_worker(self) -> None:
        prune_at = datetime.now(timezone.utc)
        while True:
            if settings.validate_supabase_db():
                try:
                    await self.reconcile_pending_recordings()
                    if datetime.now(timezone.utc) >= prune_at:
                        await self._prune_processed_webhook_events()
                        prune_at = datetime.now(timezone.utc) + timedelta(hours=24)
                except Exception as exc:
                    print(f"Recording reconciliation error: {exc}")
            await asyncio.sleep(60)

    async def get_object(self, key: str) -> bytes:
        return await self._run_in_thread(lambda: self._s3_client().get_object(Bucket=self.s3_bucket, Key=key)["Body"].read())

    async def download_object_to_path(self, key: str, destination: Path) -> None:
        """Download one private storage object straight to a local file."""
        await self._run_in_thread(lambda: self._s3_client().download_file(
            self.s3_bucket, key, str(destination)))

    def create_playback_token(self, recording_id: str, expires_at: str, ttl_seconds: int = 2 * 3600) -> str:
        """Short-lived, scoped bearer token for one recording's playlist and segments."""
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        expiry = min(int(expires.timestamp()), int((datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).timestamp()))
        payload = base64.urlsafe_b64encode(json.dumps({"r": recording_id, "e": expiry}, separators=(",", ":")).encode()).decode().rstrip("=")
        secret = (settings.RECORDING_PLAYBACK_SECRET or settings.SUPABASE_SERVICE_ROLE_KEY).encode()
        signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def create_recordings_access_token(self) -> str:
        expiry = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
        payload = base64.urlsafe_b64encode(json.dumps({"scope": "recordings", "e": expiry}, separators=(",", ":")).encode()).decode().rstrip("=")
        secret = (settings.RECORDING_PLAYBACK_SECRET or settings.SUPABASE_SERVICE_ROLE_KEY).encode()
        return f"{payload}.{hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()}"

    def verify_recordings_access_token(self, token: str) -> bool:
        try:
            payload, signature = token.rsplit(".", 1)
            secret = (settings.RECORDING_PLAYBACK_SECRET or settings.SUPABASE_SERVICE_ROLE_KEY).encode()
            if not hmac.compare_digest(signature, hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()):
                return False
            decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            return decoded["scope"] == "recordings" and int(decoded["e"]) > int(datetime.now(timezone.utc).timestamp())
        except Exception:
            return False

    def verify_playback_token(self, token: str, recording_id: str) -> bool:
        try:
            payload, signature = token.rsplit(".", 1)
            secret = (settings.RECORDING_PLAYBACK_SECRET or settings.SUPABASE_SERVICE_ROLE_KEY).encode()
            expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return False
            decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            return decoded["r"] == recording_id and int(decoded["e"]) > int(datetime.now(timezone.utc).timestamp())
        except Exception:
            return False

    async def cleanup_expired_recordings(self) -> int:
        """Idempotently delete full expired prefixes, never an active recording."""
        now = datetime.now(timezone.utc).isoformat()
        response = await self._recording_query(
            lambda: self._recordings_table().select("*").lte("expires_at", now)
            .neq("status", "recording").neq("status", "starting")
            .neq("status", "processing").execute())
        removed = 0
        for rec in response.data:
            await self.delete_recording(rec)
            removed += 1
        return removed

    async def delete_recording(self, record: Dict[str, Any]):
        """Delete all objects before metadata; safe when the prefix is already empty."""
        prefix = record["storage_prefix"]
        def _delete_prefix():
            s3 = self._s3_client()
            keys = []
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=self.s3_bucket, Prefix=prefix):
                keys.extend({"Key": item["Key"]} for item in page.get("Contents", []))
            for offset in range(0, len(keys), 1000):
                s3.delete_objects(Bucket=self.s3_bucket, Delete={"Objects": keys[offset:offset + 1000], "Quiet": True})
        await self._run_in_thread(_delete_prefix)
        await self._recording_query(lambda: self._recordings_table().delete().eq("recording_id", record["recording_id"]).execute())


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

livekit_service = LiveKitService()
