import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import class_intelligence, transcription


def test_provider_unavailable_until_config_and_adapter_are_both_present(monkeypatch):
    monkeypatch.setattr(transcription.settings, "CLASS_INTELLIGENCE_STT_PROVIDER", "")
    monkeypatch.setattr(transcription.settings, "CLASS_INTELLIGENCE_STT_MODEL", "")
    provider, reason = transcription.configured_provider()
    assert provider is None
    assert "not configured" in reason

    monkeypatch.setattr(transcription.settings, "CLASS_INTELLIGENCE_STT_PROVIDER", "future-provider")
    monkeypatch.setattr(transcription.settings, "CLASS_INTELLIGENCE_STT_MODEL", "future-model")
    provider, reason = transcription.configured_provider()
    assert provider is None
    assert "No server-side adapter" in reason


def test_retry_backoff_is_exponential_and_bounded():
    assert [transcription.retry_delay(n) for n in (1, 2, 3)] == [60, 120, 240]
    assert transcription.retry_delay(100) == transcription.MAX_RETRY_SECONDS


def test_playlist_paths_are_scoped_to_private_recording_prefix():
    keys = transcription._parse_playlist(
        "#EXTM3U\nsegment-1.ts\nrecordings/ROOM/rec-1/segment-2.ts\n",
        "recordings/ROOM/rec-1/",
    )
    assert keys == [
        "recordings/ROOM/rec-1/segment-1.ts",
        "recordings/ROOM/rec-1/segment-2.ts",
    ]
    with pytest.raises(transcription.AudioPreparationError):
        transcription._parse_playlist("#EXTM3U\n../private.ts\n", "recordings/ROOM/rec-1/")
    with pytest.raises(transcription.AudioPreparationError):
        transcription._parse_playlist("#EXTM3U\nhttps://example.test/audio.ts\n", "recordings/ROOM/rec-1/")


