import secrets
import hashlib
import asyncio
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta, timezone
import aiohttp
from livekit.api import AccessToken, VideoGrants, S3Upload
from livekit.api.room_service import RoomService, CreateRoomRequest
from livekit.api.egress_service import EgressService, RoomCompositeEgressRequest
from livekit.api import EncodedFileOutput, EncodedFileType
from .config import settings
from .models import MicrophonePolicy, CameraPolicy


# ---------------------------------------------------------------------------
# Recording State
# ---------------------------------------------------------------------------

class RecordingState:
    """In-memory metadata for active recordings (augments persistent S3 storage)."""

    def __init__(self):
        self.recordings: Dict[str, Dict[str, Any]] = {}       # recording_id -> info
        self.class_recordings: Dict[str, List[str]] = {}      # room_code -> [recording_ids]

    def add_recording(self, room_code: str, recording_id: str, egress_id: str,
                      livekit_room_name: str, class_name: str, teacher_name: str):
        now    = datetime.utcnow()
        expiry = now + timedelta(hours=20)
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
            "expires_at":        expiry,
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

    def cleanup_expired(self) -> List[str]:
        now = datetime.utcnow()
        expired = [rid for rid, r in self.recordings.items() if r["expires_at"] <= now]
        for rid in expired:
            room_code = self.recordings[rid]["room_code"]
            del self.recordings[rid]
            if room_code in self.class_recordings:
                self.class_recordings[room_code] = [
                    x for x in self.class_recordings[room_code] if x != rid
                ]
        return expired


# ---------------------------------------------------------------------------
# Session State
# ---------------------------------------------------------------------------

