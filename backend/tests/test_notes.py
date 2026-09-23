import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx

from app import class_intelligence, gemini_notes, notes, transcription


class FakeGeminiClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    async def __aenter__(self): return self
    async def __aexit__(self, *_args): self.closed = True

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        item = self.responses.pop(0)
        if isinstance(item, tuple):
            status, body = item
        else:
            status, body = 200, {"candidates": [{"content": {"parts": [{"text": item}]}}]}
        return SimpleNamespace(status_code=status, headers={"content-type": "application/json"},
                               text="safe test response", json=lambda: body)

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_gemini_provider_returns_bilingual_notes_without_external_call(monkeypatch):
    monkeypatch.setattr(gemini_notes.settings, "CLASS_INTELLIGENCE_NOTES_MODEL", "test-model")
    monkeypatch.setattr(gemini_notes.settings, "GEMINI_API_BASE_URL", "https://gemini.test/v1beta")
    client = FakeGeminiClient([json.dumps({
        "english_notes": "Key Points\n- Primary Key identifies a row.",
        "bengali_notes": "মূল বিষয়\n- Primary Key একটি সারি শনাক্ত করে।",
    }, ensure_ascii=False)])
    provider = gemini_notes.GeminiNotesProvider(
        api_key="fake-key", model="test-model", endpoint="https://gemini.test/v1beta",
        client_factory=lambda **_kwargs: client,
    )
    transcript = "The teacher explained Primary Key."

    result = await provider.generate_notes(transcript)

    assert result.english_notes == "Key Points\n- Primary Key identifies a row."
    assert result.bengali_notes == "মূল বিষয়\n- Primary Key একটি সারি শনাক্ত করে।"
    assert "মূল বিষয়" in result.bengali_notes
    assert result.provider == "gemini" and result.model == "test-model"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == "https://gemini.test/v1beta/models/test-model:generateContent"
    assert call["headers"] == {"x-goog-api-key": "fake-key"}
    assert call["json"]["generationConfig"]["responseMimeType"] == "application/json"
    assert "untrusted class content" in call["json"]["systemInstruction"]["parts"][0]["text"]
    contents = call["json"]["contents"][0]["parts"][0]["text"]
    assert json.loads(contents.split("Transcript material as a JSON string: ", 1)[1]) == transcript
    assert client.closed


@pytest.mark.asyncio
async def test_gemini_actual_generate_content_shape_is_parsed_and_safely_diagnosed(monkeypatch, caplog):
    monkeypatch.setattr(gemini_notes.settings, "CLASS_INTELLIGENCE_NOTES_MODEL", "test-model")
    monkeypatch.setattr(gemini_notes.settings, "GEMINI_API_BASE_URL", "https://gemini.test/v1beta")
    transcript = "Private class transcript with sensitive details."
    json_text = json.dumps({
        "english_notes": "Key Points\\n- A supported fact.",
        "bengali_notes": "মূল বিষয়\\n- একটি সমর্থিত তথ্য।",
    }, ensure_ascii=False)
    actual_shape = {
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": json_text, "thoughtSignature": "opaque"}]},
            "finishReason": "STOP",
            "safetyRatings": [{"category": "HARM_CATEGORY_HARASSMENT", "probability": "NEGLIGIBLE"}],
        }],
        "usageMetadata": {"promptTokenCount": 31, "candidatesTokenCount": 22, "totalTokenCount": 53},
        "modelVersion": "test-model-version",
    }
    client = FakeGeminiClient([(200, actual_shape)])
    provider = gemini_notes.GeminiNotesProvider(
        api_key="private-test-api-key", model="test-model", endpoint="https://gemini.test/v1beta",
        client_factory=lambda **_kwargs: client,
    )

    with caplog.at_level("INFO", logger="app.gemini_notes"):
        result = await provider.generate_notes(transcript)

    assert result.english_notes == "Key Points\\n- A supported fact."
    assert result.bengali_notes == "মূল বিষয়\\n- একটি সমর্থিত তথ্য।"
    diagnostic = gemini_notes._response_diagnostic(
        SimpleNamespace(status_code=200, headers={"content-type": "application/json"}),
        actual_shape, "test-model",
    )
    assert diagnostic["http_status"] == 200
    assert diagnostic["content_type"] == "application/json"
    assert diagnostic["requested_model"] == "test-model"
    assert diagnostic["candidates"][0]["finish_reason"] == "STOP"
    assert diagnostic["candidates"][0]["parts"][0]["text_is_json"] is True
    assert '"finish_reason": "STOP"' in caplog.text
    assert '"text_is_json": true' in caplog.text
    assert '"http_status": 200' in caplog.text
    assert '"content_type": "application/json"' in caplog.text
    assert transcript not in caplog.text
    assert "private-test-api-key" not in caplog.text
    assert json_text not in str(diagnostic)
    assert transcript not in str(diagnostic)


