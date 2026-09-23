import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request
from fastapi import HTTPException
from livekit.api import AccessToken
from livekit.protocol.egress import EgressInfo, EgressStatus, SegmentsInfo
from livekit.protocol.webhook import WebhookEvent

from app import main
from app.config import settings
from app.livekit_service import LiveKitService


def _record(status="recording"):
    return {
        "recording_id": "rec-test", "room_code": "ROOM1",
        "livekit_room_name": "lk-room", "class_name": "Math",
        "teacher_name": "Teacher", "egress_id": "EG-test",
        "storage_prefix": "recordings/ROOM1/rec-test/",
        "playlist_key": "recordings/ROOM1/rec-test/index.m3u8",
        "started_at": "2026-09-23T10:00:00+00:00",
        "ended_at": None, "expires_at": "2026-09-24T06:00:00+00:00",
        "status": status,
    }


@pytest.mark.asyncio
async def test_successful_start_persists_egress_and_association(monkeypatch):
    service = LiveKitService()
    record = _record("starting")
    record["egress_id"] = None
    created = AsyncMock()
    updated = AsyncMock()
    egress = SimpleNamespace(egress_id="EG-started", status=EgressStatus.EGRESS_ACTIVE)
    egress_service = SimpleNamespace(start_room_composite_egress=AsyncMock(return_value=egress))
    monkeypatch.setattr(service, "generate_recording_id", lambda: "rec-test")
    monkeypatch.setattr(service, "_create_recording_metadata", created)
    monkeypatch.setattr(service, "_update_recording_metadata", updated)
    monkeypatch.setattr(service, "get_egress_service", AsyncMock(return_value=egress_service))
    monkeypatch.setattr(service, "_associate_recording", AsyncMock(return_value="meeting-id"))
    monkeypatch.setattr("app.livekit_service.recording_state.add_recording", lambda *_args: None)
    monkeypatch.setattr("app.livekit_service.session_state.set_recording", lambda *_args: None)

    result = await service.start_recording("ROOM1", "lk-room", "Math", "Teacher")

    assert result["success"] is True
    assert result["egress_id"] == "EG-started"
    created.assert_awaited_once()
    assert created.await_args.args[0]["livekit_room_name"] == "lk-room"
    updated.assert_awaited_once_with("rec-test", {
        "egress_id": "EG-started", "status": "recording", "finalization_last_error": None,
    })
    egress_service.start_room_composite_egress.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_can_repair_missing_egress_id_by_room_and_start_time(monkeypatch):
    service = LiveKitService()
    record = _record("starting")
    record["egress_id"] = None

    class Query:
        def select(self, *_args): return self
        def eq(self, *_args): return self
        def is_(self, *_args): return self

    monkeypatch.setattr(service, "_recordings_table", lambda: Query())
    monkeypatch.setattr(service, "_recording_query", AsyncMock(return_value=SimpleNamespace(data=[record])))
    updates = AsyncMock()
    monkeypatch.setattr(service, "_update_recording_metadata", updates)
    info = SimpleNamespace(
        room_name="lk-room", started_at=1790157600, egress_id="EG-recovered"
    )

    matched = await service._match_starting_recording(info)

    assert matched["recording_id"] == "rec-test"
    assert matched["egress_id"] == "EG-recovered"
    assert matched["status"] == "processing"
    updates.assert_awaited_once_with("rec-test", {
        "egress_id": "EG-recovered", "status": "processing",
        "finalization_last_error": None,
    })


