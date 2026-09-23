import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import sarvam_stt, transcription


class FakeBatchJob:
    def __init__(self, result_payload=None, *, status=None, file_results=None):
        self.result_payload = result_payload or {}
        self.status = status or SimpleNamespace(job_state="Completed")
        self.file_results = file_results or {"successful": [{"file_name": "meeting.flac"}], "failed": []}
        self.uploaded = None
        self.started = False
        self.wait_args = None
        self.downloaded = False

    def upload_files(self, *, file_paths):
        self.uploaded = file_paths

    def start(self):
        self.started = True

    def wait_until_complete(self, **kwargs):
        self.wait_args = kwargs
        return self.status

    def get_file_results(self):
        return self.file_results

    def download_outputs(self, *, output_dir):
        self.downloaded = True
        Path(output_dir, "0.json").write_text(
            json.dumps(self.result_payload, ensure_ascii=False), encoding="utf-8"
        )


class FakeClient:
    def __init__(self, job):
        self.job = job
        self.kwargs = None
        self.speech_to_text_job = self

    def create_job(self, **kwargs):
        self.kwargs = kwargs
        return self.job


def test_provider_requires_backend_api_key(monkeypatch):
    monkeypatch.setattr(sarvam_stt.settings, "SARVAM_API_KEY", "")
    provider = sarvam_stt.SarvamBatchSTTProvider()
    assert not provider.validate_configuration()


def test_provider_initializes_with_api_key():
    provider = sarvam_stt.SarvamBatchSTTProvider(api_key="test-key")
    assert provider.validate_configuration()


def test_sarvam_adapter_is_available_through_existing_provider_registry(monkeypatch):
    monkeypatch.setattr(transcription.settings, "CLASS_INTELLIGENCE_STT_PROVIDER", "sarvam")
    monkeypatch.setattr(transcription.settings, "CLASS_INTELLIGENCE_STT_MODEL", "saaras:v4")
    monkeypatch.setattr(transcription.settings, "SARVAM_API_KEY", "test-key")

    provider, reason = transcription.configured_provider()

    assert isinstance(provider, sarvam_stt.SarvamBatchSTTProvider)
    assert not reason


@pytest.mark.asyncio
async def test_batch_uses_saaras_v4_transcribe_diarization_and_preserves_bengali(tmp_path):
    audio_path = tmp_path / "meeting.flac"
    audio_path.write_bytes(b"audio")
    job = FakeBatchJob({
        "transcript": "আজকের ক্লাসে আলোচনা হবে।",
        "language_code": "bn-IN",
        "diarized_transcript": {"entries": [{
            "transcript": "আজকের ক্লাসে আলোচনা হবে।",
            "start_time_seconds": 1.25,
            "end_time_seconds": 3.5,
            "speaker_id": "0",
        }]},
    })
    client = FakeClient(job)
    provider = sarvam_stt.SarvamBatchSTTProvider(
        api_key="test-key", client_factory=lambda key: (assert_key(key), client)[1]
    )

    result = await provider.transcribe(audio_path, {})

    assert result.text == "আজকের ক্লাসে আলোচনা হবে।"
    assert result.language == "bn-IN"
    assert result.provider == "sarvam"
    assert result.model == "saaras:v4"
    assert result.segments == [{
        "text": "আজকের ক্লাসে আলোচনা হবে।",
        "start_time_seconds": 1.25,
        "end_time_seconds": 3.5,
        "speaker_id": "0",
    }]
    assert client.kwargs == {
        "model": "saaras:v4", "mode": "transcribe", "with_diarization": True,
    }
    assert job.uploaded == [str(audio_path)]
    assert job.started and job.downloaded
    assert job.wait_args == {
        "poll_interval": sarvam_stt.POLL_INTERVAL_SECONDS,
        "timeout": sarvam_stt.JOB_TIMEOUT_SECONDS,
    }


def assert_key(value):
    assert value == "test-key"
    return True


@pytest.mark.asyncio
async def test_batch_uses_automatic_language_and_preserves_timestamp_chunks(tmp_path):
    audio_path = tmp_path / "meeting.flac"
    audio_path.write_bytes(b"audio")
    job = FakeBatchJob({
        "transcript": "Hello class.",
        "timestamps": {
            "chunks": ["Hello class."],
            "start_time_seconds": [0.2],
            "end_time_seconds": [1.7],
        },
    })
    client = FakeClient(job)
    provider = sarvam_stt.SarvamBatchSTTProvider("test-key", lambda _key: client)

    result = await provider.transcribe(audio_path, {})

    assert "language_code" not in client.kwargs
    assert result.segments == [{
        "text": "Hello class.", "start_time_seconds": 0.2, "end_time_seconds": 1.7,
    }]


@pytest.mark.asyncio
async def test_empty_transcript_is_rejected(tmp_path):
    audio_path = tmp_path / "meeting.flac"
    audio_path.write_bytes(b"audio")
    client = FakeClient(FakeBatchJob({"transcript": "  "}))
    provider = sarvam_stt.SarvamBatchSTTProvider("test-key", lambda _key: client)

    with pytest.raises(transcription.STTProviderError) as error:
        await provider.transcribe(audio_path, {})

    assert error.value.code == "empty_result"
    assert not error.value.retryable