class SessionState:
    """In-memory state for active class sessions."""

    def __init__(self):
        self.active_classes: Dict[str, Dict[str, Any]] = {}
        self.blocked_participants: Dict[str, List[str]] = {}
        self.teacher_sessions: Dict[str, str] = {}

    def create_class(self, room_code, livekit_room_name, class_name, teacher_name,
                     teacher_identity, meeting_passcode_hash, max_participants,
                     student_microphone_policy, student_camera_policy):
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
        }
        self.teacher_sessions[teacher_identity] = room_code

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
                             is_muted=False, is_camera_off=False) -> str:
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
                     can_publish_data=can_publish_data))
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
        try:
            from livekit.api.room_service import MuteRoomTrackRequest
            rs = await self.get_room_service()
            await rs.mute_room_track(MuteRoomTrackRequest(
                room=livekit_room_name, identity=identity, track_sid="microphone", muted=muted))
        except Exception as e:
            print(f"Error muting participant: {e}")

    async def mute_all_students(self, livekit_room_name: str, identities: List[str]):
        for identity in identities:
            await self.mute_participant(livekit_room_name, identity, True)

    async def disable_all_cameras(self, livekit_room_name: str, identities: List[str]):
        for identity in identities:
            try:
                from livekit.api.room_service import MuteRoomTrackRequest
                rs = await self.get_room_service()
                await rs.mute_room_track(MuteRoomTrackRequest(
                    room=livekit_room_name, identity=identity, track_sid="camera", muted=True))
            except Exception as e:
                print(f"Error disabling camera for {identity}: {e}")

    # ---- recording -------------------------------------------------------

    async def start_recording(self, room_code: str, livekit_room_name: str,
                               class_name: str, teacher_name: str) -> Dict[str, Any]:
        """Start a LiveKit Egress recording → saves MP4 to Supabase Storage."""
        try:
            recording_id  = self.generate_recording_id()
            egress_svc    = await self.get_egress_service()

            s3_config = S3Upload(
                access_key=self.s3_access_key,
                secret=self.s3_secret_key,
                region=self.s3_region,
                bucket=self.s3_bucket,
                endpoint=self.s3_endpoint,
                force_path_style=True,
            )

            output = EncodedFileOutput(
                file_type=EncodedFileType.MP4,
                filepath=f"recordings/{room_code}/{recording_id}.mp4",
                s3=s3_config,
            )

            req       = RoomCompositeEgressRequest(room_name=livekit_room_name, file=output)
            egress    = await egress_svc.start_room_composite_egress(req)
            egress_id = egress.egress_id

            recording_state.add_recording(
                room_code=room_code, recording_id=recording_id, egress_id=egress_id,
                livekit_room_name=livekit_room_name, class_name=class_name, teacher_name=teacher_name)
            session_state.set_recording(room_code, True, recording_id)

            print(f"✓ Recording started: {recording_id} (egress: {egress_id})")
            return {"success": True, "recording_id": recording_id, "egress_id": egress_id, "status": "recording"}

        except Exception as e:
            print(f"Error starting recording: {e}")
            return {"success": False, "error": str(e)}

    async def stop_recording(self, room_code: str, recording_id: str) -> Dict[str, Any]:
        """Stop the egress. File will finish uploading to Supabase automatically."""
        try:
            rec = recording_state.get_recording(recording_id)
            if not rec:
                return {"success": False, "error": "Recording not found in session. If server restarted, the recording may still be in Supabase."}

            from livekit.api.egress_service import StopEgressRequest
            egress_svc = await self.get_egress_service()
            await egress_svc.stop_egress(StopEgressRequest(egress_id=rec["egress_id"]))

            recording_state.update_status(recording_id, "processing", ended_at=datetime.utcnow())
            session_state.set_recording(room_code, False, None)

            print(f"✓ Recording stopped: {recording_id} — uploading to Supabase...")
            return {"success": True, "recording_id": recording_id, "status": "processing",
                    "message": "Recording stopped. File is being saved to Supabase Storage."}

        except Exception as e:
            print(f"Error stopping recording: {e}")
            return {"success": False, "error": str(e)}

    # ---- list recordings from Supabase S3 (persistent) ------------------

    async def list_recordings_from_storage(self) -> List[Dict[str, Any]]:
        """
        Fetch recordings directly from Supabase S3 bucket.
        Works even after server restart — Supabase is the source of truth.
        Returns recordings with pre-signed download URLs.
        """
        def _fetch():
            try:
                from botocore.exceptions import ClientError

                s3        = self._s3_client()
                results   = []
                paginator = s3.get_paginator('list_objects_v2')

                for page in paginator.paginate(Bucket=self.s3_bucket, Prefix='recordings/'):
                    for obj in page.get('Contents', []):
                        key = obj['Key']          # recordings/ROOMCODE/rec_xxx.mp4
                        if not key.endswith('.mp4'):
                            continue

                        parts = key.split('/')
                        if len(parts) < 3:
                            continue

                        room_code    = parts[1]
                        recording_id = parts[2].replace('.mp4', '')
                        last_modified = obj['LastModified']   # tz-aware
                        file_size     = obj['Size']

                        expires_at  = last_modified + timedelta(hours=20)
                        now_utc     = datetime.now(timezone.utc)

                        if expires_at <= now_utc:
                            continue   # expired — skip

                        hours_left     = int((expires_at - now_utc).total_seconds() // 3600)
                        expiry_seconds = min(int((expires_at - now_utc).total_seconds()), 604800)

                        # Generate pre-signed download URL
                        try:
                            download_url = s3.generate_presigned_url(
                                'get_object',
                                Params={'Bucket': self.s3_bucket, 'Key': key},
                                ExpiresIn=expiry_seconds,
                            )
                        except Exception:
                            download_url = None

                        # Enrich with in-memory metadata if still available
                        mem = recording_state.get_recording(recording_id)

                        results.append({
                            "recording_id": recording_id,
                            "room_code":    room_code,
                            "class_name":   mem["class_name"]   if mem else f"Class {room_code}",
                            "teacher_name": mem["teacher_name"] if mem else "Unknown",
                            "status":       "available",
                            "started_at":   mem["started_at"].isoformat() if mem else last_modified.isoformat(),
                            "ended_at":     mem["ended_at"].isoformat()   if (mem and mem.get("ended_at")) else last_modified.isoformat(),
                            "expires_at":   expires_at.isoformat(),
                            "hours_left":   hours_left,
                            "file_size_mb": round(file_size / (1024 * 1024), 2),
                            "download_url": download_url,
                            "s3_key":       key,
                        })

                results.sort(key=lambda r: r["started_at"], reverse=True)
                return results

            except Exception as e:
                print(f"Error listing recordings from Supabase: {e}")
                return []

        return await self._run_in_thread(_fetch)

    async def delete_expired_recordings_from_storage(self):
        """Delete files older than 3 days from Supabase S3 bucket."""
        def _delete():
            try:
                from datetime import timezone
                s3        = self._s3_client()
                paginator = s3.get_paginator('list_objects_v2')
                deleted   = []

                for page in paginator.paginate(Bucket=self.s3_bucket, Prefix='recordings/'):
                    for obj in page.get('Contents', []):
                        key           = obj['Key']
                        last_modified = obj['LastModified']  # tz-aware
                        expires_at    = last_modified + timedelta(hours=20)
                        now_utc       = datetime.now(timezone.utc)

                        if expires_at <= now_utc:
                            s3.delete_object(Bucket=self.s3_bucket, Key=key)
                            deleted.append(key)
                            print(f"✓ Deleted expired recording from Supabase: {key}")

                return deleted
            except Exception as e:
                print(f"⚠ Error deleting expired recordings: {e}")
                return []

        return await self._run_in_thread(_delete)
        """Generate a fresh pre-signed download URL."""
        def _gen():
            try:
                s3 = self._s3_client()
                return s3.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': self.s3_bucket, 'Key': s3_key},
                    ExpiresIn=20 * 3600,
                )
            except Exception as e:
                print(f"Error generating download URL: {e}")
                return None
        return await self._run_in_thread(_gen)


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

livekit_service = LiveKitService()
