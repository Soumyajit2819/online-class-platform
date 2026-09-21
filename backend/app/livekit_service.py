import secrets
import hashlib
import asyncio
import hmac
import base64
import json
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta, timezone
import aiohttp
from livekit.api import AccessToken, VideoGrants
from livekit.api.room_service import RoomService, CreateRoomRequest
from livekit.api.egress_service import EgressService, RoomCompositeEgressRequest
from livekit.api import (
    SegmentedFileOutput, SegmentedFileProtocol, S3Upload, EncodingOptions,
    AudioCodec, VideoCodec,
)
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
        }
        self.microphone_restrictions[(room_code, identity)] = restriction
        return restriction

    def get_microphone_restriction(self, room_code: str, identity: str) -> Optional[Dict[str, Any]]:
        # Reading a restriction must not expire it.  The timed-expiry callback
        # owns the transition so it can restore LiveKit permission atomically
        # with removing the server-side restriction.
        return self.microphone_restrictions.get((room_code, identity))

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

    def create_join_request(self, room_code: str, student_name: str, session_id: str) -> Dict[str, Any]:
        existing = self.get_join_request_for_session(room_code, session_id)
        if existing:
            return existing
        request_id = f"jr_{secrets.token_urlsafe(16)}"
        request = {
            "request_id": request_id,
            "room_code": room_code,
            "student_name": student_name.strip(),
            "student_identity": f"student_{secrets.token_urlsafe(8)}",
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
            await self._update_recording_metadata(recording_id, {"egress_id": egress.egress_id, "status": "recording"})
            recording_state.add_recording(room_code, recording_id, egress.egress_id,
                                          livekit_room_name, class_name, teacher_name)
            session_state.set_recording(room_code, True, recording_id)
            return {"success": True, "recording_id": recording_id, "egress_id": egress.egress_id, "status": "recording"}
        except Exception as e:
            # No orphan row for an egress that never started.
            try:
                await self._recording_query(lambda: self._recordings_table().delete().eq("recording_id", recording_id).execute())
            except Exception as cleanup_error:
                print(f"Could not remove failed recording metadata: {cleanup_error}")
            print(f"Error starting HLS recording: {e}")
            return {"success": False, "error": str(e)}

    async def stop_recording(self, room_code: str, recording_id: str) -> Dict[str, Any]:
        try:
            record = await self.get_recording_metadata(recording_id)
            if not record or record["room_code"] != room_code or not record.get("egress_id"):
                return {"success": False, "error": "Recording not found"}
            from livekit.api.egress_service import StopEgressRequest
            await (await self.get_egress_service()).stop_egress(StopEgressRequest(egress_id=record["egress_id"]))
            ended = datetime.now(timezone.utc)
            ended_at = ended.isoformat()
            await self._update_recording_metadata(recording_id, {
                "status": "processing", "ended_at": ended_at,
                "expires_at": (ended + timedelta(hours=20)).isoformat(),
            })
            recording_state.update_status(recording_id, "processing", datetime.utcnow())
            session_state.set_recording(room_code, False, None)
            return {"success": True, "recording_id": recording_id, "status": "processing",
                    "message": "Recording stopped; HLS playlist is being finalized."}
        except Exception as e:
            print(f"Error stopping recording: {e}")
            return {"success": False, "error": str(e)}

    async def list_recordings(self) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        response = await self._recording_query(
            lambda: self._recordings_table().select("*").gt("expires_at", now.isoformat()).neq("status", "failed").order("started_at", desc=True).execute())
        recordings = []
        for rec in response.data:
            expires = datetime.fromisoformat(rec["expires_at"].replace("Z", "+00:00"))
            if rec["status"] == "processing":
                # The final playlist is the durable completion signal when no webhook is configured.
                exists = await self.object_exists(rec["playlist_key"])
                if exists:
                    await self._update_recording_metadata(rec["recording_id"], {"status": "available"})
                    rec["status"] = "available"
                elif rec.get("ended_at"):
                    ended = datetime.fromisoformat(rec["ended_at"].replace("Z", "+00:00"))
                    # A stopped egress that has not produced a playlist after
                    # finalization time is failed, not a perpetually stale row.
                    if now - ended > timedelta(minutes=10):
                        await self.delete_recording(rec)
                        continue
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

    async def get_object(self, key: str) -> bytes:
        return await self._run_in_thread(lambda: self._s3_client().get_object(Bucket=self.s3_bucket, Key=key)["Body"].read())

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
            lambda: self._recordings_table().select("*").lte("expires_at", now).neq("status", "recording").neq("status", "starting").execute())
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