@pytest.mark.asyncio
async def test_stop_ending_state_stays_processing_and_is_not_ready(monkeypatch):
    service = LiveKitService()
    updates = []
    monkeypatch.setattr(service, "get_recording_metadata", AsyncMock(return_value=_record()))
    monkeypatch.setattr(service, "_update_recording_metadata", AsyncMock(side_effect=lambda _id, value: updates.append(value)))
    egress_service = SimpleNamespace(stop_egress=AsyncMock(return_value=SimpleNamespace(status=EgressStatus.EGRESS_ENDING)))
    monkeypatch.setattr(service, "get_egress_service", AsyncMock(return_value=egress_service))
    monkeypatch.setattr("app.livekit_service.recording_state.update_status", lambda *_args: None)
    monkeypatch.setattr("app.livekit_service.session_state.set_recording", lambda *_args: None)

    result = await service.stop_recording("ROOM1", "rec-test")

    assert result["success"] is True
    assert result["status"] == "processing"
    assert all(update.get("status") != "available" for update in updates)
    egress_service.stop_egress.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_successful_terminal_state_uses_finalizer(monkeypatch):
    service = LiveKitService()
    egress_info = SimpleNamespace(status=EgressStatus.EGRESS_COMPLETE)
    egress_service = SimpleNamespace(stop_egress=AsyncMock(return_value=egress_info))
    monkeypatch.setattr(service, "get_recording_metadata", AsyncMock(side_effect=[_record(), {"status": "available"}]))
    monkeypatch.setattr(service, "_update_recording_metadata", AsyncMock())
    monkeypatch.setattr(service, "get_egress_service", AsyncMock(return_value=egress_service))
    monkeypatch.setattr(service, "_finalize_egress", AsyncMock(return_value=True))
    monkeypatch.setattr("app.livekit_service.recording_state.update_status", lambda *_args: None)
    monkeypatch.setattr("app.livekit_service.session_state.set_recording", lambda *_args: None)

    result = await service.stop_recording("ROOM1", "rec-test")

    assert result["status"] == "available"
    service._finalize_egress.assert_awaited_once_with(_record(), egress_info)


@pytest.mark.asyncio
async def test_stop_failure_status_is_persisted_as_failed(monkeypatch):
    service = LiveKitService()
    updates = []
    egress_info = SimpleNamespace(status=EgressStatus.EGRESS_FAILED, error="encode failed", ended_at=0)
    egress_service = SimpleNamespace(stop_egress=AsyncMock(return_value=egress_info))
    monkeypatch.setattr(service, "get_recording_metadata", AsyncMock(return_value=_record()))
    monkeypatch.setattr(service, "_update_recording_metadata", AsyncMock(side_effect=lambda _id, value: updates.append(value)))
    monkeypatch.setattr(service, "get_egress_service", AsyncMock(return_value=egress_service))
    monkeypatch.setattr("app.livekit_service.recording_state.update_status", lambda *_args: None)
    monkeypatch.setattr("app.livekit_service.session_state.set_recording", lambda *_args: None)

    result = await service.stop_recording("ROOM1", "rec-test")

    assert result["success"] is False
    assert result["status"] == "failed"
    assert any(update.get("status") == "failed" for update in updates)


@pytest.mark.asyncio
async def test_stop_rpc_error_preserves_pending_state_for_reconciliation(monkeypatch):
    service = LiveKitService()
    updates = []
    monkeypatch.setattr(service, "get_recording_metadata", AsyncMock(return_value=_record()))
    monkeypatch.setattr(service, "_update_recording_metadata", AsyncMock(side_effect=lambda _id, values: updates.append(values)))
    egress_service = SimpleNamespace(stop_egress=AsyncMock(side_effect=TimeoutError("request timed out")))
    monkeypatch.setattr(service, "get_egress_service", AsyncMock(return_value=egress_service))

    result = await service.stop_recording("ROOM1", "rec-test")

    assert result["success"] is False
    assert updates[-1]["status"] == "processing"
    assert updates[-1]["finalization_lease_until"] is None
    assert "Stop request failed" in updates[-1]["finalization_last_error"]


class FakeS3:
    def __init__(self, playlist, keys):
        self.playlist = playlist
        self.keys = keys

    def get_object(self, **_kwargs):
        return {"Body": SimpleNamespace(read=lambda: self.playlist.encode())}

    def get_paginator(self, _name):
        return SimpleNamespace(paginate=lambda **_kwargs: [{"Contents": [{"Key": key} for key in self.keys]}])