@pytest.mark.asyncio
async def test_gemini_http_diagnostic_logs_retired_model_without_sensitive_content(monkeypatch, caplog):
    monkeypatch.setattr(gemini_notes.settings, "CLASS_INTELLIGENCE_NOTES_MODEL", "retired-model")
    monkeypatch.setattr(gemini_notes.settings, "GEMINI_API_BASE_URL", "https://gemini.test/v1beta")
    transcript = "Private class transcript that must never be logged."
    api_key = "private-test-api-key"
    body = {"error": {
        "code": 404, "status": "NOT_FOUND",
        "message": "This model is no longer available to new users. " + transcript + " " + api_key,
    }}
    client = FakeGeminiClient([(404, body)])
    provider = gemini_notes.GeminiNotesProvider(
        api_key=api_key, model="retired-model", endpoint="https://gemini.test/v1beta",
        client_factory=lambda **_kwargs: client,
    )

    with caplog.at_level("WARNING", logger="app.gemini_notes"):
        with pytest.raises(notes.NotesProviderError) as error:
            await provider.generate_notes(transcript)

    assert error.value.code == "provider_error"
    assert '"http_status": 404' in caplog.text
    assert '"requested_model": "retired-model"' in caplog.text
    assert '"model_unavailable_or_retired": true' in caplog.text
    assert transcript not in caplog.text
    assert api_key not in caplog.text
    assert body["error"]["message"] not in caplog.text


def test_gemini_diagnostic_summarizes_schema_error_without_logging_message_content():
    response = SimpleNamespace(status_code=400, headers={"content-type": "application/json"})
    body = {"error": {
        "code": 400, "status": "INVALID_ARGUMENT",
        "message": "responseJsonSchema is not supported for this model",
    }}

    diagnostic = gemini_notes._response_diagnostic(response, body)

    assert diagnostic["error_shape"]["status"] == "INVALID_ARGUMENT"
    assert diagnostic["error_shape"]["categories"]["schema_or_json_mode"] is True
    assert body["error"]["message"] not in str(diagnostic)


