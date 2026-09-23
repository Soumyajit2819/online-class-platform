"""Durable, meeting-scoped bilingual notes generation."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .class_intelligence import ClassNotesProvider, MeetingNotesResult
from .config import settings
from .transcription import JOB_LEASE_SECONDS, MAX_ATTEMPTS, retry_delay

logger = logging.getLogger(__name__)
_providers: dict[str, Callable[[], ClassNotesProvider]] = {}


class NotesProviderError(Exception):
    """Safe provider classification; raw provider messages are never persisted."""

    def __init__(self, code: str, retryable: bool):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def register_notes_provider(name: str, factory: Callable[[], ClassNotesProvider]) -> None:
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("Notes provider name is required")
    _providers[normalized] = factory


def configured_notes_provider() -> tuple[ClassNotesProvider | None, str]:
    name = settings.CLASS_INTELLIGENCE_NOTES_PROVIDER.strip().lower()
    model = settings.CLASS_INTELLIGENCE_NOTES_MODEL.strip()
    if not name or not model:
        return None, "Notes provider and model are not configured."
    factory = _providers.get(name)
    if factory is None and name == "gemini":
        # Lazy registration avoids importing the SDK unless this provider is selected.
        from .gemini_notes import GeminiNotesProvider
        register_notes_provider("gemini", GeminiNotesProvider)
        factory = _providers.get(name)
    if factory is None:
        return None, "No server-side notes adapter is registered for the configured provider."
    try:
        provider = factory()
        validate = getattr(provider, "validate_configuration", None)
        if not callable(validate) or not validate():
            return None, "Notes provider credentials/configuration are incomplete."
    except Exception:
        return None, "Notes provider configuration is invalid."
    return provider, ""


def _client():
    from supabase import create_client
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


async def _db(operation):
    return await asyncio.to_thread(operation)


async def _renew_lease(job: dict[str, Any], stop: asyncio.Event) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
            return
        except asyncio.TimeoutError:
            pass
        try:
            renewed = await _db(lambda: _client().rpc("renew_meeting_notes_lease", {
                "p_id": job["id"], "p_lease_token": job["lease_token"],
                "p_lease_seconds": JOB_LEASE_SECONDS,
            }).execute().data)
            if renewed is False:
                logger.warning("Notes job lease was lost; job can be recovered after lease expiry")
                return
        except Exception:
            logger.warning("Could not renew notes job lease; it can be recovered after lease expiry")


async def _finish(job: dict[str, Any], *, status: str, code: str | None = None,
                  error: str | None = None, result: MeetingNotesResult | None = None,
                  transcript_id: str | None = None, next_attempt_at: str | None = None) -> bool:
    now = datetime.now(timezone.utc)
    params = {
        "p_id": job["id"],
        "p_lease_token": job["lease_token"],
        "p_status": status,
        "p_english_notes": result.english_notes if result else None,
        "p_bengali_notes": result.bengali_notes if result else None,
        "p_provider": result.provider if result else None,
        "p_model": result.model if result else None,
        "p_source_transcript_id": transcript_id,
        "p_error_code": code,
        "p_error": error,
        "p_next_attempt_at": next_attempt_at or now.isoformat(),
        "p_completed_at": now.isoformat() if status == "ready" else None,
    }
    response = await _db(lambda: _client().rpc("finish_meeting_notes_job", params).execute())
    return bool(response.data)


async def _process_job(job: dict[str, Any], provider: ClassNotesProvider) -> None:
    stop = asyncio.Event()
    lease_task = asyncio.create_task(_renew_lease(job, stop))
    try:
        rows = await _db(lambda: _client().table("meeting_transcripts").select(
            "id,status,transcript_text,segments"
        ).eq("meeting_id", job["meeting_id"]).limit(1).execute().data or [])
        transcript = rows[0] if rows else None
        text = transcript.get("transcript_text") if transcript else None
        if (not transcript or transcript.get("status") != "ready"
                or not isinstance(text, str) or not text.strip()):
            raise NotesProviderError("invalid_transcript", retryable=False)

        result = await provider.generate_notes(text, transcript.get("segments"))
        if (not isinstance(result, MeetingNotesResult)
                or not isinstance(result.english_notes, str) or not result.english_notes.strip()
                or not isinstance(result.bengali_notes, str) or not result.bengali_notes.strip()):
            raise NotesProviderError("provider_error", retryable=False)
        saved = await _finish(job, status="ready", result=result,
                              transcript_id=transcript["id"])
        if not saved:
            logger.warning("Notes result was not persisted because the worker no longer owns its lease")
    except NotesProviderError as exc:
        code = exc.code if exc.code in {
            "authentication_failed", "rate_limited", "timeout", "provider_error",
            "invalid_transcript",
        } else "provider_error"
        attempts = int(job.get("attempts", 1))
        max_attempts = min(int(job.get("max_attempts", MAX_ATTEMPTS)), MAX_ATTEMPTS)
        retry = exc.retryable and attempts < max_attempts
        status = "pending" if retry else "failed"
        if retry:
            message = "Notes provider request failed and will be retried."
            due = datetime.now(timezone.utc) + timedelta(seconds=retry_delay(attempts))
        elif exc.retryable:
            code = "retry_exhausted"
            message = "Notes generation retry limit was reached."
            due = datetime.now(timezone.utc)
        else:
            message = {
                "authentication_failed": "Notes provider authentication failed.",
                "invalid_transcript": "A ready transcript is required to generate notes.",
                "provider_error": "Notes provider returned an invalid response.",
            }.get(code, "Notes generation could not be completed.")
            due = datetime.now(timezone.utc)
        await _finish(job, status=status, code=code, error=message,
                      next_attempt_at=due.isoformat())
    except Exception:
        # Never persist exception text: provider SDK errors may contain request data.
        attempts = int(job.get("attempts", 1))
        max_attempts = min(int(job.get("max_attempts", MAX_ATTEMPTS)), MAX_ATTEMPTS)
        retry = attempts < max_attempts
        status = "pending" if retry else "failed"
        code = "provider_error" if retry else "retry_exhausted"
        message = ("Notes provider request failed and will be retried." if retry
                   else "Notes generation retry limit was reached.")
        due = datetime.now(timezone.utc) + timedelta(seconds=retry_delay(attempts) if retry else 0)
        await _finish(job, status=status, code=code, error=message,
                      next_attempt_at=due.isoformat())
    finally:
        stop.set()
        await asyncio.gather(lease_task, return_exceptions=True)


async def process_ready_meeting_notes_once() -> int:
    """Reconcile eligible meetings and process one bounded batch of notes jobs."""
    provider, _reason = configured_notes_provider()
    client = _client()
    await _db(lambda: client.rpc("enqueue_ready_meeting_notes", {
        "p_provider_ready": provider is not None, "p_limit": 50,
    }).execute())
    if provider is None:
        return 0
    jobs = await _db(lambda: _client().rpc("claim_meeting_notes_jobs", {
        "p_batch_size": 3, "p_lease_seconds": JOB_LEASE_SECONDS,
    }).execute().data or [])
    for job in jobs:
        await _process_job(job, provider)
    return len(jobs)