@pytest.mark.asyncio
async def test_hls_validation_requires_endlist_and_every_segment(monkeypatch):
    service = LiveKitService()
    s3 = FakeS3("#EXTM3U\n#EXTINF:8,\nsegment000.ts\n#EXT-X-ENDLIST\n", [
        "recordings/ROOM1/rec-test/segment000.ts",
    ])
    monkeypatch.setattr(service, "_s3_client", lambda: s3)
    monkeypatch.setattr(service, "_run_in_thread", lambda fn: _run_sync(fn))
    info = EgressInfo(status=EgressStatus.EGRESS_COMPLETE, segment_results=[SegmentsInfo(
        segment_count=1, playlist_name="recordings/ROOM1/rec-test/index.m3u8",
    )])

    assert await service._validate_final_hls(_record("processing"), info) is True
    s3.playlist = "#EXTM3U\n#EXTINF:8,\nsegment000.ts\n"
    with pytest.raises(RuntimeError, match="ENDLIST"):
        await service._validate_final_hls(_record("processing"), info)


@pytest.mark.asyncio
async def test_hls_validation_accepts_legacy_singular_segments_result(monkeypatch):
    service = LiveKitService()
    s3 = FakeS3("#EXTM3U\n#EXTINF:8,\nsegment000.ts\n#EXT-X-ENDLIST\n", [
        "recordings/ROOM1/rec-test/segment000.ts",
    ])
    monkeypatch.setattr(service, "_s3_client", lambda: s3)
    monkeypatch.setattr(service, "_run_in_thread", lambda fn: _run_sync(fn))
    info = EgressInfo(status=EgressStatus.EGRESS_COMPLETE, segments=SegmentsInfo(
        segment_count=1, playlist_name="recordings/ROOM1/rec-test/index.m3u8",
    ))

    assert not info.segment_results
    assert info.HasField("segments")
    assert await service._validate_final_hls(_record("processing"), info) is True


@pytest.mark.asyncio
async def test_hls_validation_rejects_egress_info_without_segment_results(monkeypatch):
    service = LiveKitService()
    info = EgressInfo(status=EgressStatus.EGRESS_COMPLETE)

    with pytest.raises(RuntimeError, match="no HLS segment results"):
        await service._validate_final_hls(_record("processing"), info)


async def _run_sync(fn):
    return fn()


@pytest.mark.asyncio
async def test_transient_storage_error_leaves_recording_processing(monkeypatch):
    service = LiveKitService()
    updates = []
    info = SimpleNamespace(status=EgressStatus.EGRESS_COMPLETE)
    monkeypatch.setattr(service, "_validate_final_hls", AsyncMock(side_effect=TimeoutError("storage timeout")))
    monkeypatch.setattr(service, "_update_recording_metadata", AsyncMock(side_effect=lambda _id, values: updates.append(values)))

    result = await service._finalize_egress(_record("processing"), info)

    assert result is False
    assert updates[-1]["status"] == "processing"
    assert "storage timeout" in updates[-1]["finalization_last_error"]