@pytest.mark.asyncio
async def test_audio_preparation_downloads_segments_to_disk_and_runs_one_ffmpeg_pass(monkeypatch, tmp_path):
    class Storage:
        def __init__(self):
            self.downloaded = []

        async def get_object(self, key):
            return b"#EXTM3U\n#EXTINF:4,\nsegment.ts\n#EXT-X-ENDLIST\n"

        async def download_object_to_path(self, key, destination):
            self.downloaded.append(key)
            Path(destination).write_bytes(b"small mocked segment")

    storage = Storage()
    monkeypatch.setattr(transcription.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    calls = {}

    class Process:
        async def wait(self):
            Path(calls["output_path"]).write_bytes(b"FLAC")
            return 0

    async def fake_ffmpeg(*args, **kwargs):
        calls["args"] = args
        calls["output_path"] = args[-1]
        return Process()

    monkeypatch.setattr(transcription.asyncio, "create_subprocess_exec", fake_ffmpeg)
    recordings = [
        {"recording_id": "later", "started_at": "2026-01-02", "playlist_key": "p2",
         "storage_prefix": "recordings/R/later/"},
        {"recording_id": "first", "started_at": "2026-01-01", "playlist_key": "p1",
         "storage_prefix": "recordings/R/first/"},
    ]

    output = await transcription.prepare_meeting_audio(recordings, tmp_path, storage)

    assert output.is_file()
    assert storage.downloaded == [
        "recordings/R/first/segment.ts", "recordings/R/later/segment.ts",
    ]
    assert "-f" in calls["args"] and "concat" in calls["args"]
    assert "-c:a" in calls["args"] and "flac" in calls["args"]
    assert "-ar" in calls["args"] and "16000" in calls["args"]


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
async def test_audio_preparation_extracts_audio_from_synthetic_hls_segment(tmp_path):
    source = tmp_path / "source.ts"
    subprocess.run([
        shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:a", "aac", "-f", "mpegts", str(source),
    ], check=True, capture_output=True)
    segment_bytes = source.read_bytes()

    class Storage:
        async def get_object(self, _key):
            return b"#EXTM3U\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n"

        async def download_object_to_path(self, _key, destination):
            Path(destination).write_bytes(segment_bytes)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    output = await transcription.prepare_meeting_audio([{
        "recording_id": "synthetic", "started_at": "2026-01-01",
        "playlist_key": "recordings/R/synthetic/index.m3u8",
        "storage_prefix": "recordings/R/synthetic/",
    }], work_dir, Storage())

    assert output.stat().st_size > 0


@pytest.mark.asyncio
async def test_unfinalized_recording_never_calls_provider_and_retries(monkeypatch, tmp_path):
    job = {"id": "job", "meeting_id": "meeting", "lease_token": "lease",
           "attempts": 1, "max_attempts": 8}
    monkeypatch.setattr(transcription, "_recordings_for_meeting", _async_return([
        {"recording_id": "rec-1", "status": "processing"},
    ]))
    saved = []
    states = []
    monkeypatch.setattr(transcription, "_finish_job", _async_save(saved))
    monkeypatch.setattr(transcription, "_set_meeting_state", _async_state(states))
    monkeypatch.setattr(transcription, "_renew_lease", _async_renew())
    monkeypatch.setattr(transcription, "_db", _async_return(SimpleNamespace(data=[{}])))

    class Provider:
        async def transcribe(self, *_args):
            raise AssertionError("provider must not receive an unfinished recording")

    await transcription._process_job(job, Provider(), "fake", "model", object())

    assert saved[0]["status"] == "pending"
    assert saved[0]["error_code"] == "audio_preparation_failed"
    assert states[0][1] == "pending"


@pytest.mark.asyncio
async def test_successful_provider_result_preserves_unicode_and_marks_ready(monkeypatch, tmp_path):
    job = {"id": "job", "meeting_id": "meeting", "lease_token": "lease",
           "attempts": 1, "max_attempts": 8}
    monkeypatch.setattr(transcription, "_recordings_for_meeting", _async_return([
        {"recording_id": "rec-1", "status": "available", "started_at": "2026-01-01"},
        {"recording_id": "rec-2", "status": "available", "started_at": "2026-01-02"},
    ]))
    monkeypatch.setattr(transcription, "prepare_meeting_audio", _async_return(tmp_path / "audio.flac"))
    saved = []
    states = []
    monkeypatch.setattr(transcription, "_finish_job", _async_save(saved, result=True))
    monkeypatch.setattr(transcription, "_set_meeting_state", _async_state(states))
    monkeypatch.setattr(transcription, "_renew_lease", _async_renew())
    queries = []

    async def fake_db(fn):
        queries.append(fn)
        return SimpleNamespace(data=[{"recording_id": "rec-1"}, {"recording_id": "rec-2"}])

    monkeypatch.setattr(transcription, "_db", fake_db)

    class Provider:
        async def transcribe(self, path, metadata):
            assert path == tmp_path / "audio.flac"
            assert metadata["recording_ids"] == ["rec-1", "rec-2"]
            return transcription.TranscriptResult("বাংলা কথা", "bn", "fake", "model")

    await transcription._process_job(job, Provider(), "fake", "model", object())

    assert saved[0]["status"] == "ready"
    assert saved[0]["transcript_text"] == "বাংলা কথা"
    assert saved[0]["source_recording_ids"] == ["rec-1", "rec-2"]
    assert states == [("meeting", "ready", None)]
    assert len(queries) == 2  # recording processing jobs move through processing to complete


@pytest.mark.asyncio
async def test_provider_failure_retries_then_becomes_failed_without_secret_text(monkeypatch, tmp_path):
    job = {"id": "job", "meeting_id": "meeting", "lease_token": "lease",
           "attempts": 8, "max_attempts": 8}
    monkeypatch.setattr(transcription, "_recordings_for_meeting", _async_return([
        {"recording_id": "rec-1", "status": "available", "started_at": "2026-01-01"},
    ]))
    monkeypatch.setattr(transcription, "prepare_meeting_audio", _async_return(tmp_path / "audio.flac"))
    saved = []
    states = []
    monkeypatch.setattr(transcription, "_finish_job", _async_save(saved))
    monkeypatch.setattr(transcription, "_set_meeting_state", _async_state(states))
    monkeypatch.setattr(transcription, "_renew_lease", _async_renew())
    monkeypatch.setattr(transcription, "_db", _async_return(SimpleNamespace(data=[{}])))

    class Provider:
        async def transcribe(self, *_args):
            raise RuntimeError("Authorization: do-not-store-this-secret")

    await transcription._process_job(job, Provider(), "fake", "model", object())

    assert saved[0]["status"] == "failed"
    assert saved[0]["error_code"] == "provider_request_failed"
    assert "do-not-store-this-secret" not in saved[0]["error"]
    assert states[0][1] == "failed"


@pytest.mark.asyncio
async def test_transient_provider_failure_is_rescheduled(monkeypatch, tmp_path):
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
            raise transcription.STTProviderError("provider_timeout", retryable=True)

    await transcription._process_job(job, Provider(), "fake", "model", object())

    assert saved[0]["status"] == "pending"
    assert saved[0]["error_code"] == "provider_timeout"
    assert saved[0]["error"].endswith("It will be retried.")


@pytest.mark.asyncio
async def test_unconfigured_provider_marks_jobs_unavailable_without_claiming(monkeypatch):
    calls = []

    class Rpc:
        def __init__(self, name, params): self.name, self.params = name, params
        def execute(self):
            calls.append((self.name, self.params))
            return SimpleNamespace(data=0 if self.name != "claim_meeting_transcript_jobs" else [])

    class Client:
        def rpc(self, name, params): return Rpc(name, params)

    async def direct_db(operation): return operation()
    monkeypatch.setattr(transcription.settings, "validate_supabase_db", lambda: True)
    monkeypatch.setattr(transcription, "configured_provider", lambda: (None, "not configured"))
    monkeypatch.setattr(transcription, "_client", Client)
    monkeypatch.setattr(transcription, "_db", direct_db)

    assert await transcription.process_ready_transcripts_once(storage=object()) == 0
    assert [call[0] for call in calls] == [
        "mark_untranscribable_meetings", "enqueue_ready_meeting_transcripts",
    ]
    assert calls[1][1]["p_provider_ready"] is False


def _async_return(value):
    async def _return(*_args, **_kwargs):
        return value
    return _return


def _async_save(calls, result=True):
    async def _save(_job, values):
        calls.append(values)
        return result
    return _save


def _async_state(calls):
    async def _state(*args):
        calls.append(args)
    return _state


def _async_renew():
    async def _renew(_job, stop):
        await stop.wait()
    return _renew


def test_transcript_migration_keeps_results_meeting_scoped_and_recoverable():
    migration = (Path(__file__).parents[1] / "migrations" / "005_meeting_transcripts.sql").read_text()
    assert "meeting_id UUID NOT NULL UNIQUE" in migration
    assert "REFERENCES public.class_meetings(id) ON DELETE RESTRICT" in migration
    assert "recording_id TEXT REFERENCES" not in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FROM PUBLIC, anon, authenticated" in migration
    assert "GRANT SELECT, INSERT, UPDATE ON public.meeting_transcripts TO service_role" in migration
    assert "r.status <> 'available'" in migration
    assert "public.recording_processing_jobs" in migration
    assert "ON CONFLICT (meeting_id) DO UPDATE" in migration
    assert "meeting_transcripts.status = 'unavailable'" in migration
    assert "'provider_unconfigured'" in migration
    assert "lease_until < now()" in migration
    assert "attempts >= max_attempts" in migration
    assert "FOR UPDATE SKIP LOCKED" in migration


@pytest.mark.asyncio
async def test_transcript_retrieval_rejects_student_without_enrollment(monkeypatch):
    class Query:
        def __init__(self, data): self.data = data
        def select(self, *_args): return self
        def eq(self, *_args): return self
        def limit(self, *_args): return self
        def execute(self): return SimpleNamespace(data=self.data)

    calls = {"classes": [{"id": "class", "teacher_identity": "teacher",
                            "teacher_key_hash": class_intelligence._teacher_key_hash("owner-key")}],
             "class_meetings": [{"id": "meeting", "transcript_status": "ready"}],
             "class_enrollments": [], "meeting_transcripts": []}
    monkeypatch.setattr(class_intelligence.settings, "validate_supabase_db", lambda: True)
    monkeypatch.setattr(class_intelligence, "_client", lambda: SimpleNamespace(
        table=lambda name: Query(calls[name])))

    with pytest.raises(class_intelligence.TranscriptAccessDenied):
        await class_intelligence.get_authorized_meeting_transcript(
            room_code="ROOM", meeting_id="meeting", student_identity="google-other")


@pytest.mark.asyncio
async def test_enrolled_student_can_retrieve_their_meeting_transcript(monkeypatch):
    class Query:
        def __init__(self, data): self.data = data
        def select(self, *_args): return self
        def eq(self, *_args): return self
        def limit(self, *_args): return self
        def execute(self): return SimpleNamespace(data=self.data)

    rows = {
        "classes": [{"id": "class", "teacher_identity": "teacher",
                     "teacher_key_hash": class_intelligence._teacher_key_hash("key")}],
        "class_meetings": [{"id": "meeting", "transcript_status": "ready"}],
        "class_enrollments": [{"id": "enrollment"}],
        "meeting_transcripts": [{"status": "ready", "transcript_text": "বাংলা কথা"}],
    }
    monkeypatch.setattr(class_intelligence.settings, "validate_supabase_db", lambda: True)
    monkeypatch.setattr(class_intelligence, "_client", lambda: SimpleNamespace(
        table=lambda name: Query(rows[name])))

    result = await class_intelligence.get_authorized_meeting_transcript(
        room_code="ROOM", meeting_id="meeting", student_identity="google-approved")
    assert result["status"] == "ready"
    assert result["transcript_text"] == "বাংলা কথা"
