-- Durable LiveKit recording finalization and many-to-one meeting association.
-- Apply after 003_create_class_intelligence.sql.

CREATE TABLE IF NOT EXISTS public.meeting_recordings (
  class_meeting_id UUID NOT NULL
    REFERENCES public.class_meetings(id) ON DELETE CASCADE,
  recording_id TEXT NOT NULL
    REFERENCES public.recordings(recording_id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (class_meeting_id, recording_id),
  UNIQUE (recording_id)
);
COMMENT ON TABLE public.meeting_recordings IS
  'Authoritative meeting-to-recording relationship; class_meetings.recording_id remains only for Phase 1 compatibility.';

-- Preserve the existing single-recording association when upgrading.
INSERT INTO public.meeting_recordings (class_meeting_id, recording_id)
SELECT id, recording_id
FROM public.class_meetings
WHERE recording_id IS NOT NULL
ON CONFLICT DO NOTHING;

ALTER TABLE public.recordings
  ADD COLUMN IF NOT EXISTS livekit_room_name TEXT,
  ADD COLUMN IF NOT EXISTS stop_requested_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS finalization_attempts INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS finalization_next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS finalization_lease_until TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS finalization_last_error TEXT,
  ADD COLUMN IF NOT EXISTS finalized_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS public.livekit_egress_events (
  event_id TEXT PRIMARY KEY,
  event_name TEXT NOT NULL,
  egress_id TEXT,
  payload JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'received'
    CHECK (status IN ('received', 'processed', 'ignored')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  lease_until TIMESTAMPTZ,
  last_error TEXT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS public.recording_processing_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  recording_id TEXT NOT NULL UNIQUE
    REFERENCES public.recordings(recording_id) ON DELETE CASCADE,
  class_meeting_id UUID NOT NULL
    REFERENCES public.class_meetings(id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'complete', 'failed')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS meeting_recordings_meeting_idx
  ON public.meeting_recordings (class_meeting_id, created_at);
CREATE INDEX IF NOT EXISTS livekit_egress_events_received_idx
  ON public.livekit_egress_events (next_attempt_at, received_at)
  WHERE status = 'received';
CREATE INDEX IF NOT EXISTS recordings_finalization_due_idx
  ON public.recordings (finalization_next_attempt_at, finalization_lease_until)
  WHERE status IN ('starting', 'processing');
CREATE INDEX IF NOT EXISTS recording_processing_jobs_pending_idx
  ON public.recording_processing_jobs (created_at)
  WHERE status = 'pending';

ALTER TABLE public.meeting_recordings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.livekit_egress_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.recording_processing_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON public.meeting_recordings,
  public.livekit_egress_events, public.recording_processing_jobs
  FROM PUBLIC, anon, authenticated;
GRANT ALL PRIVILEGES ON public.meeting_recordings,
  public.livekit_egress_events, public.recording_processing_jobs TO service_role;

-- Associate a recording to the meeting that was active when it started.
CREATE OR REPLACE FUNCTION public.associate_recording_with_meeting(
  p_room_code TEXT,
  p_recording_id TEXT,
  p_recording_started_at TIMESTAMPTZ
) RETURNS UUID
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_meeting_id UUID;
BEGIN
  SELECT id INTO v_meeting_id
  FROM public.class_meetings
  WHERE room_code = p_room_code
    AND room_created_at <= p_recording_started_at
    AND (ended_at IS NULL OR ended_at >= p_recording_started_at)
  ORDER BY room_created_at DESC
  LIMIT 1;

  IF v_meeting_id IS NULL THEN
    RAISE EXCEPTION 'No class meeting matches recording start time';
  END IF;

  INSERT INTO public.meeting_recordings (class_meeting_id, recording_id)
  VALUES (v_meeting_id, p_recording_id)
  ON CONFLICT DO NOTHING;

  RETURN v_meeting_id;
END;
$$;

-- Claim a small batch using row locks so multiple app instances do not
-- continuously work the same pending recording.
CREATE OR REPLACE FUNCTION public.claim_recording_finalization_batch(
  p_batch_size INTEGER DEFAULT 20
) RETURNS SETOF public.recordings
LANGUAGE sql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
  WITH candidates AS (
    SELECT recording_id
    FROM public.recordings
    WHERE status IN ('starting', 'processing')
      AND finalization_next_attempt_at <= now()
      AND (finalization_lease_until IS NULL OR finalization_lease_until < now())
    ORDER BY finalization_next_attempt_at, started_at
    LIMIT GREATEST(1, LEAST(COALESCE(p_batch_size, 20), 100))
    FOR UPDATE SKIP LOCKED
  )
  UPDATE public.recordings AS r
  SET finalization_lease_until = now() + interval '2 minutes',
      finalization_attempts = finalization_attempts + 1
  FROM candidates
  WHERE r.recording_id = candidates.recording_id
  RETURNING r.*;
$$;

CREATE OR REPLACE FUNCTION public.enqueue_ready_recording(p_recording_id TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_inserted INTEGER;
BEGIN
  INSERT INTO public.recording_processing_jobs (recording_id, class_meeting_id)
  SELECT r.recording_id, mr.class_meeting_id
  FROM public.recordings r
  JOIN public.meeting_recordings mr ON mr.recording_id = r.recording_id
  WHERE r.recording_id = p_recording_id AND r.status = 'available'
  ON CONFLICT (recording_id) DO NOTHING;
  GET DIAGNOSTICS v_inserted = ROW_COUNT;
  RETURN v_inserted > 0;
END;
$$;

CREATE OR REPLACE FUNCTION public.claim_livekit_egress_events_batch(
  p_batch_size INTEGER DEFAULT 50
) RETURNS SETOF public.livekit_egress_events
LANGUAGE sql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
  WITH candidates AS (
    SELECT event_id
    FROM public.livekit_egress_events
    WHERE status = 'received'
      AND next_attempt_at <= now()
      AND (lease_until IS NULL OR lease_until < now())
    ORDER BY next_attempt_at, received_at
    LIMIT GREATEST(1, LEAST(COALESCE(p_batch_size, 50), 100))
    FOR UPDATE SKIP LOCKED
  )
  UPDATE public.livekit_egress_events AS e
  SET lease_until = now() + interval '2 minutes',
      attempts = attempts + 1
  FROM candidates
  WHERE e.event_id = candidates.event_id
  RETURNING e.*;
$$;

CREATE OR REPLACE FUNCTION public.enqueue_available_recordings_batch(p_limit INTEGER DEFAULT 50)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_inserted INTEGER;
BEGIN
  INSERT INTO public.recording_processing_jobs (recording_id, class_meeting_id)
  SELECT r.recording_id, mr.class_meeting_id
  FROM public.recordings r
  JOIN public.meeting_recordings mr ON mr.recording_id = r.recording_id
  LEFT JOIN public.recording_processing_jobs j ON j.recording_id = r.recording_id
  WHERE r.status = 'available' AND j.recording_id IS NULL
  ORDER BY r.finalized_at
  LIMIT GREATEST(1, LEAST(COALESCE(p_limit, 50), 100))
  ON CONFLICT (recording_id) DO NOTHING;
  GET DIAGNOSTICS v_inserted = ROW_COUNT;
  RETURN v_inserted;
END;
$$;

CREATE OR REPLACE FUNCTION public.associate_unlinked_available_recordings(p_limit INTEGER DEFAULT 50)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_inserted INTEGER;
BEGIN
  INSERT INTO public.meeting_recordings (class_meeting_id, recording_id)
  SELECT picked.class_meeting_id, picked.recording_id
  FROM (
    SELECT DISTINCT ON (r.recording_id)
      r.recording_id, m.id AS class_meeting_id
    FROM public.recordings r
    JOIN public.class_meetings m
      ON m.room_code = r.room_code
     AND m.room_created_at <= r.started_at
     AND (m.ended_at IS NULL OR m.ended_at >= r.started_at)
    LEFT JOIN public.meeting_recordings mr ON mr.recording_id = r.recording_id
    WHERE r.status = 'available' AND mr.recording_id IS NULL
    ORDER BY r.recording_id, m.room_created_at DESC
    LIMIT GREATEST(1, LEAST(COALESCE(p_limit, 50), 100))
  ) AS picked
  ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS v_inserted = ROW_COUNT;
  RETURN v_inserted;
END;
$$;

REVOKE ALL ON FUNCTION public.associate_recording_with_meeting(TEXT, TEXT, TIMESTAMPTZ)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.associate_recording_with_meeting(TEXT, TEXT, TIMESTAMPTZ)
  TO service_role;
REVOKE ALL ON FUNCTION public.claim_recording_finalization_batch(INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_recording_finalization_batch(INTEGER)
  TO service_role;
REVOKE ALL ON FUNCTION public.enqueue_ready_recording(TEXT)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.enqueue_ready_recording(TEXT)
  TO service_role;
REVOKE ALL ON FUNCTION public.claim_livekit_egress_events_batch(INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_livekit_egress_events_batch(INTEGER)
  TO service_role;
REVOKE ALL ON FUNCTION public.enqueue_available_recordings_batch(INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.enqueue_available_recordings_batch(INTEGER)
  TO service_role;
REVOKE ALL ON FUNCTION public.associate_unlinked_available_recordings(INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.associate_unlinked_available_recordings(INTEGER)
  TO service_role;