@pytest.mark.asyncio
async def test_reconciliation_finds_persisted_processing_record_after_restart(monkeypatch):
    service = LiveKitService()
    claimed = [_record("processing")]
    monkeypatch.setattr(service, "_recording_query", AsyncMock(return_value=claimed))
    info = SimpleNamespace(egress_id="EG-test", status=EgressStatus.EGRESS_COMPLETE)
    egress_service = SimpleNamespace(list_egress=AsyncMock(return_value=SimpleNamespace(items=[info])))
    monkeypatch.setattr(service, "get_egress_service", AsyncMock(return_value=egress_service))
    monkeypatch.setattr(service, "_finalize_egress", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_reconcile_received_webhooks", AsyncMock())
    monkeypatch.setattr(service, "_queue_available_recordings_without_jobs", AsyncMock())

    result = await service.reconcile_pending_recordings()

    assert result == 1
    egress_service.list_egress.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_processed_webhook_event_is_idempotent(monkeypatch):
    service = LiveKitService()
    event = WebhookEvent(event="egress_ended", id="event-1")
    event.egress_info.egress_id = "EG-test"
    event.egress_info.status = EgressStatus.EGRESS_COMPLETE
    monkeypatch.setattr(service, "_recording_query", AsyncMock(return_value={"event_id": "event-1", "status": "processed"}))
    monkeypatch.setattr(service, "_process_egress_event_row", AsyncMock())

    result = await service.receive_egress_webhook(event)

    assert result["duplicate"] is True
    service._process_egress_event_row.assert_not_awaited()


@pytest.mark.asyncio
async def test_egress_ended_webhook_is_durably_received_then_processed(monkeypatch):
    service = LiveKitService()
    event = WebhookEvent(event="egress_ended", id="event-pending")
    event.egress_info.egress_id = "EG-test"
    event.egress_info.status = EgressStatus.EGRESS_COMPLETE
    saved = {"event_id": "event-pending", "status": "received", "payload": {}}

    async def run_operation(operation):
        return saved

    processed = AsyncMock()
    monkeypatch.setattr(service, "_recording_query", run_operation)
    monkeypatch.setattr(service, "_process_egress_event_row", processed)

    result = await service.receive_egress_webhook(event)

    assert result["received"] is True
    processed.assert_awaited_once_with(saved)


@pytest.mark.asyncio
async def test_irrelevant_webhook_event_is_ignored_without_database_access(monkeypatch):
    service = LiveKitService()
    monkeypatch.setattr(service, "_recording_query", AsyncMock(side_effect=AssertionError("unexpected DB access")))

    result = await service.receive_egress_webhook(WebhookEvent(event="participant_joined"))

    assert result == {"success": True, "ignored": True}


def _signed_webhook(body: bytes, secret="test-livekit-secret-value-long-enough-123"):
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
    return (AccessToken("test-livekit-key", secret)
            .with_identity("livekit-webhook")
            .with_sha256(digest)
            .to_jwt())


@pytest.mark.asyncio
async def test_webhook_endpoint_accepts_valid_signature_and_rejects_invalid(monkeypatch):
    body = b'{"event":"egress_ended","id":"event-1","egressInfo":{"egressId":"EG-test","status":"EGRESS_COMPLETE"}}'
    monkeypatch.setattr(settings, "LIVEKIT_API_KEY", "test-livekit-key")
    monkeypatch.setattr(settings, "LIVEKIT_API_SECRET", "test-livekit-secret-value-long-enough-123")
    handled = AsyncMock(return_value={"success": True, "received": True})
    monkeypatch.setattr(main.livekit_service, "receive_egress_webhook", handled)

    async def build_request():
        sent = False
        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return Request({
            "type": "http", "method": "POST", "path": "/api/livekit/webhook",
            "headers": [], "query_string": b"", "server": ("test", 80),
            "client": ("test", 1234), "scheme": "http",
        }, receive)

    good = await main.livekit_webhook(await build_request(), _signed_webhook(body))
    assert good["received"] is True
    with pytest.raises(HTTPException) as error:
        await main.livekit_webhook(await build_request(), "not-a-signed-token")
    assert error.value.status_code == 401
    handled.assert_awaited_once()


def test_migration_supports_multiple_recordings_without_cascading_to_notes():
    migration = (Path(__file__).parents[1] / "migrations" / "004_recording_finalization.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS public.meeting_recordings" in migration
    assert "PRIMARY KEY (class_meeting_id, recording_id)" in migration
    assert "UNIQUE (recording_id)" in migration
    assert "REFERENCES public.class_meetings(id) ON DELETE CASCADE" in migration
    assert "REFERENCES public.recordings(recording_id) ON DELETE CASCADE" in migration
    assert "CREATE TABLE IF NOT EXISTS public.recording_processing_jobs" in migration
    assert "recording_id TEXT NOT NULL UNIQUE" in migration
    phase1 = (Path(__file__).parents[1] / "migrations" / "003_create_class_intelligence.sql").read_text()
    assert "meeting_id UUID NOT NULL UNIQUE REFERENCES public.class_meetings(id) ON DELETE RESTRICT" in phase1
    assert "recording_id TEXT REFERENCES public.recordings(recording_id) ON DELETE SET NULL" in phase1