def test_gemini_provider_requires_key_and_model(monkeypatch):
    monkeypatch.setattr(gemini_notes.settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(gemini_notes.settings, "CLASS_INTELLIGENCE_NOTES_MODEL", "")
    monkeypatch.setattr(gemini_notes.settings, "GEMINI_API_BASE_URL", "")
    provider = gemini_notes.GeminiNotesProvider()
    assert not provider.validate_configuration()


def test_gemini_adapter_is_registered_with_notes_provider_registry(monkeypatch):
    monkeypatch.setattr(notes.settings, "CLASS_INTELLIGENCE_NOTES_PROVIDER", "gemini")
    monkeypatch.setattr(notes.settings, "CLASS_INTELLIGENCE_NOTES_MODEL", "test-model")
    monkeypatch.setattr(notes.settings, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(notes.settings, "GEMINI_API_BASE_URL", "https://gemini.test/v1beta")
    provider, reason = notes.configured_notes_provider()
    assert isinstance(provider, gemini_notes.GeminiNotesProvider)
    assert not reason


@pytest.mark.asyncio
async def test_provider_rejects_blank_transcript(monkeypatch):
    monkeypatch.setattr(gemini_notes.settings, "CLASS_INTELLIGENCE_NOTES_MODEL", "test-model")
    monkeypatch.setattr(gemini_notes.settings, "GEMINI_API_BASE_URL", "https://gemini.test/v1beta")
    provider = gemini_notes.GeminiNotesProvider(
        "fake-key", "test-model", "https://gemini.test/v1beta", lambda **_kwargs: None
    )
    with pytest.raises(notes.NotesProviderError) as error:
        await provider.generate_notes("  ")
    assert error.value.code == "invalid_transcript"
    assert not error.value.retryable


@pytest.mark.asyncio
async def test_malformed_gemini_response_is_rejected(monkeypatch):
    monkeypatch.setattr(gemini_notes.settings, "CLASS_INTELLIGENCE_NOTES_MODEL", "test-model")
    monkeypatch.setattr(gemini_notes.settings, "GEMINI_API_BASE_URL", "https://gemini.test/v1beta")
    provider = gemini_notes.GeminiNotesProvider(
        api_key="fake-key", model="test-model", endpoint="https://gemini.test/v1beta",
        client_factory=lambda **_kwargs: FakeGeminiClient(["not-json"]),
    )
    with pytest.raises(notes.NotesProviderError) as error:
        await provider.generate_notes("The class covered databases.")
    assert error.value.code == "provider_error"
    assert not error.value.retryable


@pytest.mark.parametrize(
    ("status", "expected_code", "retryable"),
    [(401, "authentication_failed", False), (403, "authentication_failed", False),
     (429, "rate_limited", True), (503, "provider_error", True),
     (400, "provider_error", False)],
)
def test_gemini_provider_api_errors_are_classified(status, expected_code, retryable):
    result = gemini_notes._api_error(status, "sanitized test failure")
    assert result.code == expected_code
    assert result.retryable is retryable
    assert "sanitized test failure" not in str(result)


def test_gemini_timeout_is_retryable():
    result = gemini_notes._api_error(None, exception=httpx.ReadTimeout("timeout"))
    assert result.code == "timeout"
    assert result.retryable


def test_long_transcript_chunking_preserves_every_character():
    transcript = ("অধ্যায় নিয়ে আলোচনা হয়েছে। " * 2_000) + "শেষ বাক্য।"
    chunks = gemini_notes._split_transcript(transcript)
    assert len(chunks) > 1
    assert "".join(chunks) == transcript
    assert all(len(chunk) <= gemini_notes.MAX_TRANSCRIPT_CHARS_PER_REQUEST for chunk in chunks)


@pytest.mark.asyncio
async def test_long_transcript_uses_partial_facts_then_bilingual_final(monkeypatch):
    monkeypatch.setattr(gemini_notes.settings, "CLASS_INTELLIGENCE_NOTES_MODEL", "test-model")
    monkeypatch.setattr(gemini_notes.settings, "GEMINI_API_BASE_URL", "https://gemini.test/v1beta")
    transcript = "A fact. " * 3_000
    responses = [json.dumps({"facts": f"fact chunk {i}"}) for i in range(2)]
    responses.append(json.dumps({"english_notes": "A fact.", "bengali_notes": "একটি তথ্য।"}, ensure_ascii=False))
    client = FakeGeminiClient(responses)
    provider = gemini_notes.GeminiNotesProvider(
        "fake-key", "test-model", "https://gemini.test/v1beta", lambda **_kwargs: client
    )

    result = await provider.generate_notes(transcript)

    assert result.english_notes == "A fact."
    assert result.bengali_notes == "একটি তথ্য।"
    assert len(client.calls) == 3
    final_contents = client.calls[-1]["json"]["contents"][0]["parts"][0]["text"]
    assert "fact chunk 0" in final_contents


@pytest.mark.asyncio
async def test_unconfigured_notes_provider_marks_ready_transcript_unavailable(monkeypatch):
    calls = []

    class Rpc:
        def __init__(self, name, params): self.name, self.params = name, params
        def execute(self):
            calls.append((self.name, self.params))
            return SimpleNamespace(data=0)

    class Client:
        def rpc(self, name, params): return Rpc(name, params)

    monkeypatch.setattr(notes, "configured_notes_provider", lambda: (None, "not configured"))
    monkeypatch.setattr(notes, "_client", Client)
    monkeypatch.setattr(notes, "_db", _direct_db)

    assert await notes.process_ready_meeting_notes_once() == 0
    assert calls == [("enqueue_ready_meeting_notes", {"p_provider_ready": False, "p_limit": 50})]


@pytest.mark.asyncio
async def test_ready_transcript_generates_and_persists_both_languages(monkeypatch):
    job = {"id": "notes-row", "meeting_id": "meeting", "lease_token": "lease",
           "attempts": 1, "max_attempts": 8}
    calls = []

    class Query:
        def select(self, *_args): return self
        def eq(self, *_args): return self
        def limit(self, *_args): return self
        def execute(self):
            return SimpleNamespace(data=[{
                "id": "transcript-row", "status": "ready",
                "transcript_text": "Primary keys identify rows.",
                "segments": [{"text": "Primary keys identify rows.", "speaker_id": "0"}],
            }])

    class Rpc:
        def __init__(self, name, params): self.name, self.params = name, params
        def execute(self):
            calls.append((self.name, self.params))
            return SimpleNamespace(data=True)

    class Client:
        def table(self, _name): return Query()
        def rpc(self, name, params): return Rpc(name, params)

    class Provider:
        async def generate_notes(self, transcript_text, segments):
            assert transcript_text == "Primary keys identify rows."
            assert segments[0]["speaker_id"] == "0"
            return class_intelligence.MeetingNotesResult(
                "Key Concept: Primary keys identify rows.",
                "মূল ধারণা: Primary Key সারি শনাক্ত করে।",
                "gemini", "test-model",
            )

    monkeypatch.setattr(notes, "_client", Client)
    monkeypatch.setattr(notes, "_db", _direct_db)
    monkeypatch.setattr(notes, "_renew_lease", _no_renewal())

    await notes._process_job(job, Provider())

    assert calls[0][0] == "finish_meeting_notes_job"
    saved = calls[0][1]
    assert saved["p_status"] == "ready"
    assert saved["p_english_notes"] == "Key Concept: Primary keys identify rows."
    assert saved["p_bengali_notes"] == "মূল ধারণা: Primary Key সারি শনাক্ত করে।"
    assert saved["p_source_transcript_id"] == "transcript-row"
    assert saved["p_provider"] == "gemini"


@pytest.mark.asyncio
async def test_pending_transcript_is_terminally_rejected_without_provider_call(monkeypatch):
    job = {"id": "notes-row", "meeting_id": "meeting", "lease_token": "lease",
           "attempts": 1, "max_attempts": 8}
    saved = []

    class Query:
        def select(self, *_args): return self
        def eq(self, *_args): return self
        def limit(self, *_args): return self
        def execute(self):
            return SimpleNamespace(data=[{"id": "transcript", "status": "pending", "transcript_text": None}])

    class Rpc:
        def __init__(self, name, params): self.name, self.params = name, params
        def execute(self):
            saved.append((self.name, self.params))
            return SimpleNamespace(data=True)

    class Client:
        def table(self, _name): return Query()
        def rpc(self, name, params): return Rpc(name, params)

    class Provider:
        async def generate_notes(self, *_args): raise AssertionError("pending transcript must not be processed")

    monkeypatch.setattr(notes, "_client", Client)
    monkeypatch.setattr(notes, "_db", _direct_db)
    monkeypatch.setattr(notes, "_renew_lease", _no_renewal())
    await notes._process_job(job, Provider())
    assert saved[0][1]["p_status"] == "failed"
    assert saved[0][1]["p_error_code"] == "invalid_transcript"


@pytest.mark.parametrize(
    ("error_code", "retryable", "attempts", "expected_status", "expected_code"),
    [("rate_limited", True, 1, "pending", "rate_limited"),
     ("authentication_failed", False, 1, "failed", "authentication_failed"),
     ("rate_limited", True, 8, "failed", "retry_exhausted")],
)
@pytest.mark.asyncio
async def test_worker_retries_transient_failures_and_stops_permanent_or_exhausted(
    monkeypatch, error_code, retryable, attempts, expected_status, expected_code
):
    job = {"id": "notes-row", "meeting_id": "meeting", "lease_token": "lease",
           "attempts": attempts, "max_attempts": 8}
    saved = []

    class Query:
        def select(self, *_args): return self
        def eq(self, *_args): return self
        def limit(self, *_args): return self
        def execute(self):
            return SimpleNamespace(data=[{
                "id": "transcript", "status": "ready", "transcript_text": "A supported fact.",
                "segments": None,
            }])

    class Rpc:
        def __init__(self, name, params): self.name, self.params = name, params
        def execute(self):
            saved.append((self.name, self.params))
            return SimpleNamespace(data=True)

    class Client:
        def table(self, _name): return Query()
        def rpc(self, name, params): return Rpc(name, params)

    class Provider:
        async def generate_notes(self, *_args):
            raise notes.NotesProviderError(error_code, retryable)

    monkeypatch.setattr(notes, "_client", Client)
    monkeypatch.setattr(notes, "_db", _direct_db)
    monkeypatch.setattr(notes, "_renew_lease", _no_renewal())
    await notes._process_job(job, Provider())
    assert saved[0][1]["p_status"] == expected_status
    assert saved[0][1]["p_error_code"] == expected_code


def test_notes_migration_is_additive_secure_and_recovers_expired_leases():
    migration = (Path(__file__).parents[1] / "migrations" / "006_meeting_notes_jobs.sql").read_text()
    original = (Path(__file__).parents[1] / "migrations" / "003_create_class_intelligence.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS public.meeting_notes" in original
    assert "ALTER TABLE public.meeting_notes" in migration
    assert "ADD CONSTRAINT meeting_notes_source_transcript_id_fkey" in migration
    assert "ON DELETE SET NULL" in migration
    assert "meeting_id UUID NOT NULL UNIQUE" in original
    assert "REFERENCES public.class_meetings(id) ON DELETE RESTRICT" in original
    assert "source_transcript_id UUID" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FROM PUBLIC, anon, authenticated" in migration
    assert "TO service_role" in migration
    assert "t.status = 'ready'" in migration
    assert "ON CONFLICT (meeting_id) DO UPDATE" in migration
    assert "FOR UPDATE SKIP LOCKED" in migration
    assert "lease_until < now()" in migration
    assert "attempts >= max_attempts" in migration
    assert "finish_meeting_notes_job" in migration


def test_notes_provider_unconfigured_has_no_fabricated_result(monkeypatch):
    monkeypatch.setattr(notes.settings, "CLASS_INTELLIGENCE_NOTES_PROVIDER", "")
    monkeypatch.setattr(notes.settings, "CLASS_INTELLIGENCE_NOTES_MODEL", "")
    provider, reason = notes.configured_notes_provider()
    assert provider is None
    assert "not configured" in reason


@pytest.mark.asyncio
async def test_notes_retrieval_denies_unenrolled_student(monkeypatch):
    rows = {
        "classes": [{"id": "class", "teacher_identity": "teacher",
                     "teacher_key_hash": class_intelligence._teacher_key_hash("key")}],
        "class_meetings": [{"id": "meeting", "notes_status": "ready", "notes_error": None}],
        "class_enrollments": [], "meeting_notes": [],
    }
    monkeypatch.setattr(class_intelligence.settings, "validate_supabase_db", lambda: True)
    monkeypatch.setattr(class_intelligence, "_client", lambda: SimpleNamespace(
        table=lambda name: _Query(rows[name])))

    with pytest.raises(class_intelligence.TranscriptAccessDenied):
        await class_intelligence.get_authorized_meeting_notes(
            room_code="ROOM", meeting_id="meeting", student_identity="google-not-enrolled")


@pytest.mark.asyncio
async def test_enrolled_student_retrieves_persistent_bilingual_notes(monkeypatch):
    rows = {
        "classes": [{"id": "class", "teacher_identity": "teacher",
                     "teacher_key_hash": class_intelligence._teacher_key_hash("key")}],
        "class_meetings": [{"id": "meeting", "notes_status": "ready", "notes_error": None}],
        "class_enrollments": [{"id": "enrollment"}],
        "meeting_notes": [{"status": "ready", "english_notes": "Key point.",
                           "bengali_notes": "মূল বিষয়।", "provider": "gemini",
                           "model": "test-model", "error_code": None}],
    }
    monkeypatch.setattr(class_intelligence.settings, "validate_supabase_db", lambda: True)
    monkeypatch.setattr(class_intelligence, "_client", lambda: SimpleNamespace(
        table=lambda name: _Query(rows[name])))

    result = await class_intelligence.get_authorized_meeting_notes(
        room_code="ROOM", meeting_id="meeting", student_identity="google-approved")
    assert result["english_notes"] == "Key point."
    assert result["bengali_notes"] == "মূল বিষয়।"


@pytest.mark.asyncio
async def test_recordings_notes_listing_includes_permanent_notes_without_recording_rows(monkeypatch):
    rows = {
        "classes": [{"id": "class", "room_code": "ROOM", "class_name": "Astrology Class"}],
        "class_meetings": [{
            "id": "meeting", "class_id": "class", "room_code": "ROOM",
            "room_created_at": "2026-09-22T13:30:00+00:00", "ended_at": "2026-09-22T14:30:00+00:00",
            "notes_status": "ready", "notes_error": None, "recording_id": None,
        }],
        "meeting_notes": [{
            "meeting_id": "meeting", "status": "ready", "english_notes": "Important concepts.",
            "bengali_notes": "গুরুত্বপূর্ণ ধারণা।", "error_code": None,
            "updated_at": "2026-09-22T14:35:00+00:00", "completed_at": "2026-09-22T14:35:00+00:00",
        }],
        "meeting_recordings": [],
    }
    monkeypatch.setattr(class_intelligence.settings, "validate_supabase_db", lambda: True)
    monkeypatch.setattr(class_intelligence, "_client", lambda: SimpleNamespace(
        table=lambda name: _Query(rows[name])))

    result = await class_intelligence.list_recordings_meeting_notes()

    assert result == [{
        "meeting_id": "meeting", "room_code": "ROOM", "class_name": "Astrology Class",
        "meeting_started_at": "2026-09-22T13:30:00+00:00", "ended_at": "2026-09-22T14:30:00+00:00",
        "notes_status": "ready", "english_notes": "Important concepts.",
        "bengali_notes": "গুরুত্বপূর্ণ ধারণা।", "error_code": None,
        "updated_at": "2026-09-22T14:35:00+00:00", "completed_at": "2026-09-22T14:35:00+00:00",
        "recording_ids": [],
    }]


@pytest.mark.asyncio
async def test_recordings_meeting_notes_api_requires_recordings_access_token(monkeypatch):
    from app import main

    with pytest.raises(main.HTTPException) as error:
        await main.get_recordings_meeting_notes(None)

    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_recordings_meeting_notes_api_uses_existing_recordings_token(monkeypatch):
    from app import main

    monkeypatch.setattr(main.livekit_service, "verify_recordings_access_token", lambda token: token == "signed")
    monkeypatch.setattr(class_intelligence, "list_recordings_meeting_notes", AsyncMock(return_value=[
        {"meeting_id": "meeting", "class_name": "Astrology Class", "notes_status": "ready"},
    ]))

    result = await main.get_recordings_meeting_notes("Bearer signed")

    assert result["total"] == 1
    assert result["meetings"][0]["meeting_id"] == "meeting"


@pytest.mark.asyncio
async def test_recordings_notes_download_revalidates_token_and_returns_persisted_language(monkeypatch):
    from app import main

    monkeypatch.setattr(main.livekit_service, "verify_recordings_access_token", lambda token: token == "signed")
    monkeypatch.setattr(class_intelligence, "get_recordings_meeting_note", AsyncMock(return_value={
        "status": "ready", "class_name": "Astrology Class", "english_notes": "Notes in English.",
        "bengali_notes": "বাংলা নোট।",
    }))

    with pytest.raises(main.HTTPException) as unauthorized:
        await main.get_recordings_meeting_note_download("meeting", "bengali", None)
    assert unauthorized.value.status_code == 401

    result = await main.get_recordings_meeting_note_download(
        "meeting", "bengali", "Bearer signed",
    )
    assert result == {"class_name": "Astrology Class", "content": "বাংলা নোট।"}


@pytest.mark.asyncio
async def test_notes_api_requires_an_existing_class_identity(monkeypatch):
    from app import main

    with pytest.raises(main.HTTPException) as error:
        await main.get_meeting_notes("ROOM", "meeting", main.TranscriptAccessRequest())
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_notes_api_uses_existing_google_identity_authorization(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "verify_google_credential", lambda _credential: {"sub": "approved-sub"})
    captured = {}

    async def get_notes(**kwargs):
        captured.update(kwargs)
        return {"status": "ready", "english_notes": "Key point.", "bengali_notes": "মূল বিষয়।"}

    monkeypatch.setattr(class_intelligence, "get_authorized_meeting_notes", get_notes)
    result = await main.get_meeting_notes(
        "ROOM", "meeting", main.TranscriptAccessRequest(google_credential="google-token")
    )
    assert result["status"] == "ready"
    assert captured["student_identity"] == "google_approved-sub"


class _Query:
    def __init__(self, data): self.data = data
    def select(self, *_args): return self
    def order(self, *_args, **_kwargs): return self
    def range(self, *_args): return self
    def eq(self, *_args): return self
    def limit(self, *_args): return self
    def execute(self): return SimpleNamespace(data=self.data)


def _direct_db(operation):
    async def run():
        return operation()
    return run()


def _no_renewal():
    async def renew(_job, stop):
        await stop.wait()
    return renew
