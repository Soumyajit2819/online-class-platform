"""Meeting-scoped durable speech-to-text processing.

Provider adapters plug into this durable meeting/job lifecycle and keep audio
and credentials on the backend.
"""

import asyncio
import logging
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .class_intelligence import SpeechToTextProvider, TranscriptResult
from .config import settings

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
BASE_RETRY_SECONDS = 60
MAX_RETRY_SECONDS = 6 * 60 * 60
JOB_LEASE_SECONDS = 10 * 60
_providers: dict[str, Callable[[], "SpeechToTextProvider"]] = {}


class STTProviderError(Exception):
    """Provider error with safe classification; never persist its raw message."""

    def __init__(self, code: str, retryable: bool):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class AudioPreparationError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def register_provider(name: str, factory: Callable[[], SpeechToTextProvider]) -> None:
    """Register a concrete server-side provider adapter."""
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("Provider name is required")
    _providers[normalized] = factory


def configured_provider() -> tuple[SpeechToTextProvider | None, str]:
    name = settings.CLASS_INTELLIGENCE_STT_PROVIDER.strip().lower()
    model = settings.CLASS_INTELLIGENCE_STT_MODEL.strip()
    if not name or not model:
        return None, "Speech-to-text provider and model are not configured."
    factory = _providers.get(name)
    if factory is None:
        return None, "No server-side adapter is registered for the configured provider."
    try:
        provider = factory()
        validate = getattr(provider, "validate_configuration", None)
        if not callable(validate) or not validate():
            return None, "Speech-to-text provider credentials/configuration are incomplete."
    except Exception:
        return None, "Speech-to-text provider configuration is invalid."
    return provider, ""


def retry_delay(attempt: int) -> int:
    """Bounded exponential retry delay, capped at six hours."""
    return min(BASE_RETRY_SECONDS * (2 ** max(0, attempt - 1)), MAX_RETRY_SECONDS)


def _parse_playlist(text: str, storage_prefix: str) -> list[str]:
    """Return safe object keys for a finalized media playlist."""
    prefix = storage_prefix.rstrip("/") + "/"
    keys = []
    for line in text.splitlines():
        uri = line.strip()
        if not uri or uri.startswith("#"):
            continue
        parsed = urlsplit(uri)
        if parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
            raise AudioPreparationError("Finalized playlist contains an unsafe media URI")
        relative = unquote(parsed.path)
        if not relative or ".." in relative.split("/") or "\\" in relative:
            raise AudioPreparationError("Finalized playlist contains an unsafe media path")
        key = relative if relative.startswith(prefix) else prefix + relative
        if not key.startswith(prefix):
            raise AudioPreparationError("Finalized playlist media path is outside its recording prefix")
        keys.append(key)
    if not keys:
        raise AudioPreparationError("Finalized playlist contains no audio segments")
    return keys


