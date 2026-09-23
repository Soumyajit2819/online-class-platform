"""Sarvam Batch STT adapter for the durable meeting-transcription worker."""

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Callable

from .class_intelligence import TranscriptResult
from .config import settings
from .transcription import STTProviderError

MODEL = "saaras:v4"
POLL_INTERVAL_SECONDS = 10
# Sarvam documents a maximum two-hour input. Allow a full two-hour batch
# processing window; the outer worker independently renews the DB lease.
JOB_TIMEOUT_SECONDS = 2 * 60 * 60


def _sdk_client(api_key: str):
    from sarvamai import SarvamAI

    return SarvamAI(api_subscription_key=api_key)


def _read_field(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(field, default)
    return getattr(value, field, default)


def _api_error(error: Exception) -> STTProviderError:
    """Reduce SDK/network exceptions to safe lifecycle codes."""
    status = getattr(error, "status_code", None)
    if status in (401, 403):
        return STTProviderError("provider_auth_failed", retryable=False)
    if status == 429:
        return STTProviderError("provider_rate_limited", retryable=True)
    if status == 408 or (isinstance(status, int) and status >= 500):
        return STTProviderError("provider_request_failed", retryable=True)
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return STTProviderError("provider_timeout", retryable=True)
    if isinstance(status, int) and 400 <= status < 500:
        return STTProviderError("provider_request_failed", retryable=False)
    return STTProviderError("provider_request_failed", retryable=True)


def _segments(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    diarized = payload.get("diarized_transcript")
    entries = _read_field(diarized, "entries")
    if isinstance(entries, list):
        normalized = []
        for entry in entries:
            text = _read_field(entry, "transcript")
            if not isinstance(text, str):
                continue
            normalized.append({
                "text": text,
                "start_time_seconds": _read_field(entry, "start_time_seconds"),
                "end_time_seconds": _read_field(entry, "end_time_seconds"),
                "speaker_id": _read_field(entry, "speaker_id"),
            })
        return normalized or None

    timestamps = payload.get("timestamps")
    if not isinstance(timestamps, dict):
        return None
    chunks = timestamps.get("chunks")
    starts = timestamps.get("start_time_seconds")
    ends = timestamps.get("end_time_seconds")
    if not isinstance(chunks, list) or not isinstance(starts, list) or not isinstance(ends, list):
        return None
    return [
        {"text": text, "start_time_seconds": start, "end_time_seconds": end}
        for text, start, end in zip(chunks, starts, ends)
        if isinstance(text, str)
    ] or None


class SarvamBatchSTTProvider:
    """Synchronous Sarvam SDK orchestration isolated from the async worker."""

    def __init__(self, api_key: str | None = None,
                 client_factory: Callable[[str], Any] | None = None):
        self._api_key = settings.SARVAM_API_KEY if api_key is None else api_key
        self._client_factory = client_factory or _sdk_client

    def validate_configuration(self) -> bool:
        return bool(self._api_key and self._api_key.strip())

    async def transcribe(self, audio_path: Path,
                         metadata: dict[str, Any]) -> TranscriptResult:
        if not self.validate_configuration():
            raise STTProviderError("provider_auth_failed", retryable=False)
        try:
            return await asyncio.to_thread(self._transcribe_sync, audio_path)
        except STTProviderError:
            raise
        except Exception as exc:
            raise _api_error(exc) from None

    def _transcribe_sync(self, audio_path: Path) -> TranscriptResult:
        if not audio_path.is_file() or audio_path.stat().st_size == 0:
            raise STTProviderError("provider_output_invalid", retryable=False)

        try:
            client = self._client_factory(self._api_key)
            job = client.speech_to_text_job.create_job(
                model=MODEL,
                mode="transcribe",
                with_diarization=True,
            )
            job.upload_files(file_paths=[str(audio_path)])
            job.start()
            status = job.wait_until_complete(
                poll_interval=POLL_INTERVAL_SECONDS,
                timeout=JOB_TIMEOUT_SECONDS,
            )
            job_state = str(_read_field(status, "job_state", "")).lower()
            if job_state == "failed":
                raise STTProviderError("provider_job_failed", retryable=False)

            file_results = job.get_file_results()
            successful = _read_field(file_results, "successful", []) or []
            failed = _read_field(file_results, "failed", []) or []
            if failed or len(successful) != 1:
                raise STTProviderError("provider_job_failed", retryable=False)

            with tempfile.TemporaryDirectory(prefix="sarvam-output-") as output_dir:
                job.download_outputs(output_dir=output_dir)
                result_files = sorted(Path(output_dir).rglob("*.json"))
                if len(result_files) != 1:
                    raise STTProviderError("provider_output_invalid", retryable=True)
                try:
                    payload = json.loads(result_files[0].read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    raise STTProviderError("provider_output_invalid", retryable=True) from None

            if not isinstance(payload, dict):
                raise STTProviderError("provider_output_invalid", retryable=True)
            text = payload.get("transcript")
            if not isinstance(text, str) or not text.strip():
                raise STTProviderError("empty_result", retryable=False)
            language = payload.get("language_code")
            if not isinstance(language, str):
                language = None
            return TranscriptResult(
                text=text,
                language=language,
                provider="sarvam",
                model=MODEL,
                segments=_segments(payload),
            )
        except STTProviderError:
            raise
        except Exception as exc:
            raise _api_error(exc) from None