@pytest.mark.asyncio
async def test_failed_batch_job_is_rejected(tmp_path):
    audio_path = tmp_path / "meeting.flac"
    audio_path.write_bytes(b"audio")
    job = FakeBatchJob(status=SimpleNamespace(job_state="Failed"))
    provider = sarvam_stt.SarvamBatchSTTProvider(
        "test-key", lambda _key: FakeClient(job)
    )

    with pytest.raises(transcription.STTProviderError) as error:
        await provider.transcribe(audio_path, {})

    assert error.value.code == "provider_job_failed"
    assert not error.value.retryable


@pytest.mark.asyncio
async def test_batch_timeout_is_retryable(tmp_path):
    audio_path = tmp_path / "meeting.flac"
    audio_path.write_bytes(b"audio")
    job = FakeBatchJob()

    def timeout(*_args, **_kwargs):
        raise TimeoutError("provider timeout")

    job.wait_until_complete = timeout
    provider = sarvam_stt.SarvamBatchSTTProvider(
        "test-key", lambda _key: FakeClient(job)
    )

    with pytest.raises(transcription.STTProviderError) as error:
        await provider.transcribe(audio_path, {})

    assert error.value.code == "provider_timeout"
    assert error.value.retryable


@pytest.mark.parametrize(
    ("status", "expected_code", "retryable"),
    [(401, "provider_auth_failed", False), (403, "provider_auth_failed", False),
     (429, "provider_rate_limited", True), (503, "provider_request_failed", True),
     (400, "provider_request_failed", False)],
)
@pytest.mark.asyncio
async def test_api_status_errors_are_classified_without_exposing_details(
    tmp_path, status, expected_code, retryable
):
    audio_path = tmp_path / "meeting.flac"
    audio_path.write_bytes(b"audio")

    class ApiError(Exception):
        status_code = status

    def fail_client(_key):
        raise ApiError("private provider response and key")

    provider = sarvam_stt.SarvamBatchSTTProvider("test-key", fail_client)
    with pytest.raises(transcription.STTProviderError) as error:
        await provider.transcribe(audio_path, {})
    assert error.value.code == expected_code
    assert error.value.retryable is retryable
    assert "private provider response" not in str(error.value)


@pytest.mark.asyncio
async def test_missing_key_is_permanent_configuration_failure(tmp_path):
    audio_path = tmp_path / "meeting.flac"
    audio_path.write_bytes(b"audio")
    provider = sarvam_stt.SarvamBatchSTTProvider(api_key="")
    with pytest.raises(transcription.STTProviderError) as error:
        await provider.transcribe(audio_path, {})
    assert error.value.code == "provider_auth_failed"
    assert not error.value.retryable


@pytest.mark.asyncio
async def test_auth_failure_is_terminal_in_existing_durable_worker(monkeypatch, tmp_path):
    job = {"id": "job", "meeting_id": "meeting", "lease_token": "lease",
           "attempts": 1, "max_attempts": 8}
    monkeypatch.setattr(transcription, "_recordings_for_meeting", _async_return([
        {"recording_id": "rec-1", "status": "available", "started_at": "2026-01-01"},
    ]))
    monkeypatch.setattr(transcription, "prepare_meeting_audio", _async_return(tmp_path / "audio.flac"))
    saved, states = [], []
    monkeypatch.setattr(transcription, "_finish_job", _async_save(saved))
    monkeypatch.setattr(transcription, "_set_meeting_state", _async_state(states))
    monkeypatch.setattr(transcription, "_renew_lease", _async_renew())
    monkeypatch.setattr(transcription, "_db", _async_return(SimpleNamespace(data=[{}])))

    class Provider:
        async def transcribe(self, *_args):
            raise transcription.STTProviderError("provider_auth_failed", retryable=False)

    await transcription._process_job(job, Provider(), "sarvam", "saaras:v4", object())
    assert saved[0]["status"] == "failed"
    assert saved[0]["error_code"] == "provider_auth_failed"
    assert states[0][1] == "failed"


@pytest.mark.asyncio
async def test_temporary_provider_failure_remains_retryable_in_worker(monkeypatch, tmp_path):
    job = {"id": "job", "meeting_id": "meeting", "lease_token": "lease",
           "attempts": 1, "max_attempts": 8}
    monkeypatch.setattr(transcription, "_recordings_for_meeting", _async_return([
        {"recording_id": "rec-1", "status": "available", "started_at": "2026-01-01"},
    ]))
    monkeypatch.setattr(transcription, "prepare_meeting_audio", _async_return(tmp_path / "audio.flac"))
    saved = []
    monkeypatch.setattr(transcription, "_finish_job", _async_save(saved))
    monkeypatch.setattr(transcription, "_set_meeting_state", _async_state([]))
    monkeypatch.setattr(transcription, "_renew_lease", _async_renew())
    monkeypatch.setattr(transcription, "_db", _async_return(SimpleNamespace(data=[{}])))

    class Provider:
        async def transcribe(self, *_args):
            raise transcription.STTProviderError("provider_rate_limited", retryable=True)

    await transcription._process_job(job, Provider(), "sarvam", "saaras:v4", object())
    assert saved[0]["status"] == "pending"
    assert saved[0]["error_code"] == "provider_rate_limited"


def _async_return(value):
    async def _return(*_args, **_kwargs):
        return value
    return _return


def _async_save(calls):
    async def _save(_job, values):
        calls.append(values)
        return True
    return _save


def _async_state(calls):
    async def _state(*args):
        calls.append(args)
    return _state


def _async_renew():
    async def _renew(_job, stop):
        await stop.wait()
    return _renew