async def prepare_meeting_audio(recordings: list[dict[str, Any]], work_dir: Path,
                                storage: Any) -> Path:
    """Download private HLS segments to disk and extract one meeting FLAC.

    Only the small playlist is read into memory. HLS media segments and the
    resulting audio stay in a private temporary directory on local disk.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioPreparationError("FFmpeg is unavailable on the backend")
    if not recordings:
        raise AudioPreparationError("No finalized recordings are associated with this meeting")

    concat_path = work_dir / "segments.txt"
    manifest_lines: list[str] = []
    ordered = sorted(recordings, key=lambda row: row.get("started_at") or "")
    segment_index = 0
    for recording in ordered:
        playlist_bytes = await storage.get_object(recording["playlist_key"])
        playlist = playlist_bytes.decode("utf-8")
        object_keys = _parse_playlist(playlist, recording["storage_prefix"])
        for key in object_keys:
            suffix = Path(urlsplit(key).path).suffix.lower()
            if suffix not in (".ts", ".m4s", ".mp4", ".aac"):
                suffix = ".segment"
            segment_path = work_dir / f"segment-{segment_index:08d}{suffix}"
            await storage.download_object_to_path(key, segment_path)
            # Generated local names contain no quote characters.
            manifest_lines.append(f"file '{segment_path.as_posix()}'")
            segment_index += 1

    concat_path.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    audio_path = work_dir / "meeting-audio.flac"
    stderr_path = work_dir / "ffmpeg-stderr.log"
    try:
        with stderr_path.open("wb") as stderr_file:
            process = await asyncio.create_subprocess_exec(
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", str(concat_path),
                "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "flac",
                str(audio_path), stdout=asyncio.subprocess.DEVNULL, stderr=stderr_file,
            )
            result = await process.wait()
    except (OSError, asyncio.SubprocessError):
        raise AudioPreparationError("FFmpeg could not prepare meeting audio") from None
    if result != 0 or not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise AudioPreparationError("FFmpeg could not prepare meeting audio")
    return audio_path


def _client():
    from supabase import create_client
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


async def _db(operation):
    return await asyncio.to_thread(operation)


async def _recordings_for_meeting(meeting_id: str) -> list[dict[str, Any]]:
    def query():
        client = _client()
        links = (client.table("meeting_recordings").select("recording_id")
                 .eq("class_meeting_id", meeting_id).execute().data or [])
        ids = [item["recording_id"] for item in links]
        if not ids:
            return []
        return client.table("recordings").select(
            "recording_id,started_at,status,storage_prefix,playlist_key"
        ).in_("recording_id", ids).execute().data or []
    rows = await _db(query)
    return sorted(rows, key=lambda row: row.get("started_at") or "")


async def _set_meeting_state(meeting_id: str, status: str, error: str | None = None) -> None:
    await _db(lambda: _client().table("class_meetings").update({
        "transcript_status": status, "transcript_error": error,
    }).eq("id", meeting_id).execute())


async def _set_recording_jobs_state(recording_ids: list[str], status: str) -> None:
    if not recording_ids:
        return
    await _db(lambda: _client().table("recording_processing_jobs").update({
        "status": status,
    }).in_("recording_id", recording_ids).execute())


async def _renew_lease(job: dict[str, Any], stop: asyncio.Event) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
            return
        except asyncio.TimeoutError:
            pass
        try:
            result = await _db(lambda: _client().rpc("renew_meeting_transcript_lease", {
                "p_id": job["id"], "p_lease_token": job["lease_token"],
                "p_lease_seconds": JOB_LEASE_SECONDS,
            }).execute().data)
            if result is False:
                logger.warning("Transcript job lease is no longer owned; job will be recovered by another worker")
                return
        except Exception:
            logger.warning("Could not renew transcript job lease; it can be recovered after lease expiry")


async def _finish_job(job: dict[str, Any], values: dict[str, Any]) -> bool:
    values["updated_at"] = datetime.now(timezone.utc).isoformat()
    response = await _db(lambda: _client().table("meeting_transcripts").update(values)
        .eq("id", job["id"]).eq("lease_token", job["lease_token"]).execute())
    return bool(response.data)


async def _process_job(job: dict[str, Any], provider: SpeechToTextProvider,
                       provider_name: str, model: str, storage: Any) -> None:
    meeting_id = job["meeting_id"]
    source_ids: list[str] = []
    stop_renewal = asyncio.Event()
    renewal_task = asyncio.create_task(_renew_lease(job, stop_renewal))
    try:
        recordings = await _recordings_for_meeting(meeting_id)
        if not recordings or any(record.get("status") != "available" for record in recordings):
            raise AudioPreparationError(
                "All class recording sessions must be finalized before transcription",
                retryable=True,
            )
        source_ids = [record["recording_id"] for record in recordings]
        await _set_recording_jobs_state(source_ids, "processing")
        with tempfile.TemporaryDirectory(prefix="class-transcript-") as temp_dir:
            try:
                audio_path = await prepare_meeting_audio(recordings, Path(temp_dir), storage)
            except AudioPreparationError:
                raise
            except Exception:
                raise AudioPreparationError("Meeting audio preparation failed", retryable=True) from None
            try:
                result = await provider.transcribe(audio_path, {
                    "meeting_id": meeting_id,
                    "recording_ids": source_ids,
                    "language_policy": "preserve_original_spoken_language",
                    "audio_format": "flac",
                })
            except STTProviderError:
                raise
            except asyncio.TimeoutError:
                raise STTProviderError("provider_timeout", retryable=True) from None
            except Exception:
                # Do not log or persist provider exception text: it can contain
                # authorization material or request URLs.
                raise STTProviderError("provider_request_failed", retryable=True) from None

        if not isinstance(result, TranscriptResult) or not isinstance(result.text, str) or not result.text.strip():
            await _finish_job(job, {
                "status": "failed", "error_code": "empty_result",
                "error": "Speech-to-text provider returned no transcript.",
                "lease_until": None, "lease_token": None,
            })
            await _set_meeting_state(meeting_id, "failed", "Speech-to-text provider returned no transcript.")
            return

        transcript_values = {
            "status": "ready", "transcript_text": result.text,
            "language": result.language, "provider": result.provider or provider_name,
            "model": result.model or model,
            "segments": result.segments,
            "source_recording_ids": source_ids,
            "error_code": None, "error": None,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "lease_until": None, "lease_token": None,
        }
        saved = await _finish_job(job, transcript_values)
        if saved:
            await _set_meeting_state(meeting_id, "ready", None)
            await _set_recording_jobs_state(source_ids, "complete")
    except (AudioPreparationError, STTProviderError) as exc:
        if isinstance(exc, AudioPreparationError):
            code, retryable = "audio_preparation_failed", exc.retryable
            safe_error = "Meeting audio preparation failed."
        else:
            allowed_codes = {
                "provider_timeout", "provider_request_failed", "provider_auth_failed",
                "provider_rate_limited", "provider_job_failed", "provider_output_invalid",
                "empty_result",
            }
            code = exc.code if exc.code in allowed_codes else "provider_request_failed"
            retryable = exc.retryable
            safe_error = "Speech-to-text provider rejected the request."
        attempts = int(job.get("attempts", 1))
        retry = retryable and attempts < min(int(job.get("max_attempts", MAX_ATTEMPTS)), MAX_ATTEMPTS)
        if retry:
            safe_error += " It will be retried."
        elif retryable:
            safe_error += " The retry limit was reached."
        values = {
            "status": "pending" if retry else "failed",
            "error_code": code, "error": safe_error,
            "next_attempt_at": (datetime.now(timezone.utc) + timedelta(
                seconds=retry_delay(attempts) if retry else 0)).isoformat(),
            "lease_until": None, "lease_token": None,
        }
        if await _finish_job(job, values):
            await _set_meeting_state(meeting_id, values["status"], safe_error)
            await _set_recording_jobs_state(source_ids, "pending" if retry else "failed")
    finally:
        stop_renewal.set()
        await asyncio.gather(renewal_task, return_exceptions=True)


async def process_ready_transcripts_once(storage: Any = None) -> int:
    """Enqueue/recover and process one bounded batch of ended whole meetings."""
    from .livekit_service import livekit_service
    storage = storage or livekit_service
    provider, _reason = configured_provider()
    provider_ready = provider is not None
    client = _client()
    await _db(lambda: client.rpc("mark_untranscribable_meetings", {"p_limit": 50}).execute())
    await _db(lambda: client.rpc("enqueue_ready_meeting_transcripts", {
        "p_provider_ready": provider_ready, "p_limit": 50,
    }).execute())
    if not provider_ready:
        return 0

    jobs = await _db(lambda: client.rpc("claim_meeting_transcript_jobs", {
        "p_batch_size": 3, "p_lease_seconds": JOB_LEASE_SECONDS,
    }).execute().data or [])
    provider_name = settings.CLASS_INTELLIGENCE_STT_PROVIDER.strip().lower()
    model = settings.CLASS_INTELLIGENCE_STT_MODEL.strip()
    for job in jobs:
        await _process_job(job, provider, provider_name, model, storage)
    return len(jobs)


async def transcription_worker() -> None:
    """Poll durable database jobs; state survives process restarts."""
    while True:
        if settings.validate_supabase_db():
            try:
                await process_ready_transcripts_once()
                # Notes share the existing durable Class Intelligence worker
                # cycle; they are eligible only after a transcript is ready.
                from .notes import process_ready_meeting_notes_once
                await process_ready_meeting_notes_once()
            except Exception:
                # Avoid exception contents because an SDK/provider error can
                # contain request headers or private storage URLs.
                logger.warning("Transcript reconciliation cycle failed")
        await asyncio.sleep(60)


# Register the vendor adapter through the existing provider interface. The SDK
# itself is imported lazily by the adapter, so the backend can still start when
# Sarvam credentials are absent.
from .sarvam_stt import SarvamBatchSTTProvider

register_provider("sarvam", SarvamBatchSTTProvider)
